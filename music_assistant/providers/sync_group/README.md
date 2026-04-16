# Sync Group Player Provider

## Overview

The Sync Group Player provider enables creating persistent groups of compatible speakers that play audio in perfect synchronization. Unlike temporary sync operations (manually syncing players together), sync groups are permanent player entities with their own queue and configuration.

### Key Features

- **Persistent Groups**: Created groups persist across restarts and appear as regular players
- **Protocol Compatibility**: Automatically enforces that only compatible players (same sync protocol) can be grouped
- **Dynamic Membership**: Optional support for adding/removing members during playback
- **Sync Leader Selection**: Automatically selects and manages the sync leader
- **Queue Ownership**: The sync group owns the playback queue, not individual members

## How It Differs from Manual Sync

| Manual Sync | Sync Group |
|-------------|------------|
| Temporary, dissolves when stopped | Permanent player entity |
| Queue belongs to leader player | Queue belongs to the group |
| Leader is explicitly chosen | Leader is auto-selected |
| Direct player-to-player sync | Abstracted group layer |

## Architecture

### Component Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                    SyncGroupProvider                             │
│  - Discovers/registers sync group players from config           │
│  - Creates/removes sync groups via UI                           │
└─────────────────────────────────────────────────────────────────┘
                              │
              ┌───────────────┴───────────────┐
              │                               │
    ┌─────────▼─────────┐          ┌─────────▼─────────┐
    │  SyncGroupPlayer  │          │  SyncGroupPlayer  │
    │  "Living Room"    │          │  "Whole House"    │
    │                   │          │                   │
    │  sync_leader ─────┼──┐       │  sync_leader ─────┼──┐
    │  group_members:   │  │       │  group_members:   │  │
    │  - AirPlay A      │  │       │  - Sonos 1        │  │
    │  - AirPlay B      │  │       │  - Sonos 2        │  │
    │  - AirPlay C      │  │       │  - Sonos 3        │  │
    └───────────────────┘  │       └───────────────────┘  │
                           │                              │
              ┌────────────▼──────────┐      ┌────────────▼──────────┐
              │   Actual Player A     │      │   Actual Sonos 1      │
              │   (sync leader)       │      │   (sync leader)       │
              │   ┌──synced to it──┐  │      │   ┌──synced to it──┐  │
              │   │ Player B       │  │      │   │ Sonos 2        │  │
              │   │ Player C       │  │      │   │ Sonos 3        │  │
              │   └────────────────┘  │      │   └────────────────┘  │
              └───────────────────────┘      └───────────────────────┘
```

### File Structure

```
sync_group/
├── __init__.py      # Provider setup and config entries
├── provider.py      # SyncGroupProvider - creates/removes groups
├── player.py        # SyncGroupPlayer - group player implementation
├── constants.py     # Constants and feature definitions
├── manifest.json    # Provider manifest (builtin, non-disableable)
└── README.md        # This file
```

## Sync Leader Concept

The sync group doesn't directly play audio. Instead, it delegates to a **sync leader** - one of the member players that actually handles the playback and syncs the other members to itself.

### Sync Leader Selection

The sync leader is automatically selected when playback starts:

1. **Check current leader**: If a leader exists and is available, keep it
2. **Prioritize static members**: For static groups, prefer members from the configured list
3. **First available**: Select the first available member as leader

### Leader Responsibilities

The sync leader:
- Receives the actual `play_media` command
- Syncs all other group members to itself
- Reports playback state (elapsed time, playback state) to the group
- Contributes features (enqueue, gapless, volume) to the group

## Group Types

### Static Groups

- **Fixed membership**: Members defined at creation, cannot be changed during playback
- **Use case**: Permanent whole-home audio setup
- **Behavior**: All static members rejoin automatically when playback starts

### Dynamic Groups

- **Flexible membership**: Members can be added/removed at any time
- **Use case**: Ad-hoc grouping based on current needs
- **Behavior**: Supports `SET_MEMBERS` feature for runtime changes
- **Configuration**: Enable "Dynamic members" option when creating the group

## Protocol Compatibility

Players can only be grouped if they support the same sync protocol. This is enforced through the `can_group_with` mechanism:

1. **First member added**: Its `can_group_with` set becomes the reference
2. **Subsequent members**: Must be in the reference set to be added
3. **Incompatible players**: Silently skipped during group formation

### Compatible Protocol Examples

- AirPlay players can group with other AirPlay players
- Sonos players can group with other Sonos players
- Squeezelite players can group with other Squeezelite players
- **Cross-protocol grouping is NOT supported**

## Protocol Linking Integration

The sync group leverages the Player Controller's protocol linking system through its elected sync leader. This is important for devices that support multiple streaming protocols.

### How It Works

When a sync group starts playback:

1. **Sync leader is elected** from the group members
2. **Play command forwarded** to the sync leader via `_handle_play_media()`
3. **Protocol selection happens** on the sync leader using `_select_best_output_protocol()`
4. **Best protocol chosen** based on:
   - Protocol already grouped/synced with other players (highest priority)
   - User's preferred output protocol setting
   - Native playback if available
   - Best available protocol by priority

### Example Scenario

Consider a sync group mixing a Universal Player (Denon AVR with multiple protocols) and native AirPlay devices:

```
┌─────────────────────────────────────────────────────────────────┐
│                    Sync Group: "Living Room"                     │
│                                                                  │
│  Members: Denon AVR, HomePod, Apple TV                          │
│  Sync Leader: Denon AVR (Universal Player)                       │
│  Compatible via: AirPlay protocol                                │
└─────────────────────────────────────────────────────────────────┘
                              │
                              │ play_media() forwarded to leader
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│              Denon AVR (Sync Leader - Universal Player)          │
│                                                                  │
│  Linked Output Protocols:                                        │
│  - AirPlay  ◄── selected (members are AirPlay-compatible)        │
│  - Chromecast                                                    │
│  - DLNA                                                          │
│                                                                  │
│  → _select_best_output_protocol() chooses AirPlay                │
│  → Syncs HomePod and Apple TV via AirPlay protocol               │
└─────────────────────────────────────────────────────────────────┘
```

In this scenario, the Denon AVR has three output protocols available. Since the other sync group members (HomePod, Apple TV) are AirPlay devices, the protocol selection logic picks AirPlay as the output protocol. All three devices then sync together via AirPlay.

### Why This Matters

- **Unified experience**: Users interact with one sync group player
- **Automatic optimization**: The leader picks the best protocol for its device type
- **Protocol-aware syncing**: Members sync using their native protocol (Sonos-to-Sonos, AirPlay-to-AirPlay)
- **Fallback support**: If native protocol unavailable, linked protocols provide alternatives

For detailed information on protocol linking, output protocol selection, and how devices with multiple protocols are handled, see the [Player Controller README](../../controllers/players/README.md#multi-protocol-player-system).

## Playback Flow

### Starting Playback

```
1. User starts playback on SyncGroupPlayer
   │
