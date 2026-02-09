# Pocketcasts Provider - Development Status

**Last Updated:** 2026-02-09
**Branch:** `pocketcasts`
**Stage:** Beta
**Code Owner:** @ozgav

---

## Overview

Pocketcasts is a podcast streaming service provider for Music Assistant. This provider allows users to access their Pocketcasts library, browse subscribed podcasts, search for new content, and sync playback progress.

---

## Branch Status

**Current State:** ✅ Ready for testing and development
- Synced with upstream/dev (566 commits ahead of origin)
- Merged latest dev changes (2026-02-01)
- No merge conflicts

**Git Information:**
- Base branch: `dev`
- Merge commit: `b4f7b6f9`
- Original work: 4 commits (a5987e6c → 401d41c8)

---

## Provider Files

| File | Description |
|------|-------------|
| `manifest.json` | Provider metadata and configuration |
| `api_client.py` (~500 lines) | Custom API client for Pocketcasts |
| `__init__.py` (~890 lines) | Main provider implementation |
| `STATUS.md` | This file |

---

## Feature Status

### Declared Provider Features

```python
SUPPORTED_FEATURES = {
    ProviderFeature.LIBRARY_PODCASTS,        # ✅ Implemented
    ProviderFeature.BROWSE,                  # ✅ Implemented
    ProviderFeature.SEARCH,                  # ✅ Implemented
    ProviderFeature.LIBRARY_PODCASTS_EDIT,   # ✅ Implemented
}
```

### Implemented Features

| Feature | Status | Notes |
|---------|--------|-------|
| **Authentication** | ✅ Complete | JWT login, mobile token (~5 months validity) |
| **Library Podcasts** | ✅ Complete | Fetch subscribed podcasts |
| **Browse** | ✅ Complete | Podcasts + 5 special folders |
| **Search** | ✅ Complete | Search for podcasts |
| **Subscribe/Unsubscribe** | ✅ Complete | Sync library changes to Pocketcasts |
| **Playback Progress Sync** | ✅ Complete | 30-second sync, completion detection |
| **Resume Position** | ✅ Complete | Read/write position from Pocketcasts |
| **Episode Completion** | ✅ Complete | Mark played, archive, remove from Up Next |

### Browse Folders (Live Playlists)

All 5 special folders appear at root level in browse view:
- **Up Next** - User's queued episodes
- **New Releases** - Recent episodes from subscriptions
- **In Progress** - Episodes being listened to
- **Starred** - Favorited episodes
- **History** - Recently played episodes

### Playback Sync Behavior

