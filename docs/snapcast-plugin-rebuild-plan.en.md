# Snapcast Plugin-Only Rebuild Plan

## Goal

This document describes a clean rebuild of the Snapcast integration with the
goal of avoiding broad core changes in modules such as `sync_group` or other
generic player logic.

The target design gives the external bridge, proposed as `mass_bridge.py`, a
larger role while keeping the Snapcast provider responsible for live Snapserver
state, stream lifecycle, and restart recovery.

## Current Status

The `dev` rebuild is now functionally in place and has been validated live
against an external Snapserver setup.

### What Works

- Plugin-level stream registry for lookup by:
  - internal stream name
  - Snapstream id
  - visible stream name
  - `source_id`
  - `queue_id`
- External bridge through `snapserver/mass_bridge.py`
  - Music Assistant WebSocket authentication
  - queue resolution by visible stream name such as `broadcast`
  - read-only behavior while no queue is active
  - property and metadata sync towards Snapcast
  - transport command translation from Snapcast to Music Assistant
- Idempotent stream lifecycle
  - existing idle Snapstreams are reused
  - duplicate stream-name storms are fixed
  - non-retryable duplicate errors no longer trigger endless new-port retries
- Dynamic sync-group pre-materialization
  - as soon as the first Snapcast member is added to an MA sync group such as
    `broadcast`, an idle native Snapcast stream is created immediately
  - extra members can join before playback starts
- Plugin-level group restore
  - existing live Snapcast groups can be mapped back to an MA sync group after
    an MA restart without adding persistence logic to `sync_group`
- Dedicated fallback group for external Snapserver
  - for example `Media`
  - removed members fall back to that group
  - the fallback group is excluded as a restore source
- Empty sync-group cleanup
  - when `broadcast` becomes empty, the idle Snapcast stream is removed again
  - the last leader no longer remains behind as an orphan group

### Important Nuance

The original rebuild target was fully plugin-only. The current `dev` state is
very close, but not fully there yet:

- there is now one small targeted change in `sync_group/player.py`
  to make last-leader removal end in a truly empty runtime group

That change is small and tested, but in a final cleanup pass it would ideally
be pulled back into the Snapcast plugin itself.

### Live-Validated Scenarios

- create a new dynamic `broadcast` group
- add the first member and immediately materialize a native stream
- add extra members
- start playback on `broadcast`
- `mass_bridge.py` authentication and control resolution
- `play`, `pause`, `next`, `seek`
- MA restart with existing Snapserver state
- member removal back to the configured fallback group
- last-leader removal without leaving an orphan group behind
- full cleanup flow:
  - stop or clear playback
  - remove members
  - remove the group
  - remove the idle `broadcast` stream again

### Known Remaining Point

Leader handoff during an active regroup is still not fully atomic. In live
tests there is still a short double-`broadcast` transition of roughly 300 ms
before the old leader falls back to the fallback group and the new leader takes
over completely.

For the current Snap.Net client this has proven acceptable:

- the client follows the handoff correctly
- the client no longer gets stuck during leader handoff
- the final native state is correct

### Practical Transport Nuance

For the external Snapserver TCP stream, the transport model is effectively
`playing` or `idle`. A true transport-level `paused` state does not exist
there. In the current working setup, `pause` therefore means:

- Music Assistant still handles a real pause command
- the Snapcast TCP feed is intentionally stopped afterwards
- Snapserver sees the stream fall back to `idle`

That behavior matches the capabilities of the TCP stream plugin and is no
longer treated as a bug.

## Principles

- Avoid broad core changes in Music Assistant.
- External Snapserver is the primary use case.
- `mass_bridge.py` is the main bridge for control and metadata.
- Snapserver remains the source of truth for native Snapcast groups, clients,
  and streams.
- Music Assistant remains the source of truth for queue, playback state, and
  metadata.
- Restarting Music Assistant must not create duplicate Snapstreams.
- An existing Snapcast stream such as `broadcast` must be safely reusable.

## Non-Goals

- No initial focus on the built-in Snapserver variant.
- No new persistence logic in `sync_group`.
- No attempt to make the Snapcast and Music Assistant grouping models fully
  identical.
- No large UI rebuild in this phase.

## Safe Reference Layout

Do not place the old plugin as `music_assistant/providers/snapcast-old`.
Provider discovery scans directories under `music_assistant/providers` for
`manifest.json` and loads modules by provider domain.

Recommended archive location:

- `/home/thaghostnl/MusicAssistant/_archive/snapcast_old`

or, if it must stay inside the repo:

- `/home/thaghostnl/MusicAssistant/music_assistant/providers/.snapcast_old`

The active work directory remains:

- `/home/thaghostnl/MusicAssistant/music_assistant/providers/snapcast`

## Target Architecture

### Responsibilities

#### Snapserver

- Manages clients, groups, and streams.
- Remains the source of truth for live grouping state.
- Starts and manages the external bridge per stream through a control script.

#### `mass_bridge.py`

- Connects to the Music Assistant WebSocket API.
- Authenticates with an access token.
- Resolves `--stream=<stream_display_name>` to the active MA queue.
- Translates Snapcast JSON-RPC control calls into MA queue commands.
- Translates MA queue and player events into Snapcast properties and metadata.
- Stays read-only while the stream does not resolve to an active queue.
- Recovers independently after MA reconnects.

#### Snapcast Provider

- Connects to Snapserver control.
- Turns Snapclients into Music Assistant players.
- Maintains mapping between:
  - MA player id
  - Snapclient id
  - internal stream name
  - Snapstream id
  - stream display name
- Reuses existing Snapserver streams by visible name.
- Reconstructs runtime state after restart from live Snapserver data.
- Reconnects existing Snapcast groups back to MA sync groups without core
  persistence changes.

## Source-of-Truth Model

### Native Snapcast State

- group membership
- group leader
- `stream_id`
- client connectivity

This data comes from Snapserver and is read by the provider.

### Music Assistant Playback State

- active queue
- playback status
- position
- shuffle
- repeat
- metadata

This data comes from Music Assistant and is pushed to Snapcast through
`mass_bridge.py`.

## File Layout

```text
music_assistant/providers/snapcast/
├── __init__.py
├── constants.py
├── provider.py
├── player.py
├── ma_stream.py
├── socket_server.py
├── stream_registry.py
├── group_restore.py
├── group_materialize.py
└── snapserver/
    └── mass_bridge.py
```

## Acceptance Criteria

- No duplicate `broadcast` stream creation storms.
- Existing grouped Snapcast clients stay visible in the MA UI.
- An existing MA sync group can reconnect to live Snapcast groups after restart.
- `mass_bridge.py` stays read-only while no active queue resolves.
- Snapcast transport commands work again once the queue does resolve.
- Removed members fall back to the configured dedicated fallback group when set.
- The dedicated fallback group is never used as a restore source.
- Removing the last leader does not leave an orphan native Snapcast group behind.
- Leader handoff may still have a short transition, as long as:
  - the final state is correct
  - the old leader falls back cleanly
  - Snap.Net or other clients do not get stuck during the handoff

## Recommended Next Step

Keep validating the current `dev` implementation against the external
Snapserver setup and only pull remaining cleanup into the plugin once the live
behavior is stable enough to replace the temporary `sync_group` assist.
