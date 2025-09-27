# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.
This guidance is aimed at Claude Code but may as well be suitable for other AI tooling, such as Github CoPilot.

Instructions for an LLM (such as Claude) working with the code:

- Take these instructions in mind
- Look at existing provider implementations, type hints, docstrings and comments
- Propose changes to extend this document with new learnings.


## Project Overview

Music Assistant is a (async) Python 3 based music library manager that connects to streaming services and supports various connected speakers. It's designed to run as a server on always-on devices and integrates with Home Assistant.

## Development Commands

### Setup and Dependencies
- `scripts/setup.sh` - Initial development setup (creates venv, installs dependencies, configures pre-commit)
- Always re-run after pulling latest code as requirements may change

### Testing and Quality
- `pytest` - Run all tests
- `pytest tests/specific_test.py` - Run a specific test file
- `pytest --cov music_assistant` - Run tests with coverage (configured in pyproject.toml)
- `pre-commit run --all-files` - Run all pre-commit hooks

Always run `pre-commit run --all-files` after a code change to ensure the new code adheres to the project standards.

### Running the Server
- Use F5 in VS Code to start Music Assistant locally (debug mode)
- Or run from command line: `python -m music_assistant --log-level debug`
- Server runs on `localhost:8095`
- Entry point: `music_assistant.__main__:main`

## Architecture

### Core Components

1. **MusicAssistant (`music_assistant/mass.py`)** - Main orchestrator class
2. **Controllers (`music_assistant/controllers/`)** - Core functionality modules:
   - `music.py` - Music library management and provider orchestration
   - `players.py` - Player management and control
   - `player_queues.py` - Playback queue management
   - `streams.py` - Audio streaming logic
   - `webserver.py` - Web API server
   - `config.py` - Configuration management
   - `cache.py` - Caching layer
   - `metadata.py` - Metadata handling

3. **Models (`music_assistant/models/`)** - Base classes and interfaces:
   - `core_controller.py` - Base class/model for all core controllers
   - `music_provider.py` - Base for music providers
   - `player.py` - Base for players (provided by player providers)
   - `player_provider.py` - Base for player providers
   - `plugin.py` - Plugin system base

4. **Providers (`music_assistant/providers/`)** - External service integrations:
   - Music providers (Spotify, Apple Music, Tidal, etc.)
   - Player providers (Sonos, Chromecast, AirPlay, etc.)
   - Metadata providers (MusicBrainz, TheAudioDB, etc.)
   - Plugin providers for additional functionality (such as spotify connect or lastfm scrobbling)

5. **Helpers (`music_assistant/helpers/`)** - Utility modules for common tasks

### Provider Architecture

Providers are modular components that extend Music Assistant's capabilities:

- **Music Providers**: Add music sources (streaming services, local files)
- **Player Providers**: Add playback targets (speakers, media players)
- **Metadata Providers**: Add metadata sources (cover art, lyrics, etc.)
- **Plugin Providers**: Add additional functionality

Each provider has (at least):
- `__init__.py` - Main provider logic
- `manifest.json` - Provider metadata and configuration schema
- many providers choose to split up the code into several smaller files for readability and maintenance.

Template providers are available in `_demo_*_provider` directories.
These demo/example implementations have a lot of docstrings and comments to help you setup a new provider.

### Data Flow

1. **Music Library**: Controllers sync data from music providers to internal database
2. **Playback**: Stream controllers handle audio streaming to player providers
3. **Queue Management**: Player queues manage playback state and track progression
4. **Web API**: Webserver controller exposes REST API for frontend communication

## Key Configuration

- **Python**: 3.12+ required
- **Dependencies**: Defined in `pyproject.toml`
- **Database**: SQLite via aiosqlite
- **Async**: Heavy use of asyncio throughout codebase
- **External Dependencies**: ffmpeg (v6.1+), various provider-specific binaries

## Development Notes

- Uses ruff for linting/formatting (config in pyproject.toml)
- Type checking with mypy (strict configuration)
- Pre-commit hooks for code quality
- Test framework: pytest with async support
- Docker-based deployment (not standalone pip package)
- VS Code launch configurations provided for debugging

## Branching Strategy

### Branch Structure
- **`dev`** - Primary development branch, all PRs target this branch
- **`stable`** - Stable release branch for production releases

### Release Process
- **Beta releases**: Released from `dev` branch on-demand when new features are stable
- **Stable releases**: Released from `stable` branch on-demand
- **Patch releases**: Cherry-picked from `dev` to `stable` (bugfixes only)

### Versioning
- **Beta versions**: Next minor version + beta number (e.g., `2.6.0b1`, `2.6.0b2`)
- **Stable versions**: Standard semantic versioning (e.g., `2.5.5`)
- **Patch versions**: Increment patch number (e.g., `2.5.5` → `2.5.6`)

### PR Workflow
1. Contributors create PRs against `dev`
2. PRs are labeled with type: `bugfix`, `maintenance`, `new feature`, etc.
3. PRs with `backport-to-stable` label are automatically backported to `stable`

### Backport Criteria
- **`backport-to-stable`**: Only for `bugfix` PRs that fix bugs also present in `stable`
- **`bugfix`**: General bugfix label (may be for dev-only features)
- Automated backport workflow creates patch release PRs to `stable`

## Testing

- Tests located in `tests/` directory
- Fixtures for test data in `tests/fixtures/`
- Provider-specific tests in `tests/providers/`
- Uses pytest-aiohttp for async web testing
- Syrupy for snapshot testing