2. _form_syncgroup() called
   │
   ├─► Cancel any pending dissolve timer
   ├─► Ensure static members are included
   ├─► Select sync leader (if not already set)
   └─► Sync all members to the leader
   │
3. play_media() forwarded to sync leader
   │
4. Leader starts playback, synced members follow
```

### Stopping Playback

```
1. User stops playback on SyncGroupPlayer
   │
2. stop() forwarded to sync leader
   │
3. Schedule dissolve after 5 seconds
   │
4. _dissolve_syncgroup() called
   │
   ├─► Unsync all members from leader
   ├─► Clear sync_leader reference
   └─► Update group state
```

The 5-second delay before dissolving prevents unnecessary sync/unsync cycles during brief pauses or track changes.

## Dynamic Member Management

When `SET_MEMBERS` is called on a dynamic group:

### Adding Members

1. Validate member is available
2. Check compatibility with current sync leader
3. Add to internal member list
4. If playing, sync new member to leader

### Removing Members

1. Remove from internal member list
2. If removing the sync leader while playing:
   - Stop current playback
   - Dissolve sync group
   - Re-form with new leader
   - Resume playback (if was playing)

### Removing Last Member

If the last member is removed, the sync group becomes empty and cannot play until members are added.

## Feature Inheritance

The SyncGroupPlayer has limited base features but inherits additional capabilities from the sync leader:

### Base Features
- `PLAY_MEDIA` - Always supported

### Features from Sync Leader (when active)
- `ENQUEUE` - Queue next track
- `GAPLESS_PLAYBACK` - Seamless track transitions
- `VOLUME_SET` - Volume control
- `VOLUME_MUTE` - Mute control
- `MULTI_DEVICE_DSP` - DSP processing

### Dynamic Feature
- `SET_MEMBERS` - Only if group is configured as dynamic

## Configuration Options

### Group Members

Multi-select list of players to include in the group. Only non-group players are shown as options. For static groups, these are the permanent members. For dynamic groups, these are the initial members.

### Enable Dynamic Members

Boolean option to allow runtime member changes. When enabled:
- Group supports `SET_MEMBERS` feature
- Members can be added/removed via UI or API
- Group can start with zero members

## Provider Details

### Player ID Format

Sync group players use the format: `syncgroup_{random_8_chars}`

Example: `syncgroup_ab12cd34`

### Provider Features

- `CREATE_GROUP_PLAYER` - Create new sync groups
- `REMOVE_GROUP_PLAYER` - Delete sync groups

### Builtin Provider

The Sync Group provider is:
- **Builtin**: Automatically available, no installation needed
- **Single instance**: Only one provider instance exists
- **Non-disableable**: Cannot be disabled by users

## State Properties

The SyncGroupPlayer delegates most state to the sync leader:

| Property | Source |
|----------|--------|
| `playback_state` | Sync leader (or IDLE if no leader) |
| `elapsed_time` | Sync leader |
| `elapsed_time_last_updated` | Sync leader |
| `current_media` | Stored on group itself |
| `group_members` | Sync leader's reported members (preferred) or internal list |
| `can_group_with` | Computed from leader or first available member |

## Related Documentation

- [Player Controller README](../../controllers/players/README.md) - For understanding player management, protocol linking, and sync coordination
- [Universal Player README](../universal_player/README.md) - For understanding how protocol players are merged
