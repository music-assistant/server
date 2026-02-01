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
- [x] `browse()` - Browse podcasts and episodes
- [x] `search()` - Search for podcasts
- [x] `get_stream_details()` - Get streaming URLs for episodes
- [x] `get_resume_position()` - Get playback position for episodes

#### Data Conversion
- [x] `_convert_podcast()` - Convert API data to Podcast object
- [x] `_convert_episode()` - Convert API data to PodcastEpisode object
- [x] Thumbnail/image handling
- [x] Metadata extraction (title, description, duration)
- [x] Episode numbering and positioning

---

### ⚠️ Partially Implemented

#### Library Management (LIBRARY_PODCASTS_EDIT)
- [ ] **Subscribe to podcast** - Not implemented
- [ ] **Unsubscribe from podcast** - Not implemented
- [ ] **Add podcast to library** - Not implemented
- [ ] **Remove podcast from library** - Not implemented

#### Playback Progress Sync
- [x] `get_resume_position()` - Read position from Pocketcasts
- [x] `update_episode_progress()` - API method exists
- [ ] **Hook into playback events** - Not connected to player queue controller
- [ ] **Auto-sync on pause/stop** - Not implemented
- [ ] **Mark as played** - Not implemented

---

### ❌ Not Implemented

#### Advanced Features
- [ ] **Favorites/Starred Episodes** - Not implemented
- [ ] **Up Next Queue** - Pocketcasts has a queue feature
- [ ] **Filters** - Pocketcasts supports custom filters
- [ ] **Episode Notes/Show Notes** - Basic implementation exists but not fully featured
- [ ] **Download Management** - Not applicable (streaming only)
- [ ] **Playback Speed** - Should be handled by player
- [ ] **Chapter Support** - Not implemented
- [ ] **Sleep Timer** - Should be handled by player

#### Browse Features
- [ ] **Featured/Trending Podcasts** - API supports this
- [ ] **Categories/Genres** - Pocketcasts has categories
- [ ] **New Releases** - API may support this
- [ ] **Recommendations** - Not implemented

#### Episode Filtering
- [ ] **Filter by played/unplayed** - Not implemented
- [ ] **Filter by downloaded** - Not applicable
- [ ] **Sort options** - Uses default API ordering

---

## Known Issues

### Fixed Issues ✅
1. ~~**Missing lookup_key attribute**~~ - FIXED: Changed to `self.instance_id`
2. ~~**Podcast sidebar not showing**~~ - RESOLVED: Frontend cache issue

### Active Investigation ⚠️
1. **Resume Position Not Working** - See Bug #3 in Testing Status
   - `get_resume_position()` is called but returns (False, 0)
   - Episode UUID matching may be failing
   - Need to verify if MA's local position tracking is working

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

### Manual Testing Results (2026-02-01)

**Testing Environment:**
- Music Assistant: version 0.0.0 (dev branch)
- Fresh database setup
- Pocketcasts account with 2 subscribed podcasts

**Test Results:**

✅ **Working Features:**
- [x] Login with valid credentials - SUCCESS
- [x] Browse podcast library - Podcasts display correctly
- [x] Episode lists - Episodes load and display properly
- [x] Play an episode - Playback works
- [x] Podcast sidebar - Shows in UI (after frontend cache clear)
- [x] Podcast images - Thumbnails load correctly

⚠️ **Partially Working:**
- [ ] Resume playback from saved position - Needs investigation (see Bug #3)
- [ ] Sync progress back to Pocketcasts - Not tested yet

❌ **Not Tested:**
- [ ] Login with invalid credentials (error handling)
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

#### Bug #3: Resume position not working ⚠️ INVESTIGATING
- **Discovered:** 2026-02-01 during playback testing
- **Symptom:** Episodes always start from 0:00 instead of last played position
- **What Works:**
  - `get_resume_position()` IS being called by Music Assistant
  - API successfully fetches in-progress episodes (2 found)
  - No errors or exceptions thrown
- **What Doesn't Work:**
  - Episode UUID matching fails (episode not found in in-progress list)
  - Method returns `(False, 0)` - no resume position
- **Possible Causes:**
  1. Episode hasn't been played in Pocketcasts yet (not in in-progress list)
  2. Episode UUID mismatch between MA storage and Pocketcasts API
  3. Music Assistant's own local position tracking may not be working (unrelated to provider)
- **Status:** ⚠️ Needs investigation
- **Next Steps:**
  - Add debug logging to compare UUIDs
  - Test with episode that has known progress in Pocketcasts
  - Check if MA's local position tracking works independently
  - Verify `update_episode_progress()` is being called during playback

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

## API Endpoints Used

### Authentication
- `POST https://api.pocketcasts.com/user/login` - Login with email/password

### Podcasts
- `POST https://api.pocketcasts.com/user/podcast/list` - Get subscribed podcasts
- `GET https://podcast-api.pocketcasts.com/podcast/full/{uuid}` - Get podcast details and episodes
- `POST https://api.pocketcasts.com/discover/search` - Search podcasts

### Episodes
- `POST https://api.pocketcasts.com/user/in_progress` - Get in-progress episodes
- `POST https://api.pocketcasts.com/sync/update_episode` - Update playback progress

### Assets
- `https://static.pocketcasts.com/discover/images/280/{uuid}.jpg` - Podcast thumbnails

---

## Next Steps

### High Priority
1. **Test basic functionality** - Verify login, browse, and playback work
2. **Implement subscribe/unsubscribe** - Complete LIBRARY_PODCASTS_EDIT feature
3. **Connect progress sync** - Hook into player queue events to auto-sync position
4. **Add error handling** - Improve error messages and exception handling
5. **Token refresh** - Implement token refresh mechanism

### Medium Priority
6. **Add browse categories** - Implement featured/trending/categories
7. **Episode filtering** - Add played/unplayed filters
8. **Add unit tests** - Test core functionality
9. **Handle token expiration** - Graceful re-authentication
10. **Optimize API calls** - Cache podcast lists, reduce redundant calls

### Low Priority
11. **Add Up Next queue support** - If API supports it
12. **Add filters feature** - Custom episode filters
13. **Chapter support** - If episodes have chapter markers
14. **Artwork optimization** - Support multiple image sizes

### Nice to Have
15. **Multi-instance support** - Allow multiple Pocketcasts accounts
16. **Advanced search** - Search within episodes
17. **Podcast recommendations** - Based on listening history
18. **Statistics** - Listening time, favorite podcasts, etc.

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

### 2026-02-01 - Testing and Bug Fixes
- **Branch Management:**
  - Synced pocketcasts branch from upstream
  - Merged upstream/dev into pocketcasts (565 commits)
  - Rebased to create clean PR branch (4 commits only)
  - Simplified to single clean branch strategy

- **Bug Fixes:**
  - Fixed Bug #1: Changed `self.lookup_key` → `self.instance_id` (3 occurrences)
  - Lines affected: 169, 174, 215 in `__init__.py`

- **Testing:**
  - Set up fresh development environment
  - Tested provider with real Pocketcasts account
  - Documented 3 bugs (1 fixed, 1 resolved, 1 investigating)
  - Updated STATUS.md with comprehensive test results

- **Documentation:**
  - Created STATUS.md to track development progress
  - Documented known issues and testing checklist
  - Added API endpoints reference
  - Added contributing guidelines

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
