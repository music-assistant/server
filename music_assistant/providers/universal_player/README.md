# Universal Player Provider

## Overview

The Universal Player provider creates virtual players that merge multiple protocol players (AirPlay, Chromecast, DLNA, Squeezelite, SendSpin) for the same physical device into a single unified player.

## When is a Universal Player Created?

A Universal Player is automatically created by the PlayerController when:

1. **One or more protocol players are detected for the same device** - Matching prefers MAC/serial/UUID-style identifiers and only falls back to IP as a last resort
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
2. **Serial / UUID / protocol-specific IDs** - Used before any IP fallback
3. **IP address** - Last resort when strong identifiers are missing or unreliable

The controller will also try to validate or enrich reported MAC addresses with ARP before falling back to weaker matching.

### Player Creation Flow

```
1. Chromecast player registers → No native parent → delayed evaluation is scheduled
2. No native player appears → PlayerController creates a UniversalPlayer, even for this single unmatched protocol
3. AirPlay player registers → Matches existing UniversalPlayer by identifiers → gets linked to it
4. DLNA player registers → Matches existing UniversalPlayer → Added as linked protocol
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
- Remove the universal player to wipe its config and restart protocol discovery from scratch

## Cleanup

When a Universal Player is permanently removed, all protocol parent links are cleared so discovery can start over cleanly.

If a native provider is later installed (e.g., Denon integration), the Universal Player is replaced by the native player, with all protocols linked to it instead.

## Technical Details

### Player ID

Universal players use the format `up{random}`, minted once when the device is first
wrapped. The id carries no device information and is never recomputed.

This matters because the player id is the identity API consumers (e.g. the Home
Assistant integration) bind their entities to, so it has to stay stable for the
lifetime of the device. A universal player is therefore always resolved through the
`protocol_parent_id` that each of its protocol players persists, never by deriving an
id from the identifiers that happen to be available at that moment. Deriving the id
made it shift whenever a different set of protocol players was registered - from a
MAC-based to a UUID-based id, for example - which orphaned the consumer's entity.

As a consequence a universal player config is only ever deleted when the user removes
the player, or when a native player takes over the device. When the protocol players
of a universal player disappear it becomes unavailable but keeps its config, because
an opaque id cannot be recreated from the device.

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
