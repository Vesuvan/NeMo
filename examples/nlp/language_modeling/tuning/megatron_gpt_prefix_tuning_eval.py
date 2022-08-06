# Copyright (c) 2021, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
from nemo.collections.nlp.data.language_modeling.megatron.gpt_prompt_learning_dataset import GPTPromptLearningDataset

import torch
from omegaconf import OmegaConf
from omegaconf.omegaconf import open_dict
from pytorch_lightning.trainer.trainer import Trainer
from torch.utils.data import DataLoader, Dataset

from nemo.collections.nlp.models.language_modeling.megatron_gpt_prefix_tuning_model import PrefixTuningModel
from nemo.collections.nlp.modules.common.transformer.text_generation import LengthParam, SamplingParam
from nemo.collections.nlp.parts.nlp_overrides import NLPDDPPlugin
from nemo.core.config import hydra_runner
from nemo.collections.nlp.data.language_modeling.megatron.gpt_prompt_learning_dataset import RequestDataSet


"""
This is the script to run GPT text generation.

Usage:
    Assume the model has TP=1, PP=1 in the following use cases.
    a. run greedy inference from a nemo file:
        python megatron_gpt_eval.py \
            gpt_model_file=PATH_TO_MODEL \
            inference.greedy=True \
            inference.add_BOS=True \
            trainer.devices=1 \
            trainer.num_nodes=1 \
            tensor_model_parallel_size=1 \
            pipeline_model_parallel_size=1 \
            prompts=[prompt1,prompt2]

    b. run greedy inference from a PTL checkpoint file:
        python megatron_gpt_eval.py \
            checkpoint_dir=PATH_TO_CHECKPOINT_FILE \
            checkpoint_name=CHECKPOINT_FILE_NAME \
            hparams_file=HPARAMS_FILE \
            inference.greedy=True \
            inference.add_BOS=True \
            trainer.devices=1 \
            trainer.num_nodes=1 \
            tensor_model_parallel_size=1 \
            pipeline_model_parallel_size=1 \
            prompts=[prompt1,prompt2]

    c. run top_p inference from a nemo file:
        python megatron_gpt_eval.py \
            gpt_model_file=PATH_TO_MODEL \
            inference.greedy=False \
            inference.top_k=0 \
            inference.top_p=0.9 \
            inference.repetition_penalty=1.2 \
            inference.add_BOS=True \
            trainer.devices=1 \
            trainer.num_nodes=1 \
            tensor_model_parallel_size=1 \
            pipeline_model_parallel_size=1 \
            prompts=[prompt1,prompt2]

    d. If you don't need to generate tokens and need model to compute logprobs:
         python megatron_gpt_eval.py \
            gpt_model_file=PATH_TO_MODEL \
            inference.compute_logprob=True \
            trainer.devices=1 \
            trainer.num_nodes=1 \
            tensor_model_parallel_size=1 \
            pipeline_model_parallel_size=1 \
            prompts=[text to get logprob]

    e. Launch the inference server
         python megatron_gpt_eval.py \
            gpt_model_file=PATH_TO_MODEL \
            trainer.devices=1 \
            trainer.num_nodes=1 \
            tensor_model_parallel_size=1 \
            pipeline_model_parallel_size=1 \
            server=True
        
        To send a request to the server, here is one example code:
        ```python
        import json
        import requests

        batch_size = 8
        port_num = 5555
        headers = {"Content-Type": "application/json"}


        def request_data(data):
            resp = requests.put('http://localhost:{}/generate'.format(port_num),
                                data=json.dumps(data),
                                headers=headers)
            sentences = resp.json()['sentences']
            return sentences


        data = {
            "sentences": [""] * batch_size,
            "tokens_to_generate": 300,
            "temperature": 1.0,
            "add_BOS": True,
            "top_k": 0,
            "top_p": 0.9,
            "greedy": False,
            "all_probs": False,
            "repetition_penalty": 1.2,
            "min_tokens_to_generate": 2,
        }

        sentences = request_data(data)
        ```

    f. run greedy inference from a p-tuned/prompt-tuned model's nemo file:
        python megatron_gpt_eval.py \
            virtual_prompt_model_file=PATH_TO_NEMO_PROMPT_LEARNING_MODEL_FILE \
            gpt_model_file=PATH_TO_FROZEN_GPT_MODEL_FILE \
            inference.greedy=True \
            inference.add_BOS=False \
            trainer.devices=1 \
            trainer.num_nodes=1 \
            tensor_model_parallel_size=1 \
            pipeline_model_parallel_size=1 \
            prompts=[prompt1,prompt2]

        virtual_prompt_model_file should be a path to a .nemo file saved after p-tuning/prompt tuning and model file
        is still the path to the gpt model's .nemo file.         

        prompts in this case should be a list of .json or .jsonl files containing json objects similar to the ones 
        used during prompt learning. They should have keys that match the fields specified in the prompt template.
        Fields can be dropped from the prompt dict and their corresponding section of the prompt template will 
        be automatically removed. 

        For example, say the prompt template during p-tuning/prompt-tuning looked like:

        '<|VIRTUAL_PROMPT_0|> Context: {context} Question: {question} Answer: {answer}'

        but you don't want to include the answer field during inference. Just don't 
        include the answer field in the prompt dict like below:

        {"taskname": "squad", "context": "some paragraph", "question": "question related to paragraph"}
        {"taskname": "squad", "context": "another paragraph", "question": "a different question related to paragraph"}

        And the dataset class will automatically format your input to have the form:

        [
            '<|VIRTUAL_PROMPT_0|> Context: some paragraph Question: question related to paragraph Answer:',
            '<|VIRTUAL_PROMPT_0|> Context: another paragraph Question: a different question related to paragraph Answer:'
        ]

        Similarly for other senarios, just add virtual_prompt_model_file=PATH_TO_NEMO_PROMPT_LEARNING_MODEL_FILE if you're using a 
        p-tuned/prompt-tuned model. 

        g. Run Greedy Inference from a prefix tuned nemo model:
        Note: before you can run inference from a prefix tuned model you must first train it. The training script is `examples/nlp/language_modeling/megatron_gpt_prefix_tuning.py`:

        python megatron_gpt_eval.py \
            prefix_tuned_model_file=<PATH_.nemo_FILE_GENERATED_AT_END_OF_TRAINING> \
            gpt_model_file=<PATH_TO_.nemo_FILE_WITH_PRETRAINED_GPT_MODEL> \
            inference.greedy=True \
            inference.add_BOS=False \
            trainer.devices=1 \
            trainer.num_nodes=1 \
            tensor_model_parallel_size=1 \
            pipeline_model_parallel_size=1 \
            prompts=["<PATH_TO_.jsonl_FILE_TO_EVALUATE>"]
"""

