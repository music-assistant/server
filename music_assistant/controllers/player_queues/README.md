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
- **Cache Controller (`mass.cache`)** — persists and restores each queue's `PlayerQueueData` (its
  wire `PlayerQueue` snapshot plus the server-only state) and queue items per player across restarts.
- **Webserver / Auth (`mass.webserver.auth`)** — resolves the current/queue user for permission
  checks and for user-scoped operations (resume positions, recently-played), and carries the
  current user into background work.
- **Metadata Controller (`mass.metadata`)** — builds image URLs when constructing the playback
  payload for a queue item.

## Data Model

| Type | Role |
| --- | --- |
| `PlayerQueuesController` | The controller: owns all live `PlayerQueueData` records, exposes the API commands, and bridges player events to queue state. Subclass of `CoreController`. |
| `PlayerQueue` (models) | The **wire snapshot** of a queue — a simplified, client-facing view that is shared with API clients over the websocket. Carries the client-relevant playback state and behaviour flags: playback state, shuffle/repeat/crossfade/autoplay, flow mode, the `sources` (as `ItemMapping`s), the current item and index, and elapsed time. It holds no server-only state and is not itself cache-serialized. |
| `PlayerQueueData` (server) | The **complete, server-held state** of a queue (`state.py`). Wraps the wire `PlayerQueue` and adds the state that never leaves the server: the ordered `QueueItem` list, the full media items behind the dynamic `sources`, the enqueued parent items, the owning user, the transient stream-session fields, and runtime-only bookkeeping. Owns the cache (de)serialization for the pair. One per `queue_id`. |
| `QueueItem` | A single playable entry: the media-item reference, its resolved stream details, an ordering index, and per-item `extra_attributes` (e.g. playback speed). Serializable to and from the cache. |
| `Player` / `PlayerMedia` | `Player` is the device whose real-time state the queue is reconciled against (state, active source, corrected elapsed time, type). `PlayerMedia` is the resolved playback payload (uri, images, ...) handed to the player for a queue item. |
| `MediaItemType` family | The source media abstractions (track, album, artist, playlist, podcast, podcast episode, audiobook, genre, browse folder, item mapping) that the controller expands/resolves into concrete tracks and `QueueItem`s before enqueueing. |

The distinction between the two queue types is central: `PlayerQueue` is the lightweight snapshot on
the wire, while `PlayerQueueData` is the full server-side record (one per `queue_id`) that owns it —
analogous to how the Player Controller pairs runtime state with the wire `Player` model.

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
- **A replace swaps the queue's contents; it never empties it first.** The queue keeps playing what
  it has while the new media is resolved (one or more provider round-trips), and the items are
  exchanged in a single `update_items`, so clients never observe an empty queue with nothing
  playing in between. `load(keep_remaining=False, keep_played=False)` is that atomic swap; the
  dynamic path rebuilds its pool from index 0 for a replace rather than behind the playing track
  (which is what *play* wants), and holds player reconciliation off while it does, since the pool
  is fetched with the queue already truncated. The outgoing audio is released by
  `_cleanup_queue_audio_data` *before* the swap, while those items are still on the queue —
  afterwards nothing reaches them, and the track being started needs the source slot they hold.
- **Shuffle is a queue setting; only the media's own order overrides it.** Shuffle stays as the
  user left it across everything they play, except when the media carries an order of its own:
  starting an album, podcast, podcast episode, audiobook or audio source (`ORDERED_MEDIA_TYPES`)
  with *play* or *replace* switches shuffle off, because those are sequenced content rather than a
  pool of tracks. An explicit `shuffle` argument on `play_media` always wins, and the first item of
  a batch decides for the whole batch — it is the only media type known before the items are
  resolved. Switching shuffle off goes through `set_shuffle`, so the items that stay in the queue
  are restored to their original order rather than left shuffled behind a queue that now reads
  unshuffled. The options that only stage items for later (*add* / *next* / *replace next*) leave
  the shuffle state alone, and clearing the queue switches shuffle off with it. A dynamic queue is
  exempt: it is an always-on smart mix and forces shuffle on. *Replace next* is the only staging
  option that can take that dynamic source away, so it is the one exception: it switches the
  imposed shuffle back off, which `_enter_dynamic_mode` restores if the new media leaves the queue
  dynamic.

## State and Persistence

All live state lives in memory on the controller instance in one `PlayerQueueData` record per queue,
keyed by `queue_id` (`state.py`). Each record wraps the wire `PlayerQueue` and holds the rest of the
server-side state around it: the ordered `QueueItem` list, the full media items behind the dynamic
`sources`, the enqueued parent items and the owning user, plus the runtime-only fields — a
previous-state snapshot, a transitioning flag, an in-progress play-action refcount, a
last-counted-play marker, the flow-buffer-completed session, and the
current stream session's id, flow play-log and next-enqueued item id.

