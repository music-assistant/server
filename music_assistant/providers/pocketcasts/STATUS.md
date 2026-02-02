# Pocketcasts Provider - Development Status

**Last Updated:** 2026-02-01
**Branch:** `pocketcasts`
**Stage:** Beta
**Code Owner:** @ozgav

---

## Overview

Pocketcasts is a podcast streaming service provider for Music Assistant. This provider allows users to access their Pocketcasts library, browse subscribed podcasts, search for new content, and sync playback progress.

---

## Branch Status

**Current State:**
- ✅ Synced with upstream/dev (566 commits ahead of origin)
- ✅ Merged latest dev changes (2026-02-01)
- ✅ No merge conflicts
- ✅ Ready for testing and development

**Git Information:**
- Base branch: `dev`
- Merge commit: `b4f7b6f9`
- Original work: 4 commits (a5987e6c → 401d41c8)

---

## Provider Files

### Core Files
- ✅ `manifest.json` (300 bytes) - Provider metadata and configuration
- ✅ `api_client.py` (9.7 KB, 254 lines) - Custom API client for Pocketcasts
- ✅ `__init__.py` (18.6 KB, 481 lines) - Main provider implementation
- 📝 `STATUS.md` - This file

### Supported Features

**Declared in Provider:**
```python
SUPPORTED_FEATURES = {
    ProviderFeature.LIBRARY_PODCASTS,        # ✅ Implemented
    ProviderFeature.BROWSE,                  # ✅ Implemented
    ProviderFeature.SEARCH,                  # ✅ Implemented
    ProviderFeature.LIBRARY_PODCASTS_EDIT,   # ⚠️  Partially implemented
}
```

---

## Implementation Status

### ✅ Fully Implemented

#### Authentication
- [x] Username/password login via JWT
- [x] Token-based session management
- [x] Config entries for credentials

#### API Client (`api_client.py`)
- [x] `login()` - Authenticate and get JWT token
- [x] `get_subscribed_podcasts()` - Fetch user's podcast library
- [x] `get_podcast_episodes()` - Get episodes via API redirect
- [x] `get_in_progress_episodes()` - Fetch resume positions
- [x] `get_up_next_episodes()` - Fetch Up Next queue
- [x] `get_new_releases()` - Fetch new release episodes
- [x] `get_starred_episodes()` - Fetch starred/favorited episodes
- [x] `get_history()` - Fetch listening history
- [x] `update_episode_progress()` - Sync playback position to server
- [x] `search_podcasts()` - Search for podcasts
- [x] `get_podcast_details()` - Fetch podcast by UUID

#### Provider Implementation (`__init__.py`)
- [x] `handle_async_init()` - Initialize and login
- [x] `unload()` - Cleanup session on shutdown
- [x] `get_library_podcasts()` - Sync user's podcast library
- [x] `get_podcast()` - Get full podcast details
- [x] `get_podcast_episodes()` - Get all episodes for a podcast
- [x] `get_podcast_episode()` - Get single episode with resume position
- [x] `browse()` - Browse podcasts, episodes, and special folders
- [x] `_create_browse_folders()` - Create special browse folders at root
- [x] `_get_special_folder_episodes()` - Fetch episodes for special folders
- [x] `search()` - Search for podcasts
- [x] `get_stream_details()` - Get streaming URLs for episodes
- [x] `get_resume_position()` - Get playback position for episodes

#### Browse Folders (Live Playlists)
- [x] **Up Next** - User's queued episodes
- [x] **New Releases** - Recent episodes from subscriptions
- [x] **In Progress** - Currently listening episodes
- [x] **Starred** - Favorited episodes
- [x] **History** - Recently played episodes

**Implementation Notes:**
- Browse folders appear at root level before subscribed podcasts
- Up Next endpoint returns episodes as dict (UUID keys) vs list format of other endpoints
- Special handling added to extract episode UUIDs from dict keys
- Podcast UUID extraction handles both string format (Up Next) and object format (other endpoints)

