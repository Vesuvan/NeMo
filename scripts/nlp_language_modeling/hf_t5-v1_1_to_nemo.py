# Copyright (c) 2022, NVIDIA CORPORATION.  All rights reserved.
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

"""
This script generates a NeMo-Megatron compatible `.nemo` file for a Huggingface T5-v1_1 model.

List of Huggingface models that this script can covert:

1. google/t5-v1_1-small
2. google/t5-v1_1-base
3. google/t5-v1_1-large
4. google/t5-v1_1-xl
5. google/t5-v1_1-xxl
6. google/mt5-small
7. google/mt5-base
8. google/mt5-large
9. google/mt5-xl
10. google/mt5-xxl
11. google/ul2

Use instructions:

python hf_t5_to_nemo_coverter.py \
    --hf_model_name google/ul2 \
    --nemo_state_dict /path/to/nemo_state_dict.pt \
    --nemo_file_path /path/to/nemo_file.nemo
"""
import collections
import os
import tempfile
from argparse import ArgumentParser

import torch
from omegaconf.omegaconf import OmegaConf, open_dict
from pytorch_lightning import Trainer
from transformers import AutoTokenizer, T5ForConditionalGeneration

from nemo.collections.nlp.models.language_modeling.megatron_t5_model import MegatronT5Model
from nemo.collections.nlp.parts.nlp_overrides import NLPDDPPlugin, NLPSaveRestoreConnector

try:
    import accelerate
except ImportError:
    raise ImportError("Please install accelerate package via `pip install accelerate` to use this script.")


