# AirPlay Provider

## Overview

The AirPlay provider enables Music Assistant to stream audio to AirPlay-enabled devices on your local network. It supports both **RAOP (AirPlay 1)** and **AirPlay 2** protocols, providing compatibility with a wide range of devices including Apple HomePods, Apple TVs, Macs, and third-party AirPlay-compatible speakers.

### Key Features

- **Dual Protocol Support**: The cliairplay binary automatically resolves the best route (RAOP, AirPlay 2 RAOP-compat or native AirPlay 2) from the device's mDNS TXT records
- **Pairing**: Supports pairing with Apple devices (Apple TV, HomePod, Mac) using HAP/HomeKit pair-setup (via the cliairplay binary) or legacy RAOP pairing (native)
- **Multi-Room Audio**: Synchronizes playback across multiple AirPlay devices from a single wall-clock start instant, with a shared PTP clock daemon for native AirPlay 2 timing
- **Now-Playing Metadata**: Sends title/artist/album/artwork/progress — as DMAP over the stream for speakers, and additionally as MediaRemote over the native AirPlay 2 flow so an Apple TV renders the now-playing screen
- **Hi-Res Audio**: 24-bit playback (44.1/48 kHz) over the AirPlay 2 flow, enabled automatically for receivers that advertise 24-bit ALAC
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
└── bin/                 # Binary documentation and downloaded local artifacts
    ├── README.md
    └── cliairplay-*     # Downloaded during container build or local setup; not tracked
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
  - 24-bit audio support (native AirPlay 2 flow), enabled automatically from the
    formats the receiver advertises in its `/info` response (`supportedAudioFormatsExtended`,
    else the legacy `supportedFormats`). Both the realtime and buffered stream tables
    count: receivers understate them, and an Apple TV lists 24-bit for its buffered
    stream only while rendering it fine on the realtime stream.
- **Binary**: `cliairplay --protocol airplay2` (native implementation, no OwnTone dependency)

### Automatic Selection

Route selection is automatic; there is no protocol picker. The provider passes
`--protocol auto` together with the device's full `_airplay._tcp` TXT records
(`--txt`). The **binary** then resolves the route from the advertised feature
bits: RAOP vs AirPlay 2, native vs RAOP-compatible flow, transient pairing vs
stored-credential pair-verify, and PTP vs NTP timing.

For MA-side planning decisions (which pairing flavor to run, which service/port
to target) the same feature-bit test the binary uses is mirrored in
`supports_airplay2()`: any device advertising AirPlay 2 gets AirPlay 2, RAOP is
only used for devices that do not support it.

### Streaming mode (escape hatch)

The only user override is the advanced per-player `streaming_mode` selector. It
pins the protocol/timing lane for a device whose automatic route misbehaves, and
each option is offered only when the device can actually use it: the AirPlay 2
lanes need AirPlay 2 support, legacy RAOP needs an advertised `_raop` service,
and Apple receivers get every lane except NTP timing (they render silence on an
NTP-timed realtime stream). The modes map
onto the binary's `--protocol`/`--timing` arguments. Music Assistant writes the
setting itself when an automatic route has conclusively failed: a device that
advertises PTP but never answers a clock probe is switched to "AirPlay 2 - NTP
timing", while a native route whose control channel fails after its keepalive
retries is switched to "AirPlay 2 - compatibility mode". The next playback uses
the new route; setting the mode back to Automatic retries the original one.

The selector is hidden only for RAOP-only devices (no alternative lane; a
stray persisted value is ignored). Apple devices (HomePod / Apple TV) get
every lane except NTP timing — they render silence on an NTP-timed realtime
stream (hardware-measured) — leaving pinned PTP, the compatibility flow and
legacy RAOP as escape hatches for networks where the PTP ports are blocked.
AirPlay-2-only devices get the AirPlay 2 lanes without RAOP: they are the
class the NTP escape exists for.

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
   - Connects every member before anchoring playback
   - Wires each member's ffmpeg into its persistent CLI stdin and starts feeding audio
   - Waits until every member's binary confirms the feed flowing (`[STATUS] audio`),
     then sends one shared audible start instant with an anchor lead
     (400 ms solo / 500 ms warm group / 2500 ms cold group, see
     `AIRPLAY_COLD_GROUP_START_LEAD_MS`); readiness is fully event-driven, so no
     setup time is guessed and the binary bursts the receiver pre-fill after START

