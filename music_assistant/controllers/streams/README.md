# Streams Controller Architecture

This document provides an overview of the Music Assistant Streams Controller architecture, including audio buffering, streaming pipeline, and smart fades.

## Table of Contents

- [Overview](#overview)
- [Network Architecture](#network-architecture)
- [Inbound Audio](#inbound-audio)
- [Core Components](#core-components)
- [AudioBuffer](#audiobuffer)
- [StreamsAudio](#streamsaudio)
- [Streaming Pipeline](#streaming-pipeline)
- [Analyze Callbacks](#analyze-callbacks)
- [Smart Fades](#smart-fades)
- [Audio Overlay](#audio-overlay)
- [Stream Types](#stream-types)
- [Configuration](#configuration)

## Overview

The Streams Controller is a core controller that manages all audio streaming to players. It provides:
- HTTP streaming endpoints for players on the local network
- Audio buffering with configurable memory usage
- Volume normalization (dynamic, measurement-based, and fixed gain)
- Smart crossfading between tracks
- Flow mode for continuous queue playback
- Audio overlay: a looping sound effect (e.g. rain) mixed into queue playback
- Announcement and plugin source streaming
- Ahead-of-time audio analysis (loudness, beat detection) via buffer callbacks

## Network Architecture

The streams controller runs its own dedicated HTTP-only webserver on a separate port (default 8097), independent of the main webserver/API. This design is intentional:

- **No SSL/TLS**: Many audio players (especially embedded devices) have limited resources and struggle with SSL handshakes. Since the stream server only runs on the internal network, encryption is unnecessary.
- **No authentication**: Players need to access streams without credentials. Instead, stream URLs include a **session ID** that is validated on each request to prevent stale or invalid stream attempts.
- **Separate port**: Keeps audio streaming isolated from the API, allowing independent scaling and configuration.

## Inbound Audio

Live announcements (`live_announcements.py`) are the one path where audio travels *into* the stream server rather than out of it: a client pushes raw PCM while a user speaks, and it is played on a player as an ordinary announcement.

This splits across both webservers, because neither can do the job alone:

- The **inbound** half is a WebSocket on the main webserver. Audio from a client is a privileged action, so it needs the authentication and the SSL support that the stream server deliberately does not have. Browsers additionally require a secure context to reach a microphone at all, which only the main webserver can offer.
- The **outbound** half is an ordinary stream server route serving the buffered speech as a WAV. The announcement renderer only ever pulls its audio from a URL, so exposing the clip as one keeps live announcements on exactly the same path as every other announcement.

The announcement is dispatched only once the clip is complete, not while it is still being spoken. Players that announce natively need the whole clip up front: AirPlay renders it to a file and schedules a single synchronized instant across every group member from its exact duration, and Sonos needs the duration to know how long the clip runs. Handing them a clip that is still growing gives one player type a head start and truncates another, so every player gets the same finished clip instead.

A session is identified by an unguessable id that appears only in the stream URL, and it is dropped as soon as the announcement has been played.

## Core Components

```
controllers/streams/
  __init__.py          - Package init, exports StreamsController
  controller.py        - StreamsController: HTTP endpoints, public streaming API
  audio.py             - StreamsAudio: audio processing, stream acquisition, DSP/filters
  audio_buffer.py      - AudioBuffer: in-memory PCM audio buffering with seek support
  constants.py         - Shared constants (buffer sizes, config keys)
  ogg_handler.py       - Chained OGG stream stitching for radio
  smart_fades/         - Smart crossfade detection and mixing
    analyzer.py        - Beat analysis for smart fade detection
    fades.py           - Fade curve generation
    mixer.py           - Crossfade mixing logic
```

Supporting modules in `helpers/`:
- `helpers/audio.py` - Generic audio utilities (PCM helpers, format conversions, silence stripping)
- `helpers/ffmpeg.py` - FFmpeg process management

## AudioBuffer

`AudioBuffer` is the primary interface for all buffered audio streaming. It stores **raw decoded PCM audio** (no filters applied) and serves as the single source of truth for audio data.

### Design Principles

1. **Always-on buffering**: Every queue stream (tracks and radio) goes through an AudioBuffer
2. **Raw PCM only**: The buffer stores decoded audio in original sample rate and bit depth. Filters (volume normalization, playback speed, etc.) are applied when reading via `get_stream()`
3. **Pre-initialization**: Buffers are created and start filling before the player requests the stream, ensuring immediate playback start
4. **Buffer reuse**: Existing valid buffers are reused for seek operations and reconnections
5. **Smart seeking**: Forward seeks within 20 seconds of buffered data wait for the producer; larger seeks trigger a re-fetch at the seek position

### Buffer Modes

- **SEEKABLE** (tracks): Maintains a deque of 1-second PCM chunks with seek support. Old chunks are discarded when the buffer reaches max size
- **ROLLING** (radio/non-seekable): Short FIFO buffer (~15 seconds) where the consumer pops chunks sequentially

### Key Methods

- `AudioBuffer.get_buffer()` - Static factory that creates or reuses a buffer. Reads config, determines mode, starts the analysis reader, starts filling
- `AudioBuffer.get_stream()` - Get processed audio with optional filters/resampling applied
- `AudioBuffer.get_raw_stream()` - Get unprocessed raw PCM audio (playback consumer)
- `AudioBuffer.read_chunk_for_analysis()` - Read one chunk for a passive analysis reader without mutating the buffer; raises when the chunk has been evicted (reader fell behind)
- `AudioBuffer.fill()` - Start filling from an async generator of PCM chunks
- `AudioBuffer.open_provider_fill()` - Create the buffer of an item whose provider writes the PCM itself and return the `ProviderAudioFill` handle it writes into (see the Spotify README for the realtime-source flow); a later playback request finds the buffer through the usual `get_buffer()` reuse
- `AudioBuffer.undrained_seconds` - How far the source is running ahead of playback, which a source that produces faster than playback can be held back on
- `AudioBuffer.ready` - Event set when enough chunks are buffered past the seek point (threshold-based)

### Buffer Lifecycle

```
1. _load_item() fetches stream details, creates buffer with wait_ready=True
2. Buffer starts filling from get_media_stream() in background
3. Analysis (loudness, smart fades) reads the same buffer in parallel, at lower priority
4. Player requests stream -> get_queue_item_stream() calls buffer.get_stream()
5. 60s before the end of the source stream: prepare_next_audio_buffer() pre-fills next track
6. _cleanup_stale_queue_buffers() clears old buffers to free memory
```

### Error Handling

- Producer errors are captured and surfaced when consumers try to read
- Consumers can drain remaining buffered data before the error surfaces at EOF
- Errors bubble up as `AudioError` through the streaming chain

## StreamsAudio

`StreamsAudio` is the audio processing sub-controller, initialized as `self.audio` on the StreamsController. It handles all audio-related logic that needs access to the MusicAssistant instance:

- **Stream acquisition**: `get_media_stream`, `get_stream_details`, radio/HTTP/file stream helpers
- **Queue streaming**: `get_queue_item_stream`, `get_queue_item_stream_with_smartfade`, `get_queue_flow_stream`
- **Format selection**: `get_output_format`, `select_pcm_format`, `select_flow_format`
- **DSP and output plans**: `get_player_output_plan`, `get_player_dsp_details`, `get_stream_dsp_details`
- **Crossfade management**: `crossfade_allowed`, `clear_crossfade_data`
- **Loudness analysis**: `attach_loudness_analyzer` (via buffer callbacks)

`AudioProcessingManager`, initialized as `self.audio_processing` on the
StreamsController, combines queue processing and per-player output plans into complete
`AudioProcessingChain` snapshots attached to `StreamDetails`.

## Streaming Pipeline

```
Music Provider -> get_media_stream() -> FFmpeg (decode to raw PCM)
    -> AudioBuffer (raw PCM storage, analyze callbacks run here)
    -> buffer.get_stream() -> Optional: FFmpeg (volume normalization, speed, fade-in)
    -> Optional: Smart Fades (crossfade mixing between tracks)
    -> FFmpeg (encode to output format with player-specific DSP)
    -> HTTP Response / Direct PCM stream
```

### Stream Entry Points

1. **HTTP endpoints** (`serve_queue_item_stream`, `serve_queue_flow_stream`): Used by players that consume HTTP streams (Chromecast, DLNA, Sonos, etc.)
2. **Direct PCM** (`get_stream`): Used by player providers that consume raw PCM directly (AirPlay, Sendspin, etc.)

## Analyze Callbacks

AudioBuffer supports registering chunk callbacks that receive raw PCM data as it flows into the buffer. This enables ahead-of-time analysis without re-streaming:

### Loudness Measurement
- Attached automatically when a new buffer is created (tracks and radio)
- Feeds up to 2 minutes of PCM into an FFmpeg `ebur128` process
- Result stored for future volume normalization (avoids dynamic mode overhead)

### Smart Fades Beat Analysis
- Attached automatically for music tracks (MediaType.TRACK only, not podcasts/audiobooks)
- Collects first 45 seconds (intro) and last 45 seconds (outro) of audio
- Triggers librosa beat detection in a background thread
- Results cached for crossfade timing decisions

Both analyzers check for existing measurements before starting, avoiding redundant work.

## Smart Fades

The smart fades system provides intelligent crossfading between tracks:

- **Smart Crossfade**: Analyzes audio beats to detect natural fade points
- **Standard Crossfade**: Fixed-duration overlap crossfade with silence stripping
- Operates in both flow mode (continuous stream) and per-item mode (gapless playback)

## Audio Overlay

The audio overlay is a per-queue feature (configured via `player_queues/overlay`) that mixes a
looping sound effect — any `sound_effect` media item offered by a provider — into the queue's
audio stream:

- Mixing happens once per queue stream (ffmpeg `amix`, overlay looped via `-stream_loop -1`),
  so all (synced) players consuming the stream hear the identical mix.
- An active overlay forces flow mode: the overlay must play continuously across track
  boundaries, which is impossible with per-item stream requests. Radio is the exception —
  it always plays as a single long-lived stream and is wrapped per-request instead.
- The internal PCM format is upgraded to F32 (like crossfade/DSP) for clipping-free headroom.
- Failures degrade gracefully: when the overlay source can not be resolved, playback simply
  continues without overlay; when the overlay input dies mid-stream, ffmpeg keeps passing
  the main audio. Music playback is never interrupted by the overlay.
- Note: audio already sitting in a player's (pre)buffer is unaffected by overlay changes,
  which is why the queue controller restarts playback on an audible change. For the same
  reason a seek can momentarily shift the overlay position — acceptable for ambient content.

## Stream Types

| Type | AudioBuffer | Description |
|------|-------------|-------------|
| Queue tracks | Yes (SEEKABLE) | Regular track playback with full buffering |
| Radio streams | Yes (ROLLING) | Short rolling buffer, non-seekable |
| Announcements | Yes (SEEKABLE) | Short one-off audio (TTS), rendered once and shared by all consumers |
| Plugin sources | No | Real-time audio (microphone, aux), streamed directly |

## Configuration

Key configuration entries (in streams controller config):

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `buffer_size` | String | Memory-dependent (`maximum` >=8GB, `balanced` >=4GB, `minimal` <4GB) | Audio buffer size preset |
| `volume_normalization_radio` | String | `fallback_dynamic` | Normalization mode for radio |
| `volume_normalization_tracks` | String | `fallback_dynamic` | Normalization mode for tracks |
| `allow_crossfade_same_album` | Boolean | `false` | Whether to crossfade consecutive album tracks |