def convert_weights(hf_model, nemo_state_dict_path):
    hf_model = T5ForConditionalGeneration.from_pretrained(hf_model, low_cpu_mem_usage=True)
    hf_model_config = hf_model.config
    with tempfile.TemporaryDirectory() as tmp:
        torch.save(hf_model.state_dict(), os.path.join(tmp, 'model.pt'))
        hf_weights = torch.load(os.path.join(tmp, 'model.pt'))

    nemo_weights = collections.OrderedDict()

    print(f'Found {len(hf_weights.keys())} keys in the checkpoint')

    def _get_model_type_block_layer(k):
        if k.startswith('encoder'):
            model_type = 'encoder'
        elif k.startswith('decoder'):
            model_type = 'decoder'
        else:
            raise ValueError(f"Unknown model type for {k}")

        return model_type, int(k.split('.')[2]), int(k.split('.')[4])

    for k, v in hf_weights.items():
        #################################################
        ###### Enc-Dec Embeddings and Output Layer ######
        #################################################
        # Tied decoder embedding and decoder output layer.
        if k == 'shared.weight':
            pass

        elif k == 'lm_head.weight':
            nemo_weights['enc_dec_model.tokens_head.weight'] = v
            print(
                f'Mapped {k} to enc_dec_model.decoder_embedding.word_embeddings.weight and enc_dec_model.tokens_head.weight'
            )

        # Decoder embeddings
        elif k == 'decoder.embed_tokens.weight':
            nemo_weights['enc_dec_model.decoder_embedding.word_embeddings.weight'] = v

        elif k == 'encoder.embed_tokens.weight':
            nemo_weights['enc_dec_model.encoder_embedding.word_embeddings.weight'] = v
            print(f'Mapped {k} to enc_dec_model.encoder_embedding.word_embeddings.weight')

        #################################################
        ################# RPE Weights ###################
        #################################################

        elif k == 'encoder.block.0.layer.0.SelfAttention.relative_attention_bias.weight':
            nemo_weights['enc_dec_model.encoder_relative_position_embedding.relative_position_embedding.weight'] = v
            print(
                f'Mapped {k} to enc_dec_model.encoder_relative_position_embedding.relative_position_embedding.weight'
            )

        elif k == 'decoder.block.0.layer.0.SelfAttention.relative_attention_bias.weight':
            nemo_weights['enc_dec_model.decoder_relative_position_embedding.relative_position_embedding.weight'] = v
            print(
                f'Mapped {k} to enc_dec_model.decoder_relative_position_embedding.relative_position_embedding.weight'
            )

        # Block in HF corresponds to layer in NeMo.
        # Layer in HF does not correspond to anything in NeMo. Layer 0 is self attn, layer 1 is cross-attn.

        #################################################
        ############### Attention Layers ################
        #################################################

        # Self-Attention

        # Q, k, V in NeMo-Megatron is bundled into a single matrix.
        elif 'SelfAttention.q.weight' in k:
            model_type, block_number, layer_number = _get_model_type_block_layer(k)
            k_weight = hf_weights[k.replace('q.weight', 'k.weight')]
            v_weight = hf_weights[k.replace('q.weight', 'v.weight')]
            concat_weights = torch.cat([v, k_weight, v_weight], dim=0)
            nemo_weights[
                f'enc_dec_model.enc_dec_model.{model_type}.model.layers.{block_number}.self_attention.query_key_value.weight'
            ] = concat_weights
            print(
                f'Mapped {k} to enc_dec_model.enc_dec_model.{model_type}.model.layers.{block_number}.self_attention.query_key_value.weight'
            )

        # We can skip processing of k, v weights since we already concat them into qkv above.
        elif 'SelfAttention.k.weight' in k or 'SelfAttention.v.weight' in k:
            pass

        # Output self-attn matrix.
        elif 'SelfAttention.o.weight' in k:
            model_type, block_number, layer_number = _get_model_type_block_layer(k)
            block_number = int(k.split('.')[2])  # Block in HF corresponds to layer in NeMo.
            layer_number = int(
                k.split('.')[4]
            )  # Layer in HF does not correspond to anything in NeMo. Layer 0 is self attn, layer 1 is cross-attn.
            nemo_weights[
                f'enc_dec_model.enc_dec_model.{model_type}.model.layers.{block_number}.self_attention.dense.weight'
            ] = v
            print(
                f'Mapped {k} to enc_dec_model.enc_dec_model.{model_type}.model.layers.{block_number}.self_attention.dense.weight'
            )

        # Cross-Attention projection matrices are merged into K, V matrices in NeMo-Megatron
        elif 'EncDecAttention.k.weight' in k:
            model_type, block_number, layer_number = _get_model_type_block_layer(k)
            v_weight = hf_weights[k.replace('k.weight', 'v.weight')]
            concat_weights = torch.cat([v, v_weight], dim=0)
            nemo_weights[
                f'enc_dec_model.enc_dec_model.decoder.model.layers.{block_number}.inter_attention.key_value.weight'
            ] = concat_weights
            print(
                f'Mapped {k} to enc_dec_model.enc_dec_model.decoder.model.layers.{block_number}.inter_attention.key_value.weight'
            )

        # We can skip processing of v weights since we already concat them with k above.
        elif 'EncDecAttention.v.weight' in k:
            pass

        # Cross-Attention Q matrix is separate in NeMo-Megatron
        elif 'EncDecAttention.q.weight' in k:
            model_type, block_number, layer_number = _get_model_type_block_layer(k)
            nemo_weights[
                f'enc_dec_model.enc_dec_model.decoder.model.layers.{block_number}.inter_attention.query.weight'
            ] = v
            print(
                f'Mapped {k} to enc_dec_model.enc_dec_model.decoder.model.layers.{block_number}.inter_attention.query.weight'
            )

        # Cross-Attention Q matrix is separate in NeMo-Megatron
        elif 'EncDecAttention.o.weight' in k:
            model_type, block_number, layer_number = _get_model_type_block_layer(k)
            nemo_weights[
                f'enc_dec_model.enc_dec_model.decoder.model.layers.{block_number}.inter_attention.dense.weight'
            ] = v
            print(
                f'Mapped {k} to enc_dec_model.enc_dec_model.decoder.model.layers.{block_number}.inter_attention.dense.weight'
            )

        #################################################
        #################$ FFN Layers ###################
        #################################################

        elif 'DenseReluDense.wi_0.weight' in k:
            model_type, block_number, layer_number = _get_model_type_block_layer(k)
            nemo_weights[
                f'enc_dec_model.enc_dec_model.{model_type}.model.layers.{block_number}.mlp.dense_h_to_4h.weight'
            ] = v
            print(
                f'Mapped {k} to enc_dec_model.enc_dec_model.{model_type}.model.layers.{block_number}.mlp.dense_h_to_4h.weight'
            )

        elif 'DenseReluDense.wi_1.weight' in k:
            model_type, block_number, layer_number = _get_model_type_block_layer(k)
            nemo_weights[
                f'enc_dec_model.enc_dec_model.{model_type}.model.layers.{block_number}.mlp.dense_h_to_4h_2.weight'
            ] = v
            print(
                f'Mapped {k} to enc_dec_model.enc_dec_model.{model_type}.model.layers.{block_number}.mlp.dense_h_to_4h_2.weight'
            )

        elif 'DenseReluDense.wo.weight' in k:
            model_type, block_number, layer_number = _get_model_type_block_layer(k)
            nemo_weights[
                f'enc_dec_model.enc_dec_model.{model_type}.model.layers.{block_number}.mlp.dense_4h_to_h.weight'
            ] = v
            print(
                f'Mapped {k} to enc_dec_model.enc_dec_model.{model_type}.model.layers.{block_number}.mlp.dense_4h_to_h.weight'
            )

        #################################################
        #################$ LayerNorm ####################
        #################################################

        elif 'layer_norm' in k:
            if 'final' in k:
                model_type = 'encoder' if k.startswith('encoder') else 'decoder'
                nemo_weights[f'enc_dec_model.enc_dec_model.{model_type}.model.final_layernorm.weight'] = v
                print(f'Mapped {k} to enc_dec_model.enc_dec_model.{model_type}.model.final_layernorm.weight')
            else:
                model_type, block_number, layer_number = _get_model_type_block_layer(k)
                if layer_number == 0 and model_type == 'encoder':
                    nemo_weights[
                        f'enc_dec_model.enc_dec_model.{model_type}.model.layers.{block_number}.input_layernorm.weight'
                    ] = v
                    print(
                        f'Mapped {k} to enc_dec_model.enc_dec_model.{model_type}.model.layers.{block_number}.input_layernorm.weight'
                    )
                elif layer_number == 1 and model_type == 'encoder':
                    nemo_weights[
                        f'enc_dec_model.enc_dec_model.{model_type}.model.layers.{block_number}.post_attention_layernorm.weight'
                    ] = v
                    print(
                        f'Mapped {k} to enc_dec_model.enc_dec_model.{model_type}.model.layers.{block_number}.post_attention_layernorm.weight'
                    )
                elif layer_number == 0 and model_type == 'decoder':
                    nemo_weights[
                        f'enc_dec_model.enc_dec_model.{model_type}.model.layers.{block_number}.input_layernorm.weight'
                    ] = v
                    print(
                        f'Mapped {k} to enc_dec_model.enc_dec_model.{model_type}.model.layers.{block_number}.input_layernorm.weight'
                    )
                elif layer_number == 1 and model_type == 'decoder':
                    nemo_weights[
                        f'enc_dec_model.enc_dec_model.{model_type}.model.layers.{block_number}.post_attention_layernorm.weight'
                    ] = v
                    print(
                        f'Mapped {k} to enc_dec_model.enc_dec_model.{model_type}.model.layers.{block_number}.post_attention_layernorm.weight'
                    )
                elif layer_number == 2 and model_type == 'decoder':
                    nemo_weights[
                        f'enc_dec_model.enc_dec_model.{model_type}.model.layers.{block_number}.post_inter_attention_layernorm.weight'
                    ] = v
                    print(
                        f'Mapped {k} to enc_dec_model.enc_dec_model.{model_type}.model.layers.{block_number}.post_inter_attention_layernorm.weight'
                    )
                else:
                    raise ValueError("Unknown layer_norm key: {}".format(k))
        else:
            raise ValueError(f"Unknown key: {k}")

    torch.save(nemo_weights, nemo_state_dict_path)
    print("Saved weights to {}".format(nemo_state_dict_path))
    return hf_model_config


