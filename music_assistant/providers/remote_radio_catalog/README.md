# Remote Radio Catalog

[Nederlandse handleiding](README.nl.md)

Subscribe Music Assistant to externally hosted radio station lists. Each provider
instance represents exactly one catalog, with its own settings and lifecycle.
No catalog addresses or stations are bundled with this provider.

## Status

This is an experimental implementation for Music Assistant Server, not a released
provider, a Home Assistant add-on or a HACS integration. Use the complete server
branch: it includes a radio-matching correction that prevents independent catalogs
with identical station names from being merged. Copying only this directory does
not include that correction.

## Configuration

In a server build containing this provider:

1. Add **Remote Radio Catalog** in the music provider settings.
2. Enter the HTTP or HTTPS URL of one M3U, M3U8 or JSON catalog.
3. Set the refresh interval, from 1 to 1440 minutes (default: 30).
4. Optionally set a friendly name in MA's native provider name field.
5. Repeat for each additional catalog.

For example, use **My Icecast Stations** every 5 minutes, **Community Radio** every
30 minutes, and **Personal Radio Collection** every 60 minutes.

Change the interval in that provider's options, or its URL through native provider
reconfiguration. Only that instance is reloaded. A new URL does not reuse the
previous URL's catalog. Each catalog appears separately under Browse; station
source mappings use the provider's name. Removing an instance stops its tasks and
deletes its cache. Music Assistant manages the corresponding library cleanup.

## Playback and failures

Playback uses MA's existing player and stream pipeline. Configure AirPlay or Google
Cast players separately; this provider does not implement a new player protocol.
Streams must be reachable and playable. Physical speakers have not been tested in
the development environment.

On HTTP or catalog errors, the last valid list remains available for browsing and
playback, including after restart. A failed library refresh does not run a deletion
pass. Without a previous valid list, requests report a temporary error. Other
instances are unaffected.

An explicitly empty catalog is valid: `[]`, `{"stations": []}` or `#EXTM3U` alone.
MA's library-deletion settings apply to valid empty catalogs. With library sync
disabled, catalogs still refresh for Browse but stations are not imported automatically.

Use only catalogs and streams you are authorized to access. The provider cannot
verify broadcasting licenses or grant rights to content. It does not download or
archive audio or bypass access restrictions.

## Catalog formats

Catalogs must be UTF-8 and at most 2 MiB. URLs must use HTTP(S), without embedded
usernames or passwords. M3U8 here means a station list, not an HLS segment or variant
playlist. A station's stream URL may point to HLS.

```m3u
#EXTM3U
#EXTINF:-1,Example Radio
https://radio.example.org/live.mp3
```

```json
{
  "stations": [
    {
      "id": "example-radio",
      "name": "Example Radio",
      "url": "https://radio.example.org/live.mp3",
      "logo": "https://radio.example.org/logo.png"
    }
  ]
}
```

A plain JSON array is also accepted. Only `url` is required. Missing IDs use a
stable SHA-256 of the stream URL; supply stable IDs if URLs may change. Relative
stream/logo URLs resolve against the final catalog URL after redirects. Icecast
status JSON is accepted through `icestats.source` (one object or an array), using
`listenurl` and `server_name`.

Conflicting duplicate IDs inside a catalog invalidate that download. Identical IDs
across instances are supported. Validation is atomic: malformed entries never
replace the last valid snapshot with a partial list.

## Architecture and testing

- `multi_instance: true` enables native repeated configuration.
- `is_streaming_provider = False` identifies instance-local catalogs.
- Media items and unique mappings use the native instance ID. Radio comparison
  respects these mappings instead of merging unrelated stations by name.
- Every instance uses MA's `call_later` and tracked task APIs. The general recurring
  task model currently supports hourly/daily schedules, not minute intervals.
- Only `music.start_sync(providers=[instance_id])` is queued; library processing
  remains MA-owned. There is no global catalog manager or scheduler.
- Caches are scoped by instance ID and checked against a catalog URL digest.
- Concurrent requests join one download per instance. Canceling a waiter does not
  interrupt others; unloading cancels and awaits the instance's background work.
- Stream addresses stay in the server's catalog and stream-resolution path, not
  public provider mappings.

Tests in `tests/providers/remote_radio_catalog/` use real HTTP against a local test
server and MA's provider/comparison code. Configuration, cache persistence and timer
interfaces are controlled test doubles, not a full running MA server.

```sh
pytest tests/providers/remote_radio_catalog tests/helpers/test_compare.py
pre-commit run --all-files
```

Full Linux server/database testing, UI configuration/removal, and physical player
playback remain necessary before declaring the provider production-ready.
