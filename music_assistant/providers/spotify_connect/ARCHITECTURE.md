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
│  │  - Seek/position events       │  │  - Track metadata
│  │  - Volume changes             │  │
│  └───────────────────────────────┘  │
│  ┌───────────────────────────────┐  │
│  │  PluginSource                 │  │  Provides:
│  │  - Dynamic capabilities       │  │  - Playback control
│  │  - Callback routing           │  │  - Metadata display
│  │  - Web API integration        │  │  - Source selection
│  │  - Frontend queue registration│  │  - Seek bar / signal chain
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
  - Audio streaming and decoding to PCM S16LE at 44100Hz
  - Session management (connect/disconnect)
- Outputs raw PCM audio to stdout via a named pipe
- Sends events to the custom webservice via HTTP

#### 2. **events.py Webservice**
- Python script that receives event callbacks from librespot
- Runs on a custom port for each provider instance
- Provides an HTTP endpoint that librespot calls with:
  - Session connected/disconnected events
  - Track metadata (title, artist, album, artwork, duration)
  - Playback state changes (playing, paused, stopped)
  - Seek and position events with `position_ms`
  - Volume changes from Spotify app

#### 3. **PluginSource Model**
The provider creates a `PluginSource` that represents the Spotify Connect audio source:

**Static Properties:**
- `id`: Provider instance ID
- `name`: Display name (e.g., "Music Assistant")
- `passive`: False (active audio source)

**Dynamic Capabilities:**
- `can_play_pause`: Enabled when Web API control available
- `can_seek`: Enabled when Web API control available
- `can_next_previous`: Enabled when Web API control available

**Metadata:**
- Updated in real-time from librespot events
- Includes URI, title, artist, album, artwork URL, elapsed time

#### 4. **Audio Pipeline**

```
librespot → named pipe (PCM S16LE 44100Hz) → MA get_audio_stream() → player
```

The provider streams audio directly from librespot's stdout via a named pipe. This is a deliberate design choice — it bypasses MA's ffmpeg pipeline entirely, which means:

- **Lower latency**: No transcoding stage between librespot and the player
- **No resampling by MA**: Audio is delivered at librespot's native 44.1kHz/16bit. Downstream resampling (e.g. by PulseAudio) happens outside MA's pipeline and is not reflected in the signal chain display — this is correct behavior since MA only reports what it knows
- **Simpler pipeline**: Fewer failure points between Spotify and the player

The tradeoff is that MA cannot apply DSP processing (EQ, volume normalization, output limiting) to the Spotify Connect stream, as these are handled by MA's ffmpeg pipeline which is not in the audio path here.

## MA Frontend Integration

### The Frontend Queue Problem

MA's frontend resolves the active player's seek bar, progress tracking, and signal chain display through a `PlayerQueue` object. It looks up the queue via `player.active_source` — when a plugin source is active, `active_source` equals the plugin's `instance_id`. If no queue exists with that ID, the frontend has nothing to render.

The obvious solution — registering a real `PlayerQueue` in `player_queues._queues` — causes a serious side effect: the backend then routes all play/pause/play_media commands to that queue, which has no items and no stream, breaking playback control entirely.

### Frontend-Only Queue Registration

The solution is to fire a `QUEUE_ADDED` event with `queue_id=instance_id` that the frontend stores in its queue map, but deliberately never write to `player_queues._queues` on the backend. The frontend gets exactly what it needs to render the seek bar and signal chain, while the backend remains unaware of the queue and continues routing commands correctly through the plugin source callbacks.

The `_register_plugin_queue()` method handles this registration. It is called:
- When a player selects this source (`_on_source_selected`)
- After each `track_changed` event delivers metadata (0.5s delayed to ensure frontend reactivity)

The fake queue payload includes:
- `current_item` with `duration` for seek bar rendering
- `current_item.streamdetails.audio_format` (PCM S16LE 44100Hz/16bit) for signal chain display
- `current_item.streamdetails.dsp` from `get_stream_dsp_details()` for downstream player format display
- `elapsed_time` and `elapsed_time_last_updated` for progress tracking

### Seek Bar

Position tracking uses `elapsed_time` and `elapsed_time_last_updated` on `StreamMetadata`. These are updated by:
- `playing` events (with `position_ms`)
- `seeked` events (with `position_ms`)
- `paused` events (with `position_ms`)

On `seeked`, `QUEUE_TIME_UPDATED` is fired with `force_update=True` to bypass MA's change-detection guard (which filters out `elapsed_time`-only changes) and move the frontend seek bar to the seeked position.

### Signal Chain Display

The signal chain info button requires `output_format` in `player.extra_data`. Since Spotify Connect bypasses the streams controller (which normally sets this), the provider sets it manually on source select with the correct librespot output format (PCM S16LE 44100Hz/16bit). It is cleared when the player is released. `get_stream_dsp_details()` reads this value when building the DSP details included in the fake queue registration.