def package_into_nemo_file(state_dict_path, base_yaml_config, hf_model_config, nemo_file_path, hf_model_name):
    """
    Packages the state dict, config file and tokenizer into a `.nemo` file.
    """
    trainer = Trainer(devices=1, plugins=NLPDDPPlugin(), accelerator="cpu", precision=32)
    base_cfg = OmegaConf.load(base_yaml_config)
    if hf_model_config.dense_act_fn == "silu":
        act_fn = "swiglu"
    elif hf_model_config.dense_act_fn == "gelu_new":
        act_fn = "geglu"
    else:
        raise ValueError(f"Unknown dense_act_fn: {hf_model_config.dense_act_fn}")

    with open_dict(base_cfg):
        base_cfg.encoder.num_layers = hf_model_config.num_layers
        base_cfg.encoder.hidden_size = hf_model_config.d_model
        base_cfg.encoder.ffn_hidden_size = hf_model_config.d_ff
        base_cfg.encoder.kv_channels = hf_model_config.d_kv
        base_cfg.encoder.num_attention_heads = hf_model_config.num_heads
        base_cfg.encoder.activation = act_fn
        base_cfg.encoder.relative_attention_num_buckets = hf_model_config.relative_attention_num_buckets

        base_cfg.decoder.num_layers = hf_model_config.num_decoder_layers
        base_cfg.decoder.hidden_size = hf_model_config.d_model
        base_cfg.decoder.ffn_hidden_size = hf_model_config.d_ff
        base_cfg.decoder.kv_channels = hf_model_config.d_kv
        base_cfg.decoder.num_attention_heads = hf_model_config.num_heads
        base_cfg.decoder.activation = act_fn
        base_cfg.decoder.relative_attention_num_buckets = hf_model_config.relative_attention_num_buckets

    with tempfile.TemporaryDirectory() as tmp:
        tokenizer = AutoTokenizer.from_pretrained(hf_model_name)
        tokenizer_path = tokenizer.save_vocabulary(tmp)[0]
        base_cfg.tokenizer.model = tokenizer_path
        model = MegatronT5Model(base_cfg, trainer).to('cpu')
        model._save_restore_connector = NLPSaveRestoreConnector()
        model.load_state_dict(torch.load(state_dict_path))
        model.save_to(nemo_file_path)


