# Spotify Connect Provider - Architecture

## Overview

The Spotify Connect provider enables Music Assistant to integrate with Spotify's Connect protocol, allowing any Music Assistant player to appear as a Spotify Connect device in the Spotify app. This provider acts as a bridge between Spotify's proprietary Connect protocol and Music Assistant's audio streaming infrastructure.

## What is Spotify Connect?

Spotify Connect is Spotify's proprietary protocol that allows users to:
- Control playback on various devices from the Spotify app
- Transfer playback seamlessly between devices
- See what's playing with rich metadata (artwork, artist, album)
- Control volume and playback state

Unlike traditional Spotify integrations that require Web API authentication, Spotify Connect uses librespot - a reverse-engineered implementation of Spotify's audio streaming protocol.

## How It Works

### Architecture Components

```
┌─────────────────┐
│   Spotify App   │  (Mobile/Desktop/Web)
└────────┬────────┘
         │ Spotify Connect Protocol
         ▼
┌─────────────────────────────────────┐
│  Spotify Connect Provider           │
│  ┌───────────────────────────────┐  │
│  │  librespot Process            │  │  Handles:
│  │  - Authentication             │  │  - Spotify protocol
│  │  - Audio streaming            │  │  - Audio decoding
│  │  - Metadata extraction        │  │  - Session management
│  └───────────────────────────────┘  │
│  ┌───────────────────────────────┐  │
│  │  events.py Webservice         │  │  Receives:
│  │  - Session events             │  │  - Connected/disconnected
│  │  - Metadata updates           │  │  - Playback state changes
│  │  - Volume changes             │  │  - Track metadata
│  └───────────────────────────────┘  │
│  ┌───────────────────────────────┐  │
│  │  AudioSource (MediaItem)      │  │  Provides:
│  │  - Capability flags           │  │  - Playback control
│  │  - StreamMetadata (live)      │  │  - Metadata display
│  │  - Web API integration        │  │  - Browse under Live Inputs
│  └───────────────────────────────┘  │
└─────────────────┬───────────────────┘
                  │
                  ▼
┌─────────────────────────────────────┐
│  Music Assistant Player             │
│  - Receives audio stream            │
│  - Displays metadata                │
│  - Reports state changes            │
└─────────────────────────────────────┘
```

### Key Components

#### 1. **librespot Process**
- External binary that implements Spotify's Connect protocol
- Runs as a subprocess managed by the provider
- Handles all Spotify-specific communication:
  - Authentication using Spotify credentials
  - Audio streaming and decoding to PCM
  - Session management (connect/disconnect)
- Writes raw PCM audio (44.1 kHz / 16-bit / stereo) to a named FIFO via
  `--backend pipe --device <fifo>`; the core's streams controller opens the
  FIFO with ffmpeg using `-re` (rate-paced read) when the stream starts
- Sends events to the custom webservice via HTTP (see events.py)

#### 2. **events.py Webservice**
- Python script that receives event callbacks from librespot
- Runs on a custom port for each provider instance
- Provides an HTTP endpoint that librespot calls with:
  - Session connected/disconnected events
  - Track metadata (title, artist, album, artwork)
  - Playback state changes (playing, paused, stopped)
  - Volume changes from Spotify app

#### 3. **AudioSource Model**
The provider exposes an `AudioSource` MediaItem that represents the Spotify Connect audio source.
AudioSources are browsable under the global "Live Inputs" node and are played via the
standard ``play_media`` flow — they appear in the player's queue as a single live item,
the same way radio stations do.

**Static Properties:**
- `item_id`: `"main"` (combined with the provider instance_id forms the persistent
  browse/play URI listed under "Live Inputs"; AudioSource items are not favoritable
  in MA core today — see ``MusicController.add_item_to_favorites``)
- `name`: Display name (e.g., "Music Assistant")
- `exclusive`: True (a single librespot stream can only serve one queue at a time)
- `allow_external_trigger`: True (Spotify app picks MA → plugin starts playback)
- `can_initiate`: False — see "Cold-start limitation" below

**Dynamic Capabilities:**
- `can_play_pause`: Enabled when Web API control available
- `can_seek`: Enabled when Web API control available
- `can_next_previous`: Enabled when Web API control available

**Cold-start limitation (`can_initiate=False`):**
MA cannot reliably initiate Spotify Connect playback from a cold start. The Web
API's `PUT /me/player/play?device_id=X` and `PUT /me/player` transfer endpoints
return 200, but Spotify silently refuses to start when no playback context
exists — librespot logs `context is not available` and the device stays idle.
The "context" expires on any meaningful idle, so MA-initiated entry is
fundamentally flaky. We therefore only advertise external entry (Spotify app →
pick MA). Resume from MA *during* an existing Spotify session works, because a
context is still present at that point.

