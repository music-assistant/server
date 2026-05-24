# PR #3900 — Review Issue Tracking

This document tracks the current state of all review comments on
PR #3900 "Rewrite Deezer provider with GraphQL client".

https://github.com/music-assistant/server/pull/3900

**Reviewers:** OzGav (code review), marcelveldt (architectural feedback)

**Awaiting feedback:**
- Issue 18 — Confirmed the core serialization bug is real (mashumaro Union deserialization on cache roundtrip). Proposed three fix options. Waiting on reviewer preference for fix approach and scope.

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

| #   | File             | Complexity | Status             | Summary                                                                 |
| --- | ---------------- | ---------- | ------------------ | ----------------------------------------------------------------------- |
| 13  | media.py:150     | High       | Done               | Album list fetched twice for audiobook detection                        |
| 1   | helpers.py:139   | High       | Open               | Double-fetching first audiobook chapter page                            |
| 21  | streaming.py:46  | High       | Open               | Duplicated bookmark fetching logic                                      |
| 14  | media.py:606     | Medium     | Open               | Double caching in `_fetch_podcast_episodes`                             |
| 12  | media.py:110     | Medium     | Open               | Personal songs fetched 3 times during library sync                      |
| 18  | browse.py:595    | Medium     | Awaiting feedback  | Workaround for core serialization bug                                   |
| 11  | media.py:80      | Medium     | Open               | `_iter_paged` loses type safety with `Any`                              |
| 8   | provider.py:94   | Medium     | Open               | Unhandled exceptions in `handle_async_init`                             |
| 2   | parsers.py:103   | Medium     | Open               | `cover` typed as `object` defeats typing                                |
| 7   | parsers.py:840   | Medium     | Open               | `apply_web_url` uses `object` parameter type                            |
| 19  | browse.py:101    | Medium     | Open               | String literals should be provider-level constants                      |
| 10  | media.py:248     | Low        | Partially resolved | Search cached for 7 days (type suppression resolved via #3965)          |
| 20  | browse.py:946    | Low        | Open               | Caching dynamic content contradicts `is_dynamic=True`                   |
| 4   | parsers.py:870   | Low        | Open               | `parse_date` silently falls back to `datetime.now()`                    |
| 6   | parsers.py:506   | Low        | Open               | Docstring format doesn't match MA convention                            |
| 5   | parsers.py:624   | Low        | Open               | GW parsers assume keys always present                                   |
| 22  | streaming.py:332 | Low        | Open               | Missing HTTP error handling in audio stream                             |
| 3   | parsers.py:334   | Trivial    | Open               | Private attribute `_user_id` accessed from module function              |
| 15  | media.py:711     | Trivial    | Open               | Wrong exception: `NotImplementedError` → `UnsupportedFeaturedException` |
| 16  | media.py:729     | Trivial    | Open               | Wrong exception: `NotImplementedError` → `UnsupportedFeaturedException` |
| 9   | provider.py:108  | Trivial    | Open               | No `super().unload()`                                                   |
| 17  | media.py:465     | Trivial    | Open               | Inconsistent `instance_id` access                                       |

---

## Detailed Analysis

---

### Issue 1 — Double-fetching first audiobook chapter page

**File:** `helpers.py:139`
**Priority:** Must fix
**OzGav comment:** "`get_audiobook` in `media.py` already fetches with `chapters_first=200` before calling this helper. So the first page is fetched twice."

**Analysis:**

In `media.py` `get_audiobook()`:
```python
result = await self.provider.gql_client.get_audiobook(
    audiobook_id=prov_audiobook_id, chapters_first=200
)
# ... parse metadata from result ...
all_edges = await fetch_all_audiobook_chapter_edges(
    self.provider.gql_client, prov_audiobook_id
)
```

`fetch_all_audiobook_chapter_edges()` starts fresh — re-fetches the first page that was already fetched. This wastes one API call (and 200 chapter nodes of data transfer) every time an audiobook is loaded.

**Fix approach:** Refactor `fetch_all_audiobook_chapter_edges` to optionally accept an already-fetched initial result and its page_info, only paginating from where the caller left off. Alternatively, do all parsing after the helper returns so `get_audiobook` doesn't need to fetch separately at all.

---

### Issue 2 — `cover` typed as `object`

**File:** `parsers.py:103`
**Priority:** Should fix
**OzGav comment:** "Why is cover typed as object here instead of a proper type that expresses whether .urls is always present or not?"

**Analysis:**

```python
def _cover_image(provider: DeezerProvider, cover: object | None) -> MediaItemImage | None:
    if cover and hasattr(cover, "urls") and cover.urls:
```

The generated GQL models have multiple cover types (`GetTrackTrackAlbumCover`, `AlbumFieldsCover`, `PlaylistFieldsPicture`, etc.) that all share a `urls: list[str] | None` attribute. Using `object` + `hasattr` is duck-typing that mypy can't verify.

**Fix approach:** Define a `Protocol`:
```python
class HasUrls(Protocol):
    urls: list[str] | None
```
Then type the parameter as `HasUrls | None`.

---

### Issue 3 — Private attribute accessed directly

**File:** `parsers.py:334`
**Priority:** Should fix
**OzGav comment:** "Why is a private attribute being accessed directly?"

**Analysis:**

```python
if not is_editable and playlist.owner.id == provider._user_id:
    is_editable = True
```

`_user_id` is accessed from a standalone module function, violating the convention that `_` prefixed attributes are internal to the class.

**Fix approach:** Rename `_user_id` to `user_id` (make it public) since it's legitimately needed by external code, or add a `@property` accessor on the provider.

---

### Issue 4 — Silent fallback to `datetime.now()` on parse failure

**File:** `parsers.py:870`
**Priority:** Should fix
**OzGav comment:** "There is a problem with the date but rather than surfacing it you are just overwriting it with now?"

**Analysis:**

```python
def parse_date(date_value: str | None) -> datetime:
    try:
        return datetime.fromisoformat(str(date_value))
    except (ValueError, TypeError):
        return datetime.now(tz=UTC)
```

Returning `datetime.now()` is semantically wrong — items appear as "just added" when the date was simply unparsable. This hides data quality issues.

**Fix approach:** Change return type to `datetime | None`, return `None` on failure, and let callers decide. For `date_added`, `None` is perfectly valid (MA treats it as "unknown"). For `release_date`, similarly acceptable.

---

### Issue 5 — GW parser key availability assumptions

**File:** `parsers.py:624`
**Priority:** Nice to have
**OzGav comment:** "Are the API responses robust enough that these key values will always be available?"

**Analysis:**

`parse_gw_item` guards entry with `.get("ALB_ID")` checks, but inner parsers like `parse_gw_audiobook` then access `data["ALB_ID"]` directly. Since the GW API is undocumented and unofficial, defensive access would be safer.

**Fix approach:** The entry guards in `parse_gw_item` should be sufficient for normal operation. Consider wrapping inner parser calls in try/except KeyError with a warning log for robustness, or use `.get()` with fallbacks in the inner parsers.

---

### Issue 6 — Docstring format

**File:** `parsers.py:506`
**Priority:** Nice to have
**OzGav comment:** "Multi line docstrings should have the first line on its own line... Don't explain inner workings."

**Analysis:**

Per MA convention (CLAUDE.md): docstrings should provide clarity to the caller, not explain how the code works internally. Multi-line docstrings should have `"""` on its own opening line.

**Fix approach:** Audit all multi-line docstrings across the provider. Remove implementation details from docstrings (move to inline comments where needed). Ensure opening `"""` is on its own line for multi-line.

---

### Issue 7 — `apply_web_url` uses `object` parameter type

**File:** `parsers.py:840`
**Priority:** Should fix
**OzGav comment:** "The parameter type is object, which defeats typing. Should accept a typed union of the GQL models."

**Analysis:**

Same category as Issue 2. Can be solved with the same `Protocol` approach:
```python
class HasWebUrl(Protocol):
    url: HasWebUrlInner

class HasWebUrlInner(Protocol):
    web_url: str | None
```

Or a simple Union of the 2-3 GQL result types that are actually passed in.

---

### Issue 8 — Unhandled exceptions in `handle_async_init`

**File:** `provider.py:94`
**Priority:** Must fix
**OzGav comment:** "What happens if `get_me()` raises rather than returning None? And what exception does the caller see if `GWClient.setup()` raises `DeezerGWError`?"

**Analysis:**

```python
me = await self.gql_client.get_me()
if me is None:
    raise LoginFailed(...)
await self.gw_client.setup()
```

- If `get_me()` raises `GraphQLClientHttpError` or `GraphQLClientInvalidResponseError` → user sees a confusing raw error instead of `LoginFailed`.
- If `gw_client.setup()` raises `DeezerGWError` → same problem.

**Fix approach:** Wrap both in try/except, catching library-specific errors and re-raising as `LoginFailed` with a descriptive message.

---

### Issue 9 — No `super().unload()`

**File:** `provider.py:108`
**Priority:** Nice to have
**OzGav comment:** "No `super().unload()`?"

**Analysis:**

The base `Provider.unload()` is currently an empty method with no logic. Calling `super().unload()` is harmless and future-proofs against base class changes.

**Fix approach:** Add `await super().unload(is_removed)` before or after closing the GQL client.

---

### Issue 10 — Search cached for 7 days + `# type: ignore[type-var]`

**File:** `media.py:248`
**Priority:** Should fix
**Status:** Type suppression resolved (PR #3965 merged into dev); cache duration still needs fixing
**OzGav comment:** "Why would you cache a search for a week? What is the typing issue with `@use_cache` here and why is it suppressed rather than fixed?"

**Analysis:**

**Cache duration:** 7 days is far too long for search results. Users expect fresh results (new releases, etc.). Should be short (e.g., 1 hour) or removed entirely.

**Typing issue:** The `# type: ignore[type-var]` is required because `@use_cache` has `ProviderT = TypeVar("ProviderT", bound="Provider | CoreController")`. The Deezer helper classes (`DeezerMediaManager`, `DeezerBrowseManager`) are NOT subclasses of `Provider` or `CoreController` — they're plain classes with a `mass` attribute. The decorator works at runtime (it only needs `self.mass.cache` and `self.instance_id`/`self.domain`) but mypy can't verify the type bound.

**How other providers handle this:**
- **Apple Music:** Uses `@use_cache` on separate manager classes (e.g., `AppleMusicRecommendationManager`) — has the exact same issue but the entire `apple_music/` directory is **excluded from mypy** in `pyproject.toml`. Same for `ytmusic/`.
- **Yandex Music:** Uses `@use_cache` directly on the `Provider` subclass → no typing issue.
- **Other providers (Spotify, Tidal, YTMusic):** Put cached methods directly on the Provider class.

**Proposed fix — Protocol-bounded TypeVar (verified locally):**

The `@use_cache` wrapper only accesses `self.mass` (for `.cache` and `.create_task()`) and `self.domain` (via `getattr(self, "instance_id", self.domain)`). A Protocol captures exactly what's needed:

```python
# In music_assistant/controllers/cache/helpers.py:
if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

class _Cacheable(Protocol):
    @property
    def domain(self) -> str: ...

    @property
    def mass(self) -> MusicAssistant: ...

ProviderT = TypeVar("ProviderT", bound="_Cacheable")
```

This was tested locally with `mypy --strict --python-version 3.13` and passes cleanly for all three patterns:
1. `Provider` — `domain` is a `@property` ✓
2. `CoreController` — `domain` is a class attribute ✓
3. Manager classes — `domain` is a plain instance attribute ✓

Zero runtime impact, fully backwards compatible. Using `@property` in the Protocol ensures read-only properties (like `Provider.domain`) satisfy the constraint.

**Decision:** Reduce the 7-day search TTL. Propose the Protocol fix to the reviewer — if accepted, all `# type: ignore[type-var]` comments on `@use_cache` across the provider can be removed. Question posted: should this be part of this PR or a separate one?

---

### Issue 11 — `_iter_paged` loses type safety

**File:** `media.py:80`
**Priority:** Nice to have
**OzGav comment:** "This is losing type safety. Can this be done better?"

**Analysis:**

```python
async def _iter_paged(
    self,
    fetch: Callable[..., Awaitable[Any]],
    extract: Callable[..., Any],
) -> AsyncGenerator[Any, None]:
```

All type information is erased. Callers get `Any` edges and mypy can't verify anything.

**Fix approach:** This is difficult to type properly because each GQL query returns a different result type. Options:
1. Generic with TypeVar (complex but correct).
2. Use `@overload` for common signatures.
3. Accept the trade-off — the helper is internal and always used in the same pattern. The caller immediately passes the yielded edge to a typed parser function.

**Decision:** Low priority. The pattern is consistent and safe in practice. If addressed, a TypeVar approach would be ideal but may not be worth the complexity.

---

### Issue 12 — Personal songs fetched 3 times

**File:** `media.py:110`
**Priority:** Should fix
**OzGav comment:** "Personal songs are fetched separately in `get_library_artists`, `get_library_albums`, and `get_library_tracks`. This should be cached or fetched once."

**Analysis:**

`get_personal_songs(start=0, nb=500)` is called in:
- `get_library_artists()`
- `get_library_albums()`
- `get_library_tracks()`

Same endpoint, same parameters, same data — 3 redundant API calls.

**Fix approach:** Add a `_personal_songs_cache` similar to `_audiobook_ids_cache`:
```python
async def _get_personal_songs(self) -> list[dict]:
    if self._personal_songs_cache is not None:
        return self._personal_songs_cache
    result = await self.provider.gw_client.get_personal_songs(start=0, nb=500)
    self._personal_songs_cache = result.get("data", [])
    return self._personal_songs_cache
```

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
**OzGav comment:** "This method is cached and now you are also writing to an additional cache?"

**Analysis:**

Two caching layers:
1. **Outer:** `@use_cache(3600 * 24)` — caches the entire episode list for 24 hours.
2. **Inner:** Manual `cache.set()` per episode with 30-day TTL.

**Intended purpose:**
- The outer cache avoids re-running the entire method within 24h.
- The inner per-episode cache survives the outer cache expiration. When the method re-runs after 24h, it only fetches episodes that aren't individually cached yet (new episodes since last check).

**Is this over-complicated?** The two layers serve different purposes:
- Outer = "don't re-run this function for 24h"
- Inner = "don't re-fetch individual episode details for 30 days"

The logic is: after 24h the outer cache expires → method re-runs → fetches the episode ID list (cheap) → checks each ID against the 30-day inner cache → only batch-fetches truly new/expired episodes.

**Conclusion:** The design intent is sound but the implementation could be clearer:
1. Rename/restructure to make the two-layer intent explicit.
2. Add a comment block explaining the caching strategy.
3. Consider whether the outer `@use_cache(24h)` adds value at all — if the inner cache handles individual episodes efficiently, maybe just remove the outer decorator and always do the "check which episodes are cached" logic. This would mean new episodes appear within minutes rather than after 24h.

**Decision:** Simplify by removing the outer `@use_cache` decorator. The inner per-episode cache already prevents redundant fetches efficiently. This way new episodes appear immediately and the code is simpler to reason about.

---

### Issue 15 & 16 — Wrong exception type

**File:** `media.py:711` and `media.py:729`
**Priority:** Must fix
**OzGav comment:** "Should be `UnsupportedFeaturedException` from MA errors, not the Python built-in"

**Analysis:**

```python
else:
    raise NotImplementedError
```

`NotImplementedError` implies unfinished code. `UnsupportedFeaturedException` correctly signals "this media type isn't supported by this provider" — which is handled gracefully by the MA framework.

**Fix approach:** Direct replacement:
```python
from music_assistant_models.errors import UnsupportedFeaturedException
...
raise UnsupportedFeaturedException(f"Unsupported media type: {media_type}")
```

---

### Issue 17 — Inconsistent `instance_id` access

**File:** `media.py:465`
**Priority:** Nice to have
**OzGav comment:** "Elsewhere `self.provider.instance_id` is used?"

**Analysis:**

`DeezerMediaManager.__init__` copies `self.instance_id = provider.instance_id`. Some code uses `self.instance_id`, other code uses `self.provider.instance_id`. Both are equivalent.

**Fix approach:** Pick one convention. Since `self.instance_id` is shorter and already assigned in `__init__`, use it consistently. Or remove the local copies and always go through `self.provider.*`.

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
**OzGav comment:** "These should all be provider level constants"

**Analysis:**

The browse routing uses hardcoded strings:
```python
if subpath == "Made For You":
if subpath == "Explore":
if subpath == "Recently Played":
if subpath == "Shaker":
if subpath == "Discover Audiobooks":
```

These same strings appear in folder creation. A typo in one place silently breaks routing.

**Fix approach:** Define constants in `helpers.py`:
```python
BROWSE_MADE_FOR_YOU = "Made For You"
BROWSE_EXPLORE = "Explore"
BROWSE_RECENTLY_PLAYED = "Recently Played"
BROWSE_SHAKER = "Shaker"
BROWSE_AUDIOBOOKS = "Discover Audiobooks"
```

---

### Issue 20 — Caching contradicts `is_dynamic=True`

**File:** `browse.py:946`
**Priority:** Should fix
**OzGav comment:** "Should this be cached since `is_dynamic` is true?"

**Analysis:**

`_get_flow_config_tracks` is decorated with `@use_cache(3600)` but the playlist is marked `is_dynamic=True`. Dynamic playlists are supposed to return fresh content on each fetch — that's their entire purpose (Flow returns random tracks each time).

**Decision:** Remove the `@use_cache` decorator from `_get_flow_config_tracks`. Same applies to `_get_flow_tracks` which is also cached but marked dynamic.

**Affected methods to audit:**
- `_get_flow_tracks` — `is_dynamic=True` → remove cache
- `_get_flow_config_tracks` — `is_dynamic=True` → remove cache
- `_get_shaker_tracks` — `is_dynamic=True` → already not cached ✓

---

### Issue 21 — Duplicated bookmark fetching

**File:** `streaming.py:46`
**Priority:** Must fix
**OzGav comment:** "This seems to be duplicating `_fetch_all_bookmarks` in `media.py`"

**Analysis:**

`streaming.py` `get_resume_position()`:
```python
while True:
    result = await self.provider.gql_client.get_podcast_episode_bookmarks(first=50, after=cursor)
    ...
```

`media.py` `_fetch_all_bookmarks()`:
```python
while True:
    result = await self.provider.gql_client.get_podcast_episode_bookmarks(first=50, after=cursor)
    ...
```

Identical pagination logic, slightly different return format.

**Fix approach:** Extract to a shared helper in `helpers.py` or have `get_resume_position` call `self.provider.media_manager._fetch_all_bookmarks()` and look up the specific episode from the result.

---

### Issue 22 — Missing HTTP error handling in audio stream

**File:** `streaming.py:332`
**Priority:** Must fix
**OzGav comment:** "You need to add error handling here."

**Analysis:**

```python
async with self.mass.http_session.get(
    streamdetails.data["url"], headers=headers, timeout=timeout
) as resp:
    async for chunk in resp.content.iter_chunked(2048):
```

No status code check. If Deezer's CDN returns 403 (expired URL), 404 (removed track), or 5xx (server error), the code would attempt to decrypt the error response body as audio, yielding garbage data.

**Fix approach:**
```python
async with self.mass.http_session.get(...) as resp:
    if resp.status != 200:
        raise MediaNotFoundError(
            f"Failed to stream track {streamdetails.item_id}: HTTP {resp.status}"
        )
    async for chunk in resp.content.iter_chunked(2048):
```

---

## Implementation Plan (by complexity — largest first)

### Phase 0 — Architectural (deezer-python-gql repo)
- **A1:** Rewrite `base_client.py` to use aiohttp instead of httpx
- Update tests, bump version, release

### Phase 1 — High complexity (refactoring across multiple functions)
1. **Issue 13:** Single-pass album/audiobook detection — restructure `get_library_albums` to collect edges, check IDs, then yield (touches `_get_audiobook_ids_in_albums`, `_iter_paged` usage, and cache logic)
2. **Issue 1:** Eliminate double audiobook chapter fetch — refactor `fetch_all_audiobook_chapter_edges` to accept pre-fetched first page (touches `helpers.py` signature + `media.py` call site)
3. **Issue 21:** Extract shared bookmark fetching — move pagination loop to `helpers.py`, update both `streaming.py` and `media.py` callers

### Phase 2 — Medium complexity (localized but multi-line changes)
4. **Issue 14:** Simplify podcast episode caching — remove outer `@use_cache`, rely on inner per-episode cache
5. **Issue 12:** Cache personal songs — add `_personal_songs_cache` + `_get_personal_songs()` helper, update 3 call sites
6. **Issue 18:** Document serialization workaround, file upstream issue
7. **Issue 11:** Improve `_iter_paged` typing with TypeVar (optional — accept if too complex)
8. **Issue 8:** Wrap `handle_async_init` in try/except for GQL/GW errors → `LoginFailed`
9. **Issue 2 & 7:** Define `Protocol` classes for cover/url objects, update parser signatures
10. **Issue 19:** Extract browse path strings to constants, update all references

### Phase 3 — Low complexity (single-location changes)
11. **Issue 10:** Reduce search cache TTL (7 days → 1 hour), add comment about type ignore
12. **Issue 20:** Remove `@use_cache` from `_get_flow_tracks` and `_get_flow_config_tracks`
13. **Issue 4:** Change `parse_date` return type to `datetime | None`, update callers
14. **Issue 6:** Audit and fix docstring format across all provider files
15. **Issue 5:** Add try/except KeyError around inner GW parser calls
16. **Issue 22:** Add `resp.status` check before iterating audio stream chunks

### Phase 4 — Trivial (one-line fixes)
17. **Issue 3:** Rename `_user_id` → `user_id` (or add property)
18. **Issue 15 & 16:** Replace `NotImplementedError` → `UnsupportedFeaturedException` (2 lines)
19. **Issue 9:** Add `await super().unload(is_removed)`
20. **Issue 17:** Pick one `instance_id` access pattern, find-replace