if __name__ == '__main__':
    parser = ArgumentParser()
    parser.add_argument(
        "--hf_model_name",
        type=str,
        required=True,
        help="Valid Huggingface T5v1_1 model name ex: google/t5-v1_1-large or google/ul2. Example something that can be loaded with T5ForConditionalGeneration.from_pretrained()",
    )
    parser.add_argument(
        "--nemo_state_dict_path",
        type=str,
        required=True,
        help="Path to write the intermediate nemo state dict file ex: /path/to/nemo_state_dict.pt",
    )
    parser.add_argument(
        "--nemo_file_path",
        type=str,
        required=True,
        help="Path to write the converted .nemo file ex: /path/to/t5_base_converted_to_nemo.nemo",
    )
    parser.add_argument(
        "--base_yaml_config",
        type=str,
        default="hf_t5v1_1_base_config.yaml",
        help="Path to a base yaml config that we edit based on the provided model.",
    )
    args = parser.parse_args()
    if not os.path.exists(args.base_yaml_config):
        raise FileNotFoundError(f"Base yaml config file {args.base_yaml_config} does not exist.")
    hf_model_config = convert_weights(args.hf_model_name, args.nemo_state_dict_path)
    package_into_nemo_file(
        state_dict_path=args.nemo_state_dict_path,
        base_yaml_config=args.base_yaml_config,
        hf_model_config=hf_model_config,
        nemo_file_path=args.nemo_file_path,
        hf_model_name=args.hf_model_name,
    )
