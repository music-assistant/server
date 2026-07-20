# AriaCast Receiver for Music Assistant

![Android](https://img.shields.io/badge/Android-3DDC84?style=for-the-badge&logo=android&logoColor=white)

**Cast audio from any Android app to your Music Assistant speakers — like AirPlay for Android.**

Stream music, podcasts, videos, games — anything playing on your Android device — wirelessly to any speaker or group in your Music Assistant ecosystem.

---

## Features

- **High-quality streaming** — 48 kHz 16-bit stereo PCM audio
- **Zero-config discovery** — servers automatically detected on your local network
- **Rich metadata** — album art, track info, playback position
- **Bidirectional control** — control playback from both Music Assistant and your phone
- **Flexible routing** — stream to any player or speaker group

---

## Quick Start

### 1. Enable the plugin

Go to **Music Assistant → Settings → Providers**, find **AriaCast Receiver** and enable it.

### 2. Configure (optional)

| Setting | Default | Description |
|---------|---------|-------------|
| Connected Player | Auto | Target MA player. "Auto" uses the currently-playing player or the first available one. |

### 3. Install the Android app

Download the [AriaCast app](https://github.com/AriaCast/AriaCast-app) and install it on your device.

### 4. Start casting

1. Open the AriaCast app on your Android device.
2. Your Music Assistant server appears automatically.
3. Tap to connect, then play any audio — it streams to Music Assistant.

---

## How it works

```
┌──────────────────┐   UDP :12888 (DISCOVER_AUDIOCAST)   ┌─────────────────────┐
│  Android Device  │ ←──────────────────────────────────→ │  Music Assistant    │
│  (AriaCast App)  │                                      │  (This Plugin)      │
└──────────────────┘                                      └─────────────────────┘
         │                                                          │
         │   WS /audio    — 3840-byte PCM frames →                 │
         │   WS /control  ↔ {"action"/"command": "play"/"pause"/…} │
         │   WS /metadata ← live track info broadcasts             │
         │   WS /stats    ← buffer/frame stats, once per second    │
         │   POST /metadata → track info from sender               │
```

The plugin implements the [AriaCast v1.1 protocol](https://github.com/AriaCast/AriaCast-Protocol-Spec) natively in Python — no external binary required. `/control` is bidirectional: Music Assistant pushes playback commands to the sender, and the sender can push commands (including volume) back.

---

## Technical reference

### Audio format

| Parameter | Value |
|-----------|-------|
| Sample rate | 48 000 Hz |
| Channels | 2 (stereo) |
| Bit depth | 16-bit signed LE |
| Frame size | 3 840 bytes (20 ms) |

### Network endpoints

| Endpoint | Type | Port | Purpose |
|----------|------|------|---------|
| UDP broadcast | Datagram | 12888 | Server discovery |
| `GET /audio` | WebSocket | 12889 | PCM audio stream (single sender at a time) |
| `GET /control` | WebSocket | 12889 | Bidirectional playback + volume commands |
| `GET /metadata` | WebSocket | 12889 | Metadata subscription |
| `GET /stats` | WebSocket | 12889 | Buffer/frame statistics, pushed every second |
| `POST /metadata` | HTTP | 12889 | Metadata push from sender |
| `POST /api/command` | HTTP | 12889 | External command (MA / web UI) |
| `GET /artwork` | HTTP | 12889 | Cached artwork image |

---

## Troubleshooting

**Server not appearing in the app**
- Both devices must be on the same network/VLAN.
- Firewall must allow UDP 12888 (discovery) and TCP 12889 (streaming).
- Check the plugin is enabled and shows no errors in the MA log.

**No audio playback**
- Make sure at least one MA player is available and not exclusively in use.
- Check MA logs (`Settings → Logging`) for errors from `ariacast_receiver`.
- Only one Android device can stream at a time. A second connection attempt is rejected (HTTP 403) until the first one disconnects.

**Audio stutters**
- Check Wi-Fi signal strength on the Android device.
- Disable power-saving mode on the Android device.

---

## Related projects

- [AriaCast Android App](https://github.com/AriaCast/AriaCast-app)
- [AriaCast Protocol Spec](https://github.com/AriaCast/AriaCast-Protocol-Spec)
- [Music Assistant](https://music-assistant.io/)

---

## License

MIT
