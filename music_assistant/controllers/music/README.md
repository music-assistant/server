# Music Controller

Music Assistant's core controller for the music library. It aggregates and normalizes media items from all music providers (streaming services, local files, …) into the internal SQLite library database, and is the central entry point for library access (search, browse, recommendations, library edits and playback bookkeeping).

## Package Layout

- `controller.py`: the main `MusicController` — a `CoreController` (domain `music`) that holds the orchestration logic and composes the per-media-type sub-controllers.
- `database.py`: `MusicDatabaseSetupMixin`, mixed into `MusicController` — owns the library database lifecycle (connection setup, schema creation, maintenance). Kept separate because the schema code is large and self-contained.
- `migrations.py`: the versioned, step-by-step schema migrations (`migrate_database`), kept out of `database.py` as a dependency-injected function so this large block stays self-contained and individually testable.
- `media/`: the per-media-type sub-controllers (`AlbumsController`, `ArtistsController`, `TracksController`, `RadioController`, `PlaylistController`, `AudiobooksController`, `PodcastsController`, `GenreController`), all sharing `MediaControllerBase`. `MusicController` instantiates one of each and delegates per-type work to them.
- `sorting.py`: the sort field definitions and the sort options every listing offers (`LISTING_SORT_OPTIONS`, served by `music/sort_options`); the library listings push the chosen sort into SQL.
- `listing.py`: the in-memory pipeline (`apply_listing`) that searches, sorts and pages a listing assembled from the providers, such as the tracks of an album or playlist and the episodes of a podcast.
- `listing_cache.py`: keeps such an assembled listing for a while (`cached_listing`), grouped by the container's uri so an edit or refresh of the container drops it (`invalidate_listings`), as does a played episode for its podcast. Dynamic playlists and the playlists Music Assistant composes itself (builtin, smart playlists) are never kept.
- `constants.py`: config keys, the database schema version, background-task ids and tuning constants.
- `helpers.py`: stateless helper functions (needing no controller state) used by the controller.
- `sharing.py`: public provider page resolution and human-readable metadata for sharing.
- `sharing_catalogue.py`: additional audited catalogue page formats used by sharing.
- `strings.json`: translatable strings for this module (`core.music.*`), including the `manifest` name/description.

## Architecture & Design Notes

- **Layering / dependency direction.** `MusicController` is the orchestrator; the `media/` sub-controllers hold the type-specific logic. The sub-controllers never import `MusicController` back, keeping the dependency direction one-way and avoiding import cycles.
- **Database split via mixin.** The schema and migration code lives in `MusicDatabaseSetupMixin` (`database.py`) so `controller.py` stays focused on orchestration. The mixin carries no state of its own — it operates on its host (`mass`, `logger`, the `database` connection, the media sub-controllers and a couple of controller methods), declared under `TYPE_CHECKING`.
- **Startup order.** As a singleton core controller it is set up once: the database is initialized first (via the mixin), then maintenance tasks are registered and provider syncs are scheduled.
- **Library data model.** Library items use the provider id `library`; provider mappings record which provider item(s) a library item resolves to. Maintenance prunes orphaned mappings and playlog rows.
- **Schema migrations** are versioned against `DB_SCHEMA_VERSION`: on a version mismatch the database file is backed up before migrating, and a failed migration falls back to a fresh database (triggering a full rescan) so the user is never left without a working library.

## Sharing items

`music/share_item` accepts an item's Music Assistant `uri` and requires `library.read`.
For a queue item, pass the underlying media item's URI. It returns a `title`, a list of
`links` containing `provider_domain`, `provider_instance` and `url`, and nullable `text`.
When public links are available, share the selected URL. Otherwise, share `text`, which
contains human-readable item metadata. Radio text identifies the station.
Sharing resolves album and track metadata without fetching nested artists or albums.

The server resolves audited public provider pages on request and filters sources by the
calling user's access. Clients must not infer shareability from `ProviderMapping.url`:
that field can contain playback addresses or credentials. Unsupported providers use the
text fallback. Playlists and SoundCloud tracks also use text until public visibility can
be verified. SoundCloud artist pages remain supported. Adding a provider requires verifying
its public page format and covering it with sharing tests. Sharing does not change stored
mappings or create hosted pages.

URL support is specific to each provider and media type. Catalogue IDs can resolve to
music, podcast or audiobook pages; audited radio providers resolve to station pages.
Podcast directory IDs can resolve to public directory pages without exposing feed or
enclosure URLs. Existing mappings remain unchanged, and missing identity information
uses the text fallback without additional provider requests.

YouTube Music watch links require cached public visibility from an authenticated song
metadata lookup. Private uploads, unlisted videos and legacy mappings without that evidence
use text. Sharing reads this evidence without refreshing it or probing a website; the
evidence expires with the provider's track metadata cache. Album and artist catalogue
links remain supported.
Sharing lookups recheck source access after YouTube Music's cache and throttle waits,
and again in the worker thread before the authenticated song request.
Concurrent sharing requests keep separate authorization contexts for provider tasks,
while reusing the same stored metadata cache. Calls within one request still deduplicate.

Built-in and Smart Playlists share their readable names as text. Provider item URIs
can resolve stored library metadata even while that source is unavailable. Built-in
track and radio URIs that could probe an arbitrary audio URL remain excluded.
Provider fetches stay on the authorized account if it disconnects during library resolution.

## Schema Versions on `dev` vs `stable`

`DB_SCHEMA_VERSION` is a single integer, so it can only describe one linear history. `stable` normally inherits dev's numbering through releases, but a **schema-changing bugfix backported to `stable`** is renumbered against stable's own (lower) counter — from that point the same integer means something different on each branch. A database coming from `stable` then reports a version that is already higher than the `if prev_version <= N:` gates of the dev steps in the gap, so those steps never run and the schema objects they add stay missing. `__create_database_tables` cannot compensate: it is `CREATE TABLE IF NOT EXISTS`, so it never touches an existing table. The user-visible result is a library that queries columns the database never got, which has already happened once when a stable install was moved to the beta image.

Renumbering the stable backport to match dev's number does not fix it either: the database would then claim to have run every dev step in between, which it genuinely never did. Encoding that correctly needs a per-step applied-migrations ledger (alembic-style), which is out of proportion to how rarely this triggers. So when backporting a schema change to `stable`:

1. Record in the backport PR that stable's `DB_SCHEMA_VERSION` now diverges from dev's, and which value it took.
2. On `dev`, bump `DB_SCHEMA_VERSION` and add an idempotent guard step re-adding every schema object introduced between the last shared version and stable's new one.
3. Gate that guard at dev's *current* version (`<= DB_SCHEMA_VERSION - 1`), never at stable's — `_setup_database` skips migration entirely when `prev_version` already equals `DB_SCHEMA_VERSION`, so users who upgraded and broke are stamped at the current version and a lower gate never fires for them.

## Future Enhancements

- Isolate the database connection itself into a dedicated layer (e.g. its own sub-controller), so connection ownership and the SQL/query surface live behind one boundary instead of on `MusicController`. Splitting the migrations (`migrations.py`) and grouping setup in `MusicDatabaseSetupMixin` are first steps toward that; the connection (`_database` and the `database` property) currently still lives on `MusicController`.
