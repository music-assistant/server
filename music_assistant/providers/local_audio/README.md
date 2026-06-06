# Local Audio Out Provider

## Overview

The Local Audio Out provider exposes locally attached soundcards as players in Music Assistant. On Linux it supports two backends: **PulseAudio/PipeWire** (enumerates PA sinks — USB DACs, built-in audio, HDMI, remap sinks, virtual sinks, etc.) and **ALSA direct** (enumerates hardware `hw:` devices via PortAudio). On macOS it enumerates CoreAudio devices via PortAudio. It leverages the Sendspin provider for synchronization and timing, registering each device as an external Sendspin bridge client.

### Key Features

- **Automatic Device Discovery**: On Linux with PulseAudio/PipeWire, enumerates all output sinks via `pactl --format=json` — returns native sample rate and format regardless of active stream state. On Linux with ALSA direct, enumerates hardware `hw:` devices via PortAudio. On macOS, enumerates via PortAudio/sounddevice
- **Backend Selector** *(Linux)*: Choose between Auto (PulseAudio/PipeWire if available, else ALSA direct), PulseAudio/PipeWire, or ALSA direct. Auto mode detects PulseAudio/PipeWire first and falls back to ALSA if unavailable
- **Native Format Negotiation** *(Linux PulseAudio)*: Each PA sink advertises its native sample rate and bit depth (16, 24, or 32-bit) so Music Assistant transcodes to the correct format — no unnecessary resampling
- **Sendspin Integration**: Each device is registered as a Sendspin bridge client, enabling synchronized multi-room playback
- **Software Volume Control**: Per-device volume and mute via PCM sample scaling. On the PulseAudio backend, PA sinks are set to 100% hardware volume at startup so the full dynamic range is available to the software scaler. On ALSA direct, hardware mixer levels must be set by the user (e.g. via `alsamixer`) as MA does not control ALSA hardware volumes
- **Stable Player IDs**: Uses UUIDv5 derived from device name + host API index so players persist across restarts
- **Volume State Persistence**: Volume and mute state are cached and restored on restart

## Architecture

### Component Overview

```
┌──────────────────────────────────────────────────────────────┐
│                    LocalAudioProvider                         │
│  - Thin provider shell, delegates to bridge manager          │
│  - Verifies libpulse-simple present on Linux (PA backend)    │
└──────────────────────────────────────────────────────────────┘
                              │
                ┌─────────────▼──────────────┐
                │  LocalAudioBridgeManager   │
                │  - Enumerates devices      │
                │  - Creates/stops bridges   │
                └─────────────┬──────────────┘
                              │
          ┌───────────────────┼───────────────────┐
          │                                       │
┌─────────▼──────────┐              ┌─────────────▼──────────┐
│ SendspinLocalAudio  │              │ SendspinLocalAudio     │
│ Bridge (Device A)   │              │ Bridge (Device B)      │
│                     │              │                        │
│ Sendspin Client ──► │              │ Sendspin Client ──►    │
│ BridgePlayerRole    │              │ BridgePlayerRole       │
│ PA/ALSA/sounddevice │              │ PA/ALSA/sounddevice    │
└─────────────────────┘              └────────────────────────┘
```

### Audio Flow

#### Linux (PulseAudio / PipeWire)
```
Sendspin PushStream
       │
       ▼
BridgePlayerRole.on_audio_chunk
       │
       ▼ (software volume/mute applied, format conversion for 24-bit)
asyncio.Queue
       │
       ▼
PASimpleStream (libpulse-simple via ctypes)
       │
       ▼
PulseAudio Sink (hardware volume fixed at 100%)
       │
       ▼
Physical Audio Device
```

#### Linux (ALSA direct) / macOS (CoreAudio)
```
Sendspin PushStream
       │
       ▼
BridgePlayerRole.on_audio_chunk
       │
       ▼ (software volume/mute applied)
asyncio.Queue
       │
       ▼
sounddevice.RawOutputStream (PortAudio, int16)
       │
       ▼
ALSA hw: device / CoreAudio Device
```

### Volume Control

Volume and mute are controlled entirely in software — PCM samples are scaled in the bridge before being written to the output device. The MA volume slider (0–100) maps directly to the PCM scale factor.

On the PulseAudio backend, PA sink hardware volume is set to 100% at provider startup so the software scaler has the full dynamic range available.

On the ALSA direct backend (Linux) and macOS, hardware mixer levels are not controlled by MA and must be configured by the user once using a tool such as `alsamixer`. Set all relevant controls to 100% and save the state with `alsactl store` so the levels persist across reboots.