### play_media Reroute

When a user plays local MA content to a player while Spotify Connect is active, the frontend sends `play_media` with `queue_id=instance_id` (the fake queue ID). Since this ID is not in `_queues`, a `PlayerUnavailableError` would normally result.

The `play_media` method in `player_queues.py` now detects this case: if `queue_id` is not found in `_queues`, it checks whether `queue_id` matches an active plugin source via `player.state.active_source` (the computed `__final_active_source`, not the raw `player.active_source` which returns the hardware-reported value). If matched, it reroutes to the real player queue, triggering normal source takeover — librespot receives a broken pipe, Spotify pauses gracefully, and MA takes over.

Note: this reroute is general and benefits any plugin source provider, not just `spotify_connect`.

## Pause Behavior

The player stays assigned to Spotify Connect through pause. `in_use_by` is not cleared on `paused` events. This means:
- The MA UI play button resumes Spotify via the `on_play` Web API callback
- Playing local MA content while paused correctly triggers source takeover via the `play_media` reroute
- The player is only released to MA when MA explicitly selects a different source, or when the Spotify session disconnects

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

#### Callback Architecture

**PluginSource Callbacks** (defined in `models/plugin.py`):
```python
on_play: Callable[[], Awaitable[None]] | None
on_pause: Callable[[], Awaitable[None]] | None
on_next: Callable[[], Awaitable[None]] | None
on_previous: Callable[[], Awaitable[None]] | None
on_seek: Callable[[int], Awaitable[None]] | None
```

**Flow:**
1. User presses play/pause in Music Assistant UI
2. Player controller checks if active source has callbacks
3. If callbacks present, invoke them instead of player methods
4. Callbacks forward commands to Spotify Web API
5. Spotify app receives command and updates state

#### Implementation Details

**Provider Methods:**
- `_check_spotify_provider_match()`: Finds matching Spotify provider
- `_update_source_capabilities()`: Enables/disables capabilities and registers callbacks
- `_on_play/pause/next/previous/seek()`: Callback implementations

**Capability Flags:**
```python
# When Web API available:
source.can_play_pause = True
source.can_seek = True
source.can_next_previous = True

# Callbacks registered:
source.on_play = self._on_play
source.on_pause = self._on_pause
# ... etc
```

**Web API Commands:**
- `PUT /me/player/play` - Resume playback
- `PUT /me/player/pause` - Pause playback
- `POST /me/player/next` - Skip to next track (returns 204 No Content)
- `POST /me/player/previous` - Skip to previous track (returns 204 No Content)
- `PUT /me/player/seek?position_ms={ms}` - Seek to position

### Event-Driven Updates

The provider subscribes to events to maintain accurate state:

**Events Monitored:**
- `EventType.PROVIDERS_UPDATED`: Check for new Spotify provider
- Custom session events: Update username and check for matches
- Playback events (`sink`, `playing`): Trigger provider matching

**State Changes:**
- Session connected → Check for provider match
- Session disconnected → Disable Web API control
- Provider added/removed → Re-check matches

### Deepcopy Handling

The `PluginSource` contains unpicklable callbacks (functions, futures). To support player state serialization:

**Problem**: Default `deepcopy` fails on callbacks
**Solution**: `as_player_source()` method returns base `PlayerSource` without callbacks

```python
def as_player_source(self) -> PlayerSource:
    """Return as basic PlayerSource without callbacks."""
    return PlayerSource(
        id=self.id,
        name=self.name,
        passive=self.passive,
        can_play_pause=self.can_play_pause,
        can_seek=self.can_seek,
        can_next_previous=self.can_next_previous,
    )
```

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
  - Release player back to MA

### Playback Events

**`sink` / `playing`**
- Indicates playback is starting or resuming
- Payload includes `position_ms`
- Actions:
  - Check for provider match (if not already matched)
  - Select this source on the player
  - Mark source as in use
  - Update `metadata.elapsed_time` from `position_ms`

**`paused`**
- Indicates playback is paused
- Payload includes `position_ms`
- Actions:
  - Update `metadata.elapsed_time` from `position_ms`
  - Freeze `elapsed_time_last_updated` (stops auto-advance)
  - Player stays assigned to Spotify Connect (`in_use_by` not cleared)

**`seeked`**
- Indicates user seeked to a new position
- Payload includes `position_ms`
- Actions:
  - Update `metadata.elapsed_time` from `position_ms`
  - Fire `QUEUE_TIME_UPDATED` with `force_update=True` to move seek bar

### Metadata Events

**`track_changed` / `common_metadata_fields`**
- Provides track information
- Updates:
  - URI (spotify:track:...)
  - Title, artist, album
  - Album artwork URL
  - Duration
- Re-registers frontend queue (0.5s delayed) to update seek bar duration and quality indicator

