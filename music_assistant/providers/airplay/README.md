# AirPlay Provider

## Overview

The AirPlay provider enables Music Assistant to stream audio to AirPlay-enabled devices on your local network. It supports both **RAOP (AirPlay 1)** and **AirPlay 2** protocols, providing compatibility with a wide range of devices including Apple HomePods, Apple TVs, Macs, and third-party AirPlay-compatible speakers.

### Key Features

- **Dual Protocol Support**: Automatically selects between RAOP and AirPlay 2 based on device capabilities
- **Native Pairing**: Supports pairing with Apple devices (Apple TV, HomePod, Mac) using HAP (HomeKit Accessory Protocol) or RAOP pairing
- **Multi-Room Audio**: Synchronizes playback across multiple AirPlay devices with NTP timestamp precision
- **DACP Remote Control**: Receives remote control commands (play/pause/volume/next/previous) from devices while streaming
- **Late Join Support**: Allows adding players to an existing playback session without interrupting other players
- **Flow Mode Streaming**: Provides gapless playback and crossfade support by streaming the queue as one continuous audio stream

## Architecture

### Component Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                      AirPlay Provider                           │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │  MDNS Discovery (_airplay._tcp, _raop._tcp)              │  │
│  └──────────────────────────────────────────────────────────┘  │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │  DACP Server (_dacp._tcp) - Remote Control Callbacks     │  │
│  └──────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
                              │
        ┌─────────────────────┼─────────────────────┐
        │                     │                     │
┌───────▼──────┐     ┌────────▼────────┐    ┌──────▼──────┐
│ AirPlayPlayer│     │ AirPlayPlayer   │    │AirPlayPlayer│
│   (Leader)   │     │  (Sync Child)   │    │(Sync Child) │
└───────┬──────┘     └────────┬────────┘    └──────┬──────┘
        │                     │                     │
        └─────────────────────┼─────────────────────┘
                              │
                    ┌─────────▼──────────┐
                    │ AirPlayStreamSession│
                    │  (manages session)  │
                    └─────────┬──────────┘
                              │
        ┌─────────────────────┼─────────────────────┐
        │                     │                     │
┌───────▼──────┐     ┌────────▼────────┐    ┌──────▼──────┐
│  RaopStream  │     │ AirPlay2Stream  │    │ RaopStream  │
│ ┌──────────┐ │     │ ┌────────────┐  │    │┌──────────┐ │
│ │ cliraop  │ │     │ │  cliap2    │  │    ││ cliraop  │ │
│ └────▲─────┘ │     │ └─────▲──────┘  │    │└────▲─────┘ │
│      │       │     │       │         │    │     │       │
│ ┌────┴─────┐ │     │ ┌─────┴──────┐  │    │┌────┴─────┐ │
│ │  FFmpeg  │ │     │ │  FFmpeg    │  │    ││  FFmpeg  │ │
│ └──────────┘ │     │ └────────────┘  │    │└──────────┘ │
└──────────────┘     └─────────────────┘    └─────────────┘
```

### File Structure

```
airplay/
├── provider.py           # Main provider class, MDNS discovery, DACP server
├── player.py             # AirPlayPlayer implementation
├── stream_session.py     # Manages streaming sessions for synchronized playback
├── pairing.py           # HAP and RAOP pairing implementations
├── helpers.py           # Utility functions (NTP conversion, model detection, etc.)
├── constants.py         # Constants and enums
├── protocols/
│   ├── _protocol.py     # Base protocol class with shared logic
│   ├── raop.py          # RAOP (AirPlay 1) streaming implementation
│   └── airplay2.py      # AirPlay 2 streaming implementation
└── bin/                 # Platform-specific CLI binaries
    ├── cliraop-*        # RAOP streaming binaries
    └── cliap2-*         # AirPlay 2 streaming binaries
