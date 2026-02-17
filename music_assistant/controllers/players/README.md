# Player Controller Architecture

This document provides an overview of the Music Assistant Player Controller architecture, including the Player/PlayerState model, multi-protocol player system, and universal player concept.

## Table of Contents

- [Overview](#overview)
- [Player vs PlayerState](#player-vs-playerstate)
- [Core Components](#core-components)
- [Player Types](#player-types)
- [Multi-Protocol Player System](#multi-protocol-player-system)
- [Universal Player](#universal-player)
- [Protocol Linking](#protocol-linking)
- [Development Guide](#development-guide)

## Overview

The Player Controller is a core controller that manages all connected audio players from various providers. It provides:
- Unified control interface for all players (play, pause, volume, etc.)
- Multi-protocol player linking (combining AirPlay, Chromecast, DLNA for the same device)
- Universal Player wrapping for devices without native vendor support
- Sync group management for synchronized playback
- Player state management and event broadcasting
- User access control and permissions

## Player vs PlayerState

The Player Controller distinguishes between two key concepts:

### Player (Internal Model)

The `Player` class is the actual object provided by a Player Provider. It:
- Incorporates the actual state of the player (volume, playback state, etc.)
- Contains methods for controlling the player (play, pause, volume, etc.)
- Is used internally by providers and the controller
- May contain provider-specific implementation details

### PlayerState (API Model)

The `PlayerState` is a dataclass representing the final state of the player. It:
- Includes any user customizations (custom name, hidden status, etc.)
- Applies transformations (e.g., fake power/volume controls)
- Is the object exposed to the outside world via the API
- Is a snapshot created when `player.update_state()` is called
- Contains only serializable data suitable for API consumers

```
┌─────────────────────────────────────────────────────────────────┐
│                     Player (Internal)                            │
│  - Provider-specific implementation                             │
│  - Control methods (play, pause, volume_set, etc.)              │
│  - Raw state (_attr_volume_level, _attr_playback_state, etc.)   │
│  - Device info and identifiers                                  │
└─────────────────────────────────┬───────────────────────────────┘
                                  │
                                  │ update_state()
                                  ▼
┌─────────────────────────────────────────────────────────────────┐
│                   PlayerState (API)                             │
│  - Final display name (with user customizations)                │
│  - Transformed state (fake controls applied)                    │
│  - Player controls configuration                                │
│  - Serializable for API/WebSocket                               │
└─────────────────────────────────────────────────────────────────┘
```

## Core Components

### 1. PlayerController ([controller.py](controller.py))

The main orchestrator that manages:
- Player registration and lifecycle
- Player commands (play, pause, stop, volume, etc.)
- Protocol linking and evaluation
- Universal player creation
- Sync group coordination

**Key responsibilities:**
- Routes commands to appropriate players or protocol players
- Manages player availability and state
- Handles announcements and TTS playback
- Coordinates sync groups and grouped playback

### 2. ProtocolLinkingMixin ([protocol_linking.py](protocol_linking.py))

Mixin class containing all protocol linking logic:
- Matching protocol players to native players via device identifiers
- Creating and managing Universal Players
- Protocol link lifecycle (add, remove, cleanup)
- Output protocol selection for playback

### 3. Helper Utilities ([helpers.py](helpers.py))

Contains standalone helper functions and decorators:
- `handle_player_command` decorator for command validation
- `AnnounceData` type definition

## Player Types

Players in Music Assistant have different types based on their capabilities:

### PlayerType.PLAYER

A regular player with native (vendor-specific) support. Examples:
- Sonos speakers via the Sonos provider
- Apple devices via the AirPlay provider (HomePod, Apple TV)
- Google devices via the Chromecast provider (Nest Audio, Google Home)

### PlayerType.PROTOCOL

A generic protocol player without native vendor support. These are streaming endpoints discovered via generic protocols but manufactured by third parties. Examples:
- Samsung TV discovered via AirPlay (not an Apple device)
- Sony speaker discovered via Chromecast (not a Google device)
- Any DLNA/UPnP device (always PROTOCOL type)

**Important:** Protocol players with `PlayerType.PROTOCOL` are hidden from the UI and wrapped in a Universal Player or attached to an existing native player.

### PlayerType.GROUP

A group player that represents (synchronized) playback across multiple physical speakers.

### PlayerType.STEREO_PAIR

A dedicated stereo pair of two speakers acting as one player.

## Multi-Protocol Player System

Modern audio devices often support multiple streaming protocols (AirPlay, Chromecast, DLNA). The Player Controller automatically detects and links these protocols to provide a unified experience.

### How It Works

```
┌─────────────────────────────────────────────────────────────────────┐
│                     Physical Device                                 │
│                  (e.g., Samsung Soundbar)                           │
├─────────────────────────────────────────────────────────────────────┤
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐               │
│  │   AirPlay    │  │  Chromecast  │  │    DLNA      │               │
│  │   Protocol   │  │   Protocol   │  │   Protocol   │               │
│  │   Player     │  │   Player     │  │   Player     │               │
│  │  (hidden)    │  │  (hidden)    │  │  (hidden)    │               │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘               │
│         │                 │                 │                       │
│         └─────────────────┼─────────────────┘                       │
│                           │                                         │
│                           ▼                                         │
│              ┌─────────────────────────┐                            │
│              │    Universal Player     │                            │
│              │  (visible in UI)        │                            │
│              │  - Aggregates protocols │                            │
│              │  - Selects best output  │                            │
│              │  - Unified control      │                            │
│              └─────────────────────────┘                            │
└─────────────────────────────────────────────────────────────────────┘
```

### Device Identifier Matching

Protocol players are matched to the same physical device using identifiers in order of reliability:

1. **MAC_ADDRESS** - Most reliable, unique to the network interface
2. **SERIAL_NUMBER** - Unique device serial number
3. **UUID** - Universally unique identifier
4. **player_id** - Fallback for players without identifiers (e.g., Sendspin)

**Note:** IP_ADDRESS is intentionally NOT used for matching as it can change with DHCP and cause incorrect matches between different devices.

**Fallback behavior:** Protocol players that don't expose any identifiers (like Sendspin clients) will still get wrapped in a Universal Player using their player_id as the device key. This ensures all protocol players get a consistent user-facing interface.

### Output Protocol Selection

When playing media, the controller selects the best output protocol:

1. **Grouped protocol** - If a protocol is actively grouped/synced, use it
2. **User preference** - Honor user's configured preferred protocol
3. **Native playback** - Use native PLAY_MEDIA if available
4. **Best available** - Select by protocol priority (AirPlay > Chromecast > DLNA)

## Universal Player

The Universal Player is a virtual player that wraps one or more protocol players when no native vendor support exists.

### When Created

A Universal Player is created when:
1. A device is discovered via a protocol but has no native provider
2. The device's protocol player has `PlayerType.PROTOCOL`
3. There is no existing native player that matches the device identifiers

### Features

- **Aggregates Features** - Combines capabilities from all linked protocols
- **No PLAY_MEDIA** - Delegates playback to protocol players
- **Unified Control** - Single point of control for volume, power, etc.
- **Protocol Selection** - Automatically selects best protocol for playback

### Lifecycle

```
1. Protocol player registered with PlayerType.PROTOCOL
2. Controller checks for cached parent_id from previous session:
   - If found, restores link immediately (skips evaluation)
   - If parent not yet registered, waits without creating universal player
3. If no cached parent, checks for matching native player (links immediately if found)
4. If no native player, schedules delayed evaluation:
   - 10 seconds standard delay (allows other protocols to register)
   - 30 seconds if previously linked to a native player (allows native provider to start)
5. After delay, finds all matching protocol players by identifiers
6. Creates UniversalPlayer and links all protocols
7. Protocol players become hidden, Universal Player visible
```

## Protocol Linking

### Native Player Linking

When a native player (e.g., Sonos) is registered, the controller:
1. Searches for protocol players with matching identifiers
2. Links matching protocols to the native player
3. Protocol players become hidden, native player gains `output_protocols`

### Protocol to Universal

When protocol players are registered without a native match:
1. Each protocol player schedules a delayed evaluation
2. After the delay, matching protocols are grouped
3. A Universal Player is created to wrap them all
4. All protocol players link to the Universal Player

### Universal to Native Promotion

When a native player appears for a device that has a Universal Player:
1. Native player is registered
2. Controller finds matching Universal Player
3. All protocol links transfer to the native player
4. Universal Player is removed
5. Native player becomes the visible entity

## Development Guide

### Adding Protocol Support

When implementing a new protocol provider:

1. Set `_attr_type = PlayerType.PROTOCOL` for generic devices (non-vendor devices)
2. Set `_attr_type = PlayerType.PLAYER` for devices with native support (vendor's own devices)
3. Populate `device_info.identifiers` with MAC, UUID, etc. (see below)
4. Filter out devices that should only be handled by native providers (e.g., passive satellites)
5. The Player Controller handles linking automatically

### Adding Native Provider Support

When implementing a native provider (e.g., Sonos, Bluesound) that should link to protocol players:

1. Set `_attr_type = PlayerType.PLAYER` (or the property 'type') for all devices
2. **Populate device identifiers** - This is critical for protocol linking:
   ```python
   self._attr_device_info = DeviceInfo(
       model="Device Model",
       manufacturer="Manufacturer Name",
   )
   # Add identifiers in order of preference (MAC is most reliable)
   self._attr_device_info.add_identifier(IdentifierType.MAC_ADDRESS, "AA:BB:CC:DD:EE:FF")
   self._attr_device_info.add_identifier(IdentifierType.UUID, "device-uuid-here")
   ```
3. The controller will automatically:
   - Find protocol players (AirPlay, Chromecast, DLNA) with matching identifiers
   - Link them to your native player as `output_protocols`
   - Replace any existing Universal Player for that device

**Identifier Priority:**
- `MAC_ADDRESS` - Most reliable, unique to network interface
- `SERIAL_NUMBER` - Unique device serial number
- `UUID` - Universally unique identifier
- `player_id` - Fallback when no identifiers available

**Note:** `IP_ADDRESS` is NOT used for matching as it can change with DHCP.

### Testing Protocol Linking

Key scenarios to test:

1. **Single protocol device** - Should create Universal Player
2. **Multi-protocol device** - All protocols linked to one Universal Player
3. **Late protocol discovery** - New protocol added to existing Universal Player
4. **Native player appears** - Universal Player replaced by native
5. **Protocol disappears** - Handle graceful degradation

### Configuration Storage

Protocol links are persisted in player configuration:
- `linked_protocol_player_ids` - List of protocol player IDs
- Restored on restart for fast reconnection

### Key Methods (in protocol_linking.py)

- `_evaluate_protocol_links()` - Entry point for link evaluation
- `_try_link_protocol_to_native()` - Link protocol to existing native
- `_schedule_protocol_evaluation()` - Delay evaluation for batching
- `_create_or_update_universal_player()` - Create/update Universal Player
- `_check_replace_universal_player()` - Replace Universal with native
- `_select_best_output_protocol()` - Choose protocol for playback
