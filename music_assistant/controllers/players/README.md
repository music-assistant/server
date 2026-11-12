# Player Controller Architecture

This document provides an overview of the Music Assistant Player Controller architecture: the
Player and the protocols it is made up of, how a protocol finds its player, the stored config,
the lifecycle and the rules for commands and volume.

## Table of Contents

- [Overview](#overview)
- [Player, PlayerProtocol and PlayerState](#player-playerprotocol-and-playerstate)
- [Identity](#identity)
- [Legacy Ids](#legacy-ids)
- [Core Components](#core-components)
- [Locking](#locking)
- [Player Types and Protocol Roles](#player-types-and-protocol-roles)
- [Finding the Player of a Protocol](#finding-the-player-of-a-protocol)
- [Lifecycle](#lifecycle)
- [Commands, Outputs and Volume](#commands-outputs-and-volume)
- [Configuration Storage](#configuration-storage)
- [Development Guide](#development-guide)

## Overview

The Player Controller is a core controller that owns every player users and clients see. It provides:
- One player per device, made up of the protocols its providers attach (the vendor's own protocol, AirPlay, Chromecast, DLNA, Sendspin, ...)
- Unified control interface for all players (play, pause, volume, etc.)
- Players that stay known while their device is away, and come back with their settings
- Sync group management for synchronized playback
- Player state management and event broadcasting
- User access control and permissions

## Player, PlayerProtocol and PlayerState

```
┌─────────────────────────────────────────────────────────────────────┐
│                     Physical Device                                 │
│                  (e.g., Samsung Soundbar)                           │
├─────────────────────────────────────────────────────────────────────┤
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐               │
│  │    Native    │  │   AirPlay    │  │  Chromecast  │   provider    │
│  │   protocol   │  │   protocol   │  │   protocol   │   objects     │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘               │
│         │ attach          │ attach          │ attach                │
│         └─────────────────┼─────────────────┘                       │
│                           ▼                                         │
│              ┌─────────────────────────┐                            │
│              │         Player          │   owned by the controller  │
│              │  - its own config       │                            │
│              │  - control resolution   │                            │
│              │  - output selection     │                            │
│              └───────────┬─────────────┘                            │
│                          │ update_state()                           │
│                          ▼                                          │
│              ┌─────────────────────────┐                            │
│              │       PlayerState       │   what the API sends       │
│              └─────────────────────────┘                            │
└─────────────────────────────────────────────────────────────────────┘
```

### PlayerProtocol (provider model)

A `PlayerProtocol` ([player_protocol.py](../../models/player_protocol.py)) is the object a player
provider implements for one device: what the device reports (`_attr_*` values and the overridable
properties) and the commands to it. It reports its `role` (see below), whether it only exists while
connected (`session_only`) and, for a transport that rides on another protocol, the protocol it rides
on (`underlying_protocol_id`). While it is attached, `protocol.player` is its player; its
`update_state()` publishes through that player. Its config is its own record (`protocol.config`).

### Player (controller model)

A `Player` ([player.py](../../models/player.py)) is created and owned by the controller, from its
stored record or when a device is new, and is never subclassed by a provider. It holds:
- the attached protocols, each with the role it attached in; the one that stands for the device
  itself is its `own_protocol` (native, else control, else virtual), the audio ones are its
  `audio_protocols`, most preferred first;
- the ids of all protocols that belong to it, also those that are away;
- its own config (name, controls, volume limits, announcements, icon, ...);
- what it keeps for itself while it lives: the active output, the active MA source, the sleep
  timer, the stop flag, the external-pause bookkeeping, the palette, `extra_data`;
- the resolution of its power, volume and mute control and of the output that renders.

What a player reports for its device (name, device info, extra attributes, provider) comes from its
`device_protocol`: its own protocol, else its most preferred audio protocol, else what was stored
when a protocol was last attached.

The remembered MA source is cleared after five seconds of continuous idle.
Returning to playing or paused before that timeout cancels the reset, preserving
the selected queue through transient device stops during playback transitions.

### PlayerState (API model)

The `PlayerState` is a dataclass representing the final state of the player. It:
- Includes any user customizations (custom name, hidden status, etc.)
- Applies transformations (e.g., fake power/volume controls)
- Is the object exposed to the outside world via the API
- Is a snapshot created when `player.update_state()` is called
- Is derived by `derive_player_state` ([player_state.py](../../models/player_state.py)), a function of a snapshot of everything the state depends on
- Lists the protocols of the player (`protocols`: id, domain, provider, role, available, active),
  when the player was last seen and since when it is unavailable
- Contains only serializable data suitable for API consumers

A player is available while any of its protocols is (or while it is switched off at its external
power control); it is available for playback while a protocol is reachable and set up.

## Identity

A player id and a protocol id are different things. A device or group first seen by this version
gets a player id of its own (`pl` and twelve hex digits); a record that existed before the player
configs were split keeps its id, which is then also the id of its own protocol. A protocol that only
exists while connected (a browser or app player) gives the player it creates its own protocol id, so
that player has the same id in every session. Nothing else may assume the two ids are equal.

Protocols name each other by protocol id in what they report (`group_members`, `synced_to`,
`can_group_with`); the state derivation translates those to player ids. A group protocol names its
members by player id. `get_player_by_protocol` resolves either kind; `get_protocol` only knows
protocol ids and never goes through the legacy-id resolver.

### One device, two players

Which player a protocol belongs to is decided when it attaches, and that can turn out wrong: a
device ends up as two players when its second protocol was first seen while the first was away, or
before the identifiers that tie them together were known, and a protocol can end up on the player
of another device. So two players can be merged into one, and a protocol can be taken off its
player again.

**Merging.** The player that goes hands its protocols to the player that stays, which answers to
its id from then on: that id and the legacy ids of the player that goes become legacy ids of the
kept player. Group member lists and allow-lists, the player filters of users and the edges of the
protocols name the kept player. A name, a setting, DSP or queue settings only the player that goes
has are kept; where both have one, the kept player's stays. The queue of the player that goes is
dropped, and stopped first when it plays its own queue. A group it plays for keeps playing and
keeps it as a member, as the kept player. Whether its own audio goes on depends on its protocol,
which is detached to move: one that leaves its stream when it is detached, as AirPlay does, is
silent until it joins the playback again, at the latest when the group or the player is started
again. The attach rules decide whether two players can be one: not when
both have a protocol that stands for the device, or a protocol of the same domain, when a strong
identifier of one rules the other out, and never for a group or a player that only exists while
connected. What is stored changes in one step, right after that check, and is saved before
anything else of the player that goes is deleted, so after a crash either both players are as
they were or the merge is stored. The user filters are rewritten once the removal of the player
that goes reached the users limited to it; a crash in between leaves such a user limited to an id
that no player has anymore.

Two players also merge by themselves: when a protocol attaches, or is attached again, and the
identifiers of its player match another live player, while the attach rules allow it and the
younger of the two is unused. Unused means nothing about it is the user's own yet: no custom name,
no value the user set on it or on one of its protocols, no DSP switched on, no queue settings, no
queue items, nothing playing right now, no place in the member list of a group, no sync leader or members, no group that
holds it. Values the server or a provider stores by itself never count. The older player stays:
by the moment it was created, a player without one counting as the oldest, and of two equally old
ones the one with an id from before the split. Otherwise both stay, for the user to merge.

**Taking a protocol off its player.** A protocol that ended up on the wrong player gets a player of
its own, and the protocols that ride on it go along: a new player (a minted id, the name of the
protocol, default settings, an empty queue) that its stored edge names from then on. Its record
keeps the id of the player it was taken off (`unlinked_from`): the two players are never merged
by themselves again, also when either of them goes into another player later (the id resolves
like any legacy id); a merge of the two that the user asks for forgets it. The old player keeps
its id, name, settings and queue, and its output and control choices that named the protocol fall
back to their default: the identity stays with the protocols that stay, whichever role the one
taken off has, so without its own protocol the old player is a player of its audio protocols. A
protocol that rides on another, the protocol of a group, a protocol that only exists while
connected and the only protocol of a player are not taken off.

## Legacy Ids

A player keeps answering to its legacy ids: ids it was known under before, such as the id of a
player that was merged into it. They are stored with the player's config (`values.legacy_ids`)
and resolved in one place, `PlayerController.resolve_player_id`. They resolve in:

- the id-bearing arguments of an API command (`PLAYER_ID_ARGUMENTS`), which every API
  transport rewrites before the handler runs (`run_api_command` in
  [helpers/api.py](../../helpers/api.py));
- `get_player`, and the queue lookups (`get`, `queue_data`, `items`, `get_item` and the like);
- the `source` and `source_id` arguments of the player commands, which also take source ids
  that are no queue, so these commands resolve them before they compare them with the active
  source;
- `has_player_access`, when the caller passes the player it looked up.

A command called inside the server is not rewritten. The queue commands resolve the id they are
given themselves, so a plugin setting that still names a merged player reaches its queue; other
commands called inside the server take the id of the player itself. Protocol ids never resolve.

The state a player publishes lists its legacy ids (`legacy_ids`), so a client can tell which ids
it answers to.

The lookup is built from the stored player configs at startup, by a merge, which writes the
legacy ids, and again whenever a player config is deleted. An id resolves to the player that lists it only while no player is
registered under that id and no config is stored under it.

## Core Components

### 1. PlayerController ([controller.py](controller.py))

The main orchestrator. It keeps the players and, next to them, the protocols the providers attached
(also those that still wait for their player), and owns their lifecycle (`attach`, `detach`,
`remove_protocol`, `remove`, merging players and taking a protocol off its player), the player
commands, sync group coordination, availability and the state events.

### 2. ProtocolLinkingMixin ([protocol_linking.py](protocol_linking.py))

Finding the player a protocol belongs to, creating the player of a new device, holding a protocol
until it is known which player it belongs to, and the output protocol selection and grouping
translation for playback.

### 3. Identifier Matching ([matching.py](matching.py))

The pure function that decides whether a protocol stands for the device of a player, with every
veto and precedence rule in one table (see [Device Identifier Matching](#device-identifier-matching)).

### 4. Helper Utilities ([helpers.py](helpers.py))

The `handle_player_command` decorator for command validation, and `AnnounceData`.

## Locking

Player commands are serialized per player with `get_player_lock`. A command never runs without the lock: it waits for the command that holds it, up to 120 seconds per lock (a grouped player's group lock first, then its own), and then fails with a `ResourceBusyError` ("still busy with a previous command"), since two commands running at once on one player or queue corrupt it. That wait outlasts what a command legitimately holds a player for, such as a play request that loads a large playlist or an announcement, so only a holder that is stuck makes the next command fail; the warning logged then shows where the holder waits. The timeouts live in [constants.py](constants.py).

Attaching protocols is serialized by one lock of the controller, so an object that replaces
another under the same protocol id waits for an attach of that id that is still running. A detach
does not wait for it: it stops an attach of its protocol that is in progress.

### Lock Ordering

A group player always locks its members from under its own lock (forming, dissolving and member changes all go through the leader), so **a command that needs both locks must take the group's lock first**.

A command that locks a member and only then reaches the group — a power off detaching the player from its group is the typical one — has to acquire the group's lock up front. Taking them the other way round deadlocks against any concurrent command on that group. Nested acquisitions within the same task are re-entrant, so locking the group up front costs nothing further down the call chain.

`get_group_and_player_lock` does exactly that: it takes the lock of the group holding the player (following the same redirect as `cmd_set_members`) and then the player's own. A command never holds a player's lock while it waits for the lock of that player's owner: once it holds both, it checks that the owner is still the one whose lock it took first, and when the player joined or left a group meanwhile it releases both and takes them again in the new order (a few times at most, then it fails as busy). Stop, play, resume, play media, a power off, an announcement, a source selection and the queue play actions all take their locks through it, and work out where the command goes only once they hold them; they hold both locks from the release of the group to the command on the player. Releasing the session of a static group (an ungroup of its member) takes the group's lock. The re-entrancy is per task, so a group command that fans out to its members in separate tasks (a group announcement) has those tasks take the member's lock only.

A playback command on a synced player (play, stop, resume, ...) is redirected to the owner of its playback: the sync leader, or the group holding that leader when it is captured by one. That redirect, worked out under the locks, is what puts the command under the group's lock rather than the leader's. The playback commands flagged with `handle_player_command(to_playback_owner=True)` and the sleep timer commands need the caller's access to that owner as well as to the player they name. Taking the player out of its group (ungroup, power off, select source, play media with its `play_media_overrides_group` setting on) only needs access to the player itself.

## Player Types and Protocol Roles

The type of a player (`PlayerType.PLAYER`, `GROUP`, `STEREO_PAIR`, and the kinds of Sendspin
clients) is that of its own protocol, else the one stored for it. A player is never of the type
`PROTOCOL`.

What a protocol is for its device is its role (`ProtocolRole`):
- `NATIVE`: the vendor's own control plane (Sonos, Bluesound, an Apple device over AirPlay, ...).
- `CONTROL`: control without an audio path of its own; playback goes through an audio protocol of
  the same device (LinkPlay with DLNA, for example). A control protocol that reports a native group
  leader gives up the output it played on.
- `AUDIO`: an audio protocol of the device (AirPlay, Chromecast, DLNA, Sendspin on a third-party device).
- `VIRTUAL`: the protocol of a group player.

A player has at most one protocol per domain, and one protocol that stands for the device itself
(native, control or virtual). Two such protocols of one device share a player only when one ranks
above the other (`NATIVE_PRIORITY`: the vendor's own protocol above a Home Assistant media player
of the same device): the better ranked one is the own protocol, the other one attaches in the
control role. That is a reservation, never a fallback: it takes no command, is no output and does
not make the player available, also while the better one is away. A protocol ranks above another
one while the user has it switched on, as the config stores it: its provider is enabled and the
protocol is not switched off as an output. So the roles stay while the better one is away, reloads
or its provider fails to load, after a restart in any order and while the player is disabled. The
other one stands for the device again while the user has the better one's provider disabled or the
better one switched off, and once the better one is removed (its provider forgets the device, or is
removed). Two of one rank, such as the Squeezelite and Snapcast clients of one host, are two players.

A role is fixed while the protocol is attached; a protocol that reports another role, or that
attaches in another one because a protocol that ranks above it arrived or was removed, is
detached and attached again, to the same player when it still has a place for it, else to a
player of its own.

## Finding the Player of a Protocol

When a protocol attaches, the controller looks for its player in this order:
1. The player its record names, as long as none of the protocols attached to that player rules it out.
2. A player whose device the protocol matches: the live players first, then the stored ones (a
   match to a disabled player only stores the edge; nothing attaches until the player is enabled).
   A protocol that matches a player holding an offline protocol record of the same provider takes
   over that record (settings, setup data) under its own id.
3. Else a new player is created for it.

A protocol that rides on another one (`underlying_protocol_id`) belongs to the player of that
protocol: the player it is attached to, else the player its record names, so it also attaches
while that protocol is away. It waits, however long that takes, while there is no such player or
that player has a protocol of the same domain already, attached or away (a stored record of that
domain counts); once that record is removed, it attaches. An audio protocol that matches no player
and has no hardware MAC address yet waits up to 15 seconds for identifiers that tie it to other
protocols of its device; it attaches as soon as one arrives or another protocol creates the player
of its device, and gets a player of its own when the wait ends. A native protocol never waits.

### Device Identifier Matching

Whether a protocol stands for the device of a player is decided in one place: `matches_device` in [matching.py](matching.py), a pure function over what each side reports about its device:

- its identifiers as a set of `(type, value)` pairs, so a device with two MAC addresses (the one its provider reported and the one ARP resolved) holds a pair for each
- the protocol domains that reported them
- whether the native protocol of the device is among them

A player is described by its protocols together: those that are attached report for themselves,
and for one that is away the identifiers it last reported are stored on its record. The rules apply
to that merged identity, so a value one protocol of the player reported rules a newcomer out
whichever other protocol it shares an identifier with.

The rules are one table. Two sides *differ* in a type when both report it and share no value; a type only one side reports never rules a match out.

| Identifier | Differing values rule a match out | A shared value is a match |
|---|---|---|
| `MAC_ADDRESS` | never | yes |
| `SERIAL_NUMBER` | when both sides share a protocol domain | yes |
| `UUID` | when both sides share a protocol domain | yes |
| `CAST_UUID` | always | yes |
| `AIRPLAY_ID` | always | yes |
| `ZONE` | always | no |
| `IP_ADDRESS` | never | as a last resort |

Every veto is checked before any shared value may match, so two sides that differ in their AirPlay id or zone never match, even when they share an IP and MAC address.

- A UUID or serial number only vetoes within one domain: a Chromecast reports its cast uuid as UUID, DLNA its UDN, MusicCast and Bose their device id, so one device has several of them.
- A zone tells the outputs of one chassis apart, which share its MAC address and IP. It identifies nothing on its own, as every chassis has a first zone. A side that reports no zone is in the main zone (`MAIN_ZONE`) when a player is chosen for it, so the AirPlay or DLNA receiver of an AV receiver lands on its main zone and never on another one; a protocol that was attached to another zone before stays there. A chassis whose every zone has a receiver of its own (a BluOS multi-zone streamer) can not tell those receivers apart: the first one seen joins the main zone, the others are players of their own, never outputs of a wrong zone.
- A MAC address never vetoes, as a device has one per network interface. Null and broadcast addresses identify nothing, and the locally-administered bit is ignored because some protocols report the hardware address with that bit set.
- A shared IP address alone is the last resort. It never ties two native devices that each have a hardware MAC address, as one host can carry several of them.

A provider reports an identifier in the form the other protocols of the device report it: the DLNA provider strips the `_MR` suffix from the UDN of a Sonos renderer, which leaves the UUID of its Sonos player.

**Important:** A player never holds two protocols of the same domain. Software players of one
provider on one host (Snapcast clients, Squeezelite instances, Sendspin software clients) are
separate players, not protocols of one device; their provider reports an identifier of its own
instance (`UUID` or `SERIAL_NUMBER`), which vetoes within the domain.

## Lifecycle

| Call | When | What happens |
|---|---|---|
| `attach(protocol)` | a provider found a device, or found it again | the player is found or created and the protocol bound to it; `on_config_updated()` and `on_attach(player)` run. Attaching the attached object again publishes what it reports now; another object under the same id replaces it. Nothing attaches for a disabled player or an output the user switched off. |
| `detach(protocol_id)` | the device is gone, or its provider unloads | `on_detach()` runs; the player stays (unavailable while no other protocol is) and keeps its queue, which only stops when this protocol was the one that played. A player that only exists while connected goes with its last protocol. |
| `remove_protocol(protocol_id)` | a provider forgets a device | detach and delete the protocol record, and the same for the protocols that ride on it; an output or control choice of the player that names it falls back to its default; the player goes when no protocol record of it is left. |
| `remove(player_id)` | the user removes a player | every protocol is detached and gets `on_remove()`, also one whose player is not live; the player, its protocol records, DSP and queue settings, saved queue, group memberships and user filter entries go. Its providers look for devices again, so a device still on the network comes back as a new player (not for a group). |

An attach that waits on the provider (a hook, an address lookup) is stopped by a detach of its
protocol: the protocol is released again, and a player it created that nobody saw yet goes with the
config it stored for it. A failing attach is rolled back the same way.

Disabling a player detaches its protocols and announces it as removed; its config stays. Enabling
it creates it from its record and lets its providers look for its devices.

`PLAYER_REMOVED` says whether the player is removed for good (`data={"permanent": True}`, read
with `helpers.player.player_removed_for_good`): after `remove`, and for the player that goes in a
merge. A disabled player and a player that only exists while connected whose client disconnected
are gone for now: they come back with the same id and their saved queue, so a subscriber keeps
what it stores about them.

### Startup

1. The config controller loads the settings and runs the migrations, the player split last.
2. The core controllers set up; `players.post_setup` creates a player for every stored player that
   is enabled and not session-only, announces it with its queue, unavailable until a protocol
   attaches. A player without protocols whose provider is not installed says so in its
   `setup_reason` (`provider_not_installed`).
3. The webserver starts: clients get one complete list, unavailable players included.
4. The providers load and attach their protocols.

## Commands, Outputs and Volume

A playback command goes to the protocol that renders the audio of the player. A transport command
for what the device plays itself goes to the audio protocol whose app is shown on the player, else
to its own protocol. A command to an unavailable player raises `PlayerUnavailableError`; a power on
is accepted, and a play on a player switched off at its external power control powers it on first.

### Output Protocol Selection

When playing media, the controller selects the output:

1. **Grouped protocol** - If a protocol is actively grouped/synced, use it
2. **User preference** - Honor user's configured preferred protocol
3. **Native playback** - Use the own protocol when it supports PLAY_MEDIA
4. **Default domain** - The audio protocol of the domain the own protocol prefers (a LinkPlay device prefers DLNA)
5. **Best available** - Select by protocol priority (AirPlay > Squeezelite > Chromecast > Sendspin > DLNA)

### Volume and Mute

The level and mute of a player are its own. Without a control the user picked, a volume or mute
command goes to the protocol that renders the audio, else to the own protocol, else to an available
protocol with the feature; level reports are only taken from that protocol. When the output changes,
the protocol that becomes the control gets the level and mute of the player before it is made the
active output and before the stream starts.

While its own protocol is attached but can not be reached, a player keeps the level and mute it had
when that began: what any of its protocols reports may be stale then. It is shown as the quieter of
that and what the protocol in control reports, nothing is sent when the control moves, and the
output that is activated for playback is given it. A volume or mute command, an output given it, or
the own protocol back in reach ends that; the own protocol's level counts again then.

## Configuration Storage

Every player has a record under `players/{player_id}` (base keys, its name, and the values of the
entries every player gets: controls, volume limits, announcement settings, icon, visibility,
preferred output, plus what the player stores itself: legacy ids, last seen, device info,
`session_only`, `created_at`). Every protocol has a record under `player_protocols/{protocol_id}`:
its provider, the id of its player (the edge), its role, whether it is used as an output of its
player (`enabled`), the values of the entries it declares, the audio settings of its output, what
its provider stores for it (MAC addresses, identifiers, the protocol it rides on) and its setup data.
DSP and queue settings stay per player under `player_dsp/` and `player_queues/`. For a record from
before the split, the player record and the record of its own protocol share the id string.

A protocol that only exists while connected has no record until the user changes something about
its player; until then it holds its config in memory, and provider writes go there.

Stored config is read and written per scope, through the config controller:
- `get_player_value` / `set_player_value` - a value of the player itself (`PLAYER_VALUE_KEYS`)
- `get_protocol_value` / `set_protocol_value` - a value of a protocol: an entry the protocol declares, an audio setting of its output, or what its provider stores for it

The config of a player as clients read and save it (`config/players/*`) is one form: the values of
the player, the unprefixed values of its own protocol, and the values of its audio protocols
prefixed with their id. Saving it stores each value with whoever declared its entry.

The player configs were split into these two trees by a migration that keeps a copy of the
settings file from before it (`settings.json.pre-player-split`); see
[player_split.py](../config/player_split.py). Restoring that copy is the way back to an older
version. An older version that runs on the split settings stores the players it finds in the old
shape; without the copy, the next start on this version removes what the older version stored for
players.

## Development Guide

The contract for a provider is in `DEVELOPMENT.md` ("Building your own Player Provider"), and
`providers/_demo_player_provider` is the annotated example. In short:

1. Implement one `PlayerProtocol` per device and report its role (`_attr_role`); leave the type at
   `PlayerType.PLAYER` unless it is a group or stereo pair.
2. **Populate `device_info.identifiers`** with every identifier the device really has, validated:
   ```python
   from music_assistant.helpers.util import is_valid_mac_address

   # IMPORTANT: Validate MAC addresses before adding them
   if is_valid_mac_address(mac_address):
       self._attr_device_info.add_identifier(IdentifierType.MAC_ADDRESS, mac_address)
   self._attr_device_info.add_identifier(IdentifierType.IP_ADDRESS, ip_address)
   self._attr_device_info.add_identifier(IdentifierType.UUID, uuid)
   ```
3. `await mass.players.attach(protocol)` when the device is found, `detach(protocol_id)` when it
   is gone, `remove_protocol(protocol_id)` when the provider forgets it.
4. Claim per-device resources in `on_attach` and release them in `on_detach`; forget what the
   provider keeps about a device in `on_remove`.
5. `discover_players()` offers every known device that has no attached protocol, and is safe to
   repeat: enabling and removing a player both end with a discovery request.
6. Ask `mass.players.wants_protocol(protocol_id)` before setting up a connection for a device.

**Important Notes:**
- **Always validate MAC addresses** using `is_valid_mac_address()` before adding them
  - Rejects invalid MACs like `00:00:00:00:00:00` or `ff:ff:ff:ff:ff:ff`
  - Prevents false matches between unrelated devices
  - The controller will attempt ARP lookup to resolve real MACs automatically
- `IP_ADDRESS` is only used as a last resort after strong identifiers were checked first
- Read your own reported values through your own properties; read `self.player.state` only for what
  the device must display or follow (current media, position, the grouping of the player)
