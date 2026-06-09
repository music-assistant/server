# Hue Lights Sync Plugin

Syncs Philips Hue lights to music using the Entertainment API. Each entertainment area on a paired Hue bridge reacts to music in real time when joined to a playing group.

## Architecture

```
Sendspin Server (PushStream → VisualizerV1Role → FFT/spectrum)
       │
       ▼ (WebSocket, buffer-tracked delivery)
SendspinClient (VISUALIZER role, small buffer for near-realtime)
       │
       ▼ (VisualizerFrame callback, scheduled via compute_play_time)
HueAudioAnalyzer (bass beat detection, color cycling, energy pulse)
       │
       ▼ (HueStream v2 binary protocol)
HueDtlsStreamer (DTLS 1.2 + PSK, pure Python)
       │
       ▼ (encrypted UDP port 2100)
Hue Bridge → Entertainment Area Lights
```

The bridge connects as a Sendspin WebSocket client with the VISUALIZER role and a small buffer capacity (~100 bytes) so frames arrive near playback time rather than seconds ahead. Each frame is scheduled via `compute_play_time()` with Hue latency compensation.

Entertainment areas are discovered at plugin (re)load from the Hue bridge REST API. Each area gets its own Sendspin client and DTLS connection.

## Effect Modes

| Mode | Description |
|------|-------------|
| **Spectrum** | Frequency bands spread across lights with vibrant palette colors. Colors and channel assignments rotate on beats. Bass-driven beat detection triggers white strobe on high energy peaks. Energy-adaptive color cycling speed. |
| **Bass Boost** | All lights pulse with bass energy in warm tones. Beats flash with cycling palette colors or white strobe on peaks. |
| **Ambient** | Slow hue rotation with gentle energy modulation. Per-channel hue offset for depth. Relaxing, smooth transitions. |

## DTLS Implementation

Pure-Python DTLS 1.2 PSK using the `cryptography` library (AES-128-GCM) and stdlib (hmac, hashlib, socket). No ctypes, no C bindings, no external DTLS dependencies.

The HueStream v2 protocol sends per-channel RGB colors as duplicated bytes with the entertainment area UUID as a 36-byte ASCII string.

## File Structure

```
hue_entertainment/
├── hue_sendspin_bridge/       Reusable bridge package
│   ├── dtls.py                Pure-Python DTLS 1.2 PSK + HueStream protocol
│   ├── analyzer.py            Bass beat detection, color cycling, effect modes
│   ├── api.py                 Hue REST API (pairing, entertainment areas)
│   ├── models.py              EntertainmentArea, LightChannel, LightColorCommand
│   └── constants.py           Protocol and analysis constants
├── __init__.py                Config flow (pairing, settings)
├── provider.py                mDNS discovery, lifecycle management
├── bridge.py                  Sendspin visualizer client + Hue DTLS bridge
├── constants.py               MA config keys
└── manifest.json              Experimental plugin manifest
```

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

Working and tested on Hue Bridge V2 and Hue Bridge Pro. The current implementation provides a solid foundation with three effect modes and bass-driven beat detection. There is room for future improvements:

- More precise beat detection using the MA audio analyzer controller
- Genre/mood-aware effects using track metadata
- Additional effect modes (strobe, rainbow, color wash)
- Per-light position-aware effects using entertainment area spatial data
- Cover art color extraction for mood-matched lighting

## Known Limitations

- Beat detection uses bass energy spikes from the visualizer spectrum data — works well for beat-heavy music, less precise for acoustic/vocal tracks.
- Entertainment areas are discovered at plugin (re)load — adding a new area in the Hue app requires reloading the plugin.
- The Hue bridge only allows one entertainment area active at a time.
