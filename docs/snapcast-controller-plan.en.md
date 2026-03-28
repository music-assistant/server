# Standalone Snapcast Controller Hardening

## Summary

The standalone Snapcast bridge for external Snapserver setups continues to use a
name-first contract through `--stream=<stream_display_name>`. The provider resolves
that stream reference to an active Music Assistant queue, after which `mass_bridge.py`
translates metadata and transport commands bidirectionally between Snapcast and
Music Assistant.

This iteration hardens that path so only real queue-backed playback is treated as
controllable. Streams without an active queue remain read-only and do not publish a
misleading control state.

## Key Changes

- Keep `snapcast/resolve_control_stream(stream: str)` as the single authoritative resolver.
- Match on internal stream name, Snapcast stream id, and visible stream name.
- Only return queue data when the matching stream is linked to a real active queue.
- Treat unresolved streams in `mass_bridge.py` as read-only:
  `canControl=False`, no active queue id, and transport commands rejected.
- Process `queue_time_updated` so the published position in Snapcast stays current
  without a full re-resolve.
- Keep the existing mapping for `next`, `previous`, `play`, `pause`, `playPause`,
  `stop`, `setPosition`, `seek`, `shuffle`, and `loopStatus`.

## Tests

- Resolver returns `None` for streams without active queue-backed playback.
- Resolver returns queue, player, and stream details for valid queue-backed streams.
- `mass_bridge.py` stays read-only when a stream matches but does not return a queue.
- `queue_time_updated` only refreshes the position.
- Unresolved transport commands are rejected.
- Retry logic for a visible old `idle` Snapcast stream remains intact.

## Assumptions

- The external Snapserver variant remains the primary scope; the built-in Unix socket
  bridge does not change functionally.
- `queue_id` and `player_id` are still the same in Music Assistant, but both remain
  explicitly present in the resolve response.
- Access tokens continue to be supplied externally through `--ma-access-token` or
  `MASS_ACCESS_TOKEN`.
- The external Snapserver host must be able to execute `mass_bridge.py` and have the
  Python dependency `websocket-client` available.
