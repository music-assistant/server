# Smart Fades Provider

Audio analysis provider that detects **beats**, **downbeats**, **musical key**, **RMS energy**, **spectral centroid**, and **vocal activity** in real time using the [Beat This!](https://github.com/CPJKU/beat_this) neural network (CPJKU, ISMIR 2024), the S-KEY key detection model, and [FireRedVAD](https://github.com/FireRedTeam/FireRedVAD). The detected timing, tonal, and vocal information drives smart crossfade positioning in Music Assistant's playback queue.

## How it works

Beat This! is a transformer-based beat tracker that operates on log-mel spectrograms at 50 fps (frames per second). It was designed for offline use — process the entire audio file at once. This provider adapts it for **streaming** use inside Music Assistant's audio pipeline, where PCM arrives in 1-second chunks from the stream controller.

### Pipeline overview

```
PCM chunks (1s, any sample rate)
        │
        ├──────────────────────────────────┐
        ▼                                  ▼
   Buffer accumulator (10s blocks)    VQT feature extraction (per chunk)
        │                                  │
        ▼                                  ▼
   Streaming soxr resampling           [accumulate VQT features]
   → 22050 Hz mono                         │
        │                                  ▼
        ├──────────────┐             ChromaNet key inference
        ▼              ▼                   │
   Log-mel         RMS energy +            ▼
   extraction      spectral centroid   key + mode
   (delayed        (parallel)
    frames)
        │
        ▼
   [repeat for all blocks]
        │
        ▼
   Concatenate all feature blocks
        │
        ▼
   Spect2Frames model inference (quantized, single pass)
        │
        ▼
   DBN postprocessor (pure-numpy Viterbi decoding)
        │
        ▼
   beats[] + downbeats[] (timestamps in seconds)
```

### Key design decisions

#### 1. Streaming resampling (10-second blocks)

PCM arrives at the source sample rate (e.g. 44100 Hz) but Beat This! expects 22050 Hz. Resampling per 1-second chunk with a stateless resampler introduces edge artifacts because it pads each chunk independently. Instead, we accumulate 10 seconds of PCM and resample using a stateful `soxr.ResampleStream` that maintains filter state across blocks, eliminating block-boundary artifacts while matching the resampling quality of the offline reference pipeline.

#### 2. Delayed frame output in the feature extractor

The mel spectrogram uses `center=True` with `n_fft=1024`, meaning each frame needs 512 samples of context on both sides. At block boundaries, the last few frames would normally be computed with reflect-padded forward context instead of real audio.

To fix this, the feature extractor **delays the last 2 frames** (`ceil(512/441) = 2`) of each block. When the next block arrives, those frames are recomputed with real forward context from the new audio, producing spectrogram values identical to the offline pipeline. This is the single most important trick for streaming parity.

#### 3. Hop-aligned audio segments

The feature extractor aligns the start of each audio segment to a `hop_length` (441 sample) boundary. This ensures that segment-local frame indices map exactly to global frame positions via integer arithmetic, avoiding off-by-one errors that would cause 20ms beat shifts.

#### 4. Single-pass model inference at finalize

Unlike the feature extraction (which runs incrementally per block), model inference runs once on the concatenated features when the track ends. The Beat This! transformer (`Spect2Frames`, `small0` checkpoint, dynamically quantized to qint8) processes the full spectrogram in a single forward pass. The DBN postprocessor — a pure-numpy reimplementation of madmom's `DBNDownBeatTrackingProcessor` using Viterbi decoding over a bar-pointer HMM — converts frame-level logits to beat/downbeat timestamps.

#### 5. Musical key detection (S-KEY)

Key detection runs in parallel with beat tracking. Each 1-second PCM chunk is independently resampled to 22050 Hz and passed through a Variable-Q Transform (VQT) to extract tonal features. At finalization, the accumulated VQT features are concatenated and fed into ChromaNet, which classifies the track into one of 24 keys (12 pitch classes x major/minor). Per-chunk VQT extraction uses stateless one-shot resampling because each chunk is processed independently — this cannot share the streaming resampler's session state.

#### 6. RMS energy and spectral centroid

Per-block RMS energy (100ms windows) and spectral centroid (per-hop-frame via torchaudio) are computed in parallel with mel spectrogram extraction. At finalization, both are interpolated to 1800 fixed bins spanning the track duration. RMS energy is peak-normalized, and spectral centroid is zeroed where energy is negligible to suppress noise-dominated regions.

#### 7. FireRed vocal activity

A dedicated stateful soxr stream resamples source PCM to 16kHz for FireRed AED. Online Kaldi fbank extraction uses the reference 80-bin, 25ms frame, 10ms shift configuration with fixed CMVN. The bundled model has 588,931 parameters and is about 2.3MB. FireRed inference runs concurrently with the sequential beat-then-key branch through the shared analysis worker limits. Long inputs are processed in bounded chunks with model context.

The persisted `extra_data["vocal_activity"]` list contains 100ms `max(speech, singing)` probabilities aligned to the analysis duration. FireRedVAD source and AED model weights are Apache-2.0 licensed; attribution is recorded in the project `NOTICE`.
