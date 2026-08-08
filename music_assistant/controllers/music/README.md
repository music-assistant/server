# Music Controller

Music Assistant's core controller for the music library. It aggregates and normalizes media items from all music providers (streaming services, local files, …) into the internal SQLite library database, and is the central entry point for library access (search, browse, recommendations, library edits and playback bookkeeping).

## Package Layout

- `controller.py`: the main `MusicController` — a `CoreController` (domain `music`) that holds the orchestration logic and composes the per-media-type sub-controllers.
- `database.py`: `MusicDatabaseSetupMixin`, mixed into `MusicController` — owns the library database lifecycle (connection setup, schema creation, maintenance). Kept separate because the schema code is large and self-contained.
- `migrations.py`: the versioned, step-by-step schema migrations (`migrate_database`), kept out of `database.py` as a dependency-injected function so this large block stays self-contained and individually testable.
- `media/`: the per-media-type sub-controllers (`AlbumsController`, `ArtistsController`, `TracksController`, `RadioController`, `PlaylistController`, `AudiobooksController`, `PodcastsController`, `GenreController`), all sharing `MediaControllerBase`. `MusicController` instantiates one of each and delegates per-type work to them.
- `constants.py`: config keys, the database schema version, background-task ids and tuning constants.
- `helpers.py`: stateless helper functions (needing no controller state) used by the controller.
- `strings.json`: translatable strings for this module (`core.music.*`), including the `manifest` name/description.

## Architecture & Design Notes

- **Layering / dependency direction.** `MusicController` is the orchestrator; the `media/` sub-controllers hold the type-specific logic. The sub-controllers never import `MusicController` back, keeping the dependency direction one-way and avoiding import cycles.
- **Database split via mixin.** The schema and migration code lives in `MusicDatabaseSetupMixin` (`database.py`) so `controller.py` stays focused on orchestration. The mixin carries no state of its own — it operates on its host (`mass`, `logger`, the `database` connection, the media sub-controllers and a couple of controller methods), declared under `TYPE_CHECKING`.
- **Startup order.** As a singleton core controller it is set up once: the database is initialized first (via the mixin), then maintenance tasks are registered and provider syncs are scheduled.
- **Library data model.** Library items use the provider id `library`; provider mappings record which provider item(s) a library item resolves to. Maintenance prunes orphaned mappings and playlog rows.
- **Schema migrations** are versioned against `DB_SCHEMA_VERSION`: on a version mismatch the database file is backed up before migrating, and a failed migration falls back to a fresh database (triggering a full rescan) so the user is never left without a working library.

## Schema Versions on `dev` vs `stable`

`DB_SCHEMA_VERSION` is a single integer, so it can only describe one linear history. `stable` normally inherits dev's numbering through releases, but a **schema-changing bugfix backported to `stable`** is renumbered against stable's own (lower) counter — from that point the same integer means something different on each branch. A database coming from `stable` then reports a version that is already higher than the `if prev_version <= N:` gates of the dev steps in the gap, so those steps never run and the schema objects they add stay missing. `__create_database_tables` cannot compensate: it is `CREATE TABLE IF NOT EXISTS`, so it never touches an existing table. This is not hypothetical — backports #4567 and #4798 were numbered 48 and 52 on dev but 42 and 43 on stable, so moving a stable database to the beta image leapfrogged dev's `<= 41` and `<= 42` steps and broke both the Playlists page and audiobook/podcast resume (#5515).

Renumbering the stable backport to match dev's number does not fix it either: the database would then claim to have run every dev step in between, which it genuinely never did. Encoding that correctly needs a per-step applied-migrations ledger (alembic-style), which is out of proportion to how rarely this triggers. So when backporting a schema change to `stable`:

1. Record in the backport PR that stable's `DB_SCHEMA_VERSION` now diverges from dev's, and which value it took.
2. On `dev`, bump `DB_SCHEMA_VERSION` and add an idempotent guard step re-adding every schema object introduced between the last shared version and stable's new one.
3. Gate that guard at dev's *current* version (`<= DB_SCHEMA_VERSION - 1`), never at stable's — `_setup_database` skips migration entirely when `prev_version` already equals `DB_SCHEMA_VERSION`, so users who upgraded and broke are stamped at the current version and a lower gate never fires for them.

## Future Enhancements

- Isolate the database connection itself into a dedicated layer (e.g. its own sub-controller), so connection ownership and the SQL/query surface live behind one boundary instead of on `MusicController`. Splitting the migrations (`migrations.py`) and grouping setup in `MusicDatabaseSetupMixin` are first steps toward that; the connection (`_database` and the `database` property) currently still lives on `MusicController`.
