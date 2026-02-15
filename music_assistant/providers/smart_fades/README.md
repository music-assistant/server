# Smart Fades Provider

Audio analysis provider that detects **beats** and **downbeats** in real time using the [Beat This!](https://github.com/CPJKU/beat_this) neural network (CPJKU, ISMIR 2024). The detected timing information drives smart crossfade positioning in Music Assistant's playback queue.

## How it works

Beat This! is a transformer-based beat tracker that operates on log-mel spectrograms at 50 fps (frames per second). It was designed for offline use — process the entire audio file at once. This provider adapts it for **streaming** use inside Music Assistant's audio pipeline, where PCM arrives in 1-second chunks from the stream controller.

### Pipeline overview

```
PCM chunks (1s, any sample rate)
        │
        ▼
   Buffer accumulator (10s blocks)
        │
        ▼
   soxr resampling → 22050 Hz mono
        │
        ▼
   Log-mel feature extraction (streaming, with delayed frames)
        │
        ▼
   [repeat for all blocks]
        │
        ▼
   Concatenate all feature blocks
        │
        ▼
   Spect2Frames model inference (single pass)
        │
        ▼
   Postprocessor ("minimal" mode)
        │
        ▼
   beats[] + downbeats[] (timestamps in seconds)
```

### Key design decisions

#### 1. Per-block resampling (10-second blocks)

PCM arrives at the source sample rate (e.g. 44100 Hz) but Beat This! expects 22050 Hz. Resampling per 1-second chunk introduces edge artifacts because a stateless resampler pads each chunk independently. Instead, we accumulate 10 seconds of PCM and resample the entire block at once using [soxr](https://github.com/dofuuz/python-soxr), matching the resampling quality of the offline reference pipeline.

#### 2. Delayed frame output in the feature extractor

The mel spectrogram uses `center=True` with `n_fft=1024`, meaning each frame needs 512 samples of context on both sides. At block boundaries, the last few frames would normally be computed with reflect-padded forward context instead of real audio.

To fix this, the feature extractor **delays the last 2 frames** (`ceil(512/441) = 2`) of each block. When the next block arrives, those frames are recomputed with real forward context from the new audio, producing spectrogram values identical to the offline pipeline. This is the single most important trick for streaming parity.

#### 3. Hop-aligned audio segments

The feature extractor aligns the start of each audio segment to a `hop_length` (441 sample) boundary. This ensures that segment-local frame indices map exactly to global frame positions via integer arithmetic, avoiding off-by-one errors that would cause 20ms beat shifts.

#### 4. Single-pass model inference at finalize

Unlike the feature extraction (which runs incrementally per block), model inference runs once on the concatenated features when the track ends. The Beat This! transformer (`Spect2Frames`) processes the full spectrogram in a single forward pass, and the postprocessor converts frame-level logits to beat/downbeat timestamps.

## File structure

| File | Purpose |
|---|---|
| `__init__.py` | Provider entry point and `setup()` function |
| `provider.py` | `SmartFadesProvider` — session management, PCM decoding, block processing, model inference |
| `feature_extractor.py` | `AdvancedBeatFeatureExtractor` — streaming log-mel extraction with delayed frames |
| `helpers.py` | `build_smart_fades_analysis()` — converts raw beats/downbeats to `SmartFadesAnalysis` model |
| `manifest.json` | Provider metadata |

## Beat This! model details

- **Model**: `Spect2Frames` with checkpoint `final0` (default pretrained weights)
- **Postprocessor**: `minimal` mode (peak-picking, no Dynamic Bayesian Network)
- **Input**: Log-mel spectrogram — 128 mel bins, 50 fps (hop=441 at 22050 Hz)
- **Output**: Per-frame beat and downbeat logits, converted to timestamps
- **License**: MIT

## Streaming vs offline parity

With the delayed-frame and per-block resampling approach, the streaming pipeline produces output identical to Beat This! `File2Beats` on the test fixture (32 beats, 8 downbeats, 0ms maximum difference). On real-world music across 49 test songs, 44/49 songs achieve >= 99% beat match within 20ms tolerance. The remaining differences are inherent to 10-second block boundaries where minor spectrogram edge effects can shift a beat by one frame (20ms).
