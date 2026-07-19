# AirPlay Provider

## Overview

The AirPlay provider enables Music Assistant to stream audio to AirPlay-enabled devices on your local network. It supports both **RAOP (AirPlay 1)** and **AirPlay 2** protocols, providing compatibility with a wide range of devices including Apple HomePods, Apple TVs, Macs, and third-party AirPlay-compatible speakers.

### Key Features

- **Dual Protocol Support**: The cliairplay binary automatically resolves the best route (RAOP, AirPlay 2 RAOP-compat or native AirPlay 2) from the device's mDNS TXT records
- **Pairing**: Supports pairing with Apple devices (Apple TV, HomePod, Mac) using HAP/HomeKit pair-setup (via the cliairplay binary) or legacy RAOP pairing (native)
- **Multi-Room Audio**: Synchronizes playback across multiple AirPlay devices from a single wall-clock start instant, with a shared PTP clock daemon for native AirPlay 2 timing
- **Hi-Res Audio**: Optional 24-bit playback (44.1/48 kHz) over the native AirPlay 2 flow (per-player opt-in)
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
│AirPlayStream │     │  AirPlayStream  │    │AirPlayStream│
│ ┌──────────┐ │     │ ┌────────────┐  │    │┌──────────┐ │
│ │cliairplay│ │     │ │ cliairplay │  │    ││cliairplay│ │
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
├── provider.py           # Main provider class, MDNS discovery, DACP server, PTP daemon
├── player.py             # AirPlayPlayer implementation
├── stream_session.py     # Manages streaming sessions for synchronized playback
├── pairing.py           # Pairing: HAP via cliairplay --pair-setup, RAOP native
├── helpers.py           # Utility functions (binary lookup, TXT serialization, etc.)
├── constants.py         # Constants and enums
├── stream.py            # Unified AirPlayStream (RAOP + AirPlay 2) driving cliairplay
└── bin/                 # Platform-specific CLI binaries
    └── cliairplay-*     # Unified RAOP + AirPlay 2 streaming binary