```

## Protocol Selection: RAOP vs AirPlay 2

### RAOP (AirPlay 1)

- **Used for**: Older AirPlay devices, some third-party implementations
- **Features**:
  - Encryption support (can be disabled for problematic devices)
  - ALAC compression option to save network bandwidth
  - Password protection support
  - Device-reported volume feedback via DACP
- **Binary**: `cliraop` (based on [libraop](https://github.com/music-assistant/libraop))

### AirPlay 2

- **Used for**: Modern Apple devices, some third-party devices
- **Features**:
  - Better compatibility with newer devices
  - More robust protocol
  - Required for some devices that don't support RAOP
- **Binary**: `cliap2` (based on [OwnTone](https://github.com/music-assistant/cliairplay))

### Automatic Selection

When protocol is set to "Automatically select" (default):
- **Prefers AirPlay 2** for known models (e.g., Ubiquiti devices) that work better with it
- **Falls back to RAOP** for all other devices
- Users can manually override via player configuration if needed

## Discovery and Player Setup

### MDNS Service Discovery

The provider discovers AirPlay devices via two MDNS service types:

1. **`_airplay._tcp.local.`** - Primary AirPlay service (preferred)
   - Contains detailed device information
   - Announced by most modern devices

2. **`_raop._tcp.local.`** - Legacy RAOP service
   - Fallback for older devices
   - If only RAOP service is found, provider attempts to query for AirPlay service

### Player Setup Flow

1. **MDNS service discovered** → `on_mdns_service_state_change()` in [provider.py](provider.py)
2. **Extract device info** from MDNS properties:
   - Device ID (from `deviceid` property or service name)
   - Display name
   - Manufacturer and model (via `get_model_info()` in [helpers.py](helpers.py))
3. **Filter checks**:
   - Skip if player is disabled in config
   - Skip ShairportSync instances running on the same Music Assistant server (to avoid conflicts with AirPlay Receiver provider)
4. **Create player** → `AirPlayPlayer` instance
5. **Register with player controller** → `mass.players.register()`

### Player ID Format

Player IDs follow the format: `ap{mac_address}` (e.g., `ap1a2b3c4d5e6f`)

## Pairing for Apple Devices

Apple TV and Mac devices require pairing before they can be used for streaming.

### Pairing Protocols

1. **HAP (HomeKit Accessory Protocol)** - For AirPlay 2
   - 6-step SRP authentication with TLV encoding
   - Ed25519 key exchange
   - ChaCha20-Poly1305 encryption
   - Produces 192-character hex credentials

2. **RAOP Pairing** - For AirPlay 1
   - 3-step SRP authentication with plist encoding
   - Ed25519 key derivation from auth secret
   - AES-GCM encryption
   - Produces `client_id:auth_secret` format credentials

### Pairing Flow

1. **Start pairing** → POST to `/pair-pin-start` (or protocol-specific endpoint)
2. **Device displays 4-digit PIN** on screen
3. **User enters PIN** in Music Assistant configuration
4. **Complete pairing** → SRP authentication and key exchange
5. **Store credentials** in player config (protocol-specific key: `raop_credentials` or `airplay_credentials`)

**Important**: The DACP ID used during pairing must match the ID used during streaming. The provider uses the first 16 hex characters of `server_id` as a persistent DACP ID to ensure compatibility across restarts.

## Streaming Architecture

### Audio Pipeline

```
┌─────────────────────────────────────────────────────────────────┐
│                    Music Assistant Core                          │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  Queue Manager (assembles tracks into continuous stream) │   │
│  └─────────────────────────┬────────────────────────────────┘   │
└────────────────────────────┼─────────────────────────────────────┘
                             │ PCM Audio (44.1kHz, 32-bit float)
                    ┌────────▼─────────┐
                    │ StreamSession    │
                    │ _audio_streamer()│
                    └────────┬─────────┘
                             │ Chunks of PCM audio
        ┌────────────────────┼────────────────────┐
        │                    │                    │
┌───────▼──────┐    ┌────────▼────────┐   ┌──────▼──────┐
│   FFmpeg     │    │    FFmpeg       │   │   FFmpeg    │
│ (resample,   │    │  (resample,     │   │ (resample,  │
│  filter,     │    │   filter,       │   │  filter,    │
│  convert)    │    │   convert)      │   │  convert)   │
└───────┬──────┘    └────────┬────────┘   └──────┬──────┘
        │ PCM 44.1kHz 16-bit │                    │
┌───────▼──────┐    ┌────────▼────────┐   ┌──────▼──────┐
│  cliraop     │    │    cliap2       │   │  cliraop    │
│  (RAOP       │    │  (AirPlay 2     │   │  (RAOP      │
│   protocol)  │    │   protocol)     │   │   protocol) │
└───────┬──────┘    └────────┬────────┘   └──────┬──────┘
        │                    │                    │
        │ Network (RTP)      │ Network (RTP)      │ Network (RTP)
        │                    │                    │
