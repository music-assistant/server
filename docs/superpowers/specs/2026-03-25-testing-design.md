# Testing Strategy Design

**Date:** 2026-03-25
**Status:** Approved
**Scope:** Comprehensive unit + E2E test coverage for music-assistant-server

---

## Problem Statement

The repository has ~60 test files against 389 source files. Coverage is concentrated in a handful of providers (Tidal, Yandex, Kion) and some core helpers. The core controllers (`music.py`, `players.py`, `player_queues.py`, `streams.py`) have almost no test coverage, and there are no end-to-end tests covering full user journeys.

---

## Goals

1. BDD-style E2E tests covering all main user journeys
2. High unit test coverage across core controllers, helpers, and provider parsers
3. Simple, memorable commands to run tests — no script sprawl
4. Coverage targets enforced in CI, not just reported
5. Targets documented in obvious places

---

## Approach: Infrastructure-First, Then Agent Team Blitz

A single session builds the shared test infrastructure first. Once that foundation is solid, a 5-teammate agent team writes tests in parallel across non-overlapping file sets.

---

## Coverage Targets

| Area | Target |
|------|--------|
| Core controllers (`music.py`, `players.py`, `player_queues.py`, `streams.py`) | 80%+ |
| Helpers/utilities | 90%+ |
| Provider parsers/converters | 80%+ |
| Provider API clients | 50–60% |
| E2E scenarios | All main user journeys (see below) |

These targets are also documented in `tests/README.md` and `CLAUDE.md`.

**E2E scenarios to cover:**
- Library sync (provider → internal database)
- Search (query → results from provider)
- Queue a track (track → player queue)
- Playback (play, skip, stop)
- Player control (volume, grouping, state transitions)
- Provider HTTP API replay (WireMock-based)

---

## Test Commands

```bash
pytest                    # run everything (unit + E2E)
pytest tests/unit/        # unit tests only (fast, no Docker)
pytest tests/e2e/         # E2E tests only (testcontainers manages Docker)
```

No shell scripts. No manual docker commands. Testcontainers handles all container lifecycle inside pytest fixtures.

---

## Directory Structure

```
tests/
├── conftest.py                        # extend with harness fixtures
├── common.py                          # extend MockPlayer/MockProvider
├── helpers/
│   ├── harness.py                     # MusicAssistantHarness
│   ├── mock_music_provider.py         # configurable MockMusicProvider
│   ├── mock_player_provider.py        # extended MockPlayerProvider + MockPlayer
│   ├── fixture_factory.py             # Track/Album/Artist/Playlist builders
│   └── wiremock.py                    # WireMock testcontainers fixture
├── unit/
│   ├── controllers/
│   │   ├── test_music.py
│   │   ├── test_players.py
│   │   ├── test_player_queues.py
│   │   └── test_streams.py
│   ├── helpers/                       # existing helper tests (moved here)
│   └── providers/                     # existing provider tests (moved here)
├── e2e/
│   ├── conftest.py                    # harness fixture scoped for E2E
│   ├── test_library_sync.py
│   ├── test_search.py
│   ├── test_playback.py
│   ├── test_player_control.py
│   └── test_provider_integration.py  # WireMock-based
└── README.md                          # coverage targets + how to run
```

---

## Architecture Decisions

### In-Process MA Server for E2E

The MA server runs in-process (using the existing `mass` fixture pattern) rather than containerized. Reasons:

- Injecting mock providers and players is trivial in-process; containerized it would require those mocks to be network-accessible services
- Much faster — no container build/startup per run
- Full Python stack traces and log capture for debuggability
- The `mass` fixture already starts a real MA instance with real controllers, real SQLite, and real async event loop — that is a meaningful E2E test

### Docker via Testcontainers (WireMock only)

Docker is used exclusively for **WireMock** — a container that replays recorded HTTP responses for provider integration tests. This tests the full provider HTTP client + parser stack against realistic API response shapes without hitting real external services.

The WireMock container is a session-scoped pytest fixture:

