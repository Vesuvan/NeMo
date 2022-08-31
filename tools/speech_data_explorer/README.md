Speech Data Explorer
--------------------

[Dash](https://plotly.com/dash/)-based tool for interactive exploration of ASR/TTS datasets.

Features:
- dataset's statistics (alphabet, vocabulary, duration-based histograms)
- navigation across dataset (sorting, filtering)
- inspection of individual utterances (waveform, spectrogram, audio player)
- errors' analysis (Word Error Rate, Character Error Rate, Word Match Rate, Mean Word Accuracy, diff)
- visual comparation of two models (on same dataset)

Please make sure that requirements are installed. Then run:
```
python data_explorer.py path_to_manifest.json

or to try new features:

python data_explorer.py FTAll_books_train.json -c1 Conf_test.json -c2 Context_test.json

```

JSON manifest file should contain the following fields:
- "audio_filepath" (path to audio file)
- "duration" (duration of the audio file in seconds)
- "text" (reference transcript)

Errors' analysis requires "pred_text" (ASR transcript) for all utterances.

Any additional field will be parsed and displayed in 'Samples' tab.

![Speech Data Explorer](screenshot.png)

To try Comparation tool - go to corresponding tab
![image](https://user-images.githubusercontent.com/37293288/183735563-ba6c1819-a320-46bc-8eaa-14ed77e93787.png)

