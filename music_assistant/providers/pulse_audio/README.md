# Pulse Audio Out Provider

## Overview

The Pulse Audio Out provider exposes PulseAudio sinks (USB DACs, built-in audio, HDMI, remap sinks, etc.) as players in Music Assistant. It leverages the Sendspin provider for synchronization and timing, registering each sink as an external Sendspin bridge client.

### Key Features

- **Automatic Sink Discovery**: Enumerates all PulseAudio output sinks via `pactl`, including virtual sinks such as remap and combined sinks
- **Native Format Negotiation**: Each sink advertises its native sample rate and bit depth (16, 24, or 32-bit) so Music Assistant transcodes to the correct format per sink — no unnecessary resampling
- **Sendspin Integration**: Each sink is registered as a Sendspin bridge client, enabling synchronized multi-room playback
- **Software Volume Control**: Per-sink volume and mute via PCM sample scaling
- **Stable Player IDs**: Uses UUIDv5 derived from the PulseAudio sink name so players persist across restarts
- **Hardware Volume Ceiling**: Configurable per-provider PA sink volume ceiling applied at startup

## Architecture

### Component Overview

```
┌──────────────────────────────────────────────────────────────┐
│                  LocalPulseAudioProvider                      │
│  - Thin provider shell, delegates to bridge manager          │
└──────────────────────────────────────────────────────────────┘
                              │
             ┌────────────────▼────────────────┐
             │  LocalPulseAudioBridgeManager   │
             │  - Enumerates PA sinks via pactl│
             │  - Creates/stops bridges        │
             └────────────────┬────────────────┘
                              │
          ┌───────────────────┼───────────────────┐
          │                                       │
┌─────────▼──────────┐              ┌─────────────▼──────────┐
│ SendspinPulseAudio  │              │ SendspinPulseAudio     │
│ Bridge (Sink A)     │              │ Bridge (Sink B)        │
│                     │              │                        │
│ Sendspin Client ──► │              │ Sendspin Client ──►    │
│ BridgePlayerRole    │              │ BridgePlayerRole       │
│ pa_simple output    │              │ pa_simple output       │
└─────────────────────┘              └────────────────────────┘
```

### Audio Flow

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

### Bit Depth Handling

| Sink Format | MA Delivery     | PA Stream Format   | Conversion                          |
|-------------|-----------------|---------------------|-------------------------------------|
| `s16le`     | 16-bit PCM      | `PA_SAMPLE_S16LE`  | None                                |
| `s24le`     | 32-bit container (left-justified) | `PA_SAMPLE_S24LE` | Unpack int32, repack to 3-byte LE  |
| `s32le`     | 32-bit PCM      | `PA_SAMPLE_S32LE`  | None                                |

### File Structure

| File | Description |
|------|-------------|
| `__init__.py` | Provider entry point, setup, and config |
| `provider.py` | `LocalPulseAudioProvider` class |
| `sendspin_bridge.py` | Bridge manager and per-sink bridge implementation |
| `player.py` | `LocalPulseAudioPlayer` — MA player model for each sink |
| `pa_simple.py` | Minimal ctypes wrapper around `libpulse-simple` for direct PCM output |
| `helpers.py` | `find_pactl()` and `pactl_env()` utilities |
| `constants.py` | Shared constants (UUID namespace, config keys) |
| `manifest.json` | Provider metadata and dependencies |

## Dependencies

- **Sendspin provider** (`depends_on: sendspin`): Required for audio synchronization and player management
- **libpulse / libpulse-simple**: PulseAudio client libraries (must be present on the host); accessed via ctypes — no Python PulseAudio bindings required
- **pactl**: Used at startup for sink enumeration (`pulseaudio-utils` package on Debian/Ubuntu, `pulseaudio` on Alpine)
- **numpy**: Used for PCM volume scaling

## Notes

- The bundled `pactl` binary (if present) is `amd64` only. On other architectures the system `pactl` must be available in `PATH` or `PULSE_SERVER` must be set.
- Multi-channel sinks (5.1, 7.1) are supported — the bridge opens a stereo stream and PulseAudio handles channel remapping automatically.
- Virtual sinks created by `module-remap-sink` (stereo pairs split from multi-channel cards) are fully supported and are the recommended way to expose individual speaker pairs as independent MA players.

## Related Documentation

- [Sendspin Provider](../sendspin/README.md)
