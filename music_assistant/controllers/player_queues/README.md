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
- [Smart Shuffle and Smart Fades Ordering](#smart-shuffle-and-smart-fades-ordering)
- [Track Resolution](#track-resolution)
- [Queue Item Origin](#queue-item-origin)
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
  calls the queue controller's register/update/elapsed-time/detach/remove hooks, which is how the
  queue reconciles itself against real player state. The `queue_id` is the `player_id`.
- **Streams Controller (`mass.streams`)** — resolves a `QueueItem`'s stream details when loading
  and pre-loading items, and provides the audio-buffer primitive. It also drives next-track buffer
  warming: near the end of the item being streamed, or once a realtime source has delivered all
  of its audio, it calls back into the queue controller to prepare the audio buffer of the item
  that follows the streamed one.
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
| `QueueItem` | A single playable entry: the media-item reference, where it was played from (`origin`), its resolved stream details, an ordering index, and per-item `extra_attributes` (e.g. playback speed). Serializable to and from the cache. |
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
- **One queue per player.** A `PlayerQueue` object is created when the players controller
  publishes a player and goes when the player is dropped (disabled or removed; only a removal
  deletes its saved state). A protocol has no queue: the queue of a player plays through whichever
  of its protocols renders the audio, and a player whose protocols are all detached keeps its queue.
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
  pool of tracks. An artist counts as sequenced too when it plays its albums under an album
  order. Top tracks, an artist without library albums, and authors or narrators keep the queue's shuffle. An explicit `shuffle` argument on `play_media` always wins, and the first item of
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
previous-state snapshot, a transitioning flag, a halted-by-command flag, an in-progress
play-action refcount, a last-counted-play marker, the flow-buffer-completed session, and the
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
queue do not deadlock. A request waits for the running one to finish, up to 120 seconds per lock, and
then fails with a busy error rather than run alongside it; the player controller's own commands wait
for it the same way. The lookup of what a play request plays (a big artist, playlist or folder fetched
through a throttled provider can take minutes) runs under that lock, so it has a deadline of its own
(`PLAY_REQUEST_RESOLVE_TIMEOUT`), which starts once the request holds the lock: the wait for a
running command does not count. When it runs out the request fails and releases the lock, and that
of the group holding the player, before it has changed the queue, so the queue keeps its items,
enqueued items, shuffle state and playback. The deadline does not cover filling the managed pool of
a queue that stays dynamic, which fetches the tracks of each new source once the queue's sources are
stored. The next-track handover takes the same lock and only checks the queue once it holds it, so a
track handed to the player after a wait is still the one that follows the playing one.
Queue edits (moves, deletes, refills) do not take the lock, so the handover checks the queue again once
the player has the track, and hands over the real next track when an edit changed it meanwhile.
While an action is in progress an "action in progress" flag is surfaced on the queue and reported to
subscribers. A command that starts a queue that does not play raises it already while it waits for
the queue's player, or the members of its group, to start up at their power control.

A per-queue transitioning flag guards the window during track changes, so concurrent
player-update callbacks are skipped while a queue is mid-transition. A load that shuffles prepares
its new items from the list it started with, so `update_items` keeps the queue on its current item:
when the player moved on in the meantime, the index follows that item to where it now sits.
Background and delayed work — preloading the next item, buffer preparation, radio fill,
resume-on-idle, delayed clear/resume — is dispatched as tasks or timers rather than run inline, and
the relevant tasks/timers are cancelled on player removal and on stop so stale work cannot enqueue
after a queue has stopped. Long passes (such as a full shuffle) yield to the event loop while
running.

## Player-to-Queue State Reconciliation

The controller consumes player lifecycle and per-update callbacks to keep the queue model in sync
with reality. From what the player reports it determines whether the queue is active or inactive,
derives the current index and item, and recomputes elapsed time (bridging media-time against
stream-time). It diffs the incoming state against the previous-state snapshot to detect transitions
— a track played to completion on idle, the end of the queue — and emits queue and time events.

**Flow mode** is the special case here: instead of one stream per track, the whole queue is a
single continuous stream of concatenated items, so the player's cumulative stream index and
position must be mapped back to a per-track index and per-track elapsed time.

A queue whose player goes idle on its last item, with that item played (or, in flow mode,
streamed) to its end, is taken for ended 5 seconds later, unless it plays again by then: it is
refilled from its dynamic source when it has one, and marked as ended otherwise. A pause or a
stop is never that end. A queue whose playback a command halted (`halted_by_command`: set by a
pause and by the common stop handler) keeps its place until it plays again, also when the
command left its player idle (a player without a pause of its own is stopped, and shows as paused
until the pause timeout, and a paused player is stopped once the pause timeout passes). Only the
stop of a sleep timer that waited for the end of an item or a chapter ends the queue, at that
point; a timer that runs out mid-track stops the queue like any other stop. The flag is cleared
when the queue is seen playing, when its paused player is told to play on or when a play action
starts a stream session, taken back by a pause or a stop that fails, and reset when the queue is
cleared or marked as ended.

That check runs in a task of its own, holds no lock, and belongs to the playback that went idle:
it remembers the stream session the queue was last seen playing. After every wait (each of its
five rounds, the fetch of the dynamic source) it first waits for the command that holds the
player's playback lock, and then gives up when the queue is not idle or a play started since:
another stream session started last (`started_session_id`), also while its player does not
report yet that it plays and when a stop ended it before that. So it never refills, restarts or
ends a queue on behalf of a playback that is no longer its last. It also gives up when the
player is still held at the timeout of that lock.

Data flow: player state → diff against the previous snapshot → updated `PlayerQueue` fields →
signalled events + cache write.

## Look-Ahead and Buffering

To make playback gapless and to support crossfades, the controller anticipates the upcoming item:
it computes the next index, pre-resolves that item's stream details via the Streams Controller, and
hands the next item to the player ahead of time. Warming the *next track's* audio buffer is not part
of this enqueue path — it is triggered by the Streams Controller near the end of the streamed track,
via a callback into the queue controller, and always targets the item after the streamed one.
Buffers and crossfade data are released when the queue stops or is cleared, or its player refuses
a start; what the queue moves
away from while it plays goes when it leaves the queue's scope (below). Every
buffer records the session that claimed it, and a stop leaves alone only what the session playing
*now* claimed, so playback that restarted before the stop got that far keeps its audio. Everything
else goes, including what sessions that ended earlier left behind: sessions rotate without a stop,
so a claim that is no longer current marks audio nobody will come back for. A clear or a replace
drops the items themselves, so all of their audio goes with them.

A buffer that still fills holds one of its provider's stream slots, and it knows who uses it: whoever
waits for its first audio and every stream that is open on it (a speaker's request, a direct PCM
consumer, the flow stream and the fade-in it collects ahead of time, a fade into the next track). A
stream also claims the item it decided to play, from before it has any of the item's audio: an item
stream for as long as it runs, a flow stream from the moment it selects its next track. A speaker's
request for an item claims it from the moment it is accepted until it ends, so a queue change while
the request is still being set up leaves the track the speaker asked for alone. The claim is
on the item, so it holds whichever buffer the item has or gets, and every stream takes its own claim
away when it ends. A claim holds its item only for the playback session its stream is part of: once
the queue stopped or started over, or was transferred to another player (which gets the same items
and plays them in a session of its own), a stream that is still open from before holds nothing of the
playback that followed. The buffer such a stream still reads keeps it as a user. A queue keeps the
audio that is in its scope and gives the rest up. A buffer is in scope while a stream reads it or
holds it to read it. Otherwise it is in scope while it is still the buffer of its item and that item
is one the queue plays now or next (`plays_now_or_next`: the item the queue is on, the item its
player has started fetching, and the item that follows either of them), one a stream of the queue's
session claims, or the item right before the one its player fetches, once all of its audio has
arrived. A queue without a playback session has no scope, and no audio is made for it. Whoever only
waits for the first audio of a buffer, as the preparation of the next track does, does not keep it,
and a preparation that is cancelled does not take it along: the buffer stays while it is in scope.
A buffer that is out of scope gives its audio up, whether its source still fills or has delivered
everything, and whether or not its provider has a slot to spare; whoever waits for it gets no audio.
The queue asks its buffers whenever its scope can change: when a session starts or ends, when its
items change (an edit, a load, a dynamic queue that rebuilds its upcoming tracks), when its player
fetches another item or reports another one as playing, when a fade
takes the next track, when the repeat mode or the sleep timer target changes, and before it gets
audio for an item. A buffer is
also asked when a user leaves it, when the claim on its item ends, when another buffer takes its
place on the item, and when a track asks the provider for a slot and none is free. The queue is
asked once more before audio is made: before a buffer is created, before a source in an unknown
format is probed, and when the source gets its slot, so a track that left the scope during one of
those waits is not fetched. From the moment a buffer is told to give its audio up it is no longer
valid, so nobody takes it while its source unwinds: whoever wants that item's audio after that gets
a buffer of its own, through the provider's slots like any other. So a prepared track that a queue
change moved away from the head or took off the queue gives up its audio and its slot at that
change, while a track that is in use or about to play keeps both. The releases of a stop, a clear, a
replace and the start of an item do not ask for users or claims.

A source that delivers at playback pace holds its provider's slot for as long as its track plays.
When such a source gives the slot back and the playback session of its queue goes on with that
provider, the slot stays reserved for that queue for a few seconds, and the queue's next source
takes it at once. The queue goes on with a provider when the track its player fetches or a start is
loading, the track after it, or a track a stream of that session claims still gets audio from there:
a track without audio, with audio that still arrives, or without a source yet when that provider is
one the stream details could take it from (the same compatible accounts, without a copy that account
no longer finds). The track the source was for only counts once its buffer was taken off it (a seek, a
restart, a replace), so not after it delivered everything or broke off; a track that repeats itself
and is longer than its buffer keeps is therefore not covered. So a queue that moves to its next
track, skips, seeks or replaces its tracks does not lose its stream to a queue that asks in that
moment.

A reservation refuses nobody: to every other taker the slot is only busy that much longer. A preview
waits within its budget. A track that needs the slot as its last resort (a start on another queue, a
track prepared ahead of time) while it is open who keeps the slot (it is reserved for another queue,
or another queue's realtime source has it and no audio yet) waits in the provider's line, in the
order it asked, within its budget. It looks again each time a source takes a slot or a holder gets
its first audio: once a realtime source of another queue holds the slot with audio the start is
refused, naming the player to a user who may control it, and the source of a queue that gives its
slot up (a paused one, or one that borrows the account, see below) hands it over once it got its
audio, as at any other moment. A source that is being given up holds nothing
here. When the slot comes free for the start
it stays reserved for its queue, so its source takes it next, unless the queue no longer wants that
track or its consumer is gone: then it goes on to whoever waits next.

Nothing is reserved after the last track of a queue, when the tracks the queue goes on with come
from another provider, for a source that loads ahead of playback, for a queue that is paused or was
told to pause or stop (`halted_by_command`), or without a session. A play action of the queue (a
skip, a seek, a resume, a replace, a "play now") keeps its stream until it ends, though its player
still reports the pause until the new track plays: it keeps its reservation, and its slot is not
handed over. A pause during it does not end it, as the action still plays its track. A reservation
ends when its queue is paused outside its own play action (at the pause command, and at a pause its
player reports by itself), when its session ends, when a start goes to tracks of another provider,
and when a start has loaded its track without using it.

A paused queue keeps its session and its buffers until the pause watcher stops it once the
configured pause timeout passes (30 seconds by default, at most 4 minutes, so before an unread
buffer releases its audio). That goes for a pause command and for a pause its player reports by
itself while it plays the queue's stream, not for another source paused on the player. The
watcher leaves the queue alone while a command still waits for players to start up before it
acts on the player of the queue, and counts again from the end of that wait: only then can a
pause outlast the buffer.
A queue on a player without a pause of its own ends its session at the pause instead. The player
shows as paused (`Player.fake_paused`) until the pause timeout of the player controller ends
that, the queue keeps the position of the pause meanwhile, whatever the stopped player reports,
and play starts it again from there.
When playback on another queue finds no free provider stream slot and one of those buffers holds
one, the paused queue is stopped right away. It resumes later from where it was paused, with a new
source stream. Preparing the next track ahead of time never stops a paused queue. A player that
could not be told to stop still shows paused on a stream its queue has ended, so play starts the
queue again there rather than unpausing the player.

The owner of a streaming account goes first. When playback for the user who owns the account finds
no free slot of it, and a queue that plays it for someone else (another user, a guest, or anonymous
playback) holds the slot with a source that delivers at playback pace, that queue gives way, whether
or not the owner may control its player. It is stopped, and once the slot is kept for the owner's
queue it resumes where it was on another source its own playback user may use that has a free
slot; without one it stays stopped at its position. A play command of the owner is not refused over
such a queue. The owner's own playback on another player keeps its slot as before, and preparing
the next track ahead of time never makes a queue give way.

Data flow: current index → next-item computation → stream-detail resolution → player enqueue-next.
(Next-track audio-buffer warming is driven separately by the streams pipeline, relative to the
streamed item.)

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
  an infinite library mix (genre-biased, least-played), a chosen playlist, similar artists, or an
  automatic mode that tries similar first and falls back to the library mix. The similar artists
  mode plays an artist similar to the last track's artist that has no track in the queue yet, the
  same way as playing that artist. The mode is read from the per-queue config;
  the playlist for playlist-mode is a per-queue config value. A podcast episode or audiobook instead
  continues with its own successor — the next episode of the podcast, the next book in the collection
  — resolved by `media_resolver.py`, and simply ends the queue when there is none. Live sources
  (radio, audio source) have no natural end, so Autoplay does not apply to them at all.

Repeat ONE/ALL temporarily masks the effective autoplay flag off. The queue keeps its saved
autoplay preference — either a pinned per-queue override or the current global default — so turning
repeat back off restores that preference instead of changing it. Already-queued items stay in place;
only future autoplay additions are blocked, and dynamic mode keeps its own refill behaviour.

Data flow: dynamic `sources` → managed pool (per-source fetch + weighted, recency-gated allocation)
→ appended `QueueItem`s; autoplay flag → media-type dispatch → `Autoplay` (mode-based selection) or
the next episode/book → appended `QueueItem`s.

## Smart Shuffle and Smart Fades Ordering

When the option is enabled and Smart crossfade is active, Smart Shuffle can use the analysis Smart
Fades already has to improve the order of upcoming tracks. Recency stays in charge and no new tracks
are selected.

In Normal Mode, MA leaves the current/buffered part of the queue alone and reorders only the future
part it already considers safe to move, one batch of `SMART_FADE_ORDERING_BATCH` tracks at a time
(the size of a dynamic refill batch). A shuffle orders the first batch inside its recency tiers.
When playback gets close to the last ordered track, the next batch is ordered in the background:
only its transitions change, items that are not tracks (such as DJ clips) keep their place, and the
result is written back only if the queue did not change meanwhile. Tracks only move inside their
batch, so Smart Shuffle keeps deciding which tracks play next: ordering a whole queue at once
favours tracks that already have analysis, which for streaming providers are the tracks played
before. The batch end is only kept in memory; after a restart, or once playback has passed it, the
rest of the queue keeps the regular Smart Shuffle order until the next shuffle. The last fixed track
is used as the starting point.

In Dynamic Mode, Managed Pool still picks the refill tracks. Smart Fades ordering then sorts that
accepted batch from the existing queue tail. Both modes order one batch at a time and consider every
track in it.

No analysis is started for this. Unknown data stays neutral. The score uses tempo, graded Camelot
key affinity and end-to-start RMS energy. These are ranking signals, not filters. A silent outgoing
tail is ignored for the energy part of the score.

Close choices keep some randomness, and Smart Fades still decides the actual transition.

## Track Resolution

Non-track media must be expanded into the actual tracks or episodes to enqueue. Each source type
(artist, album, genre, playlist, podcast, audiobook, browse folder) resolves into a concrete track
list, applying the configured selection rules (e.g. top tracks vs library tracks),
resolving library-versus-provider variants, and optionally ordering the result. The same concern
also builds the `PlayerMedia` payload handed to the player, using the Metadata Controller for
images.

Data flow: media item → Music/Metadata controller lookups → `Track`/`QueueItem` list and
`PlayerMedia`.

A track known only by an external id (a MusicBrainz recording id or an ISRC, its `provider`
set to `external`) is the exception. It goes onto the queue as it is and is looked up on the
music providers only when the queue loads it, or when a player asks for its stream, on the
music sources the queue's stream may use. A list of such tracks then costs a lookup per
track that is reached, never one per track of the list for a single play request. Until it is
looked up it is on no music source: it still counts as enqueueable
(`helpers.can_be_enqueued`), seeds no similar-tracks refill, and a queue that holds one can
not be saved as a playlist.

## Queue Item Origin

Every queue item records where it was played from in `QueueItem.origin`: the container (album,
playlist, podcast, folder, ...) as an `ItemMapping`, and the copy that container listed for the
item as a `(provider_instance, item_id)` pair, the pin. When the Streams Controller picks the copy
to stream, the pinned one comes first, so an album, playlist or folder plays from its own source
rather than from the best copy elsewhere (the streams README describes the full order and the
settings that change it).

The origin is captured at enqueue time, as the container is expanded: `MediaResolver` returns a
`ResolvedItem(item, origin)` for every item it resolves, and `build_queue_item` stores the origin
on the queue item. `_load_item` keeps it when it swaps in the full library track. The pins follow
these rules (`helpers.origin_for` for a container's entries, `MediaResolver` for folders, albums
and items played on their own):

- A playlist or podcast pins each entry to its own provider copy, and so does a dynamic source
  for the tracks it adds once the queue has run out. A library item names no copy and an entry of
  a builtin (Music Assistant) playlist an arbitrary one, so neither is pinned; the container is
  still recorded.
- A folder pins each file it lists, and a library track standing in for a file to its copy under
  that folder. An album folder, or a disc subfolder of one, records the library album as its
  container, so it plays with album loudness like the album itself. An album inside a played
  folder (an album folder below it, or an album a folder lists) plays as part of the folder it
  sits in: its tracks keep their pins but record that folder.
- An artist, a genre or a finite radio station records itself without pinning anything.
- An audiobook or podcast episode played on its own is pinned to the copy it was started from, so
  its progress and resume stay on that account. A single track played on its own has no origin.

A track played from an album, or from the album's own folder, plays as part of that album: it
uses the album loudness, and it is not crossfaded into the next track played from the same album
unless the streams setting `allow_crossfade_same_album` is on. The first of those tracks to play to
the end counts the album as played, once until the album is played again.

**Album source.** An album plays all of its tracks from one source. `_resolve_album` asks the Music
Controller for the album's `playback_listing` (which entry each source lists for each track),
keeps the sources the playback user may play from, and lets `select_container_source` (in the
streams controller's `stream_sources.py`) choose one: local sources first in the prefer-local
mode, then the playback user's own accounts, then a source that lists every library track, then
quality. Each track is pinned to the chosen source's entry; a track that source lacks plays as the
album lists it. When every track is to be ranked on its own (the `best_quality_per_track` mode) no
source is chosen and nothing is pinned, and a track outside the library plays the best entry any
of the album's sources lists. The source is chosen at enqueue time, so a changed source mode
applies to the albums queued after it; only `best_quality_per_track` also reaches the albums
already queued, as it ignores their pins at playback. The upgrade threshold is applied at
playback, so it reaches them as well.

Data flow: media item → `ResolvedItem`s with their origin → `QueueItem.origin` → the pinned copy
first when the stream is selected.

## Play Counting and Resume

The controller decides when a track counts as played and reports it to the Music Controller. Only
an item whose audio actually reached the player (recorded when its first chunk is served) is ever
reported as played or taken as the item the queue ended on, so an item a player merely names from
its own cached copy of the queue is never credited and never ends the queue.
Plays are de-duplicated using a last-counted-play marker (with album-level handling) so a track is
not double-counted on the end-of-queue idle transition. It also computes and applies resume positions
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
├── smart_fade_ordering.py # stored-analysis-only local transition ordering shared by queue modes
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
│                   #   audio buffer, and releases a queue's audio when it stops or is cleared
├── state.py        # PlayerQueueData: the complete server-side per-queue record (the wire PlayerQueue
│                   #   snapshot + items, source/enqueued media, user, stream-session + runtime fields)
│                   #   and the cache (de)serialization the wire model no longer carries
├── helpers.py      # stateless utility layer: the previous-state snapshot type, the playback-lock /
│                   #   in-progress-flag decorator, and pure helpers (album order, dynamic-source detection,
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
- **Selection modes** — how artists and albums expand into tracks (top tracks, library tracks,
  prefer library, or the tracks of the artist's library albums), and the order an artist plays in:
  shuffled tracks, or its library albums in random or release order. Each album plays as it does
  on its own, following the album selection. The album orders leave out singles. They fall back
  to shuffled tracks for the top tracks selection or an artist without library albums. An artist
  that is not in the library plays its top tracks. Playing an artist never lists or expands the
  albums a provider has of it, as that costs a request per release.
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