```python
@pytest.fixture(scope="session")
def wiremock():
    with WireMockContainer() as wm:
        yield wm
```

No docker-compose files. No manual container management.

### BDD-Style Tests (pytest, no .feature files)

E2E tests use explicit inline BDD comments within regular pytest. No Gherkin, no `pytest-bdd` — the codebase is heavily async and developer-facing, so the ceremony of `.feature` files adds no value.

```python
async def test_track_plays_after_queue_add(harness, mock_player, mock_provider):
    """Given a provider with tracks, when a track is queued and play is triggered,
    the player receives the stream."""
    # Given a provider with tracks
    track = await mock_provider.get_track("track-1")

    # When a track is queued
    await harness.mass.player_queues.play_media(mock_player.player_id, track)

    # And play is triggered
    await harness.mass.players.cmd_play(mock_player.player_id)

    # Then the player receives the stream
    assert mock_player.current_track.item_id == track.item_id
```

---

## Test Infrastructure

### MusicAssistantHarness

Wraps the existing `mass` fixture with convenience methods:

```python
class MusicAssistantHarness:
    mass: MusicAssistant

    async def add_provider(self, provider: MockMusicProvider) -> None: ...
    async def add_player(self, player: MockPlayer) -> MockPlayer: ...
    async def sync_library(self, provider_id: str) -> None: ...
    async def wait_for_event(self, event_type: EventType, timeout: float = 5.0) -> MassEvent: ...
```

### MockMusicProvider

Implements the full music provider interface with configurable fixture data:

```python
class MockMusicProvider(MusicProvider):
    def __init__(self, tracks=None, albums=None, artists=None, playlists=None): ...
    async def search(self, search_query, media_types, limit) -> SearchResults: ...
    async def get_library_tracks(self) -> AsyncGenerator[Track]: ...
    async def get_stream_details(self, item_id) -> StreamDetails: ...
```

### fixture_factory.py

Builder functions for all model types so tests don't hand-roll data:

```python
def make_track(item_id="1", name="Test Track", artist="Test Artist", ...) -> Track: ...
def make_album(item_id="1", name="Test Album", ...) -> Album: ...
def make_artist(item_id="1", name="Test Artist", ...) -> Artist: ...
def make_playlist(item_id="1", name="Test Playlist", ...) -> Playlist: ...
```

---

## Agent Team Structure

After the infra teammate completes, four teammates run in parallel:

| Teammate | Owns | Blocked by |
|----------|------|-----------|
| **infra** | `tests/helpers/`, `tests/conftest.py`, `tests/README.md`, coverage targets in `CLAUDE.md` | — |
| **controllers-1** | `tests/unit/controllers/test_music.py`, `test_players.py` | infra |
| **controllers-2** | `tests/unit/controllers/test_player_queues.py`, `test_streams.py` | infra |
| **helpers-providers** | `tests/unit/helpers/`, `tests/unit/providers/` | infra |
| **e2e** | `tests/e2e/` | infra |

Each teammate runs `pytest` on their own files and must see green output before marking tasks done. The lead runs the full `pytest` suite with coverage at the end and verifies all targets are met before declaring the team done.

---

## CI Integration

Extends the existing `test.yml` — no new workflow files:

```yaml
- name: Run unit tests
  run: pytest tests/unit/ --cov=music_assistant --cov-report=xml

- name: Run E2E tests
  run: pytest tests/e2e/ --cov=music_assistant --cov-append --cov-report=xml
```

Coverage from both runs is merged (`--cov-append`). `--cov-fail-under=80` enforces the core controller target at the CI level — not just reported, hard-fails if missed.

`.coveragerc` provides per-module overrides (e.g., 90% for helpers, 50% for provider API clients).

---

## Non-Goals

- Containerizing the MA server for E2E (in-process is sufficient and much simpler)
- Gherkin `.feature` files or `pytest-bdd` framework
- Shell scripts or manual docker commands
- Testing every provider's API client exhaustively (50-60% is sufficient)
- Backwards-compatible test shims for removed code
