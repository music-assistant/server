# Player Queues Controller Architecture

This document gives an overview of the Music Assistant Player Queues Controller: the
`PlayerQueue`/`QueueItem` model, how media is enqueued and played, and how queue state is
kept in sync with the underlying player.

## Table of Contents

- [Overview](#overview)
- [Core Concepts](#core-concepts)
- [Module Layout](#module-layout)
- [Responsibilities](#responsibilities)
  - [Enqueueing Media](#enqueueing-media)
  - [Playback Control](#playback-control)
  - [State Synchronization](#state-synchronization)
  - [Next Item & Buffer Preloading](#next-item--buffer-preloading)
  - [Radio & Dynamic Queues](#radio--dynamic-queues)
  - [Track Resolution](#track-resolution)
  - [Play Counting & Resume](#play-counting--resume)
- [Configuration](#configuration)
- [Future Enhancements](#future-enhancements)

## Overview

The Player Queues Controller is a core controller that manages the playback queue for every
player. It owns the logic to turn user "play this" requests into an ordered list of playable
tracks, to drive transport commands (play/pause/next/seek/...), and to keep each queue's state
aligned with what its player is actually doing.

It is **loosely coupled** to the Music Controller (sources of media) and the Player Controller
(targets for playback). A Music Assistant player always has a `PlayerQueue` associated with it,
which is normally the player's active source — but a player can also play something else (an
external/native source), hence the loose coupling.

## Core Concepts

### PlayerQueue

A `PlayerQueue` (model from `music-assistant-models`) holds the queue's state: the current and
next item, elapsed time, shuffle/repeat/`dont_stop_the_music` flags, `radio_source`,
`is_dynamic`, and playback state. The controller keeps the live queues and their item lists in
memory (`_queues`, `_queue_items`) and persists them to the cache so they survive restarts.

### QueueItem

A `QueueItem` wraps a single playable media item together with its resolved stream details and
per-item `extra_attributes` (e.g. `playback_speed`). Items carry a stable `queue_item_id` used
to address them across move/delete/play-index operations.

### queue_id == player_id

A queue is addressed by its `queue_id`, which is identical to the `player_id` of the player it
belongs to (a leaf player, a group player, or a sync leader). Provider/transport commands keyed
on `queue_id` therefore target the correct player directly.

## Module Layout

```
player_queues/
├── __init__.py     # thin package entry; re-exports PlayerQueuesController
├── controller.py   # the PlayerQueuesController class (public API + state-sync logic)
├── constants.py    # config keys (default enqueue options) and cache category ids
├── helpers.py      # stateless helpers + the @handle_play_action decorator + CompareState type
├── strings.json    # translation source for the core module manifest / config entries
└── README.md
```

`helpers.py` holds the pieces that do not depend on live controller state and are independently
testable:

- `handle_play_action` — decorator that wraps transport actions: it acquires the player's
  re-entrant playback lock and sets `ATTR_PLAY_ACTION_IN_PROGRESS` for the duration, using a
  refcount so nested actions don't clear the flag prematurely.
- `is_radio_source_dynamic` — whether a radio source is a single dynamic playlist.
- `smart_shuffle` — shuffle that avoids placing identical tracks next to each other.
- `sort_tracks` — sort a track list by a named sort key.
- `get_current_playback_speed` — the playback speed of a queue's current item.
- `CompareState` — the previous-state snapshot used to diff player updates.

## Responsibilities

### Enqueueing Media

`play_media` is the main entry point. It resolves the requested media (tracks, albums, artists,
playlists, genres, podcasts, audiobooks, folders, or plain URIs) into concrete `QueueItem`s and
adds them according to the chosen `QueueOption` (e.g. play now, play next, add to end, replace).
Per-media-type defaults for the enqueue option (and artist/album track selection) come from the
controller's config entries.

### Playback Control

The transport commands — `play`, `pause`, `play_pause`, `stop`, `next`, `previous`, `skip`,
`seek`, `resume`, `play_index`, `transfer_queue` — are exposed as API commands. Actions that
change playback are wrapped with `@handle_play_action` so concurrent commands on the same player
serialize on the shared playback lock and the UI gets a consistent "action in progress" signal.

### State Synchronization

Players report their own state; the controller reconciles that against the queue. On each player
update it compares the incoming state to the stored `CompareState` snapshot to detect track
changes, completion, and end-of-queue, then updates the current index, elapsed time, and next
item accordingly. Flow mode (a single continuous stream for the whole queue) needs special
handling to map the player's cumulative stream time back to a per-track index and elapsed time.

### Next Item & Buffer Preloading

To enable gapless playback and crossfades, the controller looks ahead: it determines the next
index, preloads the next item, and prepares its audio buffer before the current track ends. Stale
buffers and crossfade data are cleaned up when the queue stops, is cleared, or moves on.

### Radio & Dynamic Queues

When radio mode (or `dont_stop_the_music`) is active, the controller keeps the queue topped up
with newly resolved tracks based on the queue's `radio_source`. Dynamic playlists are detected via
`is_radio_source_dynamic`, and freshly added items can be shuffled with `smart_shuffle` to avoid
adjacent duplicates.

### Track Resolution

Each media type has a resolver that expands it into a list of tracks: artist (respecting the
configured selection, with library/provider fallbacks), album, playlist, genre, podcast (next
unplayed episodes), audiobook (with resume point), and browse folders. Results can be ordered with
`sort_tracks`.

### Play Counting & Resume

Playback progress reports are forwarded to the music library so plays are counted once per
completed track (`_should_mark_played` guards against the duplicate end-of-queue report). For
resumable content the controller can resume from the stored playlog position.

## Configuration

`get_config_entries` exposes the per-media-type default enqueue options (artist, album, track,
genre, playlist, audiobook, podcast, podcast episode, folder, live sources) and the default
artist/album track-selection behaviour. The config keys live in `constants.py`.

## Future Enhancements

The queue is currently represented by the `PlayerQueue` model while most behaviour lives on this
controller. A planned enhancement is to move the queue's own logic into a dedicated server-side
queue model, so this controller becomes a controller *of* queue objects — mirroring the
relationship between the Player Controller and the `Player` model. That would let large parts of
`controller.py` (state synchronization, next-item/buffer handling, track resolution) move onto the
queue model itself.
