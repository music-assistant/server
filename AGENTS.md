# CLAUDE.md

Music Assistant is an async Python music library manager that connects to streaming services and speakers, integrating with Home Assistant.

## Behaviour

- NEVER automatically reply on Github (PR's or Discussions) without explicit consent from the developer.

## Usage policy

Music Assistant streams music to the user's own speakers. It is not a way to download or keep
copies of it, and we do not accept changes that make it one — see the
[Usage Policy](https://github.com/music-assistant/.github/blob/main/USAGE_POLICY.md).

Flag it in review, and never write it yourself, when a change would:

- expose a music service's own audio URL to anything outside the server (an API result, a
  media item, or a route that hands it to a caller)
- decode protected audio outside what the user's own account is entitled to, or keep the
  decoded result rather than only playing it
- call a service's API without throttling, or re-fetch what the cache already holds
- write decoded audio from a streaming provider to disk
- bypass a subscription tier, regional availability, or a service's stated concurrent-stream
  limit (see `max_concurrent_streams` in `music_assistant/models/music_provider.py`)
- add downloading, exporting, or archiving of provider audio, however it is framed

Several guards in the codebase exist only for this reason and read as removable if you do not
know that — the readrate pacing on the stream endpoints, the filesystem-only restriction on
background audio analysis, and the DRM skips in providers. Leave them in place. `DEVELOPMENT.md`
carries the same rules for provider authors.

## Development Commands

- `scripts/setup.sh` - Initial setup (venv, dependencies, pre-commit hooks). Re-run after pulling latest code.
- `pytest` - Run all tests (add `-n auto --dist loadfile` to run in parallel)
- `pytest --cov music_assistant` - Run all tests with coverage
- `pytest tests/specific_test.py` - Run a specific test file
- `pre-commit run --all-files` - Run all pre-commit hooks
- `python -m music_assistant --log-level debug` - Run server locally (localhost:8095)
- Requires ffmpeg v7.1+ and Python 3.14+ (see `.python-version` for the pinned runtime)

Always run `pre-commit run --all-files` after a code change to ensure the new code adheres to the project standards.

## Provider Development

Providers are modular: music (sources), player (speakers), metadata (art/lyrics), plugin (extras). See `_demo_*_provider` directories for annotated templates when creating new providers.

Each provider has at least `__init__.py` (logic) and `manifest.json` (metadata/config schema).

Check `helpers/` for reusable utilities before writing new ones.

## Code Style

### Comments

Only use comments to explain complex, multi-line blocks of code. Do not comment obvious operations. Inline comments in the code are there to explain code parts that need explaining, keep that in mind yourself when writing code but also respect existing comments from authors - they apparently had a reason to write the comment, dont remove them unless needed.

### Docstring Format

Use Sphinx-style docstrings with `:param:` syntax. For simple functions, a single-line docstring is fine.
Don't explain inner workings of the code in the docstrings (you can use inline comments for that if/when needed). The docstring should provide clarity to the caller of the function/method, not explain how it works technically/internally. Use our preference for multi line docstrings where the first line starts on the next line:

```python
def my_function(param1: str, param2: int, param3: bool = False) -> str:
    """
    Brief one-line description of the function.

    :param param1: Description of what param1 is used for.
    :param param2: Description of what param2 is used for.
    :param param3: Description of what param3 is used for.
    """
```

Do **not** use Google-style (`Args:`) or bullet-style (`- param:`) docstrings.

### File structure
Private methods should be at the bottom of the file, public at the top.

## Branching and PRs

- All PRs target `dev` (primary development branch). `stable` is for production releases.
- PRs labeled `bugfix` + `backport-to-stable` are automatically backported to `stable` — use only for bugs also present in `stable`.
- Backporting a **schema change** permanently diverges `DB_SCHEMA_VERSION` between the branches and needs an extra dev-side guard migration; see `music_assistant/controllers/music/README.md`.

## Debugging

MA stores its data in `$HOME/.musicassistant/`. When debugging locally:

- **Logs:** `$HOME/.musicassistant/musicassistant.log` (current), `musicassistant.log.1`, `.log.2`, etc. for older rotated logs.
- **Database:** `$HOME/.musicassistant/library.db` — query via `sqlite3`. **Only execute SELECT queries** — never write to a live database.

### Provider Mappings

Every `MediaItem` (track, album, artist, playlist) has a `provider_mappings` attribute containing the exact mappings of the item on (each) provider. For a MediaItem that comes from a musicprovider directly, this will usually contain one single ID-mapping (although it is possible that a provider has multiple mappings). For library items, this will contain all mappings of all providers connected to the item.

The ProviderMapping has this structure:

- `item_id` (str): The music provider's item ID (e.g., `"spotify--track123"`)
- `provider_domain` (str): The musicprovider's domain (e.g., `"spotify"`, `"apple_music"`, `"tidal"`)
- `provider_instance` (str): The provider instance ID (to handle multiple instances of the same provider).
- some more details such as the quality (relevant in case of album or track)

**Important patterns:**

- A media item itself also has an item_id and provider attribute. For an item that comes straight from the musicprovider itself (so not from the MA library) the provider attribute will be set to the instance_id of that provider.
- Library items are identified by `item.provider == "library"` where `item.item_id` is the library DB ID
- `provider_domain='library'` **never exists** in provider_mappings — library items don't have mappings to themselves
- To resolve a provider item to its library equivalent, use: `await mass.music.<mediatype>.get_library_item_by_prov_id(item_id, provider_instance_id_or_domain)`
- Never assume you can use `mapping.item_id` directly as a library DB ID — always use the resolution method above
