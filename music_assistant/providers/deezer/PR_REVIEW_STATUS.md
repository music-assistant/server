# PR #3900 — Review Issue Tracking

This document tracks the current state of all review comments on
PR #3900 "Rewrite Deezer provider with GraphQL client".

https://github.com/music-assistant/server/pull/3900

**Reviewers:** OzGav (code review), marcelveldt (architectural feedback)

**Final State (verified 2026-05-30):** 22/22 issues resolved.

**Resolved via models 1.1.127 (merged 2026-05-29):**
- Issue 18 — Core serialization bug workaround removed. The `_deserialize_recommendation_items` custom deserializer in `music-assistant-models` #239 properly discriminates Union types by `media_type`, fixing the `to_dict()`→`from_dict()` roundtrip corruption.

**Resolved via dev merge (2026-05-24):**
- Issue 10 (type suppression) — PR #3965 merged the `Protocol`-bounded TypeVar fix into dev. All `# type: ignore[type-var]` comments removed from our branch.

---

## Architectural Changes (marcelveldt)

### A1 — Switch `deezer-python-gql` from httpx to aiohttp

**Status:** ✅ Complete
**Rationale:** MA uses a shared `aiohttp.ClientSession` across all 50+ providers. The `deezer-python-gql` library currently uses `httpx` (inherited from ariadne-codegen's default base client). ariadne-codegen explicitly supports swapping the HTTP backend via `base_client_file_path`. The base client is already hand-written — only `execute()` and `get_data()` need modification.

**Done (in `deezer-python-gql` repo):**
- Rewrote `base_client.py` to accept `aiohttp.ClientSession` instead of `httpx.AsyncClient`
- Replaced `httpx.AsyncClient.post()` → `aiohttp.ClientSession.post()` (context manager pattern)
- Return a lightweight `GQLResponse` dataclass from `execute()` (since aiohttp responses can't escape their context manager)
- Changed `pyproject.toml` dependency from `httpx>=0.27.0` to `aiohttp>=3.9.0`
- Updated tests to mock aiohttp instead of httpx
- Dual-mode: accept external session (MA passes `self.mass.http_session`) or create internal session (standalone usage)
- All 82 tests pass, mypy clean, ruff clean

**Done (in `music-assistant/server` repo):**
- Pass `self.mass.http_session` to `DeezerGQLClient(arl=..., session=self.mass.http_session)`
- Removed `await self.gql_client.close()` from `unload()` (session lifecycle managed by MA)

**Pending:** Release new `deezer-python-gql` version to PyPI, update `requirements_all.txt` in server repo

### A2 — Pydantic models (acknowledged constraint)

**Status:** Accepted as-is
**Rationale:** ariadne-codegen only generates Pydantic models — no configuration or plugin exists for dataclass/mashumaro output. The Pydantic models are confined to the `deezer-python-gql` library boundary: they're parsed into MA's own dataclass-based `MediaItem` types in `parsers.py` and immediately discarded. They never flow through MA's core. The memory/CPU overhead is negligible compared to network I/O.

### A3 — Add `artist_str` property to `Audiobook` model

**Status:** Planned (separate PR to `music-assistant/models` repo)
**Rationale:** `player_queues.py` uses `getattr(media_item, "artist_str", "")` to populate the `PlayerMedia.artist` display field. `Album` and `Track` define this property (joining their `artists` list). `Audiobook` has `authors` and `narrators` but no `artist_str`, so the "now playing" artist line is always empty for audiobooks. This affects all audiobook providers (Deezer, Audiobookshelf, Filesystem Local).

**Scope (in `music-assistant/models` repo):**
- Add `artist_str` property to `Audiobook` that returns `"/".join(name for a in self.authors)`
- Handles both `str` and `Artist` entries in the `authors` list

**Note:** This is NOT a Deezer-specific issue and should not be part of PR #3900. Requires cloning `music-assistant/models` repo.

---

## Issue Index (ordered by implementation complexity — largest first)

| #      | File                 | Complexity  | Status   | Summary                                                                     |
| ------ | -------------------- | ----------- | -------- | --------------------------------------------------------------------------- |
| ~~13~~ | ~~media.py:150~~     | ~~High~~    | ~~Done~~ | ~~Album list fetched twice for audiobook detection~~                        |
| ~~1~~  | ~~helpers.py:139~~   | ~~High~~    | ~~Done~~ | ~~Double-fetching first audiobook chapter page~~                            |
| ~~21~~ | ~~streaming.py:46~~  | ~~High~~    | ~~Done~~ | ~~Duplicated bookmark fetching logic~~                                      |
| ~~14~~ | ~~media.py:606~~     | ~~Medium~~  | ~~Done~~ | ~~Double caching in `_fetch_podcast_episodes`~~                             |
| ~~12~~ | ~~media.py:110~~     | ~~Medium~~  | ~~Done~~ | ~~Personal songs fetched 3 times during library sync~~                      |
| 18     | browse.py:595        | Medium      | Done     | Workaround for core serialization bug (removed)                             |
| ~~11~~ | ~~media.py:80~~      | ~~Medium~~  | ~~Done~~ | ~~`_iter_paged` loses type safety with `Any`~~                              |
| ~~8~~  | ~~provider.py:94~~   | ~~Medium~~  | ~~Done~~ | ~~Unhandled exceptions in `handle_async_init`~~                             |
| ~~2~~  | ~~parsers.py:103~~   | ~~Medium~~  | ~~Done~~ | ~~`cover` typed as `object` defeats typing~~                                |
| ~~7~~  | ~~parsers.py:840~~   | ~~Medium~~  | ~~Done~~ | ~~`apply_web_url` uses `object` parameter type~~                            |
| ~~19~~ | ~~browse.py:101~~    | ~~Medium~~  | ~~Done~~ | ~~String literals should be provider-level constants~~                      |
| ~~10~~ | ~~media.py:248~~     | ~~Low~~     | ~~Done~~ | ~~Search cached for 7 days (type suppression resolved via #3965)~~          |
| ~~20~~ | ~~browse.py:946~~    | ~~Low~~     | ~~Done~~ | ~~Caching dynamic content contradicts `is_dynamic=True`~~                   |
| ~~4~~  | ~~parsers.py:870~~   | ~~Low~~     | ~~Done~~ | ~~`parse_date` silently falls back to `datetime.now()`~~                    |
| ~~6~~  | ~~parsers.py:506~~   | ~~Low~~     | ~~Done~~ | ~~Docstring format doesn't match MA convention~~                            |
| ~~5~~  | ~~parsers.py:624~~   | ~~Low~~     | ~~Done~~ | ~~GW parsers assume keys always present~~                                   |
| ~~22~~ | ~~streaming.py:332~~ | ~~Low~~     | ~~Done~~ | ~~Missing HTTP error handling in audio stream~~                             |
| ~~3~~  | ~~parsers.py:334~~   | ~~Trivial~~ | ~~Done~~ | ~~Private attribute `_user_id` accessed from module function~~              |
| ~~15~~ | ~~media.py:711~~     | ~~Trivial~~ | ~~Done~~ | ~~Wrong exception: `NotImplementedError` → `UnsupportedFeaturedException`~~ |
| ~~16~~ | ~~media.py:729~~     | ~~Trivial~~ | ~~Done~~ | ~~Wrong exception: `NotImplementedError` → `UnsupportedFeaturedException`~~ |
| ~~9~~  | ~~provider.py:108~~  | ~~Trivial~~ | ~~Done~~ | ~~No `super().unload()`~~                                                   |
| ~~17~~ | ~~media.py:465~~     | ~~Trivial~~ | ~~Done~~ | ~~Inconsistent `instance_id` access~~                                       |

---

## Detailed Analysis

---

### Issue 1 — Double-fetching first audiobook chapter page

**File:** `helpers.py:139`
**Priority:** Must fix
**Status:** Done
**OzGav comment:** "`get_audiobook` in `media.py` already fetches with `chapters_first=200` before calling this helper. So the first page is fetched twice."

**Fix applied:** `fetch_all_audiobook_chapter_edges` now accepts an optional `initial_edges` parameter. `get_audiobook()` passes the already-fetched first page edges, so pagination starts from page 2. No wasted API calls.

---

### Issue 2 — `cover` typed as `object`

**File:** `parsers.py:103`
**Priority:** Should fix
**Status:** Done
**OzGav comment:** "Why is cover typed as object here instead of a proper type that expresses whether .urls is always present or not?"

**Fix applied:** Defined `_CoverLike` Protocol with a `urls: list[str]` property. Parameter typed as `_CoverLike | None`. No more `hasattr` duck-typing — mypy verifies access statically.

---

### Issue 3 — Private attribute accessed directly

**File:** `parsers.py:334`
**Priority:** Should fix
**Status:** Done
**OzGav comment:** "Why is a private attribute being accessed directly?"

**Fix applied:** The provider stores the user ID as `self.user_id` (public attribute, set in `handle_async_init`). All access from parsers uses `provider.user_id`.

---

### Issue 4 — Silent fallback to `datetime.now()` on parse failure

**File:** `parsers.py:870`
**Priority:** Should fix
**Status:** Done
**OzGav comment:** "There is a problem with the date but rather than surfacing it you are just overwriting it with now?"

**Fix applied:** Return type changed to `datetime | None`. Returns `None` on parse failure. Callers handle `None` gracefully (MA treats it as "unknown date"). No more `datetime.now()` in the codebase.

---

### Issue 5 — GW parser key availability assumptions

**File:** `parsers.py:624`
**Priority:** Nice to have
**Status:** Done
**OzGav comment:** "Are the API responses robust enough that these key values will always be available?"

**Fix applied:** Entry point `parse_gw_item` validates required keys with `.get()` guards before calling inner parsers, and wraps all inner parser calls in `try/except KeyError` with a debug log. Inner parsers use direct access for keys already validated at entry (e.g., `data["ALB_ID"]` after `data.get("ALB_ID")` guard), and `.get()` with defaults for optional fields.

---

### Issue 6 — Docstring format

**File:** `parsers.py:506`
**Priority:** Nice to have
**Status:** Done
**OzGav comment:** "Multi line docstrings should have the first line on its own line... Don't explain inner workings."

**Fix applied:** All multi-line docstrings across the provider follow MA convention: opening `"""` on its own line, concise caller-facing descriptions, no inner implementation details. Sphinx-style `:param:` format used where applicable.

---

### Issue 7 — `apply_web_url` uses `object` parameter type

**File:** `parsers.py:840`
**Priority:** Should fix
**Status:** Done
**OzGav comment:** "The parameter type is object, which defeats typing. Should accept a typed union of the GQL models."

**Fix applied:** Defined `_HasUrl` Protocol with a `url: object` property. Parameter typed as `_HasUrl` — mypy verifies the attribute exists. Uses `getattr(gql_result.url, "web_url", None)` for the nested access since the inner URL type varies across GQL models.

---

### Issue 8 — Unhandled exceptions in `handle_async_init`

**File:** `provider.py:94`
**Priority:** Must fix
**Status:** Done
**OzGav comment:** "What happens if `get_me()` raises rather than returning None? And what exception does the caller see if `GWClient.setup()` raises `DeezerGWError`?"

**Fix applied:** Wrapped both client setup calls in a single `try/except (GraphQLClientError, DeezerGWError)` block that re-raises as `LoginFailed`. The `me is None` case raises `GraphQLClientError` internally so there's a single unified `raise LoginFailed(...)` exit point. Follows the Yandex Music provider pattern. Exception chain preserved via `from err`.

---

### Issue 9 — No `super().unload()`

**File:** `provider.py:108`
**Priority:** Nice to have
**Status:** Done
**OzGav comment:** "No `super().unload()`?"

**Fix applied:** `await super().unload(is_removed)` is called in the `unload` method.

---

### Issue 10 — Search cached for 7 days + `# type: ignore[type-var]`

**File:** `media.py:248`
**Priority:** Should fix
**Status:** Done
**OzGav comment:** "Why would you cache a search for a week? What is the typing issue with `@use_cache` here and why is it suppressed rather than fixed?"

**Fix applied:**
- **Cache TTL:** Reduced from 7 days to 15 minutes (`60 * 15`). Fresh enough for discovery, short enough to reflect library changes.
- **Type suppression:** All `# type: ignore[type-var]` comments removed. PR #3965 merged the Protocol-bounded TypeVar fix into dev, which our branch now inherits.

---

### Issue 11 — `_iter_paged` loses type safety

**File:** `media.py:80`
**Priority:** Nice to have
**Status:** Done
**OzGav comment:** "This is losing type safety. Can this be done better?"

**Fix applied:** Added Protocol-based structural contracts (`_PageInfo`, `_Connection`) that document and enforce the pagination contract. The `extract` parameter is now typed as `Callable[..., _Connection | None]` instead of `Callable[..., Any]`. mypy validates the method body accesses `.edges` and `.page_info` correctly. Full generic inference at call sites is not possible due to a mypy limitation with Protocol-based TypeVar inference on generated Pydantic models, but the Protocols provide internal correctness and clear documentation of the expected structure.

---

### Issue 12 — Personal songs fetched 3 times

**File:** `media.py:110`
**Priority:** Should fix
**Status:** Done
**OzGav comment:** "Personal songs are fetched separately in `get_library_artists`, `get_library_albums`, and `get_library_tracks`. This should be cached or fetched once."

**Fix applied:** Added `_get_personal_songs()` method with `@use_cache(3600 * 24)` (24h TTL). All 6 call sites in media.py now use this cached helper. Also fixed a pre-existing bug where only the first 500 songs were fetched — the helper now paginates fully until exhausted.

---

### Issue 13 — Album list fetched twice for audiobook detection

**File:** `media.py:150`
**Priority:** Must fix
**Status:** Done
**OzGav comment:** "`_get_audiobook_ids_in_albums` loops through all favourite albums to collect IDs. Then `get_favorite_albums` loops through the same list again. Full album list fetched twice."

**Fix applied:** Single-pass in `get_library_albums`: collect all edges into memory, call `check_audiobook_ids` once, yield non-audiobooks from the in-memory list. Renamed `_audiobook_ids_cache` → `_audiobook_ids_in_favorites` for clarity. `_get_audiobook_ids_in_albums()` retained as fallback for standalone `get_library_audiobooks()` calls.

---

### Issue 14 — Double caching in `_fetch_podcast_episodes`

**File:** `media.py:606`
**Priority:** Should fix
**Status:** Done
**OzGav comment:** "This method is cached and now you are also writing to an additional cache?"

**Fix applied:** Reduced outer `@use_cache` TTL from 24h to 1h and added a docstring explaining the two-layer caching strategy. The outer cache prevents repeated per-episode cache lookups during rapid navigation; the inner 30-day per-episode cache prevents re-fetching episode details. Keeping both layers but with a shorter outer TTL balances freshness (new episodes within 1h) and browsing performance (500-episode podcasts don't trigger 500 cache lookups on every visit).

---

### Issue 15 & 16 — Wrong exception type

**File:** `media.py:711` and `media.py:729`
**Priority:** Must fix
**Status:** Done
**OzGav comment:** "Should be `UnsupportedFeaturedException` from MA errors, not the Python built-in"

**Fix applied:** Both `library_add` and `library_remove` now raise `UnsupportedFeaturedException(f"Unsupported media type for ...: {media_type}")` with a descriptive message.

---

### Issue 17 — Inconsistent `instance_id` access

**File:** `media.py:465`
**Priority:** Nice to have
**Status:** Done
**OzGav comment:** "Elsewhere `self.provider.instance_id` is used?"

**Fix applied:** All manager classes (`DeezerMediaManager`, `DeezerBrowseManager`, `DeezerStreamingManager`) store `self.instance_id = provider.instance_id` in `__init__` and use `self.instance_id` consistently throughout. No mixed access patterns.

---

### Issue 18 — Workaround for core serialization bug

**File:** `browse.py:595`
**Priority:** Should fix
**OzGav comment:** "I think you will need to fix this rather than work around it."

**Analysis:**

The workaround:
```python
# Convert all items to ItemMapping to work around a core serialization bug
for folder in result:
    folder.items = UniqueList(
        ItemMapping.from_item(item) for item in folder.items
    )
```

**Root cause (verified locally):** The bug is specifically in mashumaro's `from_dict()` deserialization of Union-typed fields — NOT in `to_dict()`. Tested with mashumaro 3.20 (MA's pinned version):

- `to_dict()` works correctly — a `Playlist` in `items` serializes with all its fields (`is_dynamic`, `media_type=playlist`, etc.). The frontend receives correct data.
- `from_dict()` is broken — mashumaro has no discriminator for the Union `MediaItemType | ItemMapping | BrowseFolder`, so it tries types in declaration order. `Artist` is first in `MediaItemType` and its required fields overlap enough that mashumaro picks it for ALL items. A Playlist gets deserialized as `Artist`, losing `is_dynamic` etc.

**When does `from_dict()` get called?** On `@use_cache` cache hits: `cache.get()` returns raw dicts → `_reconstruct()` calls `parse_value()` → which calls `RecommendationFolder.from_dict()` → mashumaro handles nested `items` field with broken Union resolution.

**Why the ItemMapping workaround works:** `ItemMapping` dicts fail `Artist.from_dict()` (missing required `provider_mappings` field), so mashumaro falls through until it hits `ItemMapping` — the correct type. Verified locally.

**Impact across MA:** Apple Music and YTMusic both `@use_cache(3600)` their `recommendations()` with full objects in items. They have the exact same latent bug — on cache hit, items get deserialized as `Artist` regardless of actual type. Likely unnoticed because the frontend receives correct `to_dict()` output on the initial (uncached) call.

**Note:** MA's `parse_value()` helper DOES correctly handle Unions via `media_type` discrimination (checks `value["media_type"] != value_type.media_type` and falls through). But this only works at the top level — once `RecommendationFolder.from_dict()` is called, mashumaro handles nested fields internally without using `parse_value`.

**Proper fix options:**
1. **Fix in `music_assistant_models`** — add `Discriminator(field="media_type")` (mashumaro 3.20 supports this via `Annotated` on Union fields or class-level `Config`). Fixes it for all providers.
2. **Keep the ItemMapping workaround** — functionally correct, self-documenting. Items survive the roundtrip and get resolved back to full objects on playback.
3. **Drop `@use_cache` from `recommendations()` here** — sidesteps the deserialization path for Deezer but doesn't help other providers.

**Decision:** Awaiting reviewer feedback on preferred approach and whether the fix belongs in this PR or a separate one. Question posted on PR.

---

### Issue 19 — String literals should be constants

**File:** `browse.py:101`
**Priority:** Should fix
**Status:** Done
**OzGav comment:** "These should all be provider level constants"

**Fix applied:** All browse path strings defined as module-level constants in `helpers.py` (`BROWSE_MADE_FOR_YOU`, `BROWSE_EXPLORE`, `BROWSE_RECENTLY_PLAYED`, `BROWSE_SHAKER`, `BROWSE_AUDIOBOOKS`, etc.). Browse routing and folder creation both import and use these constants.

---

### Issue 20 — Caching contradicts `is_dynamic=True`

**File:** `browse.py:946`
**Priority:** Should fix
**Status:** Done
**OzGav comment:** "Should this be cached since `is_dynamic` is true?"

**Fix applied:** Removed `@use_cache` from both `_get_flow_tracks` and `_get_flow_config_tracks`. These methods now fetch fresh tracks on every call, consistent with `is_dynamic=True`. `_get_shaker_tracks` was already uncached.

---

### Issue 21 — Duplicated bookmark fetching

**File:** `streaming.py:46`
**Priority:** Must fix
**Status:** Done
**OzGav comment:** "This seems to be duplicating `_fetch_all_bookmarks` in `media.py`"

**Fix applied:** Extracted pagination logic to `fetch_all_bookmarks(gql_client)` in `helpers.py`. Both `media.py` (removed `_fetch_all_bookmarks` private method) and `streaming.py` (`get_resume_position` now does a dict lookup) use the shared helper.

---

### Issue 22 — Missing HTTP error handling in audio stream

**File:** `streaming.py:332`
**Priority:** Must fix
**Status:** Done
**OzGav comment:** "You need to add error handling here."

**Fix applied:** Added `if resp.status != 200: raise MediaNotFoundError(...)` check immediately after opening the HTTP response context manager, before iterating chunks.

---

## Implementation Plan (by complexity — largest first)

### Phase 0 — Architectural (deezer-python-gql repo) ✅
- ~~Rewrite `base_client.py` to use aiohttp instead of httpx~~ — Done
- ~~Update tests, bump version, release~~ — Done

### Phase 1 — High complexity (refactoring across multiple functions) ✅
1. ~~**Issue 13:** Single-pass album/audiobook detection~~ — Done
2. ~~**Issue 1:** Eliminate double audiobook chapter fetch~~ — Done
3. ~~**Issue 21:** Extract shared bookmark fetching~~ — Done

### Phase 2 — Medium complexity (localized but multi-line changes) ✅
4. ~~**Issue 14:** Simplify podcast episode caching~~ — Done
5. ~~**Issue 12:** Cache personal songs~~ — Done
6. **Issue 18:** Document serialization workaround — Awaiting reviewer feedback
7. ~~**Issue 11:** Improve `_iter_paged` typing with Protocol~~ — Done
8. ~~**Issue 8:** Wrap `handle_async_init` in try/except~~ — Done
9. ~~**Issue 2 & 7:** Define Protocol classes for cover/url objects~~ — Done
10. ~~**Issue 19:** Extract browse path strings to constants~~ — Done

### Phase 3 — Low complexity (single-location changes) ✅
11. ~~**Issue 10:** Reduce search cache TTL to 15 min, remove type:ignore~~ — Done
12. ~~**Issue 20:** Remove @use_cache from flow track methods~~ — Done
13. ~~**Issue 4:** Change `parse_date` return type to `None`~~ — Done
14. ~~**Issue 6:** Audit and fix docstring format~~ — Done
15. ~~**Issue 5:** Add try/except KeyError in GW parsers~~ — Done
16. ~~**Issue 22:** Add resp.status check in streaming~~ — Done

### Phase 4 — Trivial (one-line fixes) ✅
17. ~~**Issue 3:** Use `provider.user_id` property~~ — Done
18. ~~**Issue 15 & 16:** Replace NotImplementedError → UnsupportedFeaturedException~~ — Done
19. ~~**Issue 9:** Add `await super().unload(is_removed)`~~ — Done
20. ~~**Issue 17:** Use `self.instance_id` consistently~~ — Done