#### Data Conversion
- [x] `_convert_podcast()` - Convert API data to Podcast object
- [x] `_convert_episode()` - Convert API data to PodcastEpisode object
- [x] Thumbnail/image handling
- [x] Metadata extraction (title, description, duration)
- [x] Episode numbering and positioning

---

### ⚠️ Partially Implemented

#### Library Management (LIBRARY_PODCASTS_EDIT)
- [ ] **Subscribe to podcast** - Not implemented (add podcast to library)
- [ ] **Unsubscribe from podcast** - Not implemented (remove podcast from library)

#### Playback Progress Sync
- [x] `get_resume_position()` - Read position from Pocketcasts
- [x] `update_episode_progress()` - API method exists
- [ ] **Hook into playback events** - Not connected to player queue controller
- [ ] **Auto-sync on pause/stop** - Not implemented
- [ ] **Mark as played** - Not implemented

#### Authentication & Token Management
- [x] **Login** - Successfully authenticates with email/password
- [x] **Long-lived Token** - Uses mobile/API token (valid ~5 months)
- [ ] **Token Refresh** - Not implemented (low priority - mobile tokens are long-lived)

**Token Details:**
- Current implementation uses `/user/login` without `scope` parameter
- Returns mobile/API token (`"pc:tokenType":"ID"`, `"scopes":["mobile"]`)
- Token valid for ~5 months (vs 1 hour for web player tokens)
- No refresh token needed for this authentication method
- Alternative web player authentication (`scope: "webplayer"`) would require hourly token refresh

---

### ❌ Not Implemented

**Note:** See "Potential Features from Pocketcasts API" section below for detailed mapping of Pocketcasts features to Music Assistant capabilities.

---

## Potential Features from Pocketcasts API

This section maps available Pocketcasts API features to Music Assistant provider features. These are enhancement opportunities for future implementation.

### 1. Starred Episodes → `FAVORITE_PODCASTS_EDIT`
**Pocketcasts Feature:** Starred episodes (user-marked favorites)
**MA Feature:** `ProviderFeature.FAVORITE_PODCASTS_EDIT`
**Implementation:** `set_favorite()` method
**API Endpoints:**
- Get starred: `POST /user/starred`
- Star/unstar: `POST /sync/update_episode_star`

**Priority:** Medium - Useful for marking favorite episodes

---

### 2. Live Updating Playlists → `BROWSE` folders
**Pocketcasts Features:**
- New Releases - Recently published episodes from subscriptions
- In Progress - Episodes currently being listened to
- Starred - User's starred/favorited episodes
- History - Recently played episodes

**MA Feature:** Extend existing `BROWSE` implementation with dynamic folders
**Implementation:** Add these as BrowseFolder items in `browse()` method
**API Endpoints:**
- `/user/new_releases`
- `/user/in_progress`
- `/user/starred`
- `/user/history`

**Priority:** High - Great UX, leverages existing browse infrastructure

---

### 3. Up Next Queue → `PLAYLIST_TRACKS_EDIT` + `PLAYLIST_CREATE`
**Pocketcasts Feature:** User-created queue for next episodes to play
**MA Features:**
- `ProviderFeature.PLAYLIST_TRACKS_EDIT` - Modify playlist contents
- `ProviderFeature.PLAYLIST_CREATE` - Create new playlists

**Implementation:**
- Map "Up Next" to a special playlist
- Implement add/remove/reorder operations

**API Endpoints:**
- Get queue: `POST /up_next/list`
- Add to top: `POST /up_next/play_next`
- Add to end: `POST /up_next/play_last`
- Remove: `POST /up_next/remove`

**Priority:** Medium - Nice-to-have for queue management

---

### 4. Recommendations/Discover → `RECOMMENDATIONS`
**Pocketcasts Features:**
- Featured/Trending Podcasts
- Categories/Genres
- Episode recommendations
- User-based podcast recommendations

