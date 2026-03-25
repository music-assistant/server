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

| Area | Target | CI Enforcement |
|------|--------|---------------|
| Core controllers (`music.py`, `players.py`, `player_queues.py`, `streams.py`) | 80%+ | Global `--cov-fail-under=80` hard-fails CI |
| Helpers/utilities | 90%+ | Documented; reported in coverage output |
| Provider parsers/converters | 80%+ | Documented; reported in coverage output |
| Provider API clients | 50–60% | Documented; reported in coverage output |
| E2E scenarios | All main user journeys (see below) | Test suite must be green |

**Note on per-module enforcement:** `--cov-fail-under` in pytest-cov supports a single global threshold only. The 80% global floor is the hard CI gate. Per-area targets above 80% (helpers: 90%, parsers: 80%) are documented and visible in coverage reports but not independently enforced via tooling. Agents must treat them as goals, not suggestions.

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

No shell scripts. No manual docker commands. Testcontainers handles all container lifecycle inside pytest fixtures. GitHub Actions `ubuntu-latest` runners have Docker available — no additional runner configuration needed.

---

## Directory Structure

Existing tests in `tests/core/` and `tests/providers/` **stay in place**. New unit tests go into `tests/unit/`. Do not move existing tests — `tests/common.py` builds fixture paths relative to `__file__` and moving files would silently break fixture loading.

```
tests/
├── conftest.py                        # extend with harness fixtures
├── common.py                          # extend MockPlayer/MockProvider
├── support/                           # shared test infrastructure (new)
│   ├── harness.py                     # MusicAssistantHarness
│   ├── mock_music_provider.py         # configurable MockMusicProvider
│   ├── mock_player_provider.py        # extended MockPlayerProvider + MockPlayer
│   ├── fixture_factory.py             # Track/Album/Artist/Playlist builders
│   └── wiremock.py                    # WireMock testcontainers fixture
├── core/                              # existing — do not move
├── providers/                         # existing — do not move
├── unit/                              # new unit tests only
│   └── controllers/
│       ├── test_music.py
│       ├── test_players.py
│       ├── test_player_queues.py
│       └── test_streams.py
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

**Port conflict fix (infra task):** The existing `mass` fixture has a known issue where tests fail if MA is already running on port 8095. The infra teammate must fix this by configuring the test server to bind to a random available port. This is a prerequisite for E2E tests to run reliably in CI.

### Docker via Testcontainers (WireMock only)

Docker is used exclusively for **WireMock** — a container that replays recorded HTTP responses for provider integration tests. This tests the full provider HTTP client + parser stack against realistic API response shapes without hitting real external services.

The WireMock container is a session-scoped pytest fixture:

```python
@pytest.fixture(scope="session")
def wiremock():
    with WireMockContainer() as wm:
        yield wm
```

No docker-compose files. No manual container management. The `testcontainers` and `wiremock` packages must be added to `pyproject.toml` under `[project.optional-dependencies] test`.

### BDD-Style Tests (pytest, no .feature files)

E2E tests use explicit inline BDD comments within regular pytest. No Gherkin, no `pytest-bdd`.

**Rule:** Every E2E test function must use `# Given`, `# When`, `# And`, `# Then` inline comments to label each logical step. The docstring must also summarise the scenario in one sentence. This is a requirement, not a style suggestion — agents must not write E2E tests without these comments.

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

Implements the full music provider interface with configurable fixture data. Supports a configurable failure mode so tests can exercise not-found and error paths:

```python
class MockMusicProvider(MusicProvider):
    def __init__(self, tracks=None, albums=None, artists=None, playlists=None,
                 fail_stream: bool = False): ...
    async def search(self, search_query, media_types, limit) -> SearchResults: ...
    async def get_library_tracks(self) -> AsyncGenerator[Track]: ...
    async def get_stream_details(self, item_id: str) -> StreamDetails | None:
        # Returns None if fail_stream=True or item_id not found
        ...
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

After the infra teammate completes, four teammates run in parallel. No teammate may be marked done until the lead's full-suite `pytest` run passes with coverage — individual green runs are necessary but not sufficient.

| Teammate | Owns | Blocked by |
|----------|------|-----------|
| **infra** | `tests/support/`, `tests/conftest.py`, `tests/README.md`, coverage targets in `CLAUDE.md`, port-conflict fix | — |
| **controllers-1** | `tests/unit/controllers/test_music.py`, `test_players.py` | infra |
| **controllers-2** | `tests/unit/controllers/test_player_queues.py`, `test_streams.py` | infra |
| **helpers-providers** | new tests alongside existing `tests/core/` and `tests/providers/` | infra |
| **e2e** | `tests/e2e/` | infra |

Each teammate runs `pytest` on their own files and must see green output before marking their task done. After all teammates are done, the lead runs:

```bash
pytest --cov=music_assistant --cov-report=term-missing --cov-fail-under=80
```

If this fails, the team is **not done**. The lead identifies which teammate's area is below target and unblocks them to add more tests. Only a green full-suite run with passing coverage unlocks the "done" declaration.

---

## Dependencies to Add

Add to `pyproject.toml` under `[project.optional-dependencies] test`:

```toml
"testcontainers>=4.0",
"testcontainers[wiremock]>=4.0",
```

---

## CI Integration

Replace the existing single `pytest tests/` step in `test.yml` with two steps that preserve `--cov-append` merging:

```yaml
- name: Run unit tests
  run: pytest tests/unit/ tests/core/ tests/providers/ --cov=music_assistant --cov-report=xml

- name: Run E2E tests
  run: pytest tests/e2e/ --cov=music_assistant --cov-append --cov-report=xml --cov-fail-under=80
```

The `--cov-fail-under=80` is on the final (E2E) step so it evaluates the merged coverage from both runs. Docker is available on `ubuntu-latest` runners; testcontainers requires no additional setup.

---

## Non-Goals

- Containerizing the MA server for E2E (in-process is sufficient and much simpler)
- Gherkin `.feature` files or `pytest-bdd` framework
- Shell scripts or manual docker commands
- Testing every provider's API client exhaustively (50-60% is sufficient)
- Backwards-compatible test shims for removed code
- Per-module CI enforcement beyond the global 80% floor
