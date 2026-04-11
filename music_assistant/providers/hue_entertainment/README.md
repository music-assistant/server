# Philips Hue Entertainment Plugin

Syncs Philips Hue lights to music using the Entertainment API. Each entertainment area on a paired Hue bridge reacts to music in real time when joined to a playing group.

## Architecture

```
Sendspin Server (PushStream → VisualizerV1Role → FFT/spectrum)
       │
       ▼ (WebSocket, timed delivery)
SendspinClient (VISUALIZER role)
       │
       ▼ (VisualizerFrame callback at playback time)
HueAudioAnalyzer (beat detection, color cycling, bass pulse)
       │
       ▼ (HueStream v2 binary protocol)
HueDtlsStreamer (DTLS 1.2 + PSK, pure Python)
       │
       ▼ (encrypted UDP port 2100)
Hue Bridge → Entertainment Area Lights
```

The bridge connects as a Sendspin WebSocket client with the VISUALIZER role. The server handles all audio analysis (FFT, loudness, spectrum) and delivers pre-computed `VisualizerFrame` packets at the correct playback time through the connection layer's built-in scheduling.

## Effect Modes

| Mode | Description |
|------|-------------|
| **Spectrum** | Frequency bands spread across lights with vibrant palette colors. Colors cycle on beats. Bass drives brightness pulse. Beat detection triggers white flash overlay. |
| **Bass Boost** | All lights pulse with bass energy in warm tones. Beats flash with cycling palette colors. |
| **Ambient** | Slow hue rotation with gentle energy modulation. Relaxing, smooth transitions. |

## DTLS Implementation

Pure-Python DTLS 1.2 PSK using the `cryptography` library (AES-128-GCM) and stdlib (hmac, hashlib, socket). No ctypes, no C bindings, no external DTLS dependencies.

The HueStream v2 protocol sends per-channel RGB colors as duplicated bytes (Q42.HueApi convention) with the entertainment area UUID as a 36-byte ASCII string.

## File Structure

```
hue_entertainment/
├── hue_sendspin_bridge/       Reusable bridge package
│   ├── dtls.py                Pure-Python DTLS 1.2 PSK + HueStream protocol
│   ├── analyzer.py            Beat detection, color cycling, effect modes
│   ├── api.py                 Hue REST API (pairing, entertainment areas)
│   ├── models.py              EntertainmentArea, LightChannel, LightColorCommand
│   └── constants.py           Protocol and analysis constants
├── __init__.py                Config flow (pairing, settings)
├── provider.py                mDNS discovery, lifecycle management
├── bridge.py                  Sendspin client + Hue DTLS bridge
├── constants.py               MA config keys
└── manifest.json              Experimental plugin manifest
```

## Configuration

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `bridge_host` | String | — | Bridge IP (auto-discovered via mDNS) |
| `brightness` | Integer | 100 | Overall light brightness (0-100) |
| `intensity` | Integer | 70 | Beat reactivity / flash intensity (0-100) |
| `color_mode` | String | spectrum | Effect mode (spectrum / bass_boost / ambient) |
