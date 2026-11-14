# Hue Lights Sync Plugin

Syncs Philips Hue lights to music using the Entertainment API. Each entertainment area on a paired Hue bridge reacts to music in real time when joined to a playing group. Without a Hue bridge, Hue lamps on Home Assistant's ZHA (running on Ziggurat) can be synced too (experimental).

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
LightTarget (targets.py)   ← this provider
       │
       ├─ HueAreaTarget → EntertainmentSession → HueDtlsStreamer (DTLS 1.2 PSK + HueStream v2)   ← hue-entertainment lib
       │        ▼ (encrypted UDP port 2100)
       │  Hue Bridge → Entertainment Area Lights
       │
       └─ ZigbeeTarget → ZigbeeEntertainmentSession (websocket, Zigbee frames via a proxy lamp)   ← hue-entertainment lib
                ▼
          Ziggurat (ZHA's Zigbee stack) → Hue lamps
```

The bridge registers with the local Sendspin server as an **in-process external visualizer client** (`register_external_player`) — no WebSocket is involved. Its bridge visualizer and color roles subscribe directly to the playing group's visualizer/color roles and receive extracted features (spectrum, onset peaks, beat schedule, colour palette) through callbacks. Because in-process delivery follows the audio push, features arrive **ahead of the playhead** (audio is buffered seconds in advance); the analyzer queues them by playback timestamp and drains them at render time. A fixed-rate 30 Hz render loop samples the analyzer at the current server clock plus a configurable Hue-latency lead and sends one frame per tick to the light target.

On the Hue bridge backend, entertainment areas are discovered at plugin (re)load from the Hue bridge REST API. Each area gets its own in-process Sendspin client and `EntertainmentSession`. A ZHA instance has one in-process Sendspin client for its chosen lamps.

## Light targets

The bridge (`bridge.py`) streams to a `LightTarget` and does not know which backend it talks to. The target owns the session: `start()` opens the stream (raising `TargetUnavailableError` when a retry will not help), `send()` queues a frame and `aclose()` restores the lights. The backend is chosen once in the setup flow and stored as `backend` in the setup data; an instance without it uses the Hue bridge.

- `HueAreaTarget`: one per entertainment area of the paired bridge. Its client id derives from the area id.
- `ZigbeeTarget`: one per plugin instance, for the Hue lamps chosen in the setup flow (stored as `"<ieee>/<zones>"`). Its client id derives from the instance id, so choosing other lamps keeps the player. The channels sit on an evenly spaced row on the x axis, the zones of a gradient lamp next to each other. Ziggurat's URL and the lamps' network addresses come from ZHA on every start, through the Home Assistant plugin's connection (`zigbee.py`).

Music Assistant runs one Zigbee stream at a time: a start takes it over from the target that has it, which restores its lamps and stays parked until its playback stops. A lost Zigbee stream is restarted in place while music plays; the Hue bridge path does not do this.

### ZHA requirements

- The Home Assistant plugin, connected.
- ZHA running on Ziggurat. ZHA names Ziggurat by its add-on host name, which only resolves for add-ons; elsewhere Music Assistant connects to the same port (9999) on Home Assistant's host, so that port must be reachable.
- Hue lamps that support Zigbee entertainment (the Hue colour lamps), within radio range of each other: one of them relays the frames to the others.
- One streamer per lamp: while another app streams to one of the lamps, the player waits and starts once they are free.

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

Only the Sendspin-specific glue lives here: the visualizer client wiring (`bridge.py`),
the light targets (`targets.py`, `zigbee.py`) and the audio-to-color analyzer (`analyzer.py`).

## File Structure

```
hue_entertainment/
├── __init__.py                Provider entry point
├── setup_flow.py              Backend choice, bridge pairing, ZHA lamp choice
├── provider.py                mDNS discovery, lifecycle management
├── bridge.py                  Sendspin visualizer client → analyzer → LightTarget
├── targets.py                 Hue bridge area and ZHA lamp targets
├── zigbee.py                  ZHA lookups through the Home Assistant plugin
├── analyzer.py                Bass beat detection, color cycling, effect modes
├── constants.py               MA config keys + Sendspin spectrum request config
└── manifest.json              Plugin manifest (requires hue-entertainment)
```

The Hue REST API, DTLS streamer, `EntertainmentSession`, `ZigbeeEntertainmentSession` and
data models (`EntertainmentArea`, `LightChannel`, `LightColorCommand`, `ZigbeeLight`) are
imported from the `hue-entertainment` library.

## Configuration

The setup flow asks for the backend (`backend`: `hue_bridge` or `zha`), then pairs the Hue bridge (`bridge_host`, auto-discovered via mDNS, plus the credentials it returns) or takes the ZHA lamps (`zigbee_lights`) and an optional player name (`zigbee_name`). These are stored as setup data. The settings:

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `brightness` | Integer | 100 | Overall light brightness (0-100) |
| `color_mode` | String | smooth | Visualization mode (smooth / ambient / flashing / energetic) |
| `hue_latency_ms` | Integer | 20 | Lead time lights render ahead of the playhead (0-3000) |

## Quick Setup

1. In Music Assistant, go to Settings → Plugins → Add a plugin → Hue Lights Sync
2. Choose the backend: Hue Bridge, or ZHA via Ziggurat (greyed out unless the Home Assistant plugin is connected and ZHA runs on Ziggurat)
3. Hue Bridge: create an Entertainment Area in the Philips Hue app first. Enter the bridge IP (filled in when mDNS found it), press the link button on the bridge and continue within 30 seconds. Each entertainment area appears as a Light player
4. ZHA: tick the Hue lamps (only lamps that support Zigbee entertainment are listed) and optionally a name. The lamps appear together as one Light player
5. Join a Hue light player to any playing group — the lights start reacting to music

## Status

Working and tested on Hue Bridge V2, Hue Bridge Pro and Hue lamps in ZHA via Ziggurat. The current implementation provides a solid foundation with four effect modes and bass-driven beat detection. There is room for future improvements:

- More precise beat detection using the MA audio analyzer controller
- Genre/mood-aware effects using track metadata
- Additional effect modes (strobe, rainbow, color wash)
- Per-light position-aware effects using entertainment area spatial data
- Cover art color extraction for mood-matched lighting

## Known Limitations

- Beat detection uses bass energy spikes from the visualizer spectrum data — works well for beat-heavy music, less precise for acoustic/vocal tracks.
- Entertainment areas are discovered at plugin (re)load — adding a new area in the Hue app requires reloading the plugin.
- The Hue bridge only allows one entertainment area active at a time.