When capabilities flip (Web API becomes available/unavailable) the AudioSource is rebuilt
via ``_build_audio_source()`` so the next ``get_audio_sources()`` returns the updated flags.

**Stream Metadata:**
The live track info (title, artist, album, artwork, elapsed time) is published through
``StreamMetadata`` attached to the active queue item's ``StreamDetails``. Updates are pushed
via ``mass.streams.update_stream_metadata(queue_id, ...)`` — the same channel ICY radio
metadata uses.

#### 4. **Audio Pipeline**
```
librespot ──PCM──▶ FIFO ──▶ ffmpeg (-re, rate-paced) ──▶ MA player
```

The stream is exposed as ``StreamType.NAMED_PIPE`` so the streams controller
can open the FIFO with ffmpeg directly. ffmpeg's `-re` flag paces the read at
native rate, which is what keeps librespot's (non-realtime) pipe writer from
filling an unbounded buffer ahead of playback. Routing through a Python
``CUSTOM`` generator instead would re-introduce that buffering and make
pause/skip take seconds to react.

**Session-stall watchdog.** If librespot starts producing audio before the
streams controller has attached ffmpeg, the FIFO's kernel pipe buffer (~64KB)
fills, librespot's next pipe write blocks, and the daemon's main loop wedges —
no further `playing`/`paused` events fire. To recover, `_arm_session_watchdog`
is armed on every `session_connected`; if no definitive state event lands
within ``SESSION_STALL_TIMEOUT_S`` (8s), `_emergency_drain_fifo` opens the
FIFO with ``O_RDONLY | O_NONBLOCK`` for up to ``SESSION_STALL_DRAIN_TIMEOUT_S``
(2s) to unblock the write. The drain bails out early if librespot recovers or
the normal stream attaches, so it doesn't compete with ffmpeg for bytes.

## Multi-Instance Support

Each Spotify Connect provider instance:
- Runs its own librespot process
- Has its own cache directory for credentials
- Binds to a unique webservice port
- Links to a specific Music Assistant player
- Appears as a separate device in Spotify app

This allows multiple Spotify Connect devices in one Music Assistant installation, for example one per player.

## Authentication & Credentials

### Credential Storage
- **Location**: `{cache_dir}/credentials.json`
- **Format**: Librespot proprietary format
- **Contents**:
  - `username`: Spotify account username/email
  - Encrypted authentication tokens
  - Device information

### Authentication Flow
1. User opens Spotify app and selects the Music Assistant device
2. Spotify authenticates and establishes a Connect session
3. librespot receives credentials and caches them locally
4. Future connections reuse cached credentials automatically

### Username Extraction
The provider reads `credentials.json` to extract the logged-in username, which is used for matching with the Spotify music provider (see Playback Control below).

## Playback Control Integration

### Problem Statement
By default, Spotify Connect is a **passive source** - it receives audio but Music Assistant cannot control playback (play/pause/next/previous/seek) because the Connect protocol is one-way.

### Solution: Web API Integration
When the Spotify account logged into Connect matches a configured Spotify music provider, the provider enables bidirectional control by using Spotify's Web API.

### Active-device gating
All transport commands (`on_source_control`) and volume changes
(`on_volume_change`) check that MA is still the active Spotify device before
calling the Web API. Without this guard the API answers 403 for any command
issued while another Spotify device is active, surfacing as a generic failure
in the UI. The guard raises ``AudioError(NOT_ACTIVE_DEVICE_MESSAGE)`` instead,
which the queue/streams layer propagates verbatim to the frontend so the user
sees a clear "Music Assistant is not the active Spotify playback device" hint
and can re-select MA in the Spotify app.

### Architecture

#### Username Matching Process
1. **On Session Connected**: librespot reports username via events
2. **Provider Lookup**: Search all providers for Spotify music provider
3. **Username Comparison**: Match `credentials.json` username with Web API user
4. **Capability Update**: Enable control callbacks if match found

#### Timing Considerations
- Spotify music provider may not be loaded during Connect initialization
- Username match check happens when playback starts (`sink`/`playing` events)
- This ensures music provider has time to initialize

#### Control Proxying Architecture

Player controller commands (play/pause/next/previous/seek/volume) reach the active queue
item's owning provider through the standard ``PluginProvider.on_source_control`` hook:

```python
async def on_source_control(
    self,
    source_id: str,
    action: SourceControl,
    value: int | None = None,
) -> None
```

