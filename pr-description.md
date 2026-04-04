## Summary

Full rewrite of the Deezer provider, replacing the REST-based `deezer-python` dependency with `deezer-python-gql` — a typed async GraphQL client for Deezer's Pipe API.

### What changed

**Core architecture**
- All metadata (tracks, albums, artists, playlists) now fetched via typed GraphQL queries instead of REST
- Shared GraphQL fragments provide consistent field coverage across all item types
- Cursor-based pagination for nested collections (album tracks, playlist tracks, artist albums, audiobook chapters)

**New capabilities**
- Podcasts: full library sync, episode browsing, bookmark/resume state sync (read + write via `on_played`)
- Audiobooks: library sync, chapter navigation with cumulative position calculation
- Livestreams (radio): search, playback via external stream URLs
- Lyrics: synchronized (LRC) and plain text from GraphQL
- Music Together (Shaker): group discovery, suggested and curated playlists in browse/recommendations
- Flow variants: mood and genre Flows discovered dynamically via GraphQL flow config queries
- Smart Tracklists and "Made for Me" mixes surfaced in recommendations

**Dependency change**
- Removed: `deezer-python` (REST)
- Added: `deezer-python-gql` (async GraphQL, Pydantic response models)

**GW client changes**
- Retained for track streaming (URL + Blowfish decryption), listen logging, audiobook channel browsing, and country code
- Non-streaming REST calls removed

### Breaking changes

None. This is a drop-in replacement. Existing ARL token configuration is unchanged.

## Test Plan

- Verified library sync for all media types (artists, albums, tracks, playlists, podcasts, audiobooks)
- Tested search across all entity types including livestreams
- Tested playback: tracks (FLAC/MP3), podcasts, livestreams, audiobooks
- Tested playlist CRUD: create, add/remove tracks, delete
- Tested podcast resume: play → pause → resume picks up position from Deezer bookmarks
- Tested browse navigation: recommendations, Shaker groups, Flow configs, recently played
- Tested lyrics display (synced + plain text)
- Verified radio stations are searchable and can be favorited in MA

Suggested label: `new feature`
