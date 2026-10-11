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
- **Reachability probe**: `GET /info` answers with the server id and CORS headers (preflight included), so a browser on the local network can check that the published address leads to this server. The `streams/info` API command reports that address.

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
  audio_analysis.py    - AudioAnalysisController: analysis storage (audio_analysis.db) and scheduling
  audio_analysis_database.py - AudioAnalysisDatabaseMixin: connection, schema version and quarantine of audio_analysis.db
  constants.py         - Shared constants (buffer sizes, config keys)
  ogg_handler.py       - Chained OGG stream stitching for radio
  smart_fades/         - Smart crossfade planning and mixing
    mixer.py           - SmartFadesMixer: loads both analysis rows, picks the fade, runs the mix
    fades.py           - SmartCrossFade, StandardCrossFade and VoiceOverFade: the ffmpeg mix per mode
    planner/           - Transition planner, pure over the stored analysis (no audio bytes)
      planner.py       - SmartCrossFadePlanner: pipeline, rescue pass, fallbacks, per-transition log line
      context.py       - TransitionContext: per-transition facts (anchors, tier, vocals, kicks, segue facts)
      candidates.py    - Candidate generators and the CandidateFactory that times each spec
      policies.py      - Policies that reject or penalize a candidate
      selection.py     - CandidateSelector: scores every candidate and applies the style rules
      assembly.py      - PlanAssembler (winner EQ), fallback crossfade and emergency handoff factories
    renderer.py        - TransitionRenderer: turns a TransitionPlan into the filter chain and timing
    filters.py         - FFmpeg filters (EQ, time stretch, high-pass sweep, echo out, crossfade)
    models.py          - TransitionPlan, TransitionStyle, TransitionTier and the other data models
    bands.py           - Bar-level band power and kick runs from the stored band envelopes
    structure.py       - Mastered fade-out and coda detection
    vocal.py           - Vocal activity windows and the vocal collision math
    helpers.py         - Shared constants plus energy, beat grid and key helpers
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

- **SEEKABLE** (tracks): Maintains a deque of 1-second PCM chunks with seek support. Old chunks are discarded when the buffer reaches max size, keeping up to 60 seconds (a fifth of the window on the smallest setting) of played audio for skipping back
- **ROLLING** (radio/non-seekable): Short FIFO buffer (~15 seconds) where the consumer pops chunks sequentially

### Key Methods

- `AudioBuffer.get_buffer()` - Static factory that creates or reuses a buffer. Reads config, determines mode, starts the analysis reader, starts filling
- `AudioBuffer.get_stream()` - Get processed audio with optional filters/resampling applied
- `AudioBuffer.get_raw_stream()` - Get unprocessed raw PCM audio (playback consumer)
- `AudioBuffer.read_chunk_for_analysis()` - Read one chunk for a passive analysis reader without mutating the buffer; raises when the chunk has been evicted (reader fell behind)
- `AudioBuffer.fill()` - Start filling from an async generator of PCM chunks
- `AudioBuffer.ready` - Event set when enough chunks are buffered past the seek point (threshold-based)

### Buffer Lifecycle

