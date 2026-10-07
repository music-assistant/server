# Hue Lights Sync Plugin

Syncs Philips Hue lights to music using the Entertainment API. Each entertainment area on a paired Hue bridge reacts to music in real time when joined to a playing group.

## Architecture

```
Sendspin Server (PushStream → group visualizer/color roles → FFT/spectrum + beats + palette)
       │
       ▼ (in-process bridge roles subscribe to the group's visualizer/color roles)
Bridge visualizer + color roles (features keyed to playback timestamp)
       │
       ▼ (frame / beats / color callbacks)
HueAudioAnalyzer (queues by timestamp; bass beat detection, color cycling, energy pulse)   ← this provider
       │
       ▼ (30 Hz render loop drains at server-clock + Hue-latency lead → LightColorCommand frames)
EntertainmentSession → HueDtlsStreamer (DTLS 1.2 PSK + HueStream v2)   ← hue-entertainment lib
       │
       ▼ (encrypted UDP port 2100)
Hue Bridge → Entertainment Area Lights
```

The bridge registers with the local Sendspin server as an **in-process external visualizer client** (`register_external_player`) — no WebSocket is involved. Its bridge visualizer and color roles subscribe directly to the playing group's visualizer/color roles and receive extracted features (spectrum, onset peaks, beat schedule, colour palette) through callbacks. Because in-process delivery follows the audio push, features arrive **ahead of the playhead** (audio is buffered seconds in advance); the analyzer queues them by playback timestamp and drains them at render time. A fixed-rate 30 Hz render loop samples the analyzer at the current server clock plus a configurable Hue-latency lead and sends one DTLS frame per tick.

Entertainment areas are discovered at plugin (re)load from the Hue bridge REST API. Each area gets its own in-process Sendspin client and `EntertainmentSession`.

## Effect Modes

| Mode | Description |
|------|-------------|
| **Smooth** (default) | Spectrum-driven brightness with a slowly drifting palette. |
| **Ambient** | Colour cycling only, no brightness modulation — relaxing, smooth transitions. |
| **Flashing** | Brightness pulse on every beat, stronger on downbeats. |
| **Energetic** | Large brightness swings on hits plus fast palette rotation. |

## Hue streaming layer

The Hue bridge REST API (pairing, area discovery, entertainment start/stop) and the
pure-Python DTLS 1.2 PSK + HueStream v2 streaming live in the standalone
[`hue-entertainment`](https://github.com/music-assistant/hue-entertainment) library
(published on PyPI, pinned in `manifest.json`). This provider drives it through the
library's `EntertainmentSession` facade, which opens the stream on demand, runs the
blocking DTLS handshake in an executor, and enforces the bridge's single-active-stream
constraint.

Only the Sendspin-specific glue lives here: the visualizer client wiring (`bridge.py`)
and the audio-to-color analyzer (`analyzer.py`).

## File Structure

```
hue_entertainment/
├── __init__.py                Config flow (pairing, settings)
├── provider.py                mDNS discovery, lifecycle management
├── bridge.py                  Sendspin visualizer client → analyzer → EntertainmentSession
├── analyzer.py                Bass beat detection, color cycling, effect modes
├── constants.py               MA config keys + Sendspin spectrum request config
└── manifest.json              Experimental plugin manifest (requires hue-entertainment)
```

The Hue REST API, DTLS streamer, `EntertainmentSession` and data models
(`EntertainmentArea`, `LightChannel`, `LightColorCommand`) are imported from the
`hue-entertainment` library.

## Configuration

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `bridge_host` | String | — | Bridge IP (auto-discovered via mDNS) |
| `brightness` | Integer | 100 | Overall light brightness (0-100) |
| `color_mode` | String | smooth | Visualization mode (smooth / ambient / flashing / energetic) |
| `hue_latency_ms` | Integer | 20 | Lead time lights render ahead of the playhead (0-3000) |

## Quick Setup

1. Create an Entertainment Area in the Philips Hue app (Settings → Entertainment Areas)
2. In Music Assistant, go to Settings → Providers → Add Provider → Hue Lights Sync
3. Enter your Hue bridge IP address (or let mDNS discover it)
4. Press the physical button on your Hue bridge, then click "Pair"
5. Click Save — the entertainment area(s) appear as Light players
6. Join a Hue light player to any playing group — the lights start reacting to music

## Status

Working and tested on Hue Bridge V2 and Hue Bridge Pro. The current implementation provides a solid foundation with four effect modes and bass-driven beat detection. There is room for future improvements:

- More precise beat detection using the MA audio analyzer controller
- Genre/mood-aware effects using track metadata
- Additional effect modes (strobe, rainbow, color wash)
- Per-light position-aware effects using entertainment area spatial data
- Cover art color extraction for mood-matched lighting

## Known Limitations

- Beat detection uses bass energy spikes from the visualizer spectrum data — works well for beat-heavy music, less precise for acoustic/vocal tracks.
- Entertainment areas are discovered at plugin (re)load — adding a new area in the Hue app requires reloading the plugin.
- The Hue bridge only allows one entertainment area active at a time.