**Flow:**
1. User presses play/pause in Music Assistant UI
2. Player controller's command handler sees the active queue item is `MediaType.AUDIO_SOURCE`
3. Reads the AudioSource's capability flags (`can_play_pause`, `can_seek`, `can_next_previous`)
4. If supported, calls `plugin_prov.on_source_control(source_id, action, value)`
5. The Spotify Connect provider dispatches on `action` to the appropriate Web API call
6. Spotify app receives command and updates state

#### Implementation Details

**Provider Methods:**
- `_check_spotify_provider_match()`: Finds matching Spotify provider
- `_build_audio_source()`: Constructs the AudioSource with current capability flags
- `_update_source_capabilities()`: Rebuilds the AudioSource so new flags propagate
- `_on_play/pause/next/previous/seek/volume()`: Web API call implementations dispatched
  from `on_source_control`

**Web API Commands:**
- `PUT /me/player/play?device_id={id}` - Resume playback on this device
  (preferred for the play/resume path: transfer-with-play sometimes leaves the
  device paused for a long time before it actually starts)
- `PUT /me/player` with `{device_ids:[id], play:?}` - Transfer fallback
- `PUT /me/player/pause` - Pause playback
- `POST /me/player/next` - Skip to next track
- `POST /me/player/previous` - Skip to previous track
- `PUT /me/player/seek?position_ms={ms}` - Seek to position
- `PUT /me/player/volume?volume_percent={pct}` - Set volume

### Event-Driven Updates

The provider subscribes to events to maintain accurate state:

**Events Monitored:**
- `EventType.PROVIDERS_UPDATED`: Re-check Spotify music provider availability
- Custom librespot events (via the events.py webservice): drive session and
  playback state — see "Event Handling" below

**State Changes:**
- Session connected → record username, arm stall watchdog, re-check provider match
- Session disconnected → clear active player and stop the consuming MA player
- Provider added/removed → re-check matches and rebuild AudioSource capabilities

## Event Handling

### Session Events

**`session_connected`**
- Triggered when Spotify app connects
- Payload includes `user_name`
- Actions:
  - Store username
  - Check for matching Spotify provider
  - Enable Web API control if match found

**`session_disconnected`**
- Triggered when Spotify app disconnects
- Actions:
  - Clear username
  - Disable Web API control
  - Clear provider reference

### Playback Events

**`sink` / `playing`**
- Indicates playback is starting
- Actions:
  - Check for provider match (if not already matched)
  - Select this source on the player
  - Mark source as in use

### Metadata Events

**`common_metadata_fields`**
- Provides track information
- Updates:
  - URI (spotify:track:...)
  - Title
  - Artist
  - Album
  - Album artwork URL
- Triggers player update to refresh UI

**`volume_changed`**
- Spotify app changed volume
- Converts from Spotify scale (0-65535) to percentage (0-100)
- Applies to linked Music Assistant player

## Configuration

### Provider Settings

**`mass_player_id`** (required)
- Music Assistant player to link with this Spotify Connect device
- Only one Connect provider per player

**`publish_name`** (optional)
- Name displayed in Spotify app
- Default: "Music Assistant"
- Helps identify device when multiple instances exist


### Cache Directory
- Location: `{data_path}/spotify_connect/{instance_id}/`
- Contains:
  - `credentials.json`: Cached Spotify credentials
  - `audio-cache/`: Temporary audio files
  - Logs from librespot

## Error Handling

### librespot Process
- Process crashes: Automatically cleaned up
- Authentication failures: Logged as warnings
- Network issues: librespot handles reconnection

### Web API Commands
- All commands wrapped in try/except
- Failures logged as warnings
- Raises exception to notify player controller

### Volume Control
- Unsupported on player: Logged at debug level
- Invalid volume values: Clamped to 0-100 range

## Code Organization

### Main Class: `SpotifyConnectProvider`
Inherits from `PluginProvider`. File layout follows the project convention of
"public methods at the top, private helpers below."

**Public API (called by Music Assistant core):**
- `handle_async_init()` / `unload()` — provider lifecycle
- `get_audio_sources()` — exposes the single AudioSource MediaItem
- `get_stream_details()` — returns `StreamType.NAMED_PIPE` streamdetails with
  `expiration=0`. Side-effect-free; exclusivity is claimed in
  `on_source_selected`. Raises `AudioError` when MA cannot acquire the source
  (idle librespot + no Web API provider, or MA is not the active Spotify
  device).
- `on_source_selected()` — claims `_in_use_by_queue` + `_active_session_id`,
  stops a previously active MA player on cross-queue handoff, and (for
  MA-initiated resume during an existing Spotify session) kicks the Web API
  to start playback then waits for librespot's `playing` event.
- `on_source_unselected()` — releases the claim, but only if `stream_session_id`
  still matches the active session (rejects stale callbacks from superseded
  same-queue requests).
