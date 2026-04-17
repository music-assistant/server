# Local Audio Out Provider

## Overview

The Local Audio Out provider exposes locally attached soundcards as players in Music Assistant. On Linux it enumerates PulseAudio sinks (USB DACs, built-in audio, HDMI, remap sinks, virtual sinks, etc.); on macOS it enumerates PortAudio/CoreAudio devices. It leverages the Sendspin provider for synchronization and timing, registering each device as an external Sendspin bridge client.

### Key Features

- **Automatic Device Discovery**: On Linux, enumerates all PulseAudio output sinks via `pactl --format=json` — returns native sample rate and format regardless of active stream state. On macOS, enumerates via PortAudio/sounddevice
- **Native Format Negotiation** *(Linux)*: Each PA sink advertises its native sample rate and bit depth (16, 24, or 32-bit) so Music Assistant transcodes to the correct format — no unnecessary resampling
- **Sendspin Integration**: Each device is registered as a Sendspin bridge client, enabling synchronized multi-room playback
- **Hardware Volume Control**: PulseAudio sink volume on Linux, CoreAudio on macOS, with automatic fallback to software scaling
- **Software Volume Control**: Per-device volume and mute via PCM sample scaling when hardware control is unavailable or disabled
- **Stable Player IDs**: Uses UUIDv5 derived from device name + host API index so players persist across restarts
- **Hardware Volume Ceiling** *(Linux)*: Configurable per-provider PA sink volume ceiling applied at startup

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
PulseAudio Sink
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

### Bit Depth Handling (Linux)

| Sink Format | MA Delivery                       | PA Stream Format  | Conversion                         |
|-------------|-----------------------------------|-------------------|------------------------------------|
| `s16le`     | 16-bit PCM                        | `PA_SAMPLE_S16LE` | None                               |
| `s24le`     | 32-bit container (left-justified) | `PA_SAMPLE_S24LE` | Unpack int32, repack to 3-byte LE  |
| `s32le`     | 32-bit PCM                        | `PA_SAMPLE_S32LE` | None                               |

### File Structure

| File/Folder | Description |
|-------------|-------------|
| `__init__.py` | Provider entry point, setup, and config entries |
| `provider.py` | `LocalAudioProvider` class |
| `sendspin_bridge.py` | Bridge manager and per-device bridge (PA on Linux, sounddevice on macOS) |
| `player.py` | `LocalAudioPlayer` — MA player model for each device |
| `pa_simple.py` | ctypes wrapper around `libpulse-simple` for direct PCM output; sink enumeration via `pactl` *(Linux only)* |
| `constants.py` | Shared constants (UUID namespace, buffer size, config keys) |
| `manifest.json` | Provider metadata and dependencies |
| `bin/pactl` | Bundled `pactl` binary for sink enumeration (fallback if `pulseaudio-utils` not installed) |
| `bin/lib/` | Bundled `libpulsecommon` shared library required by the bundled `pactl` binary |

## Dependencies

- **Sendspin provider** (`depends_on: sendspin`): Required for audio synchronization and player management
- **libpulse-simple** *(Linux)*: PulseAudio simple client library accessed via ctypes for direct PCM streaming to sinks
- **pactl** *(Linux)*: Used for sink enumeration via `--format=json`. Provided by `pulseaudio-utils` in the MA base image, with a bundled binary as fallback
- **pulsectl** *(Linux)*: Python PulseAudio bindings used for hardware volume and mute control
- **sounddevice** *(macOS)*: Python bindings for PortAudio, used for audio output and device enumeration
- **numpy**: Used for PCM volume scaling

## Configuration

| Setting | Platform | Description |
|---------|----------|-------------|
| Volume control mode | All | `hardware` (OS-level), `software` (PCM scaling), or `disabled` |
| Hardware volume ceiling | Linux only | Sets PA sink volume on startup to cap maximum output level (0–100, default 50) |

## Notes

- On Linux, multi-channel sinks (5.1, 7.1) are supported — the bridge opens a stereo stream and PulseAudio handles channel remapping automatically.
- Virtual sinks created by `module-remap-sink` (stereo pairs split from multi-channel cards) are fully supported and are the recommended way to expose individual speaker pairs as independent MA players.
- On Linux, `pactl --format=json` is used for enumeration because it always reports the sink's native sample rate and format, unlike `pulsectl`/libpulse which reports the currently negotiated stream format when streams are active.
- On Linux, hardware volume control uses `pulsectl` to set the PA sink volume directly. If `pulsectl` is unavailable the provider falls back to software volume automatically.
- On macOS, hardware volume control uses CoreAudio. If that fails the provider falls back to software volume automatically.

## Future Work

- **sendspin-cli process approach**: A `sendspin_process.py` implementation exists that spawns native `sendspin daemon` processes per sink for potentially better multi-room sync. This requires PortAudio compiled with PulseAudio support (`--with-pulseaudio`) in the MA base image, which is not currently the case. See `Dockerfile.base` notes.

## Related Documentation

- [Sendspin Provider](../sendspin/README.md)
