# Local Audio Out Provider

## Overview

The Local Audio Out provider exposes locally attached soundcards (USB DACs, built-in speakers, HDMI audio, etc.) as players in Music Assistant. It leverages the Sendspin provider for synchronization and timing, registering each soundcard as an external Sendspin bridge client.

### Key Features

- **Automatic Device Discovery**: Enumerates all audio output devices via PortAudio/sounddevice
- **Sendspin Integration**: Each device is registered as a Sendspin bridge client, enabling synchronized multi-room playback
- **Software Volume Control**: Per-device volume and mute via PCM sample scaling
- **Stable Player IDs**: Uses UUIDv5 from device name + host API index so players persist across restarts

## Architecture

### Component Overview

```
┌──────────────────────────────────────────────────────────────┐
│                    LocalAudioProvider                         │
│  - Thin provider shell, delegates to bridge manager          │
└──────────────────────────────────────────────────────────────┘
                              │
                ┌─────────────▼──────────────┐
                │  LocalAudioBridgeManager   │
                │  - Enumerates soundcards   │
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
│ sounddevice Output  │              │ sounddevice Output     │
└─────────────────────┘              └────────────────────────┘
```

### Audio Flow

```
Sendspin PushStream
       │
       ▼
BridgePlayerRole.on_audio_chunk
       │
       ▼ (volume/mute applied)
asyncio.Queue
       │
       ▼
sounddevice.RawOutputStream (PortAudio)
       │
       ▼
Physical Soundcard
```

### File Structure

| File | Description |
|------|-------------|
| `__init__.py` | Provider entry point, setup, and config |
| `provider.py` | `LocalAudioProvider` class |
| `sendspin_bridge.py` | Bridge manager and per-device bridge implementation |
| `constants.py` | Shared constants (UUID namespace, buffer size) |
| `manifest.json` | Provider metadata and dependencies |

## Dependencies

- **Sendspin provider** (`depends_on: sendspin`): Required for audio synchronization and player management
- **sounddevice**: Python bindings for PortAudio, used for audio output
- **numpy**: Used for PCM volume scaling

## Related Documentation

- [Sendspin Provider](../sendspin/README.md)
