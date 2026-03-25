# Tests

## Running tests

```bash
pytest                    # everything (unit + E2E)
pytest tests/unit/        # unit tests only (fast, no Docker)
pytest tests/e2e/         # E2E tests only (testcontainers manages Docker)
pytest tests/core/        # existing core tests
pytest tests/providers/   # existing provider tests
```

## Coverage targets

| Area | Target |
|------|--------|
| Core controllers (`music.py`, `players/`, `player_queues.py`, `streams/`) | 80%+ |
| Helpers/utilities | 90%+ |
| Provider parsers/converters | 80%+ |
| Provider API clients | 50–60% |

The 80% global floor is enforced in CI via `--cov-fail-under=80`. Per-area targets are documented here and visible in coverage reports.

## Test layout

- `tests/core/` — existing unit tests for core controllers and helpers
- `tests/providers/` — existing provider-specific tests
- `tests/unit/` — new unit tests for core controllers
- `tests/e2e/` — BDD-style end-to-end tests (in-process MA, WireMock for HTTP replay)
- `tests/support/` — shared test infrastructure (harness, mocks, fixture factories)
- `tests/fixtures/` — static test audio files

## E2E test style

All E2E tests must use explicit BDD-style inline comments. This is a requirement:

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

## WireMock / Docker

E2E provider integration tests use WireMock via testcontainers. Docker must be running locally. In CI, Docker is available on `ubuntu-latest` runners automatically.