```

## Protocol Selection: RAOP vs AirPlay 2

### RAOP (AirPlay 1)

- **Used for**: Older AirPlay devices, some third-party implementations
- **Features**:
  - Encrypted, ALAC-compressed audio (handled automatically by the binary)
  - Password protection support
  - Device-reported volume feedback via DACP
- **Binary**: `cliairplay --protocol raop` (based on [libraop](https://github.com/music-assistant/libraop))

### AirPlay 2

- **Used for**: Modern Apple devices, some third-party devices
- **Features**:
  - Better compatibility with newer devices
  - More robust protocol
  - Required for some devices that don't support RAOP
  - 24-bit audio support (native AirPlay 2 flow)
- **Binary**: `cliairplay --protocol airplay2` (native implementation, no OwnTone dependency)

### Automatic Selection

When protocol is set to "Automatically select" (default), the provider passes
`--protocol auto` together with the device's full `_airplay._tcp` TXT records
(`--txt`). The **binary** then resolves the route from the advertised feature
bits: RAOP vs AirPlay 2, native vs RAOP-compatible flow, transient pairing vs
stored-credential pair-verify, and PTP vs NTP timing.

The per-player protocol setting acts as an override only (an escape hatch for
devices with a broken implementation of one of the protocols): forcing RAOP or
AirPlay 2 passes that protocol verbatim. For MA-side planning decisions (which
pairing flow to run, which service/port to target) the same feature-bit test
the binary uses is mirrored in `supports_airplay2()`: any device advertising
AirPlay 2 gets AirPlay 2, RAOP is only used for devices that do not support it.

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
   - Delegated to the cliairplay binary: `cliairplay --pair-setup --port <port> --dacp <id> <ip>`
   - The binary POSTs `/pair-pin-start` (device shows its PIN), reads the PIN from stdin,
     performs the SRP/HomeKit exchange and prints `CREDENTIALS: <192 hex chars>` on stdout
   - Using the binary guarantees the pairing identity is byte-identical to what
     pair-verify uses at stream time

2. **RAOP Pairing** - For AirPlay 1 (native Python implementation)
   - 3-step SRP authentication with plist encoding
   - Ed25519 key derivation from auth secret
   - AES-GCM encryption
   - Produces `client_id:auth_secret` format credentials (the secret is passed
     to the binary via `--secret` at stream time)

### Pairing Flow

1. **Start pairing** (config action) → the pair-setup process starts and the device displays its 4-digit PIN
2. **User enters PIN** in Music Assistant configuration
3. **Complete pairing** (config action) → the PIN is fed to the pairing process/exchange
4. **Store credentials** in player config (protocol-specific key: `raop_credentials` or `airplay_credentials`)

**Important**: The DACP ID used during pairing must match the ID used during streaming (pair-verify signs with it). The provider uses the first 16 hex characters of `server_id` as a persistent DACP ID to ensure compatibility across restarts.

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
        │ PCM s16le (16-bit) │ or s32le (24-bit hi-res)
        │                    │                    │
┌───────▼──────┐    ┌────────▼────────┐   ┌──────▼──────┐
│  cliairplay  │    │   cliairplay    │   │  cliairplay │
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
   - Calculates the audible start instant (`now + setup lead`) as unix epoch milliseconds
   - Every member receives the exact same start value (`--start-unix-ms`)

2. **Client Setup** (per player, `_start_client()` method)
   - Creates an `AirPlayStream` instance with the player's PCM format
     (16-bit default, 24-bit s32le when hi-res is enabled)
   - Starts the CLI process with the shared start instant
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

1. **Ring buffer**: Session maintains a few seconds of recent audio chunks in memory
2. **Immediate buffered feed**: Late joiner receives buffered chunks immediately to prime the ffmpeg/CLI pipeline
3. **Compensated start time**: The joiner's start instant accounts for the buffer duration: `start_time + (seconds_streamed - buffer_duration)`, shifted forward (with the buffer head trimmed) when it would land in the past
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

A single `cliairplay-<platform>-<arch>` binary handles both protocols; the
provider selects it based on:
- **Platform**: Linux, macOS
- **Architecture**: x86_64, arm64, aarch64

The protocol (RAOP or AirPlay 2) is chosen at runtime via the `--protocol`
flag. Binaries are located in the [bin/](bin/) directory and validated on first use.

### Binary Communication

**Input** (stdin):
- PCM audio data piped from FFmpeg: s16le for 16-bit, raw s32le for 24-bit
  (the binary truncates 32→24 internally when `--bitdepth 24` is passed)
- May be written eagerly, ahead of the scheduled start; byte 0 maps to the
  sample audible at the start instant

**Commands** (named pipe):
- Interactive commands sent via `AsyncNamedPipeWriter`
- Examples: `ACTION=PLAY`, `ACTION=PAUSE`, `VOLUME=50`, `TITLE=Song Name`

**Output** (stderr):
- Normalized `[STATUS]` messages (connected/playing/paused/eof), logs and errors

**Output** (stdout):
- `[STATUS] latency ...` line with the effective lead and the device's
  reported buffering window (parsed by `_stdout_reader()` for diagnostics)

The provider monitors stderr in a separate task (`_stderr_reader()` in [stream.py](stream.py)) to:
- Update player state
- Detect connection completion
- Handle errors and packet loss
- Track elapsed time

## Start Timing and Synchronization

The provider never handles NTP fixed-point formats: the group start is passed
to the binary as plain unix epoch milliseconds (`--start-unix-ms`), meaning
"**the first sample is audible exactly at this instant**" on every protocol
path (RAOP, AirPlay 2 RAOP-compat and native).

1. Calculate the start instant: `now + AIRPLAY_SETUP_LEAD_MS` (fixed lead that
   covers process spawn + connect + session setup)
2. Pass the **same** value to every member of a sync group (mixed RAOP +
   AirPlay 2 groups align by construction)
3. The binary owns all lead/buffer handling from there: it fills the
   receiver's buffer ahead of the audible start (clamped to the buffering
   window the device reports), so the start cannot underrun
4. Audio may be written to the binary's stdin as soon as it is available -
   byte 0 of stdin maps to the sample audible at the start instant
5. Per-player `sync_adjust` config allows fine-tuning (+/- milliseconds)

### Shared PTP Clock Daemon

Native AirPlay 2 receivers that advertise `SupportsPTP` are timed via PTP
(IEEE 1588/gPTP). Only one process per host can bind the privileged PTP ports
(UDP 319/320) and every receiver in a sync group must lock to the same
grandmaster, so the provider runs **one** `cliairplay --ptp-daemon` for its
whole lifetime (spawned at setup, terminated at unload, restarted once if it
crashes). Every AirPlay 2-capable stream is started with `--ptp-shared` while
the daemon runs, attaching it to the daemon's elected clock via shared memory.

If the daemon cannot start (ports taken, no root/`CAP_NET_BIND_SERVICE`), a
warning is logged and streams fall back to their in-process timing engine -
playback keeps working but multi-room PTP sync may be degraded.

After connect, the binary reports the effective lead and the device's
buffering window on stdout (`[STATUS] latency lead_ms=... device_min_frames=...
device_max_frames=...`), which the stream parses and logs for diagnostics.

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
- **`airplay_protocol`**: Choose RAOP, AirPlay 2, or automatic (default: automatic; automatic lets the binary resolve the route from the mDNS TXT)

### General
- **`password`**: Device password if required (RAOP)
- **`ignore_volume`**: Ignore device volume reports (default: false)
- **`airplay_latency`**: Advanced playback lead/buffer override in milliseconds (default 0 = automatic: the binary's 2000 ms AirPlay-standard default, clamped to the device-reported window)
- **`hires_playback`**: Advanced per-player opt-in for 24-bit playback over native AirPlay 2 (default: off; only shown for AirPlay 2-capable devices - some devices accept 24-bit and play silence, hence opt-in)
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

The binary can be validated with a test command:
- **cliairplay**: `cliairplay --check` (should output "cliairplay check")

### Adding New CLI Commands

To add a new command to the CLI binary:
1. Update the cliairplay binary source code (external repository)
2. Handle the command in `send_cli_command()` in [stream.py](stream.py)
3. Send command via named pipe: `await stream.send_cli_command("YOUR_COMMAND=value")`

### Debugging Streaming Issues

Enable verbose logging in Music Assistant to see:
- CLI binary arguments
- stderr output from binaries
- DACP requests
- Connection state changes
- Packet loss warnings

## Credits

- **libraop**: foundation for the cliairplay binary (RAOP + AirPlay 2) - https://github.com/music-assistant/libraop
- **OwnTone**: reference for the AirPlay 2 / HAP implementation - https://github.com/OwnTone
- **pyatv**: reference for the HAP pairing protocol - https://github.com/postlund/pyatv

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
