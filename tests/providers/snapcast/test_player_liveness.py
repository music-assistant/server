"""Tests for Snapcast player liveness detection (abrupt power-off)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

from music_assistant.providers.snapcast.constants import SNAPCLIENT_STALE_THRESHOLD
from music_assistant.providers.snapcast.player import SnapCastPlayer

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant
    from music_assistant.providers.snapcast.snap_cntrl_proto import SnapclientProto


class _Clock:
    """Controllable monotonic clock stand-in for loop.time()."""

    def __init__(self) -> None:
        self.now = 1000.0

    def time(self) -> float:
        """Return the current (fake) monotonic time."""
        return self.now


def _make_player(clock: _Clock, *, connected: bool, last_seen_sec: int) -> SnapCastPlayer:
    player = SnapCastPlayer.__new__(SnapCastPlayer)
    player._last_seen_raw = None
    player._last_seen_changed = 0.0
    player.mass = cast("MusicAssistant", SimpleNamespace(loop=clock))
    player.snap_client = cast(
        "SnapclientProto",
        SimpleNamespace(
            connected=connected,
            _client={"lastSeen": {"sec": last_seen_sec, "usec": 0}},
        ),
    )
    return player


def test_alive_while_lastseen_advances() -> None:
    """A connected client stays available as long as lastSeen keeps advancing."""
    clock = _Clock()
    player = _make_player(clock, connected=True, last_seen_sec=500)
    assert player._is_alive() is True
    for _ in range(5):
        clock.now += SNAPCLIENT_STALE_THRESHOLD + 1
        player.snap_client._client["lastSeen"]["sec"] += 1
        assert player._is_alive() is True


def test_becomes_unavailable_when_lastseen_frozen() -> None:
    """A connected client whose lastSeen freezes flips to unavailable past the threshold."""
    clock = _Clock()
    player = _make_player(clock, connected=True, last_seen_sec=500)
    assert player._is_alive() is True
    clock.now += SNAPCLIENT_STALE_THRESHOLD - 1
    assert player._is_alive() is True
    clock.now += 2
    assert player._is_alive() is False


def test_disconnected_is_unavailable() -> None:
    """A client reported disconnected by the server is unavailable regardless of lastSeen."""
    clock = _Clock()
    player = _make_player(clock, connected=False, last_seen_sec=500)
    assert player._is_alive() is False


def test_recovers_after_reconnect() -> None:
    """A stale client becomes available again once lastSeen resumes advancing."""
    clock = _Clock()
    player = _make_player(clock, connected=True, last_seen_sec=500)
    assert player._is_alive() is True
    clock.now += SNAPCLIENT_STALE_THRESHOLD + 1
    assert player._is_alive() is False
    player.snap_client._client["lastSeen"]["sec"] += 1
    assert player._is_alive() is True