```
1. _load_item() fetches stream details, creates buffer with wait_ready=True
2. Buffer starts filling from get_media_stream() in background
3. Analysis (loudness, smart fades) reads the same buffer in parallel, at lower priority
4. Player requests stream -> get_queue_item_stream() calls buffer.get_stream()
5. prepare_next_audio_buffer() pre-fills the item after the streamed one: 60s before the end of
   its stream, or for a realtime source once its audio has fully arrived
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
- **Crossfade management**: `crossfade_allowed`, `clear_crossfade_handover`

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

## Audio Analysis

When a buffer starts from the beginning of an item (not for audio sources or sound effects), `audio_buffer.py` hands it to `AudioAnalysisController.start_analysis()`. Every audio analysis provider can accept the session and reads the same retained PCM at low priority, so a track is analysed while it plays without a second fetch. Results are stored in `audio_analysis.db`.

### Loudness Measurement
- Produced by the `loudness_analysis` provider (`providers/loudness_analysis/`): FFmpeg `ebur128` over up to 10 minutes of PCM, for tracks and radio
- Result stored for future volume normalization (avoids dynamic mode overhead)

### Smart Fades Analysis
- Produced by the `smart_fades` audio analysis provider (`providers/smart_fades/`), for music tracks only (`MediaType.TRACK`): live through `AudioAnalysisController.start_analysis()` while a track streams, and by the background scan for local files
- Stores the whole track's beats, downbeats, BPM, meter, key, RMS energy, four band RMS envelopes (`band_rms_*`) and FireRed vocal activity in `audio_analysis.db`; the provider's README describes the models
- The mixer reads both rows with `get_audio_analysis()` at `SMART_FADES_ANALYSIS_DOMAIN` priority

## Smart Fades

Smart fades mix the outgoing track's held-back tail (up to 45 seconds) with the incoming track's head in one ffmpeg process, in both flow mode (continuous stream) and per-item mode (gapless playback). `SmartFadesMixer.build()` picks the fade for each boundary from the crossfade mode:

- `SMART_CROSSFADE`: `SmartCrossFade` plans the transition with `SmartCrossFadePlanner` and renders it with `TransitionRenderer`
- `STANDARD_CROSSFADE`: `StandardCrossFade`, a fixed-length overlap after stripping trailing silence
- `VOICE_OVER`: `VoiceOverFade`, a declared transition that plays as declared and is never planned

A smart crossfade falls back to the standard crossfade when a track has no analysis row with BPM and beats, when the planner raises `SmartFadeNotApplicable` (the held-back tail falls silent within its first 8 seconds, or no candidate fits), or when the build fails.

### Planner pipeline

The planner reads only the two `AudioAnalysisData` rows and the length of the held-back tail, never audio. `SmartCrossFadePlanner.plan()` runs:

1. `build_transition_context()` computes the frozen `TransitionContext`: the outgoing anchor (energy mix-out point, folded with the kick die-out and snapped to a downbeat) and audible end, the tier and what made it a quick fade, vocal masks, kick runs, mastered fade and coda zones, and the segue facts. A quiet but audible outro is anchored at its audible end and planned like any other tail.
2. The generators in `default_generators()` emit `CandidateSpec`s: bar ladders at several outgoing anchors and incoming entries, segue overlaps, filter outs and echo outs.
3. `CandidateFactory.build()` times each spec into a `Candidate`, a `TransitionPlan` (anchor, overlap, entry trim, tempo ramp) plus `PlanMetrics` (audible trim, vocal collision, kick clash), or drops it as infeasible.
4. `CandidateSelector` scores every candidate against every policy in `default_policies()` and picks the lowest total penalty among the survivors, under the style rules below. Every policy runs on every candidate, so the VERBOSE scoreboard is complete.
5. `PlanAssembler.finalize()` adds the EQ handover to the winner only.
6. `TransitionRenderer` turns the plan into the filter chain and `CrossfadeTimingInfo`.

When every candidate is rejected, or no blend or cut survives, a rescue pass tries late-anchored rungs, a segue (also for a beatmatchable pair) and the dressed styles. Without a winner there, `FallbackCrossfadeFactory` ships a plain 8 second equal-power fade, or `EmergencyHandoffFactory` a 0.4 to 1 second click-free handoff when the fallback's vocal collision exceeds twice the candidate limits.

### Transition styles

The plan's `TransitionStyle` says how it is timed and rendered. `TransitionContext.preferred_style` is `BLEND` for a beatmatchable tier and `SEGUE` for every other pair, and `OverlapPreferencePolicy` penalizes the other styles.

- `BLEND`: the beatmatched blend, on tier `FULL_BLEND` or `TEMPO_BLEND`. A pair is beatmatchable with the same meter, a BPM gap of at most 8 % and at least 8 regular downbeats before the anchor; `FULL_BLEND` also needs compatible keys, RMS data and 4/4. 8 bars, 16 on a full blend between two near-instrumental decks, with a bass, mid and high EQ handover. The outgoing track ramps its tempo (rubberband, at most 8 %) in the 10 seconds before the overlap; `_drop_unneeded_stretch()` ships the blend unstretched when a deck has no kick in the overlap. A segue never replaces a surviving blend.
- `SEGUE`: an unsynced overlap where the outgoing track has gone quiet. The quiet tail runs from the last bar-smoothed point at or above -8 dB of the track's sustained level to the audible end, the quiet head from the incoming start to its first rise to that level. The overlap is their sum, capped at 15 seconds and the room, and shrinks in steps down to the cut's length (at least 2 seconds) for the clash checks to choose from; quiet material shorter than that gives no segue. Within the quiet material a side that is already quiet plays as recorded (`nofade`) and a loud side fades equal-power; a longer overlap fades both sides. A segue that fades both sides keeps the EQ handover. A segue replaces a cut only when it lasts at least as long.
- `FILTER_OUT`: 4 or 2 outgoing bars (2 across meters) under a high-pass that sweeps from 20 to 600 Hz along with the volume fade, so the outgoing kick drops out. It suits a pair in the same meter up to a 20 % BPM gap.
- `ECHO_OUT`: the outgoing dry signal stops on a downbeat, the beat before it repeats as four decaying taps at the outgoing tempo, and the incoming track starts at full level on its first downbeat. The mix runs through a -0.5 dB limiter. It suits a BPM gap above 20 % or a pair across meters.
- `CUT`: an unsynced volume fade without EQ: 4 bars up to a 12 % BPM gap, 2 bars up to 20 %, 1 bar beyond, at most 2 bars across meters.

A dressed style (`FILTER_OUT`, `ECHO_OUT`) replaces a winning cut whose kicks overlap for more than one beat, and wins on its own only in the rescue pass. The style that suits the gap (`TransitionContext.dressed_style`) wins when one survives, the other is the fallback.

### Clash checks

Both checks only count what plays on both decks at once, so a voice or a kick on one side is never a clash.

- Vocals (`VocalCollisionPolicy`): rejects 2 seconds or more of overlapping vocals, or 0.35 seconds or more weighted by the fade's simultaneous gain (`4p(1-p)`), and penalizes less. It abstains when both decks read as singing throughout (`vocal_collision_reliable`), and such a pair gets no segue.
- Drums (`RhythmClashPolicy`): a bar carries a kick when its stored low band (20 to 120 Hz) reaches half the track's reference. It judges segues and dressed styles only, rejecting more than 2 bars of overlapping kicks, weighted the same way, and penalizing less. A blend beatmatches its kicks, and a clashing cut gives way to a dressed style at selection.

Within its steps, the clash checks decide how long a segue runs. A segue may also run up to 15 seconds over loud material when neither deck sings near the boundary (vocal data required on both) and at least one of them has almost no kick there (`_beatless_long_qualifies()`).

### Logging and replay

With the streams setting `smart_fades_log_level` at DEBUG, the planner logs one `planned transition:` line per boundary: style, tier, quick fade trigger (meter, tempo or beat grid), strategy, winning generator, bars, overlap, BPM gap, whether a blend stretches, and a reason for a segue (quiet tail and head, curves, which deck sings and kicks) or a dressed style (the kick clash of the cut it replaced). VERBOSE adds the context line and the full per-candidate scoreboard.

`scripts/smart_fades_replay.py` plans random track pairs from copies of a server's `audio_analysis.db` and `library.db` without playing anything, and writes `pairs.csv` and `summary.txt` (tiers, styles, overlap lengths, quick fade triggers, vocal and drum overlap, music style). To measure a planner change, run it on both sides with the same databases, seed and `--buffer`; `--code` loads `music_assistant` from another checkout. It reads analysis data and library metadata only, never audio.

```
python -m scripts.smart_fades_replay \
    --analysis-db ~/.musicassistant/audio_analysis.db \
    --library-db ~/.musicassistant/library.db \
    --n 3000 --seed 20261010 --buffer 45 --out /tmp/replay
```

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
