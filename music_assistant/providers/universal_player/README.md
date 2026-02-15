# Universal Player Provider

## Overview

The Universal Player provider creates virtual players that merge multiple protocol players (AirPlay, Chromecast, DLNA, Squeezelite, SendSpin) for the same physical device into a single unified player.

## When is a Universal Player Created?

A Universal Player is automatically created by the PlayerController when:

1. **Multiple protocol players are detected for the same device** - Based on MAC address or IP matching
2. **No native player provider exists** - e.g., a Denon AVR with Chromecast, AirPlay, and DLNA but no native Denon integration

## Example Scenario

Consider a Denon AVR receiver that supports:
- Chromecast built-in
- AirPlay 2
- DLNA

Without a native Denon provider in Music Assistant, the system would normally show three separate players:
- "Living Room (Chromecast)"
- "Living Room (AirPlay)"
- "Living Room (DLNA)"

With the Universal Player provider, these are merged into a single:
- "Living Room" (Universal Player)
  - Output protocols: Chromecast, AirPlay, DLNA

## How It Works

### Device Matching

Protocol players are matched to the same device using:
1. **MAC address** - Most reliable, extracted from device info
2. **IP address** - Fallback when MAC is not available

### Player Creation Flow

```
1. Chromecast player registers → No native parent, no other protocols → Stays as regular player
2. AirPlay player registers → Matches Chromecast by MAC → PlayerController creates UniversalPlayer
3. DLNA player registers → Matches existing UniversalPlayer → Added as linked protocol
```

### Feature Aggregation

The Universal Player aggregates features from all linked protocols:
- Volume control from the protocol that supports it best
- Power control from any protocol that supports it
- Pause/Play from active protocol

### Playback Routing

The Universal Player does NOT have `PLAY_MEDIA` capability. Instead:
1. User selects "Living Room" and starts playback
2. PlayerController uses `_select_best_output_protocol()` to choose best protocol
3. Playback is routed to the selected protocol player (e.g., Chromecast)
4. User can switch to different protocol in player settings

## Configuration

Universal Players are auto-created and require no user configuration. However, users can:
- Rename the player
- Choose preferred output protocol
- Disable/enable the player

## Cleanup

When all protocol players for a device are removed (e.g., provider unloaded), the Universal Player is automatically cleaned up.

If a native provider is later installed (e.g., Denon integration), the Universal Player is replaced by the native player, with all protocols linked to it instead.

## Technical Details

### Player ID Format

Universal players use the format: `up{device_key}`

Where `device_key` is typically the normalized MAC address.

### File Structure

```
universal_player/
├── __init__.py      # Provider setup
├── provider.py      # UniversalPlayerProvider class
├── player.py        # UniversalPlayer class
├── constants.py     # Constants (prefix, etc.)
├── manifest.json    # Provider manifest (builtin)
└── README.md        # This file
```

### Provider Features

The Universal Player provider has no special provider features - it doesn't support manual player creation via the UI. Players are only created automatically by the PlayerController.