- `on_source_control()` / `on_volume_change()` — proxy transport and volume to
  the Spotify Web API. Both gated on `_spotify_session_active`; bail out with
  `AudioError(NOT_ACTIVE_DEVICE_MESSAGE)` when MA isn't the active device.

**Private helpers:**
- `_build_audio_source()` / `_update_source_capabilities()` — construct and
  refresh the AudioSource, propagating capability flags onto the live queue
  item so the UI updates without waiting for the next play_media.
- `_get_target_player_id()` — apply the auto/configured-player selection rules.
- Watchdog + deferred fire: `_arm_session_watchdog`, `_cancel_session_watchdog`,
  `_session_watchdog_body`, `_emergency_drain_fifo`,
  `_deferred_play_media_fire`, `_cancel_pending_play_media`.
- `_clear_active_player()` / `_save_last_player_id()` — state reset and config
  persistence.
- `_check_spotify_provider_match()` — match the Connect username to a
  configured Spotify music provider; toggles Web API availability.
- `_on_play/pause/next/previous/seek/volume()` — Web API call implementations.
- `_get_spotify_device_id()`, `_wait_for_librespot_playing()`,
  `_ensure_active_device()` — Web API plumbing.
- `_on_provider_event()` — PROVIDERS_UPDATED subscriber.
- `_process_librespot_stderr_line()`, `_librespot_runner()`,
  `_setup_player_daemon()` — librespot subprocess lifecycle. Daemon readiness
  is signalled by the codec/backend-independent "Connecting to AP" sentinel
  in librespot's stderr.
- `_handle_custom_webservice()` — single dispatcher for all events posted by
  events.py (session connect/disconnect, sink/playing/paused, metadata,
  volume_changed).

## Dependencies

### External Binaries
- **librespot**: Spotify Connect client implementation
- **ffmpeg**: Audio format conversion

### Python Packages
- **aiohttp**: Async HTTP for webservice
- **music_assistant_models**: Data models and enums

### Music Assistant Integration
- Player controller for command routing
- Provider framework for lifecycle management
- Event system for state synchronization

## Testing

### Basic Functionality
1. Configure Spotify Connect provider with a Music Assistant player
2. Open Spotify app and select the device
3. Verify audio plays through the player
4. Check metadata displays correctly

### Web API Control
1. Configure both Spotify Connect and Spotify music providers
2. Use the same Spotify account for both
3. Start playback from Spotify app
4. Look for "Found matching Spotify music provider" in logs
5. Verify control buttons are enabled in Music Assistant UI
6. Test play/pause/next/previous/seek from Music Assistant

### Multi-Instance
1. Create multiple Spotify Connect providers
2. Link each to different players
3. Verify each appears as separate device in Spotify app
4. Test simultaneous playback on different devices

## Future Enhancements

### Potential Improvements
1. **Queue Sync**: Sync Spotify queue with Music Assistant queue
2. **Crossfade Support**: Enable crossfade if supported by player
3. **Audio Quality**: Make bitrate configurable
4. **Multi-Account**: Support multiple Spotify accounts per device
5. **Enhanced Metadata**: Chapter markers, lyrics integration
6. **Gapless Playback**: Improve transitions between tracks

### Known Limitations
1. Cannot control playback without matching Spotify provider
2. No access to user's Spotify playlists/library (use Spotify provider)
3. Volume control only works if player supports it
4. Seek requires Web API (not available in passive mode)
5. No native gapless playback support
6. **MA-initiated cold start is intentionally disabled** (`can_initiate=False`)
   because Spotify will not start playback when there is no current context;
   entry must come from the Spotify app.
7. Pausing from MA closes librespot's pipe sink so the player state transitions
   to IDLE rather than PAUSED. The queue's auto-clear-on-end is guarded for
   `MediaType.AUDIO_SOURCE` items so the queue item survives a pause-as-stop
   and resume still works.
8. Codec badge in the frontend shows PCM (the format on the FIFO), not Ogg
   Vorbis (Spotify's actual stream codec). A `--passthrough` experiment to fix
   this introduced multi-second track-change latency from chained-Ogg
   buffering and was reverted; a future migration to librespot-go (with its
   WebSocket API) or a display-only `source_audio_format` field on
   `StreamDetails` are the parked options.

## Related Documentation

- **PluginProvider Contract**: See `music_assistant/models/plugin.py`
- **AudioSource MediaItem**: See `music_assistant_models.media_items.AudioSource`
- **Player Controller**: See `music_assistant/controllers/players/`
- **Spotify Provider**: See `music_assistant/providers/spotify/`
- **librespot**: https://github.com/librespot-org/librespot

---

*This architecture document is maintained alongside the code and should be updated when significant changes are made to the provider's design or functionality.*