**MA Feature:** `ProviderFeature.RECOMMENDATIONS`
**Implementation:** `recommendations()` method returning list of `RecommendationFolder`
**API Endpoints:**
- Featured: `GET https://lists.pocketcasts.com/featured.json`
- Trending: `GET https://lists.pocketcasts.com/trending.json`
- Categories: `GET https://static.pocketcasts.com/discover/json/categories_v2.json`
- Discovery content: `GET https://static.pocketcasts.com/discover/web/content_v3.json`
- Episode recommendations: `POST /discover/recommend_episodes`
- Podcast recommendations: `GET /recommendations/podcast/{podcast_uuid}`
- Social recommendations: `GET /recommendations/social`
- User recommendations: `GET /recommendations/user_podcast`

**Priority:** High - Helps users discover new content

---

### 5. Bookmarks → `BROWSE` folders or custom feature
**Pocketcasts Feature:** Time-stamped bookmarks within episodes
**MA Feature:** Could be implemented as:
- BrowseFolder showing bookmarked episodes with timestamps
- Custom metadata on episodes
- Potentially a playlist of bookmarked positions

**Implementation:** Display in browse section
**API Endpoints:**
- List all bookmarks: `POST /user/bookmark/list`
- Add bookmark: `POST /user/bookmark/add`
- Delete bookmark: `POST /user/bookmark/delete`
- Single episode bookmarks: `POST /user/podcast/episode/bookmarks`
- Multi-episode bookmarks: `POST /user/podcast/episodes/bookmarks`

**Priority:** Low - Niche feature, complex to integrate

---

### 6. Transcripts → `LYRICS` metadata
**Pocketcasts Feature:** AI-generated transcripts (.vtt format) for premium users
**MA Feature:** `ProviderFeature.LYRICS`
**Implementation:** `get_lyrics()` method
**API Endpoints:**
- **Not documented in API reference** - Likely embedded in episode data or requires investigation
- Format: WebVTT (.vtt) file - would need parsing

**Priority:** Low - Premium feature only, requires VTT parsing, endpoint needs discovery

**Notes:**
- Transcripts are premium-only
- Would need to parse VTT format to plain text
- Could provide timestamped lyrics support
- Endpoint not confirmed in unofficial API docs

---

### 7. Filters → Custom implementation
**Pocketcasts Feature:** User-created custom filters (smart playlists)
**Potential Implementation:**
- Could be exposed as BrowseFolder items
- Each filter as a dynamic playlist

**API Endpoints:**
- **Not documented in API reference** - Endpoints need discovery
- May not be available via public API (web/mobile app only?)

**Priority:** Low - Complex feature, unclear MA mapping, endpoints unconfirmed

---

## Implementation Priority Recommendation

Based on user value and implementation complexity:

**Phase 1 - High Value, Medium Effort:**
1. ✅ **RECOMMENDATIONS** - Discover/Featured/Trending
2. ✅ **Live Updating Playlists** - Extend browse with New Releases, In Progress, Starred, History

**Phase 2 - Medium Value, Medium Effort:**
3. **FAVORITE_PODCASTS_EDIT** - Star/unstar episodes
4. **LIBRARY_PODCASTS_EDIT** - Complete subscribe/unsubscribe implementation

**Phase 3 - Medium Value, Higher Effort:**
5. **Up Next Queue** - PLAYLIST features
6. **Playback Progress Sync** - Auto-sync to Pocketcasts

**Phase 4 - Nice-to-Have:**
7. **Transcripts (LYRICS)** - Premium users only
8. **Bookmarks** - Niche use case
9. **Filters** - Complex, unclear benefit

---

## Known Issues

### Fixed Issues ✅
1. ~~**Missing lookup_key attribute**~~ - FIXED: Changed to `self.instance_id`
2. ~~**Podcast sidebar not showing**~~ - RESOLVED: Frontend cache issue
3. ~~**Resume position not working**~~ - FIXED: Two-part fix (milliseconds + allow_seek)