if not torch.cuda.is_available():
    raise EnvironmentError("GPU is needed for the inference")


@hydra_runner(config_path="conf", config_name="megatron_gpt_prefix_tuning_inference")
def main(cfg) -> None:

    # trainer required for restoring model parallel models
    trainer = Trainer(plugins=NLPDDPPlugin(), **cfg.trainer)
    assert (
        cfg.trainer.devices * cfg.trainer.num_nodes
        == cfg.tensor_model_parallel_size * cfg.pipeline_model_parallel_size
    ), "devices * num_nodes should equal tensor_model_parallel_size * pipeline_model_parallel_size"

    # Load prompt tuned model, virtual_prompt_model_file must be provided in config
    if cfg.get("prefix_tuned_model_file", None) is not None:
        # Update frozen GPT model path in case it has changed
        prefix_tuning_cfg = PrefixTuningModel.restore_from(
            cfg.prefix_tuned_model_file, trainer=trainer, return_config=True
        )
        with open_dict(prefix_tuning_cfg):
            prefix_tuning_cfg.language_model_path = cfg.gpt_model_file

        # Now load prompt learning model with frozen gpt model base
        model = PrefixTuningModel.restore_from(
            restore_path=cfg.prefix_tuned_model_file, trainer=trainer, override_config_path=prefix_tuning_cfg
        )

    # Or load regular GPT model
    else:
        raise NotImplementedError("Only inference from prefix tuned model is supported, for general inerference from a Megatron GPT model, refer to ../megatron_gpt_eval.py")

    model.freeze()

    # Have to turn off activations_checkpoint_method for inference
    try:
        model.model.language_model.encoder.activations_checkpoint_method = None
    except AttributeError:
        pass

    try:
        model.frozen_model.model.language_model.encoder.activations_checkpoint_method = None
    except AttributeError:
        pass

    length_params: LengthParam = {
        "max_length": cfg.inference.tokens_to_generate,
        "min_length": cfg.inference.min_tokens_to_generate,
    }

    sampling_params: SamplingParam = {
        "use_greedy": cfg.inference.greedy,
        "temperature": cfg.inference.temperature,
        "top_k": cfg.inference.top_k,
        "top_p": cfg.inference.top_p,
        "repetition_penalty": cfg.inference.repetition_penalty,
        "add_BOS": cfg.inference.add_BOS,
        "all_probs": cfg.inference.all_probs,
        "compute_logprob": cfg.inference.compute_logprob,
    }

    # First method of running text generation, call model.generate method
    # Input into generate method should be either list of string prompts or list of dicts
    datapaths_dict = [{"data_path": path} for path in cfg.data_paths]

    # Use for inference on a few examples
    response = model.generate(inputs=datapaths_dict, length_params=length_params, sampling_params=sampling_params)

    print("***************************")
    if cfg.output_file is not None:
        with open(cfg.output_file, "w") as f:
            for sentence in response["sentences"]:
                s = ' '.join(sentence.split('\n'))
                print(s)
                f.write(s + "\n")
    else:
        print(response)
    print("***************************") 
    return
    # Second method of running text generation, call trainer.predict

    ds = RequestDataSet(OmegaConf.to_container(cfg.prompts))
    request_dl = DataLoader(dataset=ds, batch_size=1)
    config = OmegaConf.to_container(cfg.inference)
    model.set_inference_config(config)
    responses = trainer.predict(model, request_dl)
    print("***************************")
    if cfg.output_file is not None:
        with open(cfg.output_file, "w") as f:
            for response in responses:
                s = ' '.join(response["sentences"][0].split('\n'))
                print(s)
                f.write(s + "\n")
    else:
        print(responses)
    print("***************************")

if __name__ == '__main__':
    main()  # noqa pylint: disable=no-value-for-parameter