This approach avoids exposing audio stack implementation details to the user and gives consistent behaviour across all supported platforms and sink types.

### Bit Depth Handling (Linux PulseAudio)

| Sink Format  | MA Delivery                       | PA Stream Format  | Conversion                         |
|--------------|-----------------------------------|-------------------|------------------------------------|
| `s16le`      | 16-bit PCM                        | `PA_SAMPLE_S16LE` | None                               |
| `s24le`      | 32-bit container (left-justified) | `PA_SAMPLE_S24LE` | Unpack int32, repack to 3-byte LE  |
| `s24-32le`   | 32-bit container                  | `PA_SAMPLE_S32LE` | None                               |
| `s32le`      | 32-bit PCM                        | `PA_SAMPLE_S32LE` | None                               |

The ALSA direct backend always uses 16-bit int PCM (PortAudio `int16` dtype) regardless of hardware capability.

### File Structure

| File/Folder | Description |
|-------------|-------------|
| `__init__.py` | Provider entry point, config entries (backend selector on Linux), and setup |
| `provider.py` | `LocalAudioProvider` class |
| `sendspin_bridge.py` | Bridge manager and per-device bridge (PA on Linux PulseAudio, sounddevice on Linux ALSA and macOS) |
| `pa_simple.py` | ctypes wrapper around `libpulse-simple` for direct PCM output; PA sink enumeration via `pactl`; ALSA device enumeration via PortAudio *(Linux only)* |
| `constants.py` | Shared constants (UUID namespace, buffer sizes, backend selector values) |
| `manifest.json` | Provider metadata and dependencies |


## Dependencies

- **Sendspin provider** (`depends_on: sendspin`): Required for audio synchronization and player management
- **libpulse-simple** *(Linux PulseAudio backend)*: PulseAudio simple client library accessed via ctypes for direct PCM streaming to sinks
- **pactl** *(Linux PulseAudio backend)*: Used for PA sink enumeration via `--format=json`. Requires `pulseaudio-utils` to be installed
- **sounddevice** *(Linux ALSA backend and macOS)*: Python bindings for PortAudio, used for audio output and device enumeration
- **numpy**: Used for PCM volume scaling and 24-bit format conversion

## Expanding Outputs with Stereo Pair Remap Sinks

Multi-channel sound cards (5.1, 7.1 surround) expose a single multi-channel PulseAudio sink by default. To use each channel pair as an independent MA player, PulseAudio `module-remap-sink` can split a multi-channel sink into individual stereo sinks — one per channel pair (front, rear, side, center/LFE). The Local Audio Out provider discovers and registers all remap sinks automatically alongside physical sinks, so no additional configuration is needed in MA once the remap sinks exist.

For Home Assistant OS users, the companion addon [Pulse Audio Stereo Pairs](../stereo_pairs/) automates this setup. It runs as a lightweight HA addon that creates the remap sinks on startup and reacts to audio device hot-plug and unplug events. Once both the addon and this provider are running, each channel pair of every multi-channel card appears as a separate player in Music Assistant.

## Notes

- On Linux, multi-channel sinks (5.1, 7.1) are supported on the PulseAudio backend — the bridge opens a stereo stream and PulseAudio handles channel remapping automatically.
- Virtual sinks created by `module-remap-sink` (stereo pairs split from multi-channel cards) are fully supported on the PulseAudio backend and are the recommended way to expose individual speaker pairs as independent MA players.
- On Linux, `pactl --format=json` is used for PA sink enumeration because it always reports the sink's native sample rate and format, unlike libpulse which reports the currently negotiated stream format when streams are active.
- PA sink enumeration requires `pactl` from `pulseaudio-utils` to be installed on the host.
- Sample rate and bit depth on the PulseAudio backend are determined by the PA daemon configuration (`/etc/pulse/daemon.conf`) and the sink's native hardware capabilities — they are not configurable per-player in MA.
- On the ALSA direct backend, PortAudio enumerates only real hardware `hw:` nodes. Virtual PCM plugins (`sysdefault`, `front`, `dmix`, `surround*`, etc.) are excluded. If a device cannot be opened exclusively (e.g. another process holds it), it is silently skipped during enumeration.
- On the ALSA direct backend, hardware ALSA mixer levels are not managed by MA. Set all relevant controls (Master, PCM) to 100% using `alsamixer -c <card>` and persist them with `sudo alsactl store <card>`.
- If a player provider reload is needed (e.g. after adding or removing PA sinks or ALSA devices), use **Settings → Providers → Local Audio Out → Reload** in the MA UI.

## Related Documentation

- [Sendspin Provider](../sendspin/README.md)