┌───────▼──────┐    ┌────────▼────────┐   ┌──────▼──────┐
│ AirPlay      │    │  AirPlay        │   │  AirPlay    │
│ Device 1     │    │  Device 2       │   │  Device 3   │
└──────────────┘    └─────────────────┘   └─────────────┘
```

### Stream Session Management

The `AirPlayStreamSession` class in [stream_session.py](stream_session.py) manages streaming to one or more synchronized players:

1. **Initialization** (`start()` method)
   - Calculates start time with connection delay buffer
   - Converts start time to NTP timestamp for precise synchronization

2. **Client Setup** (per player, `_start_client()` method)
   - Creates protocol instance (`RaopStream` or `AirPlay2Stream`)
   - Starts CLI process with NTP start timestamp
   - Configures FFmpeg for audio format conversion and optional DSP filters
   - Pipes FFmpeg output to CLI process stdin

3. **Audio Streaming** (`_audio_streamer()` method)
   - Receives PCM audio chunks from Music Assistant core
   - Distributes chunks to all players via FFmpeg
   - Tracks elapsed time based on bytes sent
   - Handles silence padding if audio source is slow (watchdog mechanism)

4. **Connection Monitoring**
   - Waits for all devices to connect before starting playback
   - Monitors CLI stderr for connection status and errors
   - Removes players that fail to keep up (write timeouts)

### Flow Mode Streaming

AirPlay uses **flow mode** streaming, which means:
- The entire queue is streamed as one continuous audio stream
- Enables true gapless playback between tracks
- Supports crossfade between tracks
- Once started, the stream continues until explicitly stopped


## Multi-Room Synchronization

### Synchronized Playback

The provider supports synchronized multi-room audio by:

1. **Using a single `AirPlayStreamSession`** for the group leader and all sync children
2. **Coordinating start times** via NTP timestamps
3. **Distributing identical audio** to all players simultaneously
4. **Per-player sync adjustment** via `sync_adjust` config option (in milliseconds)

### Group Management

- **Leader**: The primary player that manages the stream session
- **Members**: Child players synchronized to the leader
- **Adding members**: Use `set_members()` method in [player.py](player.py)
- **Removing members**: Stream continues for remaining players

### Late Join Support

When adding a player to an already-playing session (`add_client()` in [stream_session.py](stream_session.py)):

1. **Ring buffer**: Session maintains ~8 seconds of recent audio chunks in memory
2. **Immediate buffered feed**: Late joiner receives buffered chunks immediately to prime the ffmpeg/CLI pipeline
3. **Compensated start time**: NTP timestamp accounts for buffer duration: `start_time + (seconds_streamed - buffer_duration)`
4. **Fast catch-up**: Device processes buffered audio and catches up to real-time position
5. **Seamless sync**: Joins live stream perfectly synchronized with other players

This approach significantly reduces the delay when adding players to an active session, as the late joiner receives audio data immediately instead of waiting for new chunks.

**Config option**: `enable_late_join` (default: `True`)
- If disabled: Session restarts with all players when members change
- If enabled: New players join seamlessly without interrupting others

## DACP (Digital Audio Control Protocol)

### Purpose

DACP allows AirPlay devices to send remote control commands back to Music Assistant while streaming is active. This enables:
- Using physical buttons on devices (e.g., Apple TV remote)
- Volume control from the device
- Play/pause/next/previous commands
- Shuffle toggle
- Source switching detection

### DACP Server

The provider registers a MDNS service `_dacp._tcp.local.` (in `handle_async_init()` method in [provider.py](provider.py)) and runs a TCP server to receive HTTP requests from devices.

### Active-Remote ID

Each streaming session generates an `active_remote_id` (via `generate_active_remote_id()` in [helpers.py](helpers.py)) from the player's MAC address. This ID is:
- Passed to the CLI binary
- Sent to the device during streaming
- Used to match incoming DACP requests to the correct player

### Supported DACP Commands

Handled in `_handle_dacp_request()` in [provider.py](provider.py):

| DACP Path | Action |
|-----------|--------|
| `/ctrl-int/1/nextitem` | Skip to next track |
| `/ctrl-int/1/previtem` | Go to previous track |
| `/ctrl-int/1/play` | Resume playback |
| `/ctrl-int/1/pause` | Pause playback |
| `/ctrl-int/1/playpause` | Toggle play/pause |
| `/ctrl-int/1/stop` | Stop playback |
| `/ctrl-int/1/volumeup` | Increase volume |
| `/ctrl-int/1/volumedown` | Decrease volume |
| `/ctrl-int/1/shuffle_songs` | Toggle shuffle |
| `dmcp.device-volume=X` | Volume changed by device (RAOP only) |
| `device-prevent-playback=1` | Device switched to another source or powered off |
| `device-prevent-playback=0` | Device ready for playback again |

### Volume Feedback

Both **RAOP** and **AirPlay 2** protocols support devices reporting their volume level via DACP.

**Config option**: `ignore_volume` (default: `False`, auto-enabled for Apple devices)
- Useful when device volume reports are unreliable
- Apple devices always ignore volume feedback (handled internally)

### Device Source Switching

When `device-prevent-playback=1` is received:
- User switched the device to another input source
- Device is powered off
- Streaming session removes the player from the active session

## External CLI Binaries

### Why External Binaries?

Python is not suitable for real-time audio streaming with precise timing requirements. The AirPlay protocols (especially AirPlay 2) require:
- Accurate NTP timestamp handling
- Real-time RTP packet transmission
- Low-latency audio buffering
- Precise synchronization across multiple devices

Therefore, the provider uses C-based CLI binaries for the actual streaming.

### Binary Selection

The provider automatically selects the correct binary based on:
- **Platform**: Linux, macOS
- **Architecture**: x86_64, arm64, aarch64
- **Protocol**: RAOP (`cliraop-*`) or AirPlay 2 (`cliap2-*`)

Binaries are located in [bin/](bin/) directory and validated on first use.

### Binary Communication

**Input** (stdin):
- PCM audio data piped from FFmpeg

**Commands** (named pipe):
- Interactive commands sent via `AsyncNamedPipeWriter`
- Examples: `ACTION=PLAY`, `ACTION=PAUSE`, `VOLUME=50`, `TITLE=Song Name`

**Output** (stderr):
- Status messages and logs
- Connection state
- Playback state changes
- Elapsed time updates
- Error messages

The provider monitors stderr in a separate task (`_stderr_reader()` in [raop.py](protocols/raop.py) and [airplay2.py](protocols/airplay2.py)) to:
- Update player state
- Detect connection completion
- Handle errors and packet loss
- Track elapsed time

## NTP Timestamp Synchronization

AirPlay uses **NTP (Network Time Protocol)** timestamps for synchronized playback.

### NTP Format

- **64-bit integer**: Upper 32 bits = seconds, lower 32 bits = fractional seconds
- **NTP epoch**: January 1, 1900 (not Unix epoch 1970)
- **Precision**: Nanosecond-level timing

### Key Functions

Available in [helpers.py](helpers.py):
- `get_ntp_timestamp()`: Get current NTP time
- `ntp_to_unix_time()`: Convert NTP to Unix timestamp
- `unix_time_to_ntp()`: Convert Unix to NTP timestamp
- `add_seconds_to_ntp()`: Add offset to NTP timestamp

### Usage in Streaming

1. Calculate desired start time: `current_time + connection_buffer`
2. Convert to NTP timestamp
3. Pass to CLI binary via `-ntpstart` argument
4. All players start at the exact same NTP time
5. Per-player `sync_adjust` config allows fine-tuning (+/- milliseconds)

## Player Types

The provider creates players with different types based on whether the device is a native Apple player or a third-party AirPlay receiver.

### PlayerType.PLAYER
- **Devices**: Apple HomePod, Apple TV, Mac
- **Reason**: These are standalone music players with native AirPlay support
- **Behavior**: Exposed as top-level players in Music Assistant UI
- **Not merged**: These players are NOT combined with other protocols

### PlayerType.PROTOCOL
- **Devices**: Third-party AirPlay receivers (Sonos, receivers, smart speakers, soundbars)
- **Reason**: AirPlay is just one output protocol among many for these devices (often supporting Chromecast, DLNA, etc.)
- **Behavior**: Automatically merged into a **Universal Player** if other protocols are detected for the same device
- **Example**: A Sonos speaker supporting both AirPlay and Chromecast will appear as a single "Sonos" player with selectable output protocols

**Detection**: Player type is determined in [player.py](player.py) `__init__()` method based on `manufacturer == "Apple"`

**For more details on output protocols and protocol linking**, see the [Player Controller README](../../controllers/players/README.md), which explains:
- How multiple protocol players for the same physical device are automatically linked
- The Universal Player concept for devices without native vendor support
- Protocol selection and device identifier matching
- Native player linking vs. Universal Player creation

## Configuration Options

### Protocol Selection
- **`airplay_protocol`**: Choose RAOP, AirPlay 2, or automatic (default: automatic)

### RAOP-Specific
- **`encryption`**: Enable/disable encryption (default: enabled)
- **`alac_encode`**: Enable ALAC compression to save bandwidth (default: enabled)
- **`ignore_volume`**: Ignore device volume reports (default: false)

### General
- **`password`**: Device password if required
- **`sync_adjust`**: Per-player timing adjustment in milliseconds (default: 0)

### Pairing (Apple devices only)
- **`raop_credentials`**: Stored RAOP pairing credentials (hidden)
- **`airplay_credentials`**: Stored AirPlay 2 pairing credentials (hidden)

## Known Issues

### Broken AirPlay Models

Some devices have known broken AirPlay implementations (see `BROKEN_AIRPLAY_MODELS` in [constants.py](constants.py)):
- **Samsung devices**: Known issues with both RAOP and AirPlay 2
- These players are disabled by default

### Limitations

1. **DACP remote control**: Only active while streaming (not when idle)
2. **Pause while synced**: Not supported; uses stop instead
3. **Companion protocol**: Not yet implemented for idle state monitoring

## Development Notes

### Testing CLI Binaries

Each binary can be validated with a test command:
- **cliraop**: `cliraop -check` (should output "cliraop check")
- **cliap2**: `cliap2 --testrun` (should output "cliap2 check")

### Adding New CLI Commands

To add a new command to the CLI binaries:
1. Update the CLI binary source code (external repositories)
2. Update `send_cli_command()` method in [_protocol.py](protocols/_protocol.py)
3. Send command via named pipe: `await stream.send_cli_command("YOUR_COMMAND=value")`

### Debugging Streaming Issues

Enable verbose logging in Music Assistant to see:
- CLI binary arguments
- stderr output from binaries
- DACP requests
- Connection state changes
- Packet loss warnings

## Credits

- **libraop**: RAOP streaming implementation - https://github.com/music-assistant/libraop
- **OwnTone**: AirPlay 2 implementation - https://github.com/OwnTone
- **pyatv**: Reference for HAP pairing protocol - https://github.com/postlund/pyatv

## Sendspin Bridge

AirPlay players can be bridged to the Sendspin protocol, enabling cross-protocol grouping between AirPlay devices and native Sendspin players.

### How It Works

When the Sendspin provider is enabled, each AirPlay player is automatically registered as an external Sendspin client:

1. **Registration**: The bridge registers the AirPlay player with the Sendspin server using the device's MAC address as the `client_id`
2. **Protocol Linking**: The player controller links the SendspinPlayer (created by Sendspin provider) with the AirPlayPlayer via MAC address matching
3. **Audio Flow**: When grouped, Sendspin handles timing and synchronization while AirPlay streams the audio

```
┌─────────────────────┐     ┌─────────────────────┐
│   SendspinPlayer    │◀───▶│   AirPlayPlayer     │
│  (protocol linked)  │     │                     │
└─────────┬───────────┘     └──────────┬──────────┘
          │                            │
          │ MAC address match          │
          │                            │
┌─────────▼───────────┐     ┌──────────▼──────────┐
│ Sendspin PushStream │────▶│ BridgePlayerRole    │
│  (timing/sync)      │     │      │              │
└─────────────────────┘     │      ▼              │
                            │ AirPlay CLI Process │
                            └─────────────────────┘
```

### Architecture

The bridge consists of:

- **`BridgePlayerRole`**: A custom Sendspin role that receives audio chunks from PushStream
- **`SendspinAirPlayBridge`**: Manages the bridge for a single AirPlay player
- **`SendspinBridgeManager`**: Manages bridges for all AirPlay players

### Requirements

- Sendspin provider must be enabled
- AirPlay player must have a valid MAC address for protocol linking

### Files

| File | Description |
|------|-------------|
| `sendspin_bridge.py` | Bridge implementation for Sendspin to AirPlay integration |

## Future Enhancements

- **Companion protocol**: Implement idle state monitoring for Apple devices
- **AirPlay 2 volume feedback**: Add DACP volume support for AirPlay 2