`PlayerQueueData` owns the pair's (de)serialization; the wire `PlayerQueue` carries no cache logic of
its own. Durable state is persisted to the cache controller under two categories — queue state (a
versioned envelope: the `PlayerQueue` snapshot plus the enqueued/source media and the owning user)
and queue items — keyed by `queue_id`, with this controller's domain as the provider. Writes are
debounced and marked persistent, and each category is written only when its content actually changed:
the items list only when the items change, and the state only when its persist-worthy content changes
(volatile playback-progress fields such as elapsed time are ignored), so neither is re-serialized on
every state tick. Restore is resilient — the queue's settings survive even if some media items no
longer deserialize, and an incompatible cache-format version is discarded rather than misread. On
player registration `PlayerQueueData.from_cache` restores both, recomputes the dynamic-source flag,
and resets the play-action-in-progress flag in case the server was killed mid-action (the runtime-only
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
queue stops, is cleared, or advances. A stop scopes that cleanup to the playback session it was
issued for, using the session recorded on each item's stream details: if playback restarted before
the stop got that far, the replacement session's buffers are left filling. A clear or a replace
drops the items themselves, so all of their audio goes with them.

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
  sources by weight, recency-gates every candidate, prefers the least-recently-played and nudges
  recently-heard artists back, then best-effort spaces the assembled batch so adjacent tracks avoid
  sharing an artist (seam-aware against the current tail). A "radio" is just a dynamic playlist from
  the `radio_playlist` provider.
- **Autoplay** is the single "keep going" switch; what it appends is dispatched on the media type of
  the queue's last item, since that is the item the appended ones follow. Music continues with the
  per-queue configured mode, owned by `autoplay.py`: similar tracks (seeded from the enqueued items),
  an infinite library mix (genre-biased, least-played), a chosen playlist, or an automatic mode that
  tries similar first and falls back to the library mix. The mode is read from the per-queue config;
  the playlist for playlist-mode is a per-queue config value. A podcast episode or audiobook instead
  continues with its own successor — the next episode of the podcast, the next book in the collection
  — resolved by `media_resolver.py`, and simply ends the queue when there is none. Live sources
  (radio, audio source) have no natural end, so Autoplay does not apply to them at all.

Data flow: dynamic `sources` → managed pool (per-source fetch + weighted, recency-gated allocation)
→ appended `QueueItem`s; autoplay flag → media-type dispatch → `Autoplay` (mode-based selection) or
the next episode/book → appended `QueueItem`s.

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
├── constants.py    # config keys + default values for enqueue options, artist/album selection
│                   #   modes and client click actions, the autoplay/crossfade config keys, plus the
│                   #   two cache category identifiers (queue state, queue items)
├── autoplay.py     # Autoplay + AutoplayMode: resolves the per-queue autoplay mode and
│                   #   produces the next batch of tracks for the library-/playlist-based modes
├── smart_shuffle.py # SmartShuffle: recency-aware, well-spaced ordering of the upcoming items
├── managed_pool.py # ManagedPool: bounded dynamic-source pool, topped up + recency-gated, with
│                   #   finite sources materialized to play through once
├── media_resolver.py # MediaResolver: resolves source media items (artist/album/genre/playlist/
│                   #   audiobook/podcast/browse folder) into the concrete tracks to enqueue, plus
│                   #   the successor (next episode/book) of an item that finished playing
├── queue_loader.py # QueueLoaderMixin: applies the enqueue option, loads single items, resume-from-
│                   #   playlog, next-index, and the dynamic/autoplay queue refills
├── playback_tracker.py # PlaybackTrackerMixin: reconciles queue state from player updates, end-of-
│                   #   queue, playback-progress reporting + user-initiated/album-credit play-counting
├── stream_feeder.py # StreamFeederMixin: enqueues the next item on the player, preloads/prepares its
│                   #   audio buffer, and cleans up stale buffers
├── state.py        # PlayerQueueData: the complete server-side per-queue record (the wire PlayerQueue
│                   #   snapshot + items, source/enqueued media, user, stream-session + runtime fields)
│                   #   and the cache (de)serialization the wire model no longer carries
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
mechanism) that configure default enqueue behaviour, in three groups:

- **Per-media-type default enqueue option** — for artist, album, track, genre, live sources, and
  playlist (plus the hidden audiobook, podcast, podcast-episode, and folder types), each defaulting
  to *play* or *replace*.
- **Selection modes** — how artists and albums expand into tracks (e.g. top tracks, library tracks,
  prefer library, all tracks).
- **Click actions** — what a client does when an artist, album, track, genre, radio, or playlist is
  clicked (*browse* or *play*), and what the play button on a track row inside an album
  or playlist starts (*play from here* or *play track*).

The first two groups are read back at enqueue time to decide how a given media item is turned into
queue items. The click actions are **not read by the server at all**: they live here so every client
resolves the same behaviour from one discoverable, translated schema instead of each defining its
own local preferences. The config keys and their default values live in `constants.py`.

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

Server-only state has now been fully consolidated onto `PlayerQueueData`: the fields that never go
over the wire (the enqueued items, the owning user and the transient stream-session fields) were
moved off `PlayerQueue`, leaving it a pure client-facing snapshot while `PlayerQueueData` holds the
complete server state and owns the cache format.
