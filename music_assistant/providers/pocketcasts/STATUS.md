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
- ✅ `api_client.py` (~500 lines) - Custom API client for Pocketcasts
- ✅ `__init__.py` (~890 lines) - Main provider implementation
- 📝 `STATUS.md` - This file

### Supported Features

**Declared in Provider:**
```python
SUPPORTED_FEATURES = {
    ProviderFeature.LIBRARY_PODCASTS,        # ✅ Implemented
    ProviderFeature.BROWSE,                  # ✅ Implemented
    ProviderFeature.SEARCH,                  # ✅ Implemented
    ProviderFeature.LIBRARY_PODCASTS_EDIT,   # ✅ Implemented
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
- [x] `update_episode_progress()` - Sync playback position to server (status=2)
- [x] `mark_episode_played()` - Mark episode as played (status=3)
- [x] `mark_episode_unplayed()` - Reset episode to unplayed (status=1, position=0)
- [x] `archive_episode()` - Archive/unarchive episodes
- [x] `remove_from_up_next()` - Remove episode from Up Next queue
- [x] `play_now()` - Add episode to Up Next at "play now" position when starting playback
- [x] `get_episode_details()` - Fetch accurate duration and playback status from `/user/episode`
- [x] `search_podcasts()` - Search for podcasts
- [x] `get_podcast_details()` - Fetch podcast by UUID
- [x] `subscribe_podcast()` - Subscribe to a podcast
- [x] `unsubscribe_podcast()` - Unsubscribe from a podcast

#### Provider Implementation (`__init__.py`)
- [x] `handle_async_init()` - Initialize and login
- [x] `unload()` - Cleanup session on shutdown
- [x] `get_library_podcasts()` - Sync user's podcast library
- [x] `library_add()` - Subscribe to a podcast
- [x] `library_remove()` - Unsubscribe from a podcast
- [x] `get_podcast()` - Get full podcast details
- [x] `get_podcast_episodes()` - Get all episodes for a podcast
- [x] `get_podcast_episode()` - Get single episode with resume position
- [x] `browse()` - Browse podcasts, episodes, and special folders
- [x] `_create_browse_folders()` - Create special browse folders at root
- [x] `_get_special_folder_episodes()` - Fetch episodes for special folders
- [x] `search()` - Search for podcasts
- [x] `get_stream_details()` - Get streaming URLs for episodes
- [x] `get_resume_position()` - Get playback position for episodes
- [x] `on_played()` - Sync playback progress, mark played, archive on completion

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

#### Library Management (LIBRARY_PODCASTS_EDIT)
- [x] **`library_add()` method** - Subscribe to podcast via API
  - Called when user adds podcast to library
  - API: `POST /user/podcast/subscribe`
  - Parameters: `{"uuid": podcast_uuid}`
- [x] **`library_remove()` method** - Unsubscribe from podcast via API
  - Called when user removes podcast from library
  - API: `POST /user/podcast/unsubscribe`
  - Parameters: `{"uuid": podcast_uuid}`

**Status:** ✅ Fully implemented - Syncs library changes back to Pocketcasts

**API Behavior Notes:**
- API validates UUID format but not existence
- Malformed UUIDs → 400 error (correctly rejected)
- Well-formed but non-existent UUIDs → 200 success (silently accepted)
- In practice, not a concern: UUIDs come from Pocketcasts search/discovery APIs
- Implementation trusts API status codes (200 = success)

---

### ✅ Implemented

#### Playback Progress Sync
- [x] `get_resume_position()` - Read position from Pocketcasts
- [x] `update_episode_progress()` - API method (fixed to match web player format)
- [x] `mark_episode_played()` - Mark episode as played (status=3)
- [x] `mark_episode_unplayed()` - Mark episode as unplayed (status=1, position=0)
- [x] `archive_episode()` - Archive/unarchive completed episodes
- [x] `remove_from_up_next()` - Remove episode from Up Next on completion
- [x] `get_episode_details()` - Fetch real duration and status from `/user/episode`
- [x] `on_played()` callback - Hook into MA playback events
- [x] Episode status on refresh - Fetches in-progress and history data

**Sync Behavior:**
- Progress syncs every 30 seconds during playback (MA's callback interval)
- Completion detected using **real duration** from `/user/episode` endpoint, not MA's duration
- When `position >= real_duration - 45`: sync to end → mark played → remove from up next → archive
  - 45-second threshold ensures last callback before episode end always triggers completion
- Starting playback: episode added to Up Next via `/up_next/play_now`
- Replaying a played episode: unarchives it and resets status to in-progress (status=2)
- Mark as unplayed: resets position to 0 + unarchives
- Episode list refresh shows in-progress and played status from Pocketcasts
- Uses correct web player API format (uuid, status codes 1/2/3, position as string)
- MA's `fully_played` flag is intentionally ignored (MA uses wrong/shorter duration from static API)

**Duration Discrepancy:**
- The static API (`podcast-api.pocketcasts.com/podcast/full/{uuid}`) often returns a **shorter** duration than the actual episode length
- The `/user/episode` endpoint returns the **correct** duration
- `on_played()` fetches the real duration each sync to determine actual completion
- The seek bar in MA may show the wrong end time (known limitation, cosmetic only)

**Episode Completion Sequence (matches web player):**
1. `update_episode` with status=2, position=real_duration (sync to end)
2. `update_episode` with status=3 (mark played)
3. `up_next/remove` (remove from Up Next queue)
4. `update_episodes_archive` with archive=true (archive episode)

**API Status Codes:**
| Status | Meaning |
|--------|---------|
| 1 | Unplayed |
| 2 | In Progress |
| 3 | Played |

---

### ⚠️ Partially Implemented

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

### 1. Starred Episodes → ~~`FAVORITE_PODCASTS_EDIT`~~ Not Applicable
**Pocketcasts Feature:** Starred episodes (individual episode favorites)
**MA Feature:** ❌ Not applicable - `FAVORITE_PODCASTS_EDIT` is for podcast-level favorites
**API Endpoints:**
- Get starred: `POST /user/starred`
- Star/unstar: `POST /sync/update_episode_star`

**Status:** ✅ Already handled via "Starred" browse folder
- Starred episodes are browsable via the special "Starred" browse folder
- Episode starring is not a library management feature in MA
- MA doesn't have a `FAVORITE_PODCAST_EPISODES_EDIT` feature
- `FAVORITE_PODCASTS_EDIT` would be for marking entire podcasts as favorites (which Pocketcasts doesn't support)

**Priority:** N/A - Already implemented as browse folder

---

### 2. Live Updating Playlists → `BROWSE` folders ✅ IMPLEMENTED
**Pocketcasts Features:**
- New Releases - Recently published episodes from subscriptions
- In Progress - Episodes currently being listened to
- Starred - User's starred/favorited episodes
- History - Recently played episodes
- Up Next - User's queued episodes

**Status:** ✅ Fully implemented as browse folders
- All 5 special folders appear at root level in browse view
- Implemented in `_create_browse_folders()` and `_get_special_folder_episodes()`
- API Endpoints used:
  - `/user/new_releases`
  - `/user/in_progress`
  - `/user/starred`
  - `/user/history`
  - `/up_next/list`

**Priority:** N/A - Already implemented

---

### 3. Up Next Queue → Multiple Implementation Options
**Pocketcasts Feature:** User-created queue for next episodes to play

#### Option A: Plugin Source (Recommended)
**Architecture:** Create a separate `pocketcasts_queue_source` plugin provider
**MA Feature:** `ProviderFeature.AUDIO_SOURCE` (PluginProvider)
**UI Integration:** Appears in player's "Select source" dropdown alongside "Music Assistant Queue"

**Implementation Approach:**
- Create new plugin provider that inherits from `PluginProvider`
- Returns `PluginSource` with name "Pocketcasts Up Next"
- When selected: Streams episodes from Pocketcasts Up Next queue in order
- Separate from MA's library queue (key requirement)
- Can reuse API client from existing Pocketcasts music provider

**Benefits:**
- Clean separation: Pocketcasts queue completely independent from MA queue
- Follows MA architecture patterns (similar to Spotify Connect)
- User explicitly switches between queue sources
- Queue management syncs directly to Pocketcasts API

**Implementation Requirements:**
1. Create plugin provider with `AUDIO_SOURCE` feature
2. Implement `get_source()` returning `PluginSource` object
3. Implement audio streaming for podcast episodes
4. Add callbacks for next/previous/play/pause controls
5. Integrate with Pocketcasts Up Next API endpoints
6. Handle queue synchronization

**Reference Implementations:**
- `/music_assistant/providers/spotify_connect/__init__.py` - Complete plugin source example
- `/music_assistant/providers/_demo_plugin_provider/__init__.py` - Simple template

#### Option B: Playlist Provider
**MA Features:**
- `ProviderFeature.PLAYLIST_TRACKS_EDIT` - Modify playlist contents
- `ProviderFeature.PLAYLIST_CREATE` - Create new playlists

**Implementation:**
- Map "Up Next" to a special playlist in library
- Implement add/remove/reorder operations
- Would NOT keep queues separate (mixed with MA library)

**API Endpoints:**
- Get queue: `POST /up_next/list`
- Add to top: `POST /up_next/play_next`
- Add to end: `POST /up_next/play_last`
- Remove: `POST /up_next/remove`

**Priority:** Medium - Nice-to-have, but Option A provides better UX and separation

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

**Phase 2 - Core Library Management (High Priority):**
3. ✅ **LIBRARY_PODCASTS_EDIT** - Complete subscribe/unsubscribe implementation
   - Essential for syncing library changes back to Pocketcasts
   - Allows users to manage subscriptions from MA

**Phase 3 - Medium Value, Higher Effort:**
4. **Up Next Queue** - Plugin source provider (Option A recommended)
   - Create separate plugin provider for "Pocketcasts Up Next" queue source
   - Appears in player "Select source" dropdown
   - Keeps Pocketcasts queue separate from MA queue
   - Complex: requires new plugin provider + audio streaming
5. ✅ **Playback Progress Sync** - Auto-sync to Pocketcasts (completed 2026-02-05)

**Phase 4 - Nice-to-Have:**
6. **Transcripts (LYRICS)** - Premium users only
7. **Bookmarks** - Niche use case
8. **Filters** - Complex, unclear benefit

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
- [ ] **Duration display in seek bar** - MA shows wrong (shorter) duration from static API; playback works correctly but seek bar end time is inaccurate

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
- [x] Subscribe to podcast - SUCCESS (2026-02-03)
  - Syncs to Pocketcasts account
  - API validates UUID format (rejects malformed UUIDs with 400)
  - API accepts well-formed UUIDs (200) even if podcast doesn't exist
- [x] Unsubscribe from podcast - SUCCESS (2026-02-03)
  - Syncs removal to Pocketcasts account
  - MA shows generic warning (not customizable per provider)

- [x] Sync progress back to Pocketcasts - SUCCESS (2026-02-04/05)
  - Progress syncs every 30 seconds during playback
  - Episode marked as played when within 15 seconds of real end
  - Completion sequence: sync to end → mark played → remove from up next → archive
  - Replaying played episodes correctly unarchives and resets status
  - Continues playing past the "wrong" (shorter) duration without confusion
- [x] Episode details from `/user/episode` - Correct duration and status (2026-02-04)

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
- `POST /user/podcast/subscribe` - Subscribe to a podcast
- `POST /user/podcast/unsubscribe` - Unsubscribe from a podcast
- `POST /discover/search` - Search for podcasts

#### Podcasts (podcast-api.pocketcasts.com)
- `GET /podcast/full/{uuid}` - Get podcast details and episodes (redirects to static JSON)

#### Episodes (api.pocketcasts.com)
- `POST /user/in_progress` - Get in-progress episodes with resume positions
- `POST /user/episode` - Get episode details (correct duration, playback status, resume position)
- `POST /user/history` - Get listening history
- `POST /user/new_releases` - Recently published episodes from subscriptions
- `POST /user/starred` - User's starred/favorited episodes
- `POST /sync/update_episode` - Update episode playback progress and status (status 1/2/3)
- `POST /sync/update_episodes_archive` - Archive/unarchive episodes

#### Up Next Queue (api.pocketcasts.com)
- `POST /up_next/list` - Get Up Next queue
- `POST /up_next/play_now` - Add episode to Up Next at "play now" position (top)
- `POST /up_next/remove` - Remove episode from Up Next queue

#### Assets (static.pocketcasts.com)
- `GET /discover/images/280/{uuid}.jpg` - Podcast thumbnails (280x280)
- `GET /discover/images/metadata/{image_id}.json` - Image metadata

---

### Available but Not Yet Implemented

#### Authentication & Account (api.pocketcasts.com)
- `POST /user/login_pocket_casts` - Alternative login endpoint
- `POST /user/token` - Token refresh (for web player tokens only, not needed for mobile tokens)
- `GET /subscription/status` - Check premium subscription status

#### Up Next Queue Management (api.pocketcasts.com)
- `POST /up_next/play_next` - Add episode to top of queue
- `POST /up_next/play_last` - Add episode to end of queue

#### Favorites/Starred (api.pocketcasts.com)
- `POST /sync/update_episode_star` - Toggle episode star status (star/unstar)

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

### Phase 2: Library Management (High Priority)
8. ✅ **Complete LIBRARY_PODCASTS_EDIT** - Subscribe/unsubscribe to podcasts
   - ✅ Implement `library_add()` method for subscribing
   - ✅ Implement `library_remove()` method for unsubscribing

### Phase 3: Sync & Polish (Medium Priority)
10. ✅ **Connect progress sync** - Hook into player queue events to auto-sync position to Pocketcasts
   - ✅ Implement `on_played()` callback with 30-second throttled sync
   - ✅ Add mark_episode_played/unplayed API methods
   - ✅ Add archive_episode and remove_from_up_next API methods
   - ✅ Add get_episode_details for real duration detection
   - ✅ Completion detection uses real duration from `/user/episode`, not MA's duration
   - ✅ Replaying played episodes unarchives and resets status
11. **Optimize API calls** - Cache podcast lists, reduce redundant calls

### Phase 4: Advanced Features (Nice to Have)
12. **Up Next Queue Source** - Create plugin provider for separate Pocketcasts queue
   - Implement as `PluginProvider` with `AUDIO_SOURCE` feature
   - Appears in player "Select source" dropdown as "Pocketcasts Up Next"
   - Keeps Pocketcasts queue separate from MA queue (not mixed)
   - See "Potential Features" section for detailed architecture notes
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

### 2026-02-09 - Up Next Integration & Completion Threshold Fix

- **New Feature: Add episode to Up Next when starting playback**
  - Added `play_now()` API method - calls `/up_next/play_now`
  - Called in `get_stream_details()` when MA requests stream URL
  - Matches web player behavior: episode appears in Pocketcasts Up Next queue

- **Bug Fix: Episode completion threshold increased from 15 to 45 seconds**
  - Problem: 15-second threshold could miss completion if last callback landed 16+ seconds before end
  - MA calls `on_played` every 30 seconds, so last callback can be up to 29 seconds before end
  - 45-second threshold guarantees the last callback always triggers completion

- **Removed provider-side throttle**
  - MA already controls callback cadence (every 30 seconds + on state changes)
  - Provider throttle was blocking important state-change callbacks (pause, end of track)

### 2026-02-04/05 - Playback Progress Sync (Major Rework)

- **Reworked playback progress sync with real duration detection**
  - Progress now syncs every 30 seconds (changed from 10s to match web player)
  - Episode completion uses **real duration** from `/user/episode` endpoint, not MA's duration
  - MA's `fully_played` flag is intentionally ignored (MA uses wrong/shorter duration from static API)
  - Completion triggers when `position >= real_duration - 15` seconds
  - Replaying a played episode unarchives it and resets status from 3 (played) to 2 (in-progress)
  - Removed incorrect "skip if already played" logic that blocked progress sync

- **New API Client Methods:**
  - Added `get_episode_details()` - Fetches correct duration, playback status, and resume position from `/user/episode`
  - Added `remove_from_up_next()` - Removes episode from Up Next queue via `/up_next/remove`

- **Episode Completion Sequence (matches web player behavior):**
  1. `update_episode` with status=2, position=real_duration (sync position to end)
  2. `update_episode` with status=3 (mark as played)
  3. `up_next/remove` (remove from Up Next queue)
  4. `update_episodes_archive` with archive=true (archive episode)

- **Duration Discrepancy Discovery:**
  - Static API (`podcast-api.pocketcasts.com`) returns wrong (shorter) duration for many episodes
  - `/user/episode` endpoint returns the correct duration
  - MA seek bar shows wrong end time (cosmetic, playback unaffected)
  - Episode plays to actual end regardless of displayed duration

- **Bugs Fixed:**
  - Fixed "skip if status=3" logic that prevented progress sync on replayed episodes
  - Fixed position being overwritten after mark-as-played sequence
  - Fixed episode marked as played too early (at wrong/shorter duration instead of actual end)

### 2026-02-03 - Playback Progress Sync (Initial)

- **New Feature: Initial playback progress sync to Pocketcasts**
  - Implemented `on_played()` callback to hook into MA playback events
  - Fixed `update_episode_progress()` to match web player API format
  - Added `mark_episode_played()` for completed episodes (status=3)
  - Added `mark_episode_unplayed()` for resetting episodes (status=1)
  - Added `archive_episode()` for archiving/unarchiving completed episodes
  - Completed episodes are automatically marked as played and archived
  - "Mark as unplayed" resets position and unarchives the episode

- **API Format Corrections:**
  - Fixed status codes: 1=unplayed, 2=in_progress, 3=played
  - Changed `episode` field to `uuid` to match web player
  - Position now sent as string instead of integer
  - Removed unnecessary `duration` field from progress updates

### 2026-02-03 - Library Management (Subscribe/Unsubscribe)

- **New Feature: Complete LIBRARY_PODCASTS_EDIT implementation**
  - Implemented `library_add()` method for subscribing to podcasts
  - Implemented `library_remove()` method for unsubscribing from podcasts
  - Library changes now sync back to Pocketcasts

- **API Client Additions:**
  - Added `subscribe_podcast()` - POST `/user/podcast/subscribe`
  - Added `unsubscribe_podcast()` - POST `/user/podcast/unsubscribe`

- **Provider Implementation:**
  - Added `library_add()` - Handles podcast subscription via API
  - Added `library_remove()` - Handles podcast unsubscription via API
  - Both methods delegate to base class for non-podcast media types

- **Testing:**
  - Tested subscribe/unsubscribe functionality - both working correctly
  - Verified API validates UUID format (rejects malformed UUIDs with 400)
  - Discovered API accepts well-formed but non-existent UUIDs (returns 200)
  - Not a practical concern: UUIDs come from Pocketcasts' own APIs in normal usage

- **Documentation:**
  - Clarified difference between `LIBRARY_PODCASTS_EDIT` (subscribe/unsubscribe) and `FAVORITE_PODCASTS_EDIT` (not applicable)
  - Updated STATUS.md to mark LIBRARY_PODCASTS_EDIT as fully implemented
  - Removed incorrect reference to FAVORITE_PODCASTS_EDIT feature
  - Documented API behavior regarding UUID validation

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