### To Investigate
1. **Image URLs** - Static CDN URLs used, should verify they're always accessible
2. **Episode ID Format** - Using composite ID `podcast_uuid:episode_uuid`, ensure this is compatible with all MA features
3. **Error Handling** - Some methods return empty lists on error instead of raising exceptions
4. **Multi-instance Support** - Manifest has `multi_instance: false`, should this be true?

### Potential Bugs
- [ ] **Episode URL parsing** - Should validate URLs before returning in `get_stream_details()`
- [ ] **Session cleanup** - Verify session is properly closed on errors
- [ ] **Token expiration** - No token refresh mechanism implemented
- [ ] **Rate limiting** - No rate limiting protection
- [ ] **Position Sync to Pocketcasts** - `update_episode_progress()` exists but may not be called during playback

---

## Testing Status

### Manual Testing Results (2026-02-01 to 2026-02-02)

**Testing Environment:**
- Music Assistant: version 0.0.0 (dev branch)
- Fresh database setup
- Pocketcasts account with 2 subscribed podcasts

**Test Results:**

✅ **Working Features:**
- [x] Login with valid credentials - SUCCESS
- [x] Login error handling - Invalid credentials show 401 error (2026-02-02)
- [x] Browse podcast library - Podcasts display correctly
- [x] Episode lists - Episodes load and display properly
- [x] Play an episode - Playback works
- [x] Podcast sidebar - Shows in UI (after frontend cache clear)
- [x] Podcast images - Thumbnails load correctly
- [x] Resume playback from saved position - SUCCESS (after Bug #3 fixes)
- [x] Browse folders - All 5 special folders working (2026-02-02)
  - [x] Up Next - Shows queued episodes
  - [x] New Releases - Shows recent episodes from subscriptions
  - [x] In Progress - Shows episodes being listened to
  - [x] Starred - Shows favorited episodes
  - [x] History - Shows recently played episodes

⚠️ **Partially Working:**
- [ ] Sync progress back to Pocketcasts - Not tested yet

❌ **Not Tested:**
- [ ] Search for podcasts
- [ ] Test with large libraries (100+ podcasts)
- [ ] Test with podcasts that have many episodes (500+)
- [ ] Test episode image fallbacks

### Bugs Found During Testing

#### Bug #1: Missing `lookup_key` attribute ✅ FIXED
- **Discovered:** 2026-02-01 during initial testing
- **Symptom:** Episodes failed to convert with error: `'PocketCastsProvider' object has no attribute 'lookup_key'`
- **Location:** Lines 169, 174, 215 in `__init__.py`
- **Root Cause:** Code used `self.lookup_key` which doesn't exist on Provider base class
- **Fix:** Changed `self.lookup_key` → `self.instance_id` (correct attribute)
- **Status:** ✅ Fixed and tested
- **Commit:** Pending

#### Bug #2: No Podcast option in UI sidebar ✅ RESOLVED
- **Discovered:** 2026-02-01 during UI testing
- **Symptom:** Podcasts didn't appear in main sidebar navigation
- **Root Cause:** Frontend cache issue
- **Fix:** Hard refresh of browser (Ctrl+Shift+R)
- **Status:** ✅ Resolved - not a provider issue
- **Notes:** Podcasts ARE syncing correctly to database

#### Bug #3: Resume position not working ✅ FIXED
- **Discovered:** 2026-02-01 during playback testing
- **Symptom:** Episodes always start from 0:00 instead of last played position
- **Root Causes:** Two separate issues found through debugging:
  1. **Wrong units:** `get_resume_position()` returned seconds instead of milliseconds
  2. **Missing flag:** `get_stream_details()` didn't set `allow_seek=True`
- **Investigation Process:**
  1. Confirmed `get_resume_position()` was being called
  2. Confirmed API was returning correct position (194 seconds)
  3. Discovered return value was in seconds, but MA expects milliseconds
  4. Fixed units, but audio still started at 0:00
  5. UI showed correct position (3:14) but audio didn't seek
  6. Traced through code: player_queues → streams → audio.py → ffmpeg
  7. Found ffmpeg only applies `-ss` seek if `allow_seek=True`
  8. Provider was only setting `can_seek=True` (different flag)
- **Fix Applied:**
  - **Part 1:** Line 478 - Changed return to `played_up_to * 1000` (milliseconds)
  - **Part 2:** Line 360 - Added `allow_seek=True` to StreamDetails
- **Status:** ✅ Fixed and tested
- **Commit:** Pending
- **Learned:**
  - `can_seek` = Stream format supports seeking (informational)
  - `allow_seek` = Permission for ffmpeg to apply `-ss` parameter (required!)

### Unit Tests
- [ ] No unit tests exist yet
- [ ] Should add tests for API client methods
- [ ] Should add tests for data conversion methods
- [ ] Should add tests for error handling

### Integration Tests
- [ ] No integration tests exist yet
- [ ] Should test with actual Pocketcasts API (mock or real)

---

## Required Dependencies

**Python Packages:**
- `aiohttp` - Already a Music Assistant dependency
- No additional packages required ✅

**External Services:**
- Pocketcasts account with active subscription
- Internet connectivity for API access

---

## API Endpoints

Reference: [Unofficial Pocketcasts API Documentation](https://github.com/yfhyou/api_pocketcasts/blob/main/reference/endpoints.md)

### Currently Implemented ✅

#### Authentication (api.pocketcasts.com)
- `POST /user/login` - Login with email/password (returns JWT token)

#### Podcasts (api.pocketcasts.com)
- `POST /user/podcast/list` - Get user's subscribed podcasts
- `POST /discover/search` - Search for podcasts

#### Podcasts (podcast-api.pocketcasts.com)
- `GET /podcast/full/{uuid}` - Get podcast details and episodes (redirects to static JSON)

#### Episodes (api.pocketcasts.com)
- `POST /user/in_progress` - Get in-progress episodes with resume positions
- `POST /sync/update_episode` - Update episode playback progress and status

#### Assets (static.pocketcasts.com)
- `GET /discover/images/280/{uuid}.jpg` - Podcast thumbnails (280x280)
- `GET /discover/images/metadata/{image_id}.json` - Image metadata

---

### Available but Not Yet Implemented

#### Authentication & Account (api.pocketcasts.com)
- `POST /user/login_pocket_casts` - Alternative login endpoint
- `POST /user/token` - Token refresh (for web player tokens only, not needed for mobile tokens)
- `GET /subscription/status` - Check premium subscription status

#### Library Management (api.pocketcasts.com)
- `POST /user/podcast/subscribe` - Subscribe to a podcast
- `POST /user/podcast/unsubscribe` - Unsubscribe from a podcast
- `POST /user/episode` - Episode-level management

#### Up Next Queue (api.pocketcasts.com)
- `POST /up_next/list` - Get Up Next queue
- `POST /up_next/play_next` - Add episode to top of queue
- `POST /up_next/play_last` - Add episode to end of queue
- `POST /up_next/remove` - Remove episode from queue

#### Live Playlists (api.pocketcasts.com)
- `POST /user/new_releases` - Recently published episodes from subscriptions
- `POST /user/starred` - User's starred/favorited episodes
- `POST /user/history` - Recently played episodes

#### Favorites/Starred (api.pocketcasts.com)
- `POST /sync/update_episode_star` - Toggle episode star status (star/unstar)
- `POST /sync/update_episodes_archive` - Archive/unarchive episodes

#### Bookmarks (api.pocketcasts.com)
- `POST /user/bookmark/list` - List all user bookmarks
- `POST /user/bookmark/add` - Create a new bookmark at timestamp
- `POST /user/bookmark/delete` - Delete a bookmark
- `POST /user/podcast/episode/bookmarks` - Get bookmarks for single episode
- `POST /user/podcast/episodes/bookmarks` - Get bookmarks for multiple episodes

#### Recommendations (api.pocketcasts.com)
- `POST /discover/recommend_episodes` - Get recommended episodes
- `GET /recommendations/podcast/{podcast_uuid}` - Podcast-specific recommendations
- `GET /recommendations/social` - Social recommendations
- `GET /recommendations/user_podcast` - User-based podcast recommendations

#### Discovery (lists.pocketcasts.com)
- `GET /featured.json` - Featured podcasts
- `GET /trending.json` - Trending podcasts
- `GET /{uuid}.json` - Specific list by UUID

#### Discovery (static.pocketcasts.com)
- `GET /discover/json/categories_v2.json` - Podcast categories
- `GET /discover/web/content_v3.json` - Discovery content

#### Show Notes (shownotes.pocketcasts.com)
- `POST /show_notes/{podcast_uuid}/episodes_{timestamp}.json` - Episode show notes

#### Episodes (podcasts.pocketcasts.com)
- `GET /{podcast_uuid}/episodes_full_{timestamp}.json` - Full episode list with metadata

#### Stats & History (api.pocketcasts.com)
- `POST /user/stats/add` - Record listening statistics
- `POST /history/do` - Record history event

---

## Next Steps

See "Potential Features from Pocketcasts API" section above for detailed feature mapping and implementation priorities.

### Immediate Priority (Core Functionality)
1. ✅ **Test basic functionality** - DONE: Login, browse, playback all working
2. ✅ **Test login error handling** - DONE: Invalid credentials show 401 error
3. **Add error handling** - Improve error messages and exception handling for API calls
4. **Add unit tests** - Test core functionality (API client, data conversion)

### Phase 1: Discovery & Browse Enhancement (High Value)
5. **Implement RECOMMENDATIONS** - Featured/trending podcasts, categories
6. ✅ **Extend BROWSE** - DONE: Added 5 live playlists (Up Next, New Releases, In Progress, Starred, History)
7. **Test search functionality** - Verify search works as expected

### Phase 2: Library Management (Medium Priority)
8. **Complete LIBRARY_PODCASTS_EDIT** - Subscribe/unsubscribe to podcasts
9. **Implement FAVORITE_PODCASTS_EDIT** - Star/unstar episodes

### Phase 3: Sync & Polish (Medium Priority)
10. **Connect progress sync** - Hook into player queue events to auto-sync position to Pocketcasts
11. **Optimize API calls** - Cache podcast lists, reduce redundant calls

### Phase 4: Advanced Features (Nice to Have)
12. **Up Next Queue** - PLAYLIST_TRACKS_EDIT/PLAYLIST_CREATE features
13. **Token refresh mechanism** - Low priority (current mobile tokens valid ~5 months)
    - Would allow graceful handling of token expiration
    - Could enable web player token support (requires hourly refresh)
    - Not urgent: users typically restart MA or update config before expiry
14. **Transcripts as LYRICS** - For premium users (VTT parsing required)
15. **Bookmarks** - Show time-stamped bookmarks in browse
16. **Multi-instance support** - Allow multiple Pocketcasts accounts

---

## Configuration

**Required Settings:**
- `username` (email) - Pocketcasts account email
- `password` - Pocketcasts account password

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

### Code Documentation
- Most methods have docstrings
- Type hints are used throughout
- Follows Music Assistant code style (Sphinx-style docstrings)

---

## Contributing

### Before Submitting PR
- [ ] Run `pre-commit run --all-files`
- [ ] Run `pytest` to ensure tests pass
- [ ] Test basic functionality manually
- [ ] Update this STATUS.md file
- [ ] Add any new features to the "Implemented" section
- [ ] Document any known issues

### PR Checklist
- [ ] Code follows Music Assistant style guidelines
- [ ] Docstrings use Sphinx-style (`:param:` format)
- [ ] Type hints are complete
- [ ] Error handling is appropriate
- [ ] Logging uses appropriate levels
- [ ] No sensitive data (tokens, passwords) in logs

---

## Notes

- **API Documentation:** Pocketcasts doesn't have official public API documentation. This implementation is based on reverse-engineering the web/mobile app API.
- **API Stability:** Since the API is undocumented, endpoints may change without notice.
- **Authentication:** Uses JWT bearer tokens. Token expiration handling should be added.
- **Episode IDs:** Using composite format `podcast_uuid:episode_uuid` to maintain uniqueness across the provider.
- **Resume Positions:** Pocketcasts stores positions on their server, which is synced via the API.

---

## Changelog

### 2026-02-02 - Browse Folders & Token Documentation

- **New Feature: Browse Folders (Live Playlists)**
  - Implemented 5 special browse folders at root level:
    - **Up Next** - User's queued episodes
    - **New Releases** - Recent episodes from subscriptions
    - **In Progress** - Episodes being listened to
    - **Starred** - Favorited episodes
    - **History** - Recently played episodes

- **API Client Additions:**
  - Added `get_up_next_episodes()` - POST `/up_next/list`
  - Added `get_new_releases()` - POST `/user/new_releases`
  - Added `get_starred_episodes()` - POST `/user/starred`
  - Added `get_history()` - POST `/user/history`
  - Note: `get_in_progress_episodes()` already existed

- **Provider Implementation:**
  - Added `_create_browse_folders()` helper method
  - Added `_get_special_folder_episodes()` to handle folder-specific API calls
  - Updated `browse()` to show browse folders at root level
  - **Special Handling for Up Next:**
    - Up Next endpoint returns episodes as dict (UUID keys) instead of list
    - Modified iteration logic to handle both dict and list formats
    - Extract episode UUID from dict key when missing from data
    - Handle podcast field as both string (Up Next) and object (other endpoints)

- **Testing:**
  - Tested all 5 browse folders successfully
  - Verified episode display and playback from folders
  - Confirmed login error handling shows 401 for invalid credentials

- **Documentation:**
  - Documented token authentication behavior (mobile vs web player tokens)
  - Mobile tokens valid ~5 months, no refresh needed
  - Moved token refresh to Phase 4 (low priority)
  - Updated API endpoints with all discovered endpoints from unofficial API
  - Added comprehensive feature mapping (7 potential features documented)

### 2026-02-01 - Testing and Bug Fixes
- **Branch Management:**
  - Synced pocketcasts branch from upstream
  - Rebased to create clean PR branch (4 commits only)
  - Simplified to single clean branch strategy
  - Pushed to fork: github.com/yfhyou/MAserver

- **Bug Fixes:**
  - **Bug #1:** Changed `self.lookup_key` → `self.instance_id` (3 occurrences)
    - Lines affected: 169, 174, 215 in `__init__.py`
  - **Bug #3 (Part 1):** Fixed resume position units (seconds → milliseconds)
    - Line 478: Return `played_up_to * 1000` instead of `played_up_to`
    - Updated docstring to document milliseconds return value
  - **Bug #3 (Part 2):** Added missing `allow_seek=True` flag
    - Line 360: Added `allow_seek=True` to StreamDetails
    - Enables ffmpeg to apply `-ss` seek parameter for resume playback

- **Testing:**
  - Set up fresh development environment
  - Tested provider with real Pocketcasts account
  - Documented and fixed 3 bugs (all resolved!)
  - Verified resume playback works correctly (starts at 3:14 for 194 second position)
  - Updated STATUS.md with comprehensive test results

- **Documentation:**
  - Created STATUS.md to track development progress (340+ lines)
  - Documented known issues and testing checklist
  - Added API endpoints reference
  - Added contributing guidelines
  - Documented all bug findings and fixes

### Previous Work (upstream/pocketcasts)
- **401d41c8** - More drafting
- **d638f1db** - more drafting
- **d6b2b977** - continue development
- **a5987e6c** - initial drafting

---

## Contact

For questions or issues with this provider:
- Code owner: @ozgav
- Music Assistant: https://github.com/music-assistant/server
- Fork: https://github.com/yfhyou/MAserver