**`volume_changed`**
- Spotify app changed volume
- Converts from Spotify scale (0-65535) to percentage (0-100)
- Applies to linked Music Assistant player
- Ignores initial event fired immediately after session connect

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
- Broken pipe on source takeover: Expected behavior — MA grabs the named pipe, librespot loses its write end and pauses gracefully

### Web API Commands
- All commands wrapped in try/except
- Failures logged as warnings
- Raises exception to notify player controller
- next/previous use `want_result=False` since Spotify returns 204 No Content

### Volume Control
- Unsupported on player: Logged at debug level
- Invalid volume values: Clamped to 0-100 range

## Code Organization

### Main Class: `SpotifyConnectProvider`
Inherits from `PluginProvider`

**Key Methods:**
- `handle_async_init()`: Setup provider, start webservice, load credentials
- `unload()`: Cleanup, stop processes
- `get_audio_stream()`: Provide audio to player via named pipe
- `get_source()`: Return PluginSource details
- `_register_plugin_queue()`: Register frontend-only queue for seek bar and signal chain display
- `_clear_active_player()`: Release player back to MA, clear output_format

**Event Handlers:**
- `_handle_session_connected()`: Process session connect
- `_handle_session_disconnected()`: Process session disconnect
- `_handle_playback_started()`: Initialize playback
- `_handle_metadata_update()`: Update track info
- `_handle_volume_changed()`: Sync volume
- `_handle_custom_webservice()`: Main event dispatcher

**Playback Control:**
- `_check_spotify_provider_match()`: Find matching provider
- `_update_source_capabilities()`: Toggle control features
- `_on_play/pause/next/previous/seek()`: Control callbacks

**Utilities:**
- `_load_cached_username()`: Read credentials file
- `_get_active_plugin_source()`: Find active source by `in_use_by`

## Dependencies

### External Binaries
- **librespot**: Spotify Connect client implementation
- **ffmpeg**: Not used in the Spotify Connect audio path

### Python Packages
- **aiohttp**: Async HTTP for webservice
- **music_assistant_models**: Data models and enums

### Music Assistant Integration
- Player controller for command routing
- Provider framework for lifecycle management
- Event system for state synchronization
- `player_queues.play_media` reroute for source takeover

## Testing

### Basic Functionality
1. Configure Spotify Connect provider with a Music Assistant player
2. Open Spotify app and select the device
3. Verify audio plays through the player
4. Check metadata displays correctly
5. Verify seek bar moves during playback
6. Verify quality indicator (LQ) appears on player card immediately

### Web API Control
1. Configure both Spotify Connect and Spotify music providers
2. Use the same Spotify account for both
3. Start playback from Spotify app
4. Look for "Found matching Spotify music provider" in logs
5. Verify control buttons are enabled in Music Assistant UI
6. Test play/pause/next/previous/seek from Music Assistant
7. Seek from Spotify app and verify bar moves in MA UI

### Pause and Resume
1. Pause from MA UI — verify bar freezes at correct position
2. Resume from MA UI — verify playback resumes at correct position
3. Pause from Spotify app — verify MA UI shows paused state

### Source Takeover
1. While Spotify is playing, Play Now a local album to the same player
2. Verify Spotify pauses and MA queue starts playing
3. While Spotify is paused, Play Now a local album to the same player
4. Verify MA queue starts playing correctly

### Signal Chain
1. Start Spotify Connect playback
2. Click the info button on the player card
3. Verify signal chain shows 44.1kHz/16bit input and correct downstream formats

### Multi-Instance
1. Create multiple Spotify Connect providers
2. Link each to different players
3. Verify each appears as separate device in Spotify app
4. Test simultaneous playback on different devices

## Known Limitations

1. Cannot control playback without matching Spotify music provider
2. No access to user's Spotify playlists/library (use Spotify provider)
3. Volume control only works if player supports it
4. MA DSP processing (EQ, normalization, output limiting) is not applied to the Spotify Connect stream since it bypasses the ffmpeg pipeline
5. No native gapless playback support
6. Generic plugin icon shown in signal chain input (not Spotify logo)
7. Quality indicator shows LQ — correct since source is 320kbps OGG regardless of PCM delivery format

## Future Enhancements

1. **Queue Sync**: Sync Spotify queue with Music Assistant queue
2. **Crossfade Support**: Enable crossfade if supported by player
3. **Audio Quality**: Make bitrate configurable
4. **Multi-Account**: Support multiple Spotify accounts per device
5. **Enhanced Metadata**: Chapter markers, lyrics integration
6. **Gapless Playback**: Improve transitions between tracks

## Related Documentation

- **PluginSource Model**: See `music_assistant/models/plugin.py`
- **Player Controller**: See `music_assistant/controllers/players/`
- **Spotify Provider**: See `music_assistant/providers/spotify/`
- **librespot**: https://github.com/librespot-org/librespot

---

*This architecture document is maintained alongside the code and should be updated when significant changes are made to the provider's design or functionality.*