- Progress syncs every 30 seconds during playback (MA's callback interval)
- Completion detected using **real duration** from `/user/episode` endpoint
- When `position >= real_duration - 45`: sync to end → mark played → remove from up next → archive
- Starting playback: episode added to Up Next via `/up_next/play_now`
- Replaying a played episode: unarchives and resets status to in-progress
- MA's `fully_played` flag is intentionally ignored (MA uses wrong duration from static API)

**API Status Codes:**
| Status | Meaning |
|--------|---------|
| 1 | Unplayed |
| 2 | In Progress |
| 3 | Played |

---

## Roadmap & Potential Features

### High Priority

| Feature | MA Feature | Status | Notes |
|---------|------------|--------|-------|
| Error handling improvements | - | 🔲 Pending | Better error messages, exception handling |
| Unit tests | - | 🔲 Pending | Test API client, data conversion |
| API call optimization | - | 🔲 Pending | Cache podcast lists, reduce redundant calls |

### Medium Priority

| Feature | MA Feature | Status | Notes |
|---------|------------|--------|-------|
| **Recommendations** | `RECOMMENDATIONS` | 🔲 Pending | Featured/trending podcasts, categories |
| **Up Next Queue Source** | `AUDIO_SOURCE` | 🔲 Pending | Plugin provider for separate Pocketcasts queue |

**Up Next Queue Source Architecture:**
- Create separate `pocketcasts_queue_source` plugin provider
- Appears in player's "Select source" dropdown alongside "Music Assistant Queue"
- Keeps Pocketcasts queue completely separate from MA queue
- Reference: `/music_assistant/providers/spotify_connect/__init__.py`

### Low Priority (Nice-to-Have)

| Feature | MA Feature | Status | Notes |
|---------|------------|--------|-------|
| **Transcripts** | `LYRICS` | 🔲 Pending | Premium only, requires VTT parsing |
| **Bookmarks** | `BROWSE` | 🔲 Pending | Time-stamped bookmarks, niche use case |
| **Filters** | Custom | 🔲 Pending | Smart playlists, unclear API availability |
| Token refresh | - | 🔲 Pending | Low priority - mobile tokens valid ~5 months |
| Multi-instance | - | 🔲 Pending | Multiple Pocketcasts accounts |

### Not Applicable

| Feature | Reason |
|---------|--------|
| Starred Episodes Edit | MA lacks `FAVORITE_PODCAST_EPISODES_EDIT`; already available as browse folder |
| Podcast Favorites | Pocketcasts doesn't support podcast-level favorites |

---

## Known Issues

### To Investigate

| Issue | Description |
|-------|-------------|
| Image URLs | Static CDN URLs - verify always accessible |
| Episode ID format | Using `podcast_uuid:episode_uuid` - verify MA compatibility |
| Error handling | Some methods return empty lists instead of raising exceptions |
| Multi-instance | Manifest has `multi_instance: false` - should this be true? |

### Potential Bugs

| Bug | Status |
|-----|--------|
| Episode URL parsing | 🔲 Should validate URLs in `get_stream_details()` |
| Session cleanup | 🔲 Verify session closes on errors |
| Token expiration | 🔲 No refresh mechanism (low impact - 5 month validity) |
| Rate limiting | 🔲 No rate limiting protection |
| Duration in seek bar | ⚠️ Known limitation - MA shows wrong (shorter) duration; playback works correctly |

---

## Testing Status

### Manual Testing (2026-02-01 to 2026-02-09)

**Environment:** Music Assistant dev branch, fresh database, Pocketcasts account

| Test | Result | Date |
|------|--------|------|
| Login with valid credentials | ✅ Pass | 2026-02-01 |
| Login error handling (401) | ✅ Pass | 2026-02-02 |
| Browse podcast library | ✅ Pass | 2026-02-01 |
| Episode lists | ✅ Pass | 2026-02-01 |
| Play episode | ✅ Pass | 2026-02-01 |
| Resume playback | ✅ Pass | 2026-02-01 |
| All 5 browse folders | ✅ Pass | 2026-02-02 |
| Subscribe to podcast | ✅ Pass | 2026-02-03 |
| Unsubscribe from podcast | ✅ Pass | 2026-02-03 |
| Progress sync to Pocketcasts | ✅ Pass | 2026-02-04/05 |
| Episode completion sequence | ✅ Pass | 2026-02-09 |
| Add to Up Next on playback | ✅ Pass | 2026-02-09 |

**Not Yet Tested:**
- Search for podcasts
- Large libraries (100+ podcasts)
- Podcasts with many episodes (500+)
- Episode image fallbacks

**Automated Tests:** None yet

---

## Required Dependencies

**Python Packages:** None additional - uses `aiohttp` (MA dependency)

**External Services:**
- Pocketcasts account
- Internet connectivity for API access

---

## API Endpoints

Reference: [Unofficial Pocketcasts API Documentation](https://github.com/yfhyou/api_pocketcasts/blob/main/reference/endpoints.md)

### Currently Implemented

#### Authentication (api.pocketcasts.com)
- `POST /user/login` - Login with email/password (returns JWT token)

#### Podcasts (api.pocketcasts.com)
- `POST /user/podcast/list` - Get user's subscribed podcasts
- `POST /user/podcast/subscribe` - Subscribe to a podcast
- `POST /user/podcast/unsubscribe` - Unsubscribe from a podcast
- `POST /discover/search` - Search for podcasts

#### Podcasts (podcast-api.pocketcasts.com)
- `GET /podcast/full/{uuid}` - Get podcast details and episodes

#### Episodes (api.pocketcasts.com)
- `POST /user/in_progress` - Get in-progress episodes
- `POST /user/episode` - Get episode details (correct duration, status)
- `POST /user/history` - Get listening history
- `POST /user/new_releases` - Recent episodes from subscriptions
- `POST /user/starred` - User's starred episodes
- `POST /sync/update_episode` - Update playback progress (status 1/2/3)
- `POST /sync/update_episodes_archive` - Archive/unarchive episodes

#### Up Next Queue (api.pocketcasts.com)
- `POST /up_next/list` - Get Up Next queue
- `POST /up_next/play_now` - Add episode at "play now" position
- `POST /up_next/remove` - Remove episode from queue

#### Assets (static.pocketcasts.com)
- `GET /discover/images/280/{uuid}.jpg` - Podcast thumbnails

### Available but Not Implemented

| Category | Endpoints |
|----------|-----------|
| **Auth/Account** | `/user/login_pocket_casts`, `/user/token`, `/subscription/status` |
| **Up Next** | `/up_next/play_next`, `/up_next/play_last` |
| **Starred** | `/sync/update_episode_star` |
| **Bookmarks** | `/user/bookmark/list`, `/user/bookmark/add`, `/user/bookmark/delete` |
| **Recommendations** | `/discover/recommend_episodes`, `/recommendations/podcast/{uuid}`, `/recommendations/social`, `/recommendations/user_podcast` |
| **Discovery** | `lists.pocketcasts.com/featured.json`, `lists.pocketcasts.com/trending.json`, `static.pocketcasts.com/discover/json/categories_v2.json` |
| **Show Notes** | `shownotes.pocketcasts.com/show_notes/{uuid}/...` |
| **Stats** | `/user/stats/add`, `/history/do` |

---

## Configuration

| Setting | Type | Description |
|---------|------|-------------|
| `username` | string | Pocketcasts account email |
| `password` | password | Pocketcasts account password |

**Provider Settings:**
- Domain: `pocketcasts`
- Type: `music` (podcast provider)
- Multi-instance: `false`
- Stage: `beta`

---

## Documentation

### References
- Pocketcasts API: Unofficial/undocumented (reverse-engineered)
- Provider documentation: https://music-assistant.io/music-providers/pocketcasts/

### Code Style
- Sphinx-style docstrings (`:param:` format)
- Type hints throughout
- Follows Music Assistant code style

---

## Contributing

### Before Submitting PR
- [ ] Run `pre-commit run --all-files`
- [ ] Run `pytest` to ensure tests pass
- [ ] Test basic functionality manually
- [ ] Update this STATUS.md file

### PR Checklist
- [ ] Code follows Music Assistant style guidelines
- [ ] Docstrings use Sphinx-style (`:param:` format)
- [ ] Type hints are complete
- [ ] No sensitive data in logs

---

## Notes

- **API Documentation:** Pocketcasts doesn't have official public API documentation. Implementation based on reverse-engineering the web/mobile app.
- **API Stability:** Since the API is undocumented, endpoints may change without notice.
- **Authentication:** Uses JWT bearer tokens. Mobile tokens valid ~5 months.
- **Episode IDs:** Using composite format `podcast_uuid:episode_uuid` for uniqueness.
- **Duration Discrepancy:** Static API returns wrong (shorter) duration; `/user/episode` returns correct duration. MA seek bar shows wrong end time (cosmetic only).

---

## Changelog

### 2026-02-09 - Up Next Integration & Completion Threshold Fix

- **New Feature: Add episode to Up Next when starting playback**
  - Added `play_now()` API method - calls `/up_next/play_now`
  - Called in `get_stream_details()` when MA requests stream URL
  - Matches web player behavior

- **Bug Fix: Episode completion threshold increased from 15 to 45 seconds**
  - MA calls `on_played` every 30 seconds, so last callback can be up to 29 seconds before end
  - 45-second threshold guarantees last callback always triggers completion

- **Removed provider-side throttle**
  - MA already controls callback cadence (every 30 seconds + on state changes)

### 2026-02-04/05 - Playback Progress Sync (Major Rework)

- **Reworked playback progress sync with real duration detection**
  - Episode completion uses real duration from `/user/episode`, not MA's duration
  - Completion triggers when `position >= real_duration - 45` seconds
  - Replaying played episode unarchives and resets status

- **New API Client Methods:**
  - `get_episode_details()` - Correct duration, playback status
  - `remove_from_up_next()` - Remove from Up Next queue

- **Episode Completion Sequence:**
  1. `update_episode` with position=real_duration
  2. `update_episode` with status=3 (played)
  3. `up_next/remove`
  4. `update_episodes_archive` with archive=true

### 2026-02-03 - Library Management & Progress Sync (Initial)

- **Library Management:** Implemented `library_add()` and `library_remove()` for subscribe/unsubscribe
- **Progress Sync:** Implemented `on_played()` callback, `mark_episode_played/unplayed()`, `archive_episode()`

### 2026-02-02 - Browse Folders & Token Documentation

- **Browse Folders:** Added 5 special folders (Up Next, New Releases, In Progress, Starred, History)
- **Token Documentation:** Documented mobile token behavior (~5 months validity)

### 2026-02-01 - Initial Testing and Bug Fixes

- **Bug Fixes:** `self.lookup_key` → `self.instance_id`, resume position units, `allow_seek=True`
- **Testing:** Set up dev environment, verified core functionality

### Previous Work (upstream/pocketcasts)
- 401d41c8, d638f1db, d6b2b977, a5987e6c - Initial drafting and development

---

## Contact

- Code owner: @ozgav
- Music Assistant: https://github.com/music-assistant/server
- Fork: https://github.com/yfhyou/MAserver
