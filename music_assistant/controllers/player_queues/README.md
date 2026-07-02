# Player Queues Controller Architecture

This document describes the architecture of the Music Assistant Player Queues Controller: how it
turns "play this" requests into actual playback, how the `PlayerQueue`/`QueueItem` model and the
controller's in-memory state relate, and how queue state is reconciled with what each player
actually reports. It is intentionally architecture-only — per-method behaviour lives in the code's
docstrings.

## Table of Contents

- [Overview](#overview)
- [Position in the System](#position-in-the-system)
- [Data Model](#data-model)
- [Invariants](#invariants)
- [State and Persistence](#state-and-persistence)
- [Concurrency Model](#concurrency-model)
- [Player-to-Queue State Reconciliation](#player-to-queue-state-reconciliation)
- [Look-Ahead and Buffering](#look-ahead-and-buffering)
- [Radio and Dynamic Continuation](#radio-and-dynamic-continuation)
- [Track Resolution](#track-resolution)
- [Play Counting and Resume](#play-counting-and-resume)
- [Module Layout](#module-layout)
- [Configuration](#configuration)
- [Future Direction](#future-direction)

## Overview

The `PlayerQueuesController` is a core controller that turns requests to play media items into
actual playback on players. Each Music Assistant player owns one `PlayerQueue`, which holds that
player's queue items and playback state. The controller is responsible for three things: accepting
and applying enqueue requests, driving transport, and keeping the in-memory queue state reconciled
with what the player actually reports.

It is reached through a set of `player_queues/*` websocket API commands — grouped into queue
queries, transport, enqueueing, and queue mutation — and, in the other direction, broadcasts queue
lifecycle events (queue added/updated, items changed, elapsed-time progress, item played) that
clients and the rest of the system observe.

A player's `PlayerQueue` is normally its active source, but a player can also play something else
(an external or native source). The relationship is therefore deliberately a **loose coupling**:
the queue is the usual active source, not the only possible one.

## Position in the System

The controller subclasses `CoreController`, so it is a configurable core module with a manifest,
the domain `player_queues`, config entries, and a lifecycle. It sits between the sources of media
and the targets for playback, and coordinates with several sibling controllers:

- **Music Controller (`mass.music`)** — the heavy dependency for resolving *what* to play. It
  looks up media items by URI, expands artists/albums/genres/playlists/podcasts/folders into
  tracks, fetches dynamic radio and recently-played tracks, supplies resume positions, and records
  play counts. The queue controller resolves abstract media references into concrete
  `QueueItem`/`Track` lists through it.
- **Player Controller (`mass.players`)** — a bidirectional coupling. Outbound, the queue
  controller drives the player (stop/pause/play/enqueue-next, grouping/ungrouping, active-queue
  lookup) and serializes transport via the player's playback lock. Inbound, the player controller
  calls the queue controller's register/update/elapsed-time/remove hooks, which is how the queue
  reconciles itself against real player state. The `queue_id` is the `player_id`.
- **Streams Controller (`mass.streams`)** — resolves a `QueueItem`'s stream details when loading
  and pre-loading items, and provides the audio-buffer primitive. It also drives next-track buffer
  warming: from inside the active-track streaming pipeline, near the end of the current track, it
  calls back into the queue controller to prepare the next item's audio buffer.
- **Config Controller (`mass.config`)** — supplies this controller's own core config values
  (default enqueue options and selection modes), read back at enqueue time.
- **Cache Controller (`mass.cache`)** — persists and restores `PlayerQueue` state and queue items
  per player across restarts.
- **Webserver / Auth (`mass.webserver.auth`)** — resolves the current/queue user for permission
  checks and for user-scoped operations (resume positions, recently-played), and carries the
  current user into background work.
- **Metadata Controller (`mass.metadata`)** — builds image URLs when constructing the playback
  payload for a queue item.

## Data Model

| Type | Role |
| --- | --- |
| `PlayerQueuesController` | The controller: owns all live `PlayerQueue` objects and their items, exposes the API commands, and bridges player events to queue state. Subclass of `CoreController`. |
| `PlayerQueue` | Per-player queue model holding the playback state and the flags that drive behaviour — playback state, shuffle/repeat, autoplay, flow mode, dynamic sources, the current item and index, and elapsed time. Serializable to and from the cache. |
| `QueueItem` | A single playable entry: the media-item reference, its resolved stream details, an ordering index, and per-item `extra_attributes` (e.g. playback speed). Serializable to and from the cache. |
| `Player` / `PlayerMedia` | `Player` is the device whose real-time state the queue is reconciled against (state, active source, corrected elapsed time, type). `PlayerMedia` is the resolved playback payload (uri, images, ...) handed to the player for a queue item. |
| `MediaItemType` family | The source media abstractions (track, album, artist, playlist, podcast, podcast episode, audiobook, genre, browse folder, item mapping) that the controller expands/resolves into concrete tracks and `QueueItem`s before enqueueing. |

The live queue (its `PlayerQueue` and its ordered `QueueItem` list) is held in a single per-queue
`PlayerQueueData` record keyed by `queue_id`; that record also owns serialization to and from the
cache.

## Invariants

- **`queue_id == player_id`.** A `PlayerQueue`'s id is always the id of the player that owns it
  (a leaf player, a group player, or a sync leader). Every per-queue record on the controller
  and the cache entries are keyed by this shared id, so provider and transport commands keyed on
  `queue_id` target the correct player.
- **One queue object per player; reconciliation gated by type.** A `PlayerQueue` object is created
  for every player on register and removed on player remove. `PROTOCOL` players are never
  reconciled, however — their player-update callbacks are ignored, so a protocol player's queue
  object stays effectively inert.
- **Active means active-source.** A queue is considered active only when the player's active source
  is this queue's id (or none); when inactive with no prior state it is forced idle.
- **Transitioning queues are skipped.** While a `queue_id` is marked as transitioning, incoming
  player updates for it are ignored, to avoid mid-track-change reconciliation glitches.
- **Media-time vs stream-time.** The queue's elapsed time is stored in media-time (usable directly
  as a resume position), whereas the player reports stream-time (post-atempo). The two are bridged
  by scaling with the current item's playback speed.

## State and Persistence

All live state lives in memory on the controller instance in one `PlayerQueueData` record per queue,
keyed by `queue_id` (`state.py`). Each record bundles the `PlayerQueue`, its ordered `QueueItem`
list, the full dynamic-source media items, and the runtime-only fields: a previous-state snapshot, a
transitioning flag, an in-progress play-action refcount, a last-counted-play marker, and the
flow-buffer-completed session.

`PlayerQueueData` owns its (de)serialization. Durable state is persisted to the cache controller
under two categories — queue state (the `PlayerQueue` plus its dynamic-source items) and queue items
— keyed by `queue_id`, with this controller's domain as the provider; the items category is written
only when the items actually change, so the list is not re-serialized on every state tick. On player
registration `PlayerQueueData.from_cache` restores both, recomputes the dynamic-source flag, and
resets the play-action-in-progress flag in case the server was killed mid-action (the runtime-only
fields reset to their defaults). On permanent player removal the whole record and both cache entries
are dropped.

## Concurrency Model

Transport and playback actions on a given queue are serialized through the player's shared playback
lock, obtained from the Player Controller. That lock is re-entrant, so nested actions on the same
queue do not deadlock. While an action is in progress an "action in progress" flag is surfaced on
the queue and reported to subscribers.

A per-queue transitioning flag guards the window during track changes, so concurrent
player-update callbacks are skipped while a queue is mid-transition. Background and delayed work —
preloading the next item, buffer preparation, radio fill, resume-on-idle, delayed clear/resume — is
dispatched as tasks or timers rather than run inline, and the relevant tasks/timers are cancelled on
player removal and on stop so stale work cannot enqueue after a queue has stopped. Long passes (such
as a full shuffle) yield to the event loop while running.

## Player-to-Queue State Reconciliation

The controller consumes player lifecycle and per-update callbacks to keep the queue model in sync
with reality. From what the player reports it determines whether the queue is active or inactive,
derives the current index and item, and recomputes elapsed time (bridging media-time against
stream-time). It diffs the incoming state against the previous-state snapshot to detect transitions
— a track played to completion on idle, the end of the queue — and emits queue and time events.

**Flow mode** is the special case here: instead of one stream per track, the whole queue is a
single continuous stream of concatenated items, so the player's cumulative stream index and
position must be mapped back to a per-track index and per-track elapsed time.

Data flow: player state → diff against the previous snapshot → updated `PlayerQueue` fields →
signalled events + cache write.

## Look-Ahead and Buffering

To make playback gapless and to support crossfades, the controller anticipates the upcoming item:
it computes the next index, pre-resolves that item's stream details via the Streams Controller, and
hands the next item to the player ahead of time. Warming the *next track's* audio buffer is not part
of this enqueue path — it is triggered by the Streams Controller near the end of the current track,
via a callback into the queue controller. Stale buffers and crossfade data are cleaned up when the
queue stops, is cleared, or advances.

Data flow: current index → next-item computation → stream-detail resolution → player enqueue-next.
(Next-track audio-buffer warming is driven separately by the streams pipeline near track end.)

## Radio and Dynamic Continuation

When a queue has one or more dynamic sources (its `sources`) or autoplay enabled, the controller
keeps it topped up as it nears its end by fetching additional tracks and appending them as new
`QueueItem`s. Freshly added items can be shuffled to avoid placing identical tracks next to each
other.

Two distinct refill paths share the same "running low" trigger:

- A queue with **dynamic sources** is kept as a small bounded **managed pool** (`managed_pool.py`).
  Each source contributes candidates by its fill mode — a dynamic playlist (a radio playlist or a
  provider station) yields its own self-managing batch (`DYNAMIC`), while a finite item mixed into
  the pool rotates its own unplayed tracks (`TRACKS`). Each top-up apportions slots across the
  sources by weight, recency-gates every candidate, and prefers the least-recently-played. A "radio"
  is just a dynamic playlist from the `radio_playlist` provider.
- **Autoplay** refills using the per-queue configured mode, owned by `autoplay.py`: similar tracks
  (seeded from the enqueued items), an infinite library mix (genre-biased, least-played), a chosen
  playlist, or an automatic mode that tries similar first and falls back to the library mix. The
  mode is read from the per-queue config; the playlist for playlist-mode is a per-queue config value.

Data flow: dynamic `sources` → managed pool (per-source fetch + weighted, recency-gated allocation)
→ appended `QueueItem`s; autoplay flag → `Autoplay` (mode-based selection) → appended
`QueueItem`s.

## Track Resolution

Non-track media must be expanded into the actual tracks or episodes to enqueue. Each source type
(artist, album, genre, playlist, podcast, audiobook, browse folder) resolves into a concrete track
list, applying the configured selection rules (e.g. top tracks vs library tracks vs all tracks),
resolving library-versus-provider variants, and optionally ordering the result. The same concern
also builds the `PlayerMedia` payload handed to the player, using the Metadata Controller for
images.

Data flow: media item → Music/Metadata controller lookups → `Track`/`QueueItem` list and
`PlayerMedia`.

## Play Counting and Resume

The controller decides when a track counts as played and reports it to the Music Controller. Plays
are de-duplicated using a last-counted-play marker (with album-level handling) so a track is not
double-counted on the end-of-queue idle transition. It also computes and applies resume positions
for audiobooks and podcast episodes, and restores a previously playing queue from the play log.

Data flow: playback-progress reports / idle transitions → should-count decision → record play count;
resume-position lookups → seek/resume.

## Module Layout

```
player_queues/
├── __init__.py     # package entry point; documents purpose + loose coupling; re-exports PlayerQueuesController
├── controller.py   # PlayerQueuesController(QueueLoaderMixin, PlaybackTrackerMixin, StreamFeederMixin):
│                   #   the public face — in-memory state, config entries, the API commands and inter-
│                   #   controller event hooks, the core load/items/signal-update/persistence primitives
│                   #   and transport commands; the mixins below carry the loading/tracking/feeding
│                   #   logic, the stateful helper services the rest
├── base.py         # _PlayerQueuesBase(CoreController): the shared base the three logic mixins extend;
│                   #   declares the per-queue state, the helper services and the core-op signatures
│                   #   so each mixin type-checks on its own
├── constants.py    # config keys + default values for enqueue options and artist/album selection
│                   #   modes, the autoplay/crossfade config keys, plus the two cache category
│                   #   identifiers (queue state, queue items)
├── autoplay.py     # Autoplay + AutoplayMode: resolves the per-queue autoplay mode and
│                   #   produces the next batch of tracks for the library-/playlist-based modes
├── smart_shuffle.py # SmartShuffle: recency-aware, well-spaced ordering of the upcoming items
├── managed_pool.py # ManagedPool: bounded dynamic-source pool, topped up + recency-gated, with
│                   #   finite sources materialized to play through once
├── media_resolver.py # MediaResolver: resolves source media items (artist/album/genre/playlist/
│                   #   audiobook/podcast/browse folder) into the concrete tracks to enqueue
├── queue_loader.py # QueueLoaderMixin: applies the enqueue option, loads single items, resume-from-
│                   #   playlog, next-index, and the dynamic/autoplay queue refills
├── playback_tracker.py # PlaybackTrackerMixin: reconciles queue state from player updates, end-of-
│                   #   queue, playback-progress reporting + user-initiated/album-credit play-counting
├── stream_feeder.py # StreamFeederMixin: enqueues the next item on the player, preloads/prepares its
│                   #   audio buffer, and cleans up stale buffers
├── state.py        # PlayerQueueData: the server-side per-queue record (its PlayerQueue, ordered
│                   #   QueueItem list, dynamic-source items, runtime-only fields) + cache (de)serialization
├── helpers.py      # stateless utility layer: the previous-state snapshot type, the playback-lock /
│                   #   in-progress-flag decorator, and pure helpers (sort, dynamic-source detection,
│                   #   current playback speed). Never imports the controller; the play-action
│                   #   decorator types it via a local Protocol (_PlayActionHost) to avoid a cycle
├── strings.json    # localization manifest: translatable name + description of the core module
└── README.md       # this document
```

The package shape follows from `controller.py`'s size: the supporting constants and the stateless
helper layer are split into their own modules. The stateful helper services import `controller.py`
only under `TYPE_CHECKING` (for annotations), while `helpers.py` avoids importing it at all — its
play-action decorator types the controller through a local `_PlayActionHost` Protocol — so there is no
controller↔helper import cycle. Like the other package-style core controllers (cache, players,
streams, discovery, webserver), it co-locates a `strings.json` manifest carrying the module's
translatable display name and description, whereas single-module core controllers (config, metadata,
music) ship without one.

## Configuration

As a core module, the controller exposes config entries (returned through the standard core-config
mechanism) that configure default enqueue behaviour, in two groups:

- **Per-media-type default enqueue option** — for artist, album, track, genre, live sources, and
  playlist (plus the hidden audiobook, podcast, podcast-episode, and folder types), each defaulting
  to *play* or *replace*.
- **Selection modes** — how artists and albums expand into tracks (e.g. top tracks, library tracks,
  prefer library, all tracks).

These values are read back at enqueue time to decide how a given media item is turned into queue
items. The config keys and their default values live in `constants.py`.

Separately, the controller exposes **per-queue** config entries (via `get_queue_config_entries`,
surfaced by the Config Controller) grouped into categories: *autoplay* (the refill mode and, for
playlist mode, the playlist) and *crossfade* (mode and duration), plus volume normalization. Each
group is introduced by a label entry. These are read back per queue when refilling or streaming.

## Future Direction

The per-queue state that used to live in a pile of parallel `queue_id`-keyed dictionaries on the
controller is now consolidated into one `PlayerQueueData` record per queue (`state.py`), which also
owns its cache (de)serialization — so the controller works with a single record per queue instead of
a dictionary per field. The `PlayerQueue` model remains the wire-facing object; `PlayerQueueData` is
its server-side companion (analogous to how the Player Controller pairs runtime state with the
`Player` model).

`controller.py` is now the public face: it keeps the API commands, the inter-controller event hooks,
and the core state/load/signal-update/persistence primitives. The heavy concern logic is split two
ways. The stateless loading, playback-tracking and stream-feeding logic lives on mixins —
`QueueLoaderMixin` (enqueue/fill), `PlaybackTrackerMixin` (player→queue reconciliation + play-counting)
and `StreamFeederMixin` (next-item/buffer feed) — over a shared `_PlayerQueuesBase`, so it operates on
the controller's own per-queue state directly. The stateful helpers — `MediaResolver` (media→tracks),
`Autoplay`, `SmartShuffle` and `ManagedPool` — stay composition objects, each constructed with the
controller and reaching back through it (`self.queues.…`). The controller owns no heavy concern logic
of its own.

Further out, the loose-coupling story could be tightened by folding more of `PlayerQueueData` directly
into the `PlayerQueue` model so the wire object and its server-side companion converge.
