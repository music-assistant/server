# Player Controller Architecture

This document describes the architecture of the Music Assistant Player Controller: the model of a
player and the protocols it is made up of, how a protocol finds its player, the lifecycle and the
stored config, how a state reaches clients, and the rules for commands, grouping and locks.

What the controller guarantees, scenario by scenario and each proven by tests, is written out in
[SCENARIOS.md](SCENARIOS.md).

## Table of Contents

- [Overview](#overview)
- [Player, PlayerProtocol and PlayerState](#player-playerprotocol-and-playerstate)
- [Core Components](#core-components)
- [Player Types and Protocol Roles](#player-types-and-protocol-roles)
- [Identity](#identity)
- [Lifecycle](#lifecycle)
- [Configuration Storage](#configuration-storage)
- [State and Events](#state-and-events)
- [Commands, Outputs, Volume and Power](#commands-outputs-volume-and-power)
- [Grouping](#grouping)
- [Locking](#locking)
- [What a Provider Has to Do](#what-a-provider-has-to-do)
- [Scenarios](#scenarios)

## Overview

The Player Controller is a core controller that owns every player users and clients see. A player
stands for one device or one group. The player providers do not own players: they register a
protocol object for every way they reach a device (the vendor's own protocol, AirPlay, Chromecast,
DLNA, Sendspin, ...), and the controller attaches it to the player of that device. The controller
decides which player a protocol belongs to, derives the state of the player from what its protocols
report, and routes every command to the protocol that has to carry it out.

A player outlives its protocols. It is created from its stored config at startup, before any
provider loads, and stays listed (unavailable) while its device is away, with its settings and its
queue. [Lifecycle](#lifecycle) says when it goes.

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
│              │  - its final values     │   player.<field>           │
│              └───────────┬─────────────┘                            │
│                          │ update_state()                           │
│                          ▼                                          │
│              ┌─────────────────────────┐                            │
│              │       PlayerState       │   player.state: the        │
│              │                         │   snapshot the API sends   │
│              └─────────────────────────┘                            │
└─────────────────────────────────────────────────────────────────────┘
```

Four models, each with one owner:

| Model | Owner | Is |
|---|---|---|
| `PlayerProtocol` | a player provider, which subclasses it | the provider's handle on one device over one protocol |
| `Player` | the controller; never subclassed | the player users, clients and the queue see; it gives the final value of every field of its state |
| `PlayerState` | derived by the player | the published snapshot of a player, what the API sends |
| `PlayerConfig` | the config controller | what is stored about a player or a protocol |

### PlayerProtocol (provider model)

A `PlayerProtocol` ([player_protocol.py](../../models/player_protocol.py)) is the object a player
provider implements for one device: what the device reports (`_attr_*` values and the overridable
properties) and the commands to it. It reports its `role` (see below), whether it only exists while
connected (`session_only`) and, for a transport that rides on another protocol, the protocol it rides
on (`underlying_protocol_id`). While it is attached, `protocol.player` is its player; its
`update_state()` publishes through that player. Its config is its own record (`protocol.config`).

A protocol has no published state of its own. Its provider finds its own protocol objects through
`provider.get_protocol()` and `provider.protocols`.

### Player (controller model)

A `Player` ([player.py](../../models/player.py)) is created and owned by the controller, from its
stored record or when a device is new. It holds:
- the attached protocols, each with the role it attached in; the one that stands for the device
  itself is its `own_protocol` (native, else control, else virtual), the audio ones are its
  `audio_protocols`, most preferred first;
- the ids of all protocols that belong to it, also those that are away;
- its own config (name, controls, volume limits, announcements, icon, ...);
- what it keeps for itself while it lives: the active output, the active MA source and when an
  idle player forgets it, the sleep timer, the stop flag, the external-pause bookkeeping, a power
  on or power off under way, the palette, `extra_data`;
- the resolution of its power, volume and mute control and of the output that renders.

A player gives the final value of every field of its state under the name of that field:
`player.volume_level`, `player.group_members`, `player.available` are what it published last, the
values of `player.state`. That is what code reads to know how the player is. What those values are
derived from is private to the player, except where other code has to ask what is true of the
device right now. Four of those questions have a final value next to them:

| Final value | What is true right now |
|---|---|
| `available`: clients can use the player; also true while it is switched off at its power control | `available_for_playback`: it can be played to right now (enabled, reachable and set up); for the controller's own use |
| `type`: the type it published | `reported_type`: the type its own protocol reports now, else the stored one; the attach, merge and removal code reads it, before a change is published, and it is the type that is stored |
| `protocols`: all protocols that belong to it, attached or not (`PlayerProtocolInfo`) | `attached_protocols`: the protocol objects that are attached, by id |
| `provider`: the provider instance id it published | `provider_id`: the instance id its attached protocols give it now, else the stored one |

What a player reports for its device comes from its `device_protocol`: its own protocol, else its
most preferred audio protocol. Its device info also carries the identifiers its other protocols
add. While no protocol is attached, its device info and provider are what was stored, and it has
no extra attributes. So are the defaults its own protocol gives it for hiding it in the UI, exposing
it to Home Assistant and keeping it private: what the user set wins, and a protocol that attaches
with other defaults stores those. Its name is the custom name, else its stored name. The stored name
follows the name its own protocol reports while that is attached; an audio protocol sets it only for
a player without a protocol of its own, or one that has no name stored yet. Its default icon is the
one its device model names, else that of the provider of its own protocol (also while that is
away), else the neutral speaker icon.

The remembered MA source is cleared five seconds after the player went idle.
Returning to playing or paused, or selecting a source, before that timeout cancels the reset,
preserving the selected queue through transient device stops during playback transitions.

### PlayerState (API model)

The `PlayerState` is a dataclass representing the final state of the player, and `player.state` is
the one the player published last. Code that wants a single value reads it off the player
(`player.<field>`); `player.state` is for the snapshot as a whole: to send it, to keep it for a
comparison, to hand it on. It:
- Includes any user customizations (custom name, hidden status, etc.)
- Applies transformations (e.g., fake power/volume controls)
- Is the object exposed to the outside world via the API
- Is a snapshot created when `player.update_state()` is called
- Is derived by `derive_player_state` ([player_state.py](../../models/player_state.py)), a function of a snapshot of everything the state depends on
- Lists the protocols of the player (`protocols`: id, domain, provider, role, available, active),
  its legacy ids, when the player was last seen and since when it is unavailable
- Marks one of those protocols `active`, its current playback path: the protocol of an app from
  outside that the player shows, else the one of its active output (the own protocol for
  `"native"`), none without either; `active_output_protocol` only names Music Assistant's own output
- Contains only serializable data suitable for API consumers

A player is available while it is enabled and one of its protocols that can render audio reaches
its device (a control protocol alone does not count, nor a protocol that rides on another one
while that one can not play), or while it is switched off at its external power control; not
while it only has a protocol that still needs setup. It is available for
playback while such a protocol is reachable and set up; that is what the controller checks
before it sends anything to the device. Everyone else reads `available`, see
[Power](#power).

## Core Components

The controller is one class, `PlayerController` ([controller.py](controller.py)), composed of one
mixin per concern. Every mixin builds on the typed base in [base.py](base.py). `controller.py`
keeps the composition, the state the controller sets up, and its setup and teardown.

The modules, by concern:
- [base.py](base.py): the registry of the players and the protocols attached to them, and the
  lookups on it, the owner of a player's playback, the access check and the player controls. It
  declares the operations one concern calls on another, so each mixin type-checks on its own.
- [lifecycle.py](lifecycle.py): attaching and detaching protocols; creating, publishing, removing,
  enabling and disabling players.
- [protocol_linking.py](protocol_linking.py): which player a protocol belongs to: the attach rules
  and roles, the identity of its device, holding it until that is known, and its stored record.
- [merging.py](merging.py): merging two players, and taking a protocol off its player.
- [state_updates.py](state_updates.py): publishing the state of a player, acting on its changes,
  and polling.
- [config_changes.py](config_changes.py): what a changed player, protocol or DSP config does.
- [locks.py](locks.py): the player command locks.
- The player commands: [playback.py](playback.py), [outputs.py](outputs.py) (the output that
  renders), [audio_sources.py](audio_sources.py) (selecting a source, and live source sessions),
  [power.py](power.py), [volume.py](volume.py), [group_commands.py](group_commands.py) (the entries
  of a group change), [sleep_timer.py](sleep_timer.py) and [announcements.py](announcements.py).
- [grouping_plan.py](grouping_plan.py) and [grouping.py](grouping.py): the plan of a group change
  and the executor that carries it out.
- [matching.py](matching.py): whether a protocol stands for the device of a player.
- [helpers.py](helpers.py): the `handle_player_command` decorator.
  [constants.py](constants.py): the lock timeouts and purposes, and `SwitchedOffAction`.

Dependencies point one way:
- `controller.py` imports the mixins, and every mixin imports the base; within the package only
  `__init__.py` imports `controller.py`.
- A mixin reaches another concern through `self`, as the base declares it, not through its
  module. `merging.py` is the one exception: it uses a few module-level names of `lifecycle.py`
  and `protocol_linking.py`.
- `matching.py`, `grouping_plan.py` and `constants.py` import nothing of the controller.
  `grouping.py` builds the snapshot from the controller and runs the plan through it; the mixins
  that start a group change call it.

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

A role is not identity. It is fixed while the protocol is attached; a protocol that reports another
role, or that attaches in another one because a protocol that ranks above it arrived or was
removed, is detached and attached again, to the same player when it still has a place for it, else
to a player of its own.

## Identity

A player id and a protocol id are different things. When the controller creates a player, it
mints its id (`pl` and twelve hex digits). A player that already has an id in the stored config
keeps that id, which can be the id of one of its protocols.

A protocol that only exists while connected (a browser or app player) gives the player it creates
its own protocol id, so that player has the same id in every session. Nothing may assume that a
player id is the id of one of its protocols.

Protocols name each other by protocol id in what they report (`group_members`, `synced_to`,
`can_group_with`); the state derivation translates those to player ids. A group protocol names its
members by player id. `get_player_by_protocol` resolves either kind, a protocol first;
`get_player_or_protocol_player` resolves either kind, a player first, for an id that is a player
id but may be a protocol's (the members of a group protocol, a player id a plugin stored);
`get_protocol` only knows protocol ids and never goes through the legacy-id resolver.

### Legacy Ids

A player keeps answering to its legacy ids: ids it was known under before, such as the id of a
player that was merged into it. They are stored with the player's config (`values.legacy_ids`) and
resolved in one place, `PlayerController.resolve_player_id` ([base.py](base.py)). They resolve in:

- the id-bearing arguments of an API command (`PLAYER_ID_ARGUMENTS`), which every API
  transport rewrites before the handler runs (`run_api_command` in
  [helpers/api.py](../../helpers/api.py));
- `get_player`, and the queue lookups (`get`, `queue_data`, `items`, `get_item` and the like);
- the `source` and `source_id` arguments of the player commands, which also take source ids
  that are no queue, so these commands resolve them before they compare them with the active
  source;
- `has_player_access`, when the caller passes the player it looked up.

A call inside the server is not rewritten, but the player and queue commands look the player up
through those lookups, so they take a legacy id too. Code that compares a stored id with the id of
an event or a live player resolves it first. Protocol ids never resolve.

The state a player publishes lists its legacy ids (`legacy_ids`), so a client can tell which ids
it answers to.

The lookup is built from the stored player configs at startup, by a merge, which writes the
legacy ids, and again whenever a player config is deleted. An id resolves to the player that
lists it only while no player is registered under that id and no config is stored under it.

### Finding the Player of a Protocol

When a protocol attaches, the controller ([protocol_linking.py](protocol_linking.py)) looks for its
player in this order:
1. The player its record names, as long as none of the protocols attached to that player rules it out.
2. A player whose device the protocol matches. A player holding an offline protocol record of
   the same provider is tried first: the device came back under another id, and the protocol takes
   over that record (settings, setup data) under its own id. Then live players come before stored
   ones. A match to a disabled player only stores the edge; nothing attaches until the player is
   enabled.
3. Else a new player is created for it.

A group protocol and a protocol that only exists while connected match no device: without a record
that names its player, each gets a player of its own.

A protocol that rides on another one (`underlying_protocol_id`) belongs to the player of that
protocol: the player it is attached to, else the player its record names, so it also attaches
while that protocol is away. It waits, however long that takes, while there is no such player or
that player has a protocol of the same domain already: attached, or away and not switched off by
the user. Once that one is removed or switched off, it attaches.

An audio protocol that matches no player and has no globally administered MAC address yet waits up
to 15 seconds for identifiers that tie it to other protocols of its device; it attaches as soon as
one arrives or another protocol creates the player of its device, and gets a player of its own when
the wait ends. A native, control or virtual protocol and a protocol that only exists while
connected never wait.

### Device Identifier Matching

Whether a protocol stands for the device of a player is decided in one place: `matches_device` ([matching.py](matching.py)), a pure function over what each side reports about its device:

- its identifiers as a set of `(type, value)` pairs, so a device with two MAC addresses (the one its provider reported and the one ARP resolved) holds a pair for each
- the protocol domains that reported them
- whether the native protocol of the device is among them

A player is described by its protocols together: those that are attached report for themselves,
and for one that is away the identifiers it last reported are stored on its record, without its IP
address, which the network can hand to another device meanwhile. The rules apply
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
- A zone tells the outputs of one chassis apart, which share its MAC address and IP. It identifies nothing on its own, as every chassis has a first zone.
- A side that reports no zone is in the main zone (`MAIN_ZONE`) when a player is chosen for it. So the AirPlay or DLNA receiver of an AV receiver lands on its main zone, never on another one. A protocol that was attached to another zone before stays there.
- A chassis whose every zone has a receiver of its own (a BluOS multi-zone streamer) can not tell those receivers apart. The first one seen joins the main zone; the others are players of their own, never outputs of a wrong zone.
- A MAC address never vetoes, as a device has one per network interface. Null and broadcast addresses identify nothing, and the locally-administered bit is ignored because some protocols report the hardware address with that bit set.
- A shared IP address alone is the last resort. It never ties two native devices that each have a hardware MAC address, as one host can carry several of them.

A provider reports an identifier in the form the other protocols of the device report it: the DLNA provider strips the `_MR` suffix from the UDN of a Sonos renderer, which leaves the UUID of its Sonos player.

**Important:** A player never holds two protocols of the same domain. Software players of one
provider on one host (Snapcast clients, Squeezelite instances, Sendspin software clients) are
separate players, not protocols of one device; their provider reports an identifier of its own
instance (`UUID` or `SERIAL_NUMBER`), which vetoes within the domain.

### One device, two players

Which player a protocol belongs to is decided when it attaches, and that can turn out wrong: a
device ends up as two players when its second protocol was first seen while the first was away, or
before the identifiers that tie them together were known, and a protocol can end up on the player
of another device. So two players can be merged into one (`players/merge`), and a protocol can be
taken off its player again (`players/unlink_protocol`), both in [merging.py](merging.py).

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
both have a protocol that stands for the device and neither ranks above the other, or both have a
protocol of the same domain, when a strong
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
queue items, nothing playing right now, no place in the member list of a group, no sync leader or
members, no group that holds it. Values the server or a provider stores by itself never count. The
older player stays: by the moment it was created, a player without one counting as the oldest, and
of two equally old ones the one whose id is not minted. Otherwise both stay, for the user to merge.

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

## Lifecycle

The lifecycle of players and protocols lives in [lifecycle.py](lifecycle.py).

| What happens | What it means for the player and its protocols |
|---|---|
| A provider finds a device, or finds it again (`register`) | The protocol is attached to the player of its device, which is created when the device is new. `on_config_updated()` and `on_attach(player)` run. Another object under the same id replaces the attached one. Nothing attaches for a disabled player or an output the user switched off. |
| A device goes away, or its provider unloads (`unregister`) | The protocol is detached and `on_detach()` runs. The player stays, unavailable while no other protocol is, and keeps its queue; the queue only stops when this protocol was the one that played. A player that only exists while connected and has no record goes with its last protocol, unless its queue has items: then it gets its record first (as `keep_player` gives it) and stays. |
| A provider forgets a device (`remove_protocol`) | The protocol record goes, and so do the protocols that ride on it. An output or control choice of the player that names it falls back to its default. The player goes for good when no protocol record of it is left. |
| The user removes a player | The player goes for good, with its protocol records, DSP and queue settings, saved queue, group memberships and user filter entries; `PLAYER_REMOVED` is sent once, also for a disabled player, which is not live. Every protocol gets `on_remove()`, also one whose player is not live. A device still on the network comes back as a new player. |
| The user removes what is unavailable | Every player that no protocol reaches goes for good, once its providers are loaded and had time to find their devices again. While only players remain whose providers still need that time, the request is refused, also as a dry run. |
| The user disables a player | It is powered off (or stopped, without a power control), its protocols are detached and the player leaves the registry with a last `PLAYER_UPDATED` that says it is disabled and unavailable, whatever its power control, followed by the `PLAYER_CONFIG_UPDATED` of the save; it is not removed. Its config, its id and its saved queue stay. |
| The user enables a player | It is created from its record, unavailable until a protocol attaches. |
| A plugin keeps a player (`keep_player`) | A player that only exists while connected and has no record gets one, as on the first save of its config: from then on it stays, unavailable, while its client is away. Nothing happens for a player that has a record. |
| Two players turn out to be one device, or a protocol sits on the wrong player | The user merges them, or takes the protocol off; see [One device, two players](#one-device-two-players). |

An attach that waits on the provider (a hook, an address lookup) is stopped by a detach of its
protocol: the protocol is released again, and a player it created that nobody saw yet goes with the
config it stored for it. A failing attach is rolled back the same way.

The controller asks providers to look for their devices again a few seconds later
(`provider.schedule_discovery()`) when a device may be found again: after a player is enabled,
after a player that is not a group is removed, and when a protocol that is not attached moves to
another player (in a merge, or when it is taken off its player) or is switched on again as an
output.

### Startup

1. The config controller loads the settings and runs the migrations, with the conversion of
   player records of the older single-record shape at the end.
2. The core controllers set up; `players.post_setup` creates a player for every stored player that
   is enabled, announces it with its queue, unavailable until a protocol attaches. A player without
   protocols whose provider is not installed says so in its `setup_reason`
   (`provider_not_installed`). A provider is installed when its manifest is registered, whichever
   folder it loads from; the reason follows a manifest that is registered or removed while the
   server runs (`on_provider_manifests_changed`).
3. The webserver starts: clients get one complete list, unavailable players included.
4. The providers load and register their protocols.

## Configuration Storage

Every player has a record under `players/{player_id}` (base keys, its name, and the values of the
entries every player gets: controls, volume limits, announcement settings, icon, visibility,
preferred output, plus what the player stores itself: legacy ids, last seen, device info, the defaults of its own
protocol (`protocol_defaults`), `session_only`, `created_at`). Every protocol has a record under `player_protocols/{protocol_id}`:
its provider, the id of its player (the edge), its role, whether it is used as an output of its
player (`enabled`), the values of the entries it declares, the audio settings of its output, what
its provider stores for it (MAC addresses, identifiers, the protocol it rides on) and its setup data.
DSP and queue settings stay per player under `player_dsp/` and `player_queues/`. A player id that
is not minted can also be the id of one of its protocols; both records then have that id.

The edge lives on the protocol record and nowhere else: a player finds its protocols through the
records that name it.

A protocol that only exists while connected gets no record of its own until the user changes
something about its player, a plugin keeps the player (`keep_player()`, for something the user set
up for it, such as a DJ on its queue), or its client disconnects while the queue of the player has
items; until then it holds its config in memory, and provider writes go there, and the player
exists only while the protocol is connected. From then on the player is stored like the player of
a device: it is created at startup and stays, unavailable, while its client is away.
Its record keeps `session_only`, so no protocol of a device joins it.
A record the same protocol has as an output of a device stays, and does not apply to it while it
connects on its own.

The config controller ([players.py](../config/players.py)) reads and writes stored config per scope.
What is set for the device as a whole belongs to the player (`PLAYER_VALUE_KEYS`): controls, volume
limits, announcement settings, preferred output and what the controller keeps about the player. What
belongs to one way of reaching the device belongs to its protocol: the entries it declares, the
audio settings of its output, what its provider stores for it and its setup data.

The config of a player as clients read and save it (`config/players/*`) is one form: the values of
the player, the unprefixed values of its own protocol, and the values of its audio protocols
prefixed with their id. Saving it stores each value with whoever declared its entry.

Player records of the older single-record shape are converted to these two trees once, by
[player_split.py](../config/player_split.py), which first keeps a copy of the settings file it
started from (`settings.json.pre-player-split`).

## State and Events

A provider never publishes a state itself. It changes what its protocol reports and calls
`protocol.update_state()`, which hands over to the player the protocol is attached to (a protocol
that still waits for its player is matched again instead). The player then:
1. returns at once when none of its own inputs changed and nothing marked its state dirty;
2. collects everything its state depends on (`StateInputs`: its protocols, config, controls, the
   peers and groups it refers to, its queues and sources) and calls `derive_player_state`, which
   returns the new `PlayerState` and what has to happen besides publishing it (`StateEffects`);
3. carries out those effects and diffs the new state against the last one.

A change the player derives from elsewhere (group topology, the active queue, a config change)
comes with `mark_state_dirty()`; the controller does that on all its notification paths
(`trigger_player_update`, which also debounces, and the fan-out below).

`player.<field>` is the value that was last published. It changes only inside
`player.update_state()`. Code that changed something a field is derived from and needs the new
value at once calls `player.update_state()` first (`refresh_state()` when the change is not one of
the player's own inputs). Code that has to know what is true of the device at this moment asks the
member that says so: `available_for_playback`, `offline_by_power_control`, `attached_protocols`,
`reported_type`, `resolve_output_protocol()`.

A change of the position anchor alone is published only when the position jumped. For a change
that is published, the controller ([state_updates.py](state_updates.py)):
1. schedules the update of the queue of the player;
2. runs its reactions: membership cleanup when availability or power changed, the volume limits, a
   check for a source takeover;
3. calls the internal subscribers (`subscribe_player_state_update`);
4. emits the events;
5. forwards the change to the related players: the members of a group or sync leader, the groups
   that hold the player, and its sync leader.

| Event | When |
|---|---|
| `PLAYER_ADDED` | a player object is published: at startup from its config, for a new device or a protocol taken off its player, when the user enables a player, when a player that only exists while connected and has no record connects |
| `PLAYER_UPDATED` | every published change of a player, attach and detach included; a device that goes away or comes back is a change of `available` |
| `PLAYER_OPTIONS_UPDATED` | the options of a player changed |
| `PLAYER_REMOVED` | the player object goes: it is removed or merged into another player, or it only exists while connected and its client disconnected while it had no record and an empty queue |
| `PLAYER_SLEEP_TIMER_UPDATED` | the sleep timer of a player changed; the data is when it expires, `None` when it was cleared or waits for paused playback |
| `PLAYER_CONFIG_UPDATED` | a config of the player was saved; also when its features or its power, volume or mute control change, for the Home Assistant integration, which only re-reads them on that event |

`PLAYER_REMOVED` is only sent for a player that is gone for good, and carries no data: a
subscriber drops what it keeps about it. A device that goes away is no removal: its player stays,
unavailable. Neither is disabling a player: it leaves the registry with a last `PLAYER_UPDATED`,
disabled and unavailable, and comes back under the same id when the user enables it, announced with
`PLAYER_ADDED`. A player that only exists while connected stays when its client disconnects,
unavailable, once it has a record or its queue has items; else it is removed, and a client that
connects again is a new player under the same id, announced with `PLAYER_ADDED`.

The queue of a player lives as long as the player object. It is created and restored from the
saved queue right after `PLAYER_ADDED`, so a subscriber of that event can not count on the queue
yet. When the protocol that rendered the audio of the player is detached, the queue releases its
audio and pending work and keeps its items and position. It goes with the player object, and its
saved state when the player is removed (not when it is disabled).

## Commands, Outputs, Volume and Power

Every player command validates its player first (`handle_player_command`): it checks that the
player exists and is available and that the user may use it, and decides what to do with a player
that is switched off at its external power control. A command to an unavailable player raises
`PlayerUnavailableError`; a power on is accepted.

A playback command ([playback.py](playback.py)) goes to the protocol that renders the audio of the
player. A transport command for what the device plays itself goes to the audio protocol whose app is
shown on the player, else to its own protocol. A playback command on a player that plays along with
a group or sync leader goes to the owner of its playback (see [Locking](#locking)). Play media
instead takes the player out of its group while its `play_media_overrides_group` setting is on,
which is the default.

### Output Protocol Selection

When playing media, the controller ([outputs.py](outputs.py)) selects the output:

1. **Grouped protocol** - If a protocol is actively grouped/synced, use it
2. **User preference** - Honor user's configured preferred protocol
3. **Native playback** - Use the own protocol when it supports PLAY_MEDIA
4. **Default domain** - The audio protocol of the domain the own protocol prefers (a LinkPlay device prefers DLNA)
5. **Best available** - Select by protocol priority (AirPlay > Squeezelite > Chromecast > Sendspin > DLNA)

A command that travels with the audio (enqueue, pause, an announcement the device can not play
itself) goes to the active output, else to the own protocol, else to the output the user picked,
else to the best available audio protocol.

### Volume and Mute

The level and mute of a player are its own. Without a control the user picked, a volume or mute
command ([volume.py](volume.py)) goes to the protocol that renders the audio, else to the own
protocol, else to an available protocol with the feature; level reports are only taken from that
protocol. The `volume_control` and `mute_control` of the player state name where such a command
goes right now, so while another output than the own protocol renders the audio they are the id of
that protocol. A client only reads from them whether the player has a control (`none` if not).
When the output changes, the protocol that becomes the control gets the level and mute of the
player before it is made the active output and before the stream starts.

While its own protocol is attached but can not be reached, a player keeps the level and mute it had
when that began: what any of its protocols reports may be stale then. It is shown as the quieter of
that and what the protocol in control reports, nothing is sent when the control moves, and the
output that is activated for playback is given it. A volume or mute command, an output given it, or
the own protocol back in reach ends that; the own protocol's level counts again then.

The group volume of a group player or sync leader is set on its powered members together, keeping
their balance. It has a lock purpose of its own, so it runs next to the volume commands of the
members.

### Power

The power control of a player ([power.py](power.py)) is the one the user picked: native, a fake
control that only keeps the state in Music Assistant (kept across restarts, except for a group),
none, or an external power control (a `PlayerControl`, such as a smart plug another provider
registers). Without a choice it is native when the own protocol supports power, else none.

A player switched off at its external power control is published as available and powered off, so
a client can still power it on or play to it. What a command does with it is part of the command
(`SwitchedOffAction`): play, resume, play media and select source power it on first and wait for
the device; stop and pause call off a power on under way and otherwise do nothing; volume, mute and
ungroup do nothing; power, play/pause and announcements handle it themselves; every other command
is refused.

Such a player is the one case of a player that is available and can not play yet, and it stays
that while a power on still waits for its device, when it already shows as powered on. Code
outside the controller reads the published `available` of another player to decide whether that
player may take part, as a client does, and `offline_by_power_control` next to it where it has to
know that the device is there now. It never switches the player on through its protocol or its
control: the controller does that.

A command that starts a group player waits for the members that are switched off at their power
control: a play, a resume, a play of an item or of media on its queue, a play or a power on of
the group player, a play on a member that goes to its group, and a queue transfer that plays on
it. Before it takes any lock it powers those members on and waits until their devices are
online, all at once and each for at most 90 seconds (`_power_on_members`). Then it runs as it
does for a group whose members are on, so the group starts once, on all of them. `cmd_set_members`
does the same for the players it adds to a leader or a group.

A member that does not come online in time, or whose control fails to switch it on, is left out
with one warning that names the member and the group, and the group starts on the others (a sync
group that forms without a member also logs that, as it does for any member it can not reach).
When no member can play, the command fails as it does for any group without a member that can
play.

A stop, a pause or a power off of the group calls that wait off (`_cancel_member_power_ons`), also
a stop or a pause of a member whose playback the group owns, and a power off of a group that still
reads as off while it waits. The command that waits fails as unavailable and starts nothing, also
not on the members that are on. The power controls stay on, and a device that comes online
afterwards stays idle (a sync group with a fixed member list that is formed takes a returning
member back, as it does without this). Stopping the leader or group that a player is added to ends
the addition the same way, and removing that player from it ends the wait for that player alone. A
new start right after that waits again for a member whose device still boots: its control is on,
switched on less than those 90 seconds ago, and it is waited for until they are over, without
switching its control once more.

Only such a command switches a member on; nothing does because a state changed. What a command
runs under the lock of the group leaves the members as they are, and so does playback that the
server continues by itself (`without_member_power_on`): the next items of a queue that played out,
a restart of a playing queue for a changed setting, and a next or previous within a queue. An
announcement to a group switches no member on either, also not when it resumes the group
afterwards: it plays on the members that are on. An announcement to one switched-off player powers
that player on.

A player whose power is turned off outside of Music Assistant while it plays its own queue has that
queue stopped shortly after; a group member leaves its group instead. A player with an idle power
off time is powered off once it was available, powered on and stopped for that long. An
announcement that plays, or a power on that still waits for the device, holds that off.

### Sleep Timer, Announcements and Audio Sources

- **Sleep timer** ([sleep_timer.py](sleep_timer.py)): held by the owner of the playback of the
  player it is set on. A timer for a duration is kept by that player and stops it when it expires; a
  timer to the end of the item or chapter is kept by its queue.
- **Announcements** ([announcements.py](announcements.py)): played natively by the own protocol when
  it supports that, else by the output that plays, or by any audio protocol while nothing plays.
  Without such a protocol the controller plays the announcement as media and restores what the
  player did before. It does that too when the native route would ignore a requested volume, as
  long as it can set and restore that volume itself: the player has a volume control and a known
  level. A group announcement goes to the
  members when they can start it together. An announcement holds the player (and its group) for as
  long as it plays.
- **Audio sources** ([audio_sources.py](audio_sources.py)): a live source of a plugin (an
  `AudioSource`, such as Spotify Connect) plays on a player independently of its queue. The
  controller keeps the session per player and passes the transport commands for it to the plugin
  that owns it; the queue keeps its items and goes inactive meanwhile.
- **An app from outside**: an app Music Assistant did not start can play on an audio protocol of the
  player, such as a stream cast from a phone to its Chromecast receiver. The player shows it unless
  an audio protocol is its active output, its own protocol plays (a paused player keeps its place
  until the app plays), it is synced or in a group, or it has a live source session. The playback
  state, position, media, source and its entry in the source list then come from that protocol,
  the pause, seek and next and previous it supports are added to the features of the player, the
  transport commands go to it, and it is the protocol marked `active`.

## Grouping

How a group change reaches the devices is decided in one place, [grouping_plan.py](grouping_plan.py),
by pure functions of a `GroupingSnapshot`: per player and protocol what grouping reads (the published
state, the protocols with their features, the groups they report and what they can group with, the
outputs, the active and the preferred output, the queue it plays). `plan_group_change` returns the
steps of a change: who leads, what is released first, what is refused and why, the lines for the log.
`GroupRules` takes the decisions of the join that completes a member change: which protocol of each
member carries it, what is refused, whether the leader's output moves and is handed over. The
snapshot builds the view of a player or protocol when a decision first reads it, and asks whether two
protocols group natively only for the pair a decision asks about, so a change costs what it reads. A
playback start whose player has no grouped audio protocol has nothing to regroup and takes no
snapshot at all. `apply_group_change` ([grouping.py](grouping.py)) runs the steps and the join, and is
the only code of the controller that calls a protocol's grouping methods. The entries stay what
callers use: `cmd_set_members`, `cmd_ungroup`, and `_handle_set_members` and `_detach_member` for a
group provider or a protocol that changes the group of its leader itself.

A plan stops at a step whose outcome decides what follows, and the change is planned again on a new
snapshot from where it got to (`GroupProgress`). That is the admission of the members to add: it
stops after a member is released from another leader, gives up a group of its own, or is powered
on, and checks the next member on the state after it.

The join is one sequence of device calls (`_join`). With new content the takeover hook of the
group's protocol runs first, and a failure later in the join aborts the takeover it opened. Then:
- each joining member's output moves to the group's protocol, then that protocol's join call adds
  and removes the members;
- the members that left drop their output, and the leader's output moves to the protocol;
- members that rode the leader's own stream are released from its own protocol, which stops when
  the leader hands its output over;
- on a hand-over the protocol the leader rendered on before stops and lets its members go, the
  leader resumes when it played, and those members join it again;
- the members of the leader's own protocol join last, and a dissolved leader stops.

A regroup at a fresh start takes the members off the protocol none of them needs anymore and runs
the same join. The join reads the players again after each device call and takes its next decision
on that read. What it decided or read before such a call is carried and not read again: the members
per protocol, whether the leader played and what it rendered on before its output moved, the
members of the old protocol before it stopped. So the hand-over of the leader's output is decided on
the state after the join call: a leader paused on the device while that call ran hands its output
over without resuming.

For the whole change the executor keeps the objects it addresses (the leader, the protocol the
group joins, the protocol it leaves). It takes no lock of its own: a change runs under the locks
its entry holds (`cmd_set_members` takes the target's, after that of a static group holding it),
and the nested commands it calls take theirs.

A trigger (play media with `play_media_overrides_group`, select source, power off, disabling, an
announcement, a queue transfer) decides whether a player leaves what holds it and through which
entry; the plan decides how. A group player's provider (sync group, universal group) decides its
own members, its leader and when it forms, and reaches the plan through `_handle_set_members` on
its leader.

## Locking

Player commands are serialized per player and purpose with `get_player_lock` ([locks.py](locks.py)):
playback, volume and group volume each have a lock of their own (`PlayerLockPurpose`), so a volume
command does not wait for a play command. A command that takes a lock never runs without it. It
waits for the command that holds it, up to 120 seconds per lock (a grouped player's group lock
first, then its own), and then fails with a `ResourceBusyError` ("still busy with a previous
command"): two commands running at once on one player or queue corrupt it. That wait outlasts what
most commands hold a player for: a play request that loads a large playlist gives up after 60
seconds of looking up what to play, and lets go of its locks before it has changed the queue. It
can also turn away a command behind long work that is legitimate: an announcement holds the player
for as long as it plays. The warning logged at the timeout shows where the holder waits.

Attaching protocols is serialized by one lock of the controller, so an object that replaces
another under the same protocol id waits for an attach of that id that is still running. A detach
does not wait for it: it stops an attach of its protocol that is in progress.

### Lock Ordering

A group player locks its members from under its own lock (forming, dissolving and member changes
all go through the leader), so **a command that needs both locks must take the group's lock
first**.

A command that locks a member and only then reaches the group, such as a power off that detaches
the player from its group, takes the group's lock up front. The other order deadlocks against any
concurrent command on that group. Nested acquisitions within one task are re-entrant, so locking
the group up front costs nothing further down the call chain. The re-entrancy is per task: when a
group command fans out to its members in tasks of their own (a group announcement), those tasks
take the member's lock only.

A command that waits for a device holds no lock meanwhile: a power on of a player that is
switched off at its power control, and that of the members of a group that is started or of the
players that are added, run before the command takes its first lock. A command that already
holds the group does not wait for a member.

A command holds the lock of the player it works on, and of no player it only passes through:
- A command that works on the player under its group uses `get_group_and_player_lock`. It takes
  the lock of the group holding the player (following the same redirect as `cmd_set_members`),
  then the player's own, and holds both from the release of the group to the command on the
  player. A power off, an announcement, a source selection, a member change on a player under a
  static group, a play media that takes the player out of its group and the queue play actions
  use it.
- A playback command that is redirected to the owner (stop, play, resume, and play media with the
  override off) works on the owner and holds the owner's lock alone. The owner's handler locks
  each member it works on itself; a universal group plays each member in a task of its own, which
  takes that member's lock.
- A queue transfer works on two players and uses `get_playback_locks`, which holds each of them
  the way `get_group_and_player_lock` does. It holds the source's and the target's player from
  before it reads either queue until the target plays. A queue play action on either player
  (play, stop, resume, next, previous, seek, play index, play media) waits for it, and it for such
  an action; a pause and a clear of a queue take no lock, so they do not wait. A transfer that
  plays on a target switched off at its power control powers it on before it takes the locks, as
  a play action does, so one that then fails as busy has switched it on. A member change, or a
  group that takes its members itself (a sync group that forms, a universal group that powers
  on), does not take the lock of the players it takes, so a player that a transfer holds can still
  get another owner. The transfer then starts the target once it let go of both players, as a
  play action of its own, which takes the new owner's lock first.
- The hand-over of a playing sync leader's queue to a new leader takes no lock of its own. It runs
  under the locks its caller holds (a member change on the old leader, such as its ungroup, holds
  that leader's); the queue move inside it holds both leaders while it moves the queue, and the
  regroup and the resume take their own locks, so a play action can come between them.

Either way the command never holds a player's lock while it waits for the lock of that player's
owner:
- It takes the owner's lock before the player's.
- Once it holds both, it checks that the owner is still the one whose lock it took.
- When the player joined or left a group meanwhile, it releases them and takes them again in the
  new order, a few times at most; then it fails as busy.
- A command that already holds the player and finds it in a group whose lock it does not hold fails
  at once.
- The transfer never waits for one player while it holds another that it took itself: it waits
  for the locks of one of them (and its owner) only, and takes those of the other when they are
  free. When one is busy, it lets go of all it took, waits for that one first and tries again, a
  few times at most and within one lock timeout; then it fails as busy, before it changed
  anything. Transfers over the same players wait for the same player first, so they usually take
  turns, but one of two that collide can still fail as busy, for instance when an owner changes
  while they wait. Transfers between other players do not wait for it at all.

Group changes nobody issued as a command:
- Releasing the session of a static group (an ungroup of its member) takes the group's lock.
- A sync group's formation takes the leader's lock before it records that leader. Its dissolve
  holds the recorded leader's lock, and that of the player a provider handed the members to.
- The re-form after an unjoin and the hand-over to a new leader record a leader that already
  belongs to the group (its pending re-form, or the old leader, keeps it the group's), so that
  player's owner does not change there.
- A sync group that re-adds members that came back takes the group's lock first, only when it is
  free (`get_player_lock_if_free`), then the leader's, then each member's only when it is free. It
  holds them while it adds the members. A busy group is left as it is and a busy member is left
  out; the leader's next report tries again.
- Work that a handler hands to tasks of its own and waits for never waits for a lock its caller
  holds: a universal group plays a member whose lock the calling command holds (a play action on
  that member's own queue) in the command's own task.
- A change that comes from a device itself (speakers grouped in the vendor's app) holds no lock of
  ours. A command notices the new owner at its next check; a wait for that owner's lock ends in a
  busy error at the timeout, never in running without the lock.
- The queue controller's delayed check of the end of a queue takes the group's lock and the
  player's (`get_group_and_player_lock`) only to wait for the command that holds them, and lets
  go of them before it reads the queue. A free lock does not tell it what plays: it reads from
  the queue whether it plays or another stream session started meanwhile. A wait that ends in a
  busy error ends the check.

A playback command on a synced player (play, stop, resume, ...) is redirected to the owner of its
playback: the sync leader, or the group holding that leader when it is captured by one. That
redirect, worked out under the owner's lock, is what puts the command under the group's lock rather
than the leader's. The playback commands flagged with `handle_player_command(to_playback_owner=True)`
and the sleep timer commands need the caller's access to that owner as well as to the player they
name. Taking the player out of its group (ungroup, power off, select source, play media with its
`play_media_overrides_group` setting on) only needs access to the player itself.

## What a Provider Has to Do

The contract for a provider is in `DEVELOPMENT.md` ("Building your own Player Provider"), and
`providers/_demo_player_provider` is the annotated example. In short:

1. Implement one `PlayerProtocol` per device and report its role (`_attr_role`) and its type
   (`PlayerType.PLAYER`, unless it is a group, a stereo pair or another kind of client the enum
   names). Report besides, where it applies: `session_only` for something that is no device to
   keep track of (a browser tab, an app), `underlying_protocol_id` for a transport that rides on
   another protocol, `enabled_by_default` for a device that starts out disabled,
   `default_output_protocol_domain` for a control protocol
   whose device plays best over one audio protocol, `prefer_native_grouping` for a device that runs
   its own multiroom.
2. **Populate `device_info.identifiers`** with every identifier the device really has, validated:
   ```python
   from music_assistant.helpers.util import is_valid_mac_address

   # IMPORTANT: Validate MAC addresses before adding them
   if is_valid_mac_address(mac_address):
       self._attr_device_info.add_identifier(IdentifierType.MAC_ADDRESS, mac_address)
   self._attr_device_info.add_identifier(IdentifierType.IP_ADDRESS, ip_address)
   self._attr_device_info.add_identifier(IdentifierType.UUID, uuid)
   ```
3. `await mass.players.register(protocol)` when the device is found, `unregister(protocol_id)`
   when it is gone, `remove_protocol(protocol_id)` when the provider forgets it. The controller
   attaches a registered protocol once it knows its player, which can be after `register` returns:
   `on_attach` tells when.
4. Claim per-device resources in `on_attach` and release them in `on_detach`; forget what the
   provider keeps about a device in `on_remove`.
5. `discover_players()` offers every known device that has no attached protocol, and is safe to
   repeat: every discovery request of the controller (see [Lifecycle](#lifecycle)) runs it.
6. Ask `mass.players.wants_protocol(protocol_id)` before setting up a connection for a device.
   Pass the protocol object too when a client can connect either as a device or as a player of its
   own: whether the stored config applies depends on that.

**Important Notes:**
- **Always validate MAC addresses** using `is_valid_mac_address()` before adding them
  - Rejects invalid MACs like `00:00:00:00:00:00` or `ff:ff:ff:ff:ff:ff`
  - Prevents false matches between unrelated devices
  - The controller will attempt ARP lookup to resolve real MACs automatically
- `IP_ADDRESS` is only used as a last resort after strong identifiers were checked first
- Read your own reported values through your own properties; read the final values of the player
  (`self.player.<field>`) only for what the device must display or follow (current media, position,
  the grouping of the player)
- A property of a protocol is read while the state of its player is derived, so it reports from the
  protocol itself and from what is true of the player right now (`attached_protocols`,
  `resolve_output_protocol()`), never from a final value of its own player: during that update
  `self.player.<field>` is still the previous value

## Scenarios

[SCENARIOS.md](SCENARIOS.md) writes out what a user, a provider and a client can rely on, one
scenario per rule. Every scenario has a stable id and names the tests that prove it
([tests/scenarios](../../../tests/scenarios)). This document describes the model those scenarios
run on and does not repeat them.
