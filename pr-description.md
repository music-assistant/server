## Summary

Full rewrite of the Deezer provider, replacing the REST-based `deezer-python` dependency with [`deezer-python-gql`](https://github.com/music-assistant/deezer-python-gql) — a typed async GraphQL client for Deezer's Pipe API.

### Core architecture

- All metadata (tracks, albums, artists, playlists, podcasts, audiobooks) now fetched via typed GraphQL queries with Pydantic response models
- Shared GraphQL fragments provide consistent field coverage across all item types
- Cursor-based pagination for nested collections (album tracks, playlist tracks, artist albums, audiobook chapters)
- Self-managed `httpx.AsyncClient` pool with JWT auto-refresh (replaces `aiohttp`-based REST client)

### New capabilities

- **Podcasts**: full library sync, episode browsing, bookmark/resume state sync (read + write via `on_played`)
- **Audiobooks**: library sync, chapter navigation with cumulative position calculation
- **Livestreams (radio)**: search and playback via external stream URLs
- **Lyrics**: synchronized (LRC) and plain text from GraphQL
- **Music Together (Shaker)**: group discovery, suggested and curated playlists in browse/recommendations
- **Flow variants**: mood and genre Flows discovered dynamically via flow config queries
- **Smart Tracklists**: "Made for Me" mixes surfaced in browse and recommendations
- **Dynamic playlists** (`is_dynamic`): Flow, FlowConfig, SmartTracklist, recommended tracks, and Shaker playlists return fresh tracks on each playback — enables endless radio-style queue refill
- **Share URL parsing**: Deezer URLs (`deezer.com/{type}/{id}`) are now resolved from search/paste, with Deezer added to `PROVIDERS_WITH_SHAREABLE_URLS`
- **Personal songs**: user-uploaded tracks accessible via "My Uploads" virtual playlist (GW API `personal_song.getList`)
- **Browse folders**: Made For You, Explore (charts, new releases, editorial playlists), Recently Played (including SmartTracklist items), Shaker, Discover Audiobooks

### Dependency change

- **Removed**: `deezer-python` (REST, unmaintained)
- **Added**: `deezer-python-gql==0.10.0` (async GraphQL, Pydantic models, auto-generated typed client)

### GW client changes

- Retained for: track streaming (URL + Blowfish decryption), listen logging, audiobook channel browsing, country code, personal songs
- Non-streaming REST calls removed
- Streaming size calculation made robust for personal tracks (`FILESIZE_MP3_MISC` fallback)

### Breaking changes

None. Drop-in replacement. Existing ARL token configuration is unchanged.

## Test plan

- Library sync for all media types (artists, albums, tracks, playlists, podcasts, audiobooks)
- Search across all entity types including livestreams
- Playback: tracks (FLAC/MP3), podcasts, livestreams, audiobooks
- Playlist CRUD: create, add/remove tracks, delete
- Podcast resume: play → pause → resume picks up position from Deezer bookmarks
- Browse navigation: recommendations, Shaker groups, Flow configs, recently played
- Lyrics display (synced + plain text)
- Share URL resolution (`https://deezer.com/track/123` → resolved in search)
- Dynamic playlists: Flow queue refills with fresh tracks on each play

## Related

- Library: https://github.com/music-assistant/deezer-python-gql