2. **Client Setup** (per player, `_start_client()` method)
   - Creates an `AirPlayStream` instance with the player's PCM format
     (16-bit default, 24-bit s32le for a 24-bit capable receiver)
   - Starts the CLI process and connects to the receiver without anchoring playback
   - Configures FFmpeg for audio format conversion and optional DSP filters,
     feeding its output into the CLI's persistent stdin

3. **Audio Streaming** (`_audio_streamer()` method)
   - Receives PCM audio chunks from Music Assistant core
   - Distributes chunks to all players via FFmpeg
   - Tracks elapsed time based on bytes sent
   - Handles silence padding if audio source is slow

4. **Connection Monitoring**
   - Waits for all devices to connect and confirm audio flowing before anchoring playback
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
2. **Anchored past receiver readiness**: The joiner's START is commanded no earlier than the instant its binary projects the receiver's clock becomes usable, and the binary acks the instant it can truly honour
3. **Anchor first, then prime**: The joiner's START is sent before the buffered chunks; pre-START the binary only buffers its bounded ring and sends nothing, so anchoring first lets it drain the prime as it streams in
4. **Content mapped onto the acked instant**: The stream position due at that instant is primed from the ring tail (when it is at or behind the write head) or skipped off the head of the live feed (when it is ahead). There is no catch-up: the binary makes the first post-START stdin byte audible exactly at the acked instant and freezes the anchor there

**Note**: The projection can only push a joiner's anchor later, never earlier — `AIRPLAY_LATE_JOIN_MIN_HEADROOM_MS` is the floor, and the one the anchor rests on whenever no projection arrives or the projection does not clear it. The binary also runs a post-commit clock verification that can pull an anchor forward, but it only arms when the receiver has still not probed by the time it reads the START, and only for an anchor that clears the receiver queue depth plus 500 ms. The deeper defaults in `AIRPLAY_BUFFER_DEPTH_DEFAULTS` ([constants.py](constants.py)) reach past ~2 s of effective depth, where a joiner's anchor no longer clears that — by design: the queue starts releasing frames one depth *before* the anchor, and a line with audio on the wire cannot move.

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
| `dmcp.device-volume=X` | Volume changed at the device |
| `device-prevent-playback=1` | Device switched to another source or powered off |
| `device-prevent-playback=0` | Device ready for playback again |

### Volume Ownership

An AirPlay volume command sets the receiver's own volume, and that level stays behind on the device after the session ends. Music Assistant therefore only sends one when nothing else owns the volume of this output: on a device that is also reachable through a native provider or another protocol (a Sonos speaker, an AV receiver), the stream simply plays at the level the device is already set to, and volume stays with that provider.

A volume is still sent when the AirPlay output itself is the resolved volume control, when a mute has to travel with the stream, and when a session asks for a specific level (an announcement).

### Volume Feedback

Devices can report their own volume changes back over DACP; Music Assistant applies them unless `ignore_volume` is set. Genuine Apple devices are auto-set to ignore these reports (they manage volume internally).

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

