# Music Assistant - Copilot Instructions

## Project Overview
Music Assistant is an async Python 3.12+ server that manages music libraries from streaming services and controls connected speakers. It runs on always-on devices and integrates with Home Assistant.

## Architecture

### Core Structure
- **`mass.py`** - Main orchestrator (`MusicAssistant` class) initializes all controllers and providers
- **Controllers** (`controllers/`) - Core functionality modules that manage different aspects:
  - `music.py` - Library management, syncs from providers to SQLite database
  - `players/player_controller.py` - Player state and control
  - `player_queues.py` - Playback queue management
  - `streams/` - Audio streaming pipeline
  - `webserver.py` - REST/WebSocket API server (port 8095)
- **Providers** (`providers/`) - Modular plugins for external integrations
- **Models** (`models/`) - Base classes providers must inherit from
- **Helpers** (`helpers/`) - Shared utilities

### Provider Types
Providers extend MA capabilities by inheriting from base models:
- **Music** (`MusicProvider`) - Sources like Spotify, Tidal, local files
- **Player** (`PlayerProvider`) - Playback targets like Sonos, Chromecast
- **Metadata** (`MetadataProvider`) - Cover art, lyrics from MusicBrainz, etc.
- **Plugin** - Additional features like scrobbling

Each provider requires:
- `__init__.py` - Implementation inheriting from base model
- `manifest.json` - Metadata, config schema, requirements

**Reference implementations:**
- Templates: `_demo_music_provider/`, `_demo_player_provider/`
- Complex example: `spotify/` (multiple files, external binary)

### Data Flow
1. Music providers sync → SQLite database (`aiosqlite`)
2. Stream controllers → Audio to player providers
3. API exposed via WebSocket/REST with `@api_command` decorator

## Development Commands

```bash
# Initial setup (creates venv, installs deps, pre-commit)
scripts/setup.sh

# Run pre-commit after changes (REQUIRED before PR)
pre-commit run --all-files

# Run tests
pytest
pytest tests/specific_test.py
pytest --cov music_assistant

# Start server (VS Code F5 or)
python -m music_assistant --log-level debug
```

**Important:** Re-run `scripts/setup.sh` after pulling - requirements may change.

## Code Conventions

### Async Pattern
Everything is async. Use `asyncio.to_thread()` for blocking calls from non-async libraries:
```python
result = await asyncio.to_thread(blocking_function, arg1, arg2)
```

### Docstrings (Sphinx-style)
```python
def my_function(param1: str, param2: int) -> str:
    """Brief description.

    :param param1: Description of param1.
    :param param2: Description of param2.
    """
```

### API Commands
Register API endpoints with the `@api_command` decorator:
```python
@api_command("players/cmd/play")
async def cmd_play(self, player_id: str) -> None:
    """Start playback."""
```

### Provider Features
Providers declare capabilities via `ProviderFeature` enum:
```python
SUPPORTED_FEATURES = {
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
    ProviderFeature.LIBRARY_ARTISTS,
}
```

## Provider Configuration

### manifest.json Structure
Every provider needs a `manifest.json` with these fields:

```json
{
  "type": "music",              // "music", "player", "metadata", or "plugin"
  "domain": "my_provider",      // Unique provider ID (snake_case)
  "name": "My Provider",        // Display name
  "description": "...",         // User-facing description
  "codeowners": ["@github_user"],
  "requirements": ["package==1.0.0"],  // pip dependencies
  "documentation": "https://...",
  "multi_instance": true,       // Allow multiple accounts/configs
  "stage": "stable",            // "stable" or "experimental"
  "builtin": false,             // Auto-loaded without user setup
  "icon": "spotify",            // Material Design icon name (optional)
  "mdns_discovery": ["_airplay._tcp.local."]  // Player providers only
}
```

### ConfigEntry Pattern
Define user-configurable settings via `get_config_entries()`:

```python
from music_assistant_models.config_entries import ConfigEntry, ConfigValueType
from music_assistant_models.enums import ConfigEntryType

async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,  # None for new setup
    action: str | None = None,       # For multi-step flows (e.g., OAuth)
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    return (
        ConfigEntry(
            key="username",
            type=ConfigEntryType.STRING,
            label="Username",
            required=True,
        ),
        ConfigEntry(
            key="password",
            type=ConfigEntryType.SECURE_STRING,  # Encrypted storage
            label="Password",
            required=True,
        ),
        ConfigEntry(
            key="quality",
            type=ConfigEntryType.STRING,
            default_value="high",
            options=[ConfigValueOption("High", "high"), ConfigValueOption("Low", "low")],
            label="Audio Quality",
        ),
    )
```

**ConfigEntryType options:** `STRING`, `SECURE_STRING`, `INTEGER`, `BOOLEAN`, `FLOAT`, `LABEL`, `ALERT`

**Access values in provider:** `self.config.get_value("username")`

**OAuth flows:** Use `action` parameter with `AuthenticationHelper` (see `deezer/` provider)

## Key External Dependencies
- **ffmpeg 6.1+** - Required for audio processing
- **music-assistant-models** - Shared models (separate repo)
- **music-assistant-frontend** - Pre-built UI served at port 8095

## Testing
- Tests in `tests/` with pytest + pytest-aiohttp
- Fixtures in `tests/fixtures/`
- Use `mass` fixture for MusicAssistant instance in tests:
```python
async def test_something(mass: MusicAssistant) -> None:
    ...
```

## Branching
- **`dev`** - All PRs target this branch
- **`stable`** - Production releases
- Bugfixes use `backport-to-stable` label for cherry-picking
