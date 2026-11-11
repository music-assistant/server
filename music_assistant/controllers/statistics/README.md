# Personal listening history

`statistics/play_history` returns one `PlayHistoryItem` per recorded play, newest
first. It retains the existing `ItemMapping` fields and adds `event_id`,
`played_at` (Unix seconds) and the artist credits recorded at playback time.
Repeated plays have distinct event IDs even when their media URI or timestamp is
the same. Deleted or unavailable media remain in the history.

Both `statistics/play_history` and `statistics/plays_over_time` accept an optional
item filter: `item_id`, `provider` and `media_type`. Supply all three together.
Supported item types are `track`, `album` and `artist`.

- Track filters include recorded provider and library identities of that track.
- Album filters include completed track plays linked to that library album,
  across all discs. They do not count album enqueue actions or complete album
  listens. Provider-only albums without a library mapping cannot be attributed.
- Artist filters use the artist credits stored on completed track plays, not
  compilation album artists. Provider credits resolve through current library
  mappings; missing historic credits are not reconstructed.

History is always personal to the authenticated user. Existing permissions for
combined-user time series are unchanged. `period` and `timezone` on history use
the same boundaries as the other statistics commands; the default is all time.
`played_after_timestamp`, when supplied, further restricts that period.
Pagination uses `limit` and `offset`, ordered by timestamp and event ID. Clients
should restart pagination when refreshing after a newly completed play.

`statistics/play_count` remains the all-history total. It is not reconstructed
from mutable progress, manual marks or provider-reported play counts.

The dated history fields and item filters are available from API schema 109.
`PlayHistoryItem` is included in `music-assistant-models` 1.1.238, which is pinned
by the server. Clients not bundled with the server can use the schema version to
check for support.
