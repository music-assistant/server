# Local Audio Out Provider

## Overview

The Local Audio Out provider exposes locally attached soundcards as players in Music Assistant. On Linux it enumerates PulseAudio sinks (USB DACs, built-in audio, HDMI, remap sinks, virtual sinks, etc.); on macOS it enumerates PortAudio/CoreAudio devices. It leverages the Sendspin provider for synchronization and timing, registering each device as an external Sendspin bridge client.

### Key Features

- **Automatic Device Discovery**: On Linux, enumerates all PulseAudio output sinks via `pactl --format=json` — returns native sample rate and format regardless of active stream state. On macOS, enumerates via PortAudio/sounddevice
- **Native Format Negotiation** *(Linux)*: Each PA sink advertises its native sample rate and bit depth (16, 24, or 32-bit) so Music Assistant transcodes to the correct format — no unnecessary resampling
- **Sendspin Integration**: Each device is registered as a Sendspin bridge client, enabling synchronized multi-room playback
- **Software Volume Control**: Per-device volume and mute via PCM sample scaling. PA sinks are set to 100% hardware volume at startup so the full dynamic range is available to the software scaler
- **Stable Player IDs**: Uses UUIDv5 derived from PA sink name + host API index so players persist across restarts
- **Volume State Persistence**: Volume and mute state are cached and restored on restart

## Architecture

### Component Overview

```
┌──────────────────────────────────────────────────────────────┐
│                    LocalAudioProvider                         │
│  - Thin provider shell, delegates to bridge manager          │
│  - Verifies libpulse-simple present on Linux at init         │
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
│ PA/sounddevice out  │              │ PA/sounddevice out     │
└─────────────────────┘              └────────────────────────┘
```

### Audio Flow

#### Linux (PulseAudio)
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

#### macOS (CoreAudio)
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
sounddevice.RawOutputStream (PortAudio)
       │
       ▼
CoreAudio Device
```

### Volume Control

Volume and mute are controlled entirely in software — PCM samples are scaled in the bridge before being written to the PA sink. On Linux, PA sink hardware volume is set to 100% at provider startup so the software scaler has the full dynamic range available. The MA volume slider (0–100) maps directly to the PCM scale factor.

This approach avoids exposing audio stack implementation details to the user and gives consistent behaviour across all supported platforms and sink types.

### Bit Depth Handling (Linux)

| Sink Format  | MA Delivery                       | PA Stream Format  | Conversion                         |
|--------------|-----------------------------------|-------------------|------------------------------------|
| `s16le`      | 16-bit PCM                        | `PA_SAMPLE_S16LE` | None                               |
| `s24le`      | 32-bit container (left-justified) | `PA_SAMPLE_S24LE` | Unpack int32, repack to 3-byte LE  |
| `s24-32le`   | 32-bit container                  | `PA_SAMPLE_S32LE` | None                               |
| `s32le`      | 32-bit PCM                        | `PA_SAMPLE_S32LE` | None                               |

### File Structure

| File/Folder | Description |
|-------------|-------------|
| `__init__.py` | Provider entry point and setup |
| `provider.py` | `LocalAudioProvider` class |
| `sendspin_bridge.py` | Bridge manager and per-device bridge (PA on Linux, sounddevice on macOS) |
| `player.py` | `LocalAudioPlayer` — MA player model for each device |
| `pa_simple.py` | ctypes wrapper around `libpulse-simple` for direct PCM output; sink enumeration via `pactl` *(Linux only)* |
| `constants.py` | Shared constants (UUID namespace, buffer size) |
| `manifest.json` | Provider metadata and dependencies |
| `bin/pactl` | Bundled `pactl` binary for sink enumeration (fallback if `pulseaudio-utils` not installed) |
| `bin/lib/` | Bundled `libpulsecommon` shared library required by the bundled `pactl` binary |

## Dependencies

- **Sendspin provider** (`depends_on: sendspin`): Required for audio synchronization and player management
- **libpulse-simple** *(Linux)*: PulseAudio simple client library accessed via ctypes for direct PCM streaming to sinks
- **pactl** *(Linux)*: Used for sink enumeration via `--format=json`. Provided by `pulseaudio-utils` in the MA base image, with a bundled binary as fallback
- **pulsectl** *(Linux)*: Python PulseAudio bindings used to set PA sink hardware volume to 100% at startup
- **sounddevice** *(macOS)*: Python bindings for PortAudio, used for audio output and device enumeration
- **numpy**: Used for PCM volume scaling

## Expanding Outputs with Stereo Pair Remap Sinks

Multi-channel sound cards (5.1, 7.1 surround) expose a single multi-channel PulseAudio sink by default. To use each channel pair as an independent MA player, PulseAudio `module-remap-sink` can split a multi-channel sink into individual stereo sinks — one per channel pair (front, rear, side, center/LFE). The Local Audio Out provider discovers and registers all remap sinks automatically alongside physical sinks, so no additional configuration is needed in MA once the remap sinks exist.

For Home Assistant OS users, the companion addon [Pulse Audio Stereo Pairs](../stereo_pairs/) automates this setup. It runs as a lightweight HA addon that creates the remap sinks on startup and reacts to audio device hot-plug and unplug events. Once both the addon and this provider are running, each channel pair of every multi-channel card appears as a separate player in Music Assistant.

## Notes

- On Linux, multi-channel sinks (5.1, 7.1) are supported — the bridge opens a stereo stream and PulseAudio handles channel remapping automatically.
- Virtual sinks created by `module-remap-sink` (stereo pairs split from multi-channel cards) are fully supported and are the recommended way to expose individual speaker pairs as independent MA players.
- On Linux, `pactl --format=json` is used for enumeration because it always reports the sink's native sample rate and format, unlike `pulsectl`/libpulse which reports the currently negotiated stream format when streams are active.
- The system `pactl` binary is preferred for sink enumeration; the bundled binary in `bin/pactl` is used only as a fallback when `pulseaudio-utils` is not installed.
- Sample rate and bit depth are determined by the PA daemon configuration (`/etc/pulse/daemon.conf`) and the sink's native hardware capabilities — they are not configurable per-player in MA.

## Related Documentation

- [Sendspin Provider](../sendspin/README.md)
