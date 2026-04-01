# CLAUDE.md

Music Assistant is an async Python music library manager that connects to streaming services and speakers, integrating with Home Assistant.

## Development Commands

- `scripts/setup.sh` - Initial setup (venv, dependencies, pre-commit hooks). Re-run after pulling latest code.
- `pytest` - Run all tests
- `pytest tests/specific_test.py` - Run a specific test file
- `pre-commit run --all-files` - Run all pre-commit hooks
- `python -m music_assistant --log-level debug` - Run server locally (localhost:8095)
- Requires ffmpeg v6.1+ and Python 3.12+

Always run `pre-commit run --all-files` after a code change to ensure the new code adheres to the project standards.

## Provider Development

Providers are modular: music (sources), player (speakers), metadata (art/lyrics), plugin (extras). See `_demo_*_provider` directories for annotated templates when creating new providers.

Each provider has at least `__init__.py` (logic) and `manifest.json` (metadata/config schema).

Check `helpers/` for reusable utilities before writing new ones.

## Code Style

### Comments

Only use comments to explain complex, multi-line blocks of code. Do not comment obvious operations.

### Docstring Format

Use Sphinx-style docstrings with `:param:` syntax. For simple functions, a single-line docstring is fine.
Don't explain inner workings of the code in the docstrings (you can use inline comments for that if/when needed). The docstring should provide clarity to the caller of the function/method, not explain how it works technically/internally.

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

## Branching and PRs

- All PRs target `dev` (primary development branch). `stable` is for production releases.
- PRs labeled `bugfix` + `backport-to-stable` are automatically backported to `stable` — use only for bugs also present in `stable`.

## Debugging

MA stores its data in `$HOME/.musicassistant/`. When debugging locally:

- **Logs:** `$HOME/.musicassistant/musicassistant.log` (current), `musicassistant.log.1`, `.log.2`, etc. for older rotated logs.
- **Database:** `$HOME/.musicassistant/library.db` — query via `sqlite3`. **Only execute SELECT queries** — never write to a live database.