The executables are not stored in this source repository or its Python packages.
Official Linux container builds download the pinned, architecture-specific asset
from the [airplay-cli releases](https://github.com/music-assistant/airplay-cli/releases)
and verify it against that release's `SHA256SUMS`. For local source development,
`scripts/setup.sh` downloads and verifies the same pinned asset when it is absent.

### Binary Communication

**Input** (stdin):
- A single persistent PCM stream for the whole CLI lifetime: s16le for 16-bit,
  raw s32le for 24-bit (the binary truncates 32→24 internally when
  `--bitdepth 24` is passed). The binary reads stdin into one ring buffer.
- May be written eagerly, ahead of the scheduled start; byte 0 maps to the
  sample audible at the start instant. A seek/next flushes the ring in place and
  refills it — the stdin connection is never closed between tracks (only the
  per-seek ffmpeg feeding it is restarted).

**Commands** (named pipe):
- Interactive commands sent via `AsyncNamedPipeWriter`
- `ACTION=START` + `START_UNIX_MS=<t>` anchors/re-anchors playback;
  `ACTION=FLUSH` flushes the live stream in place (acknowledged by
  `[STATUS] flushed`) so the same stdin can be refilled for a seek/next
- Examples: `ACTION=PLAY`, `ACTION=PAUSE`, `VOLUME=50`, `TITLE=Song Name`
- MA creates the pipe and sends text metadata immediately after process start;
  timeline-anchored metadata and artwork are refreshed once the receiver connects

**Output** (stderr):
- Normalized `[STATUS]` messages (connected/playing/paused/flushed/audio/eof), logs
  and errors. `[STATUS] audio` is a one-shot per start cycle (re-armed by each
  FLUSH) reporting the first stdin bytes arriving; MA anchors START only after it

**Output** (stdout):
- `[STATUS] latency ...` line with the effective lead and the device's
  reported buffering window (parsed by `_stdout_reader()` for diagnostics)

The provider monitors stderr in a separate task (`_stderr_reader()` in [stream.py](stream.py)) to:
- Update player state
- Detect connection completion
- Handle errors and packet loss
- Track elapsed time

## Start Timing and Synchronization

The provider never handles NTP fixed-point formats: playback is anchored over
the command pipe. `START_UNIX_MS` is a plain unix epoch millisecond meaning
"**the first pending stdin sample is audible exactly at this instant**" on every
protocol path (RAOP, AirPlay 2 RAOP-compat and native).

1. Start every CLI and wait until every group member reports connected
2. Wire each member's ffmpeg into its persistent stdin, begin feeding PCM and
   wait until every member confirms the feed flowing (`[STATUS] audio`)
3. Send one shared `START` (now + 400 ms solo / 500 ms warm group / 2500 ms cold
   group, see `AIRPLAY_COLD_GROUP_START_LEAD_MS`) to every member; readiness is
   event-confirmed so a warm anchor covers only the receiver re-anchor, and the
   binary bursts the receiver pre-fill from START
4. **Warm seek / next-track / grouped resume** reuse the live connections: MA
   stops feeding old audio, kills the per-seek ffmpeg (never the persistent
   stdin), sends `ACTION=FLUSH` to every member and awaits `[STATUS] flushed`,
   then feeds a fresh ffmpeg into the same stdin, awaits `[STATUS] audio` and
   sends one shared `START`.
   Standby keeps each protocol connection alive for the same flush-refill resume
5. Sendspin starts ride the same persistent-stdin flush-refill (cold connect +
   `START`, warm `FLUSH` + `START`) instead of a cold reconnect. They anchor as
   a join, so the binary reports the instant it really scheduled and the bridge
   maps the group's audio onto that instant rather than the one it asked for
6. Per-player `sync_adjust` config allows fine-tuning (+/- milliseconds)

### Shared PTP Clock Daemon

Native AirPlay 2 receivers that advertise `SupportsPTP` are timed via PTP
(IEEE 1588/gPTP). Only one process per host can bind the privileged PTP ports
(UDP 319/320) and every receiver in a sync group must lock to the same
grandmaster, so the provider runs **one** `cliairplay --ptp-daemon` for its
whole lifetime (spawned at setup, terminated at unload, restarted once if it
crashes). AirPlay 2-capable streams are started with `--ptp-shared` once the
daemon reports it is serving, attaching them to its elected clock via shared
memory. A sync group resolves that choice once and applies it to every member,
so a group never mixes members on the shared clock with members off it.

Sendspin-bridged players hold the same line across their Sendspin group. Their
processes are spawned independently and can outlive several tracks, so a bridge
adopts the choice a live group member is already running with and only asks the
daemon when the group has no such member. That keeps members which start minutes
apart, or which keep a warm process across a track change, on one clock.

The official Music Assistant container runs as root, allowing the daemon to bind
these ports. A custom container running Music Assistant as a non-root user must
grant the binary `CAP_NET_BIND_SERVICE` (for example,
`setcap cap_net_bind_service=+ep <path-to-cliairplay>`) and retain that capability
in the container's bounding set. UDP 319/320 must also be free on the container's
network namespace.

If the daemon cannot bind, the provider logs a warning and native AirPlay 2
streams fall back to NTP timing. Playback keeps working, but native AirPlay 2
multi-room sync may be degraded.

After connect, the binary reports the effective lead and the device's
buffering window on stdout (`[STATUS] latency lead_ms=... device_min_frames=...
device_max_frames=...`), which the stream parses and logs for diagnostics.

## Player Types

The provider uses a shared AirPlay streaming base with separate player models.

### PlayerType.PLAYER
- **Devices**: Apple TV, HomePod
- **Reason**: These are standalone players with a native control and
  playback-monitoring plane (Companion/MRP) Music Assistant can use
- **Behavior**: Exposed as top-level players, with external playback monitoring
  and native controls when the device advertises them
- **Not merged**: These players are NOT combined with other protocols

### PlayerType.PROTOCOL
- **Devices**: All other AirPlay receivers
- **Reason**: AirPlay is just one output protocol among many for these devices (often supporting Chromecast, DLNA, etc.)
- **Behavior**: Automatically merged into a **Universal Player** if other protocols are detected for the same device
- **Example**: A Sonos speaker supporting both AirPlay and Chromecast will appear as a single "Sonos" player with selectable output protocols

**Detection**: [provider.py](provider.py) selects `AirPlayControlPlayer` for
Apple TV and HomePod devices (`is_apple_device`); all other endpoints use
`GenericAirPlayPlayer`. The model is decided from the device's own identity
only and never changes for a registered player: the model determines the
player id exposed to API consumers (protocol endpoint behind a parent player
vs standalone player), which must stay stable for Home Assistant and other API
clients. Unlike the separate Companion/MRP mDNS records, the identity is
always available in the very record that creates the player, so the decision
cannot vary with discovery timing. Which control features are offered on a
control-capable player is then decided from the advertised capabilities
(pairable Companion, native MRP service, or AirPlay MRP tunnel), degrading
gracefully when a device does not advertise them.

### Independent device control

Control-capable receivers keep AirPlay streaming separate from their monitoring
and control connections:

- **Companion** tracks power state and controls wake, power, native playback,
  and volume independently of Music Assistant streaming.
- **AirPlay MRP tunnel** tracks external playback, including the active app,
  metadata, elapsed time, and transport state.
- Music Assistant explicitly wakes a sleeping device before starting or
  resuming an AirPlay stream.
- AirPlay streaming, Companion control, and MRP playback monitoring keep
  independent pairing credentials. This lets pyatv retain the complete
  accessory identity required by MRP without changing the streaming identity.
  A failed MRP monitor does not interrupt a healthy Companion connection, and
  vice versa.

Apple TV models generally expose pairable Companion and an AirPlay MRP tunnel.
Current HomePod firmware advertises Companion without a PIN-pairing path, so
Companion power/wake control is not available; HomePods instead use AirPlay's
transient MRP tunnel with no PIN or persisted credentials. Third-party receivers
remain protocol endpoints regardless of advertised control capabilities, which
keeps their exposed player id stable and their Universal Player merging intact.

**For more details on output protocols and protocol linking**, see the [Player Controller README](../../controllers/players/README.md), which explains:
- How multiple protocol players for the same physical device are automatically linked
- The Universal Player concept for devices without native vendor support
- Protocol selection and device identifier matching
- Native player linking vs. Universal Player creation

## Configuration Options

### Protocol Selection
- **`streaming_mode`**: Advanced per-player pin of the protocol/timing lane (default: Automatic). Options are offered per advertised capability; route selection is otherwise fully automatic (the binary resolves it from the mDNS TXT). Auto-pinned to NTP timing when the device never answers the PTP clock, or to compatibility mode after the native control channel conclusively fails

### General
- **`password`**: Device password, stored encrypted (hidden). It is entered through the player's setup flow, not the settings form: a device that announces password protection without one stored - or that rejects the stored one - is marked as needing setup, which offers the password step again
- **`ignore_volume`**: Ignore device volume reports (default: false)
- **`sync_adjust`**: Per-player audio synchronization delay correction in milliseconds (default: 0; negative = play earlier, e.g. to compensate for a TV/AV receiver that adds latency). The playback lead is handled automatically by the binary.
- **`buffer_depth`**: Advanced per-player override of how much audio the receiver keeps queued ahead of playback, in milliseconds. Defaults to the depth the device's family needs, or Automatic when no family matches; Automatic resolves through that same table at stream time, so it never downgrades an affected device. Receivers whose internal pipeline starves at the shallow default render nothing behind an otherwise healthy session, and deepening their queue is what makes them play. Applies to the AirPlay 2 route only - a player forced to RAOP keeps the binary's own depth. The cost is the delay under Known Issues below

### Pairing
- **`raop_credentials`**: Stored RAOP pairing credentials (hidden)
- **`airplay_credentials`**: Stored AirPlay 2 pairing credentials (hidden)
- **`companion_credentials`**: Stored Companion credentials (hidden and paired
  separately)
- **`mrp_credentials`**: Stored AirPlay-tunneled MRP credentials (hidden;
  transient MRP requires none)
- **`native_mrp_credentials`**: Stored native MRP credentials (hidden)

## Known Issues

### Limitations

1. **DACP remote control**: Only active while streaming; controlled devices use
   Companion/MRP for idle and external playback control
2. **Pause while synced**: Parks the whole session instead of pausing members
   individually, so they can resume sample-aligned; a member that has lost its
   connection falls back to stop. The park belongs to the session rather than to
   the group membership, so breaking up a paused group leaves the remaining
   player parked, and only a queue-driven re-anchor revives it
3. **HomePod power control**: Current HomePod firmware does not advertise
   Companion PIN pairing, so explicit power/wake control is unavailable
4. **Apple TV artwork for non-public images**: Cover art only reachable through
   the imageproxy (e.g. filesystem-provider images with no public URL) does not
   currently render on the Apple TV's now-playing screen, while externally-hosted
   art does
5. **Warm boundaries wait for the queued audio** (native AirPlay 2): Pause, seek
   and track changes leave the audio the receiver already holds in place, so it
   renders that first and `buffer_depth` is also the delay before the boundary
   is heard. Dropping the queue instead produced audible noise bursts (measured
   on Apple receivers), so keeping it is an accepted trade-off. It is most
   noticeable on pause, where playback is expected to stop at once. On a
   receiver that needs a deep queue to render at all, the delay cannot be tuned
   away without silencing it

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
- **pyatv**: Apple Companion and MRP implementation - https://github.com/postlund/pyatv

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

### Transport Loss

The CLI accepts and discards audio once its process is gone, so a lost transport is only visible on the `AirPlayStream` itself. The bridge checks it on every chunk: the dead stream is released and a fresh one is cold-started and re-anchored on the group's live timeline, so the speaker rejoins where the rest of the group is playing.

Giving up on a stream — a start that raised, a protocol that never became ready, or a transport that dropped again within `BRIDGE_TRANSPORT_RECOVERY_GUARD_SECONDS` of a recovery — takes the speaker out of the Sendspin session. Sendspin reports playback from the group's own state, and the protocol gives a player no way to report that it went silent, so a bridge that merely stopped feeding its speaker would hold the visible player on PLAYING for the rest of the stream. Leaving is what surfaces that silence: a shared group plays on without this speaker, a solo one stops.

Leaving a shared group schedules a bounded re-join through the ordinary `SendspinGroup.add_client`, on the delays in `BRIDGE_REJOIN_ATTEMPT_DELAYS`, so a speaker that was only briefly away comes back on its own. A bridge that gives up again within `BRIDGE_TRANSPORT_RECOVERY_GUARD_SECONDS` of being put back is left out for good — that is what keeps a device which cannot hold a connection from cycling in and out of the group, since re-joining re-runs the very start that just failed. The attempt is abandoned when the speaker has meanwhile been given a group or a stream of its own, is streaming outside the bridge, or the group it left no longer exists. A speaker missing from discovery is not re-joined but is looked for again on the next attempt, because a device that rebooted stays absent for a while after it starts answering. A solo bridge has nothing to re-join, since leaving is what stops it.

The group re-join recovery in `stream.py` only covers native AirPlay grouping — a bridged player's group membership lives on its Sendspin player, not on the AirPlay one.

### Stalled Receiver Clocks

A receiver that never answers the server's PTP clock renders silence. The bridge warns and anchors anyway, which follows a native group start rather than a late joiner: a joiner is dropped because the session plays on without it, whereas here dropping would stop the speaker — and a stall is not evidence enough for that. The binary reports it as a diagnosis rather than a verdict (a receiver that begins probing late reports probing and then ready as usual) and re-arms that reporting on every `FLUSH` and `START`, while the server latches the last reading it parsed — `state=cold` lines carry no projection and are dropped. The re-armed report waits on the audio loop's next pass, which the flush ack ordinarily beats, so a warm re-anchor is reading the cycle before it. Nothing is lost by that: a flush leaves the receiver's clock alone, so the projection still describes the same acquisition and the anchor is right to sit past it whether or not that instant has arrived; for a receiver that is not answering, that reading is the only evidence there is. Nor is this the give-up case above: the transport is healthy, so the bridge stays in its Sendspin session and the stall reaches the user through the warning the binary's report raises, which names the device and the UDP ports to check.

The binary diagnoses a stall deliberately more slowly than it projects readiness (see `AIRPLAY_CLOCK_READY_TIMEOUT_MS`), so a cold start reads `UNREPORTED` and anchors without a projection; a stall is what a warm re-anchor sees. Either way the receiver has not probed, so the post-commit clock verification described under Late Join Support arms wherever the anchor clears the receiver queue depth, and holds the join's `started` ack until it gives up short of the commanded anchor — bounded by that anchor, and well inside `AIRPLAY_JOIN_START_ACK_TIMEOUT_MS`.

### Requirements

- Sendspin provider must be enabled
- AirPlay player must have a valid MAC address for protocol linking

### Files

| File | Description |
|------|-------------|
| `sendspin_bridge.py` | Bridge implementation for Sendspin to AirPlay integration |
