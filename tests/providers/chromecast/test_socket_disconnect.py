"""
Tests for how ChromecastPlayer closes its Cast socket when it is unloaded.

During shutdown the socket thread is deliberately not waited for: it is a daemon thread
that dies with the process, and waiting on it stalls the shutdown. pychromecast reports
that skipped wait as a TimeoutError, which must not escape the unload.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast
from unittest.mock import MagicMock

from music_assistant.providers.chromecast.player import ChromecastPlayer

from .conftest import FakeChromecast


def _fake_player(chromecast: Any, *, closing: bool) -> Any:
    """
    Build a Cast player on mocked collaborators, so it disconnects a real Cast connection.

    :param chromecast: Cast connection the player's on_unload should disconnect.
    :param closing: Whether Music Assistant is shutting down.
    """
    # __init__ is skipped: it needs a provider, cast info and a live Chromecast connection.
    # Typed as Any because the collaborators below are read back as the mocks they are.
    fake = cast("Any", ChromecastPlayer.__new__(ChromecastPlayer))
    fake.mass = MagicMock()
    fake.mass.closing = closing
    fake.logger = MagicMock()
    fake.cc = chromecast
    fake.mz_controller = None
    fake.status_listener = MagicMock()
    fake._app_quit_task_id = "cast_quit_app_test"
    # display_name is a cached property, normally built from the config in __init__
    fake._cache = {"display_name": "Test Cast"}
    # used by the base Player.on_unload
    fake._on_unload_callbacks = []
    return fake


async def test_unload_while_closing_closes_socket_without_waiting(
    live_chromecast_factory: Callable[[], FakeChromecast],
) -> None:
    """A shutdown closes the socket but does not wait for the daemon thread to exit."""
    chromecast = live_chromecast_factory()
    player = _fake_player(chromecast, closing=True)

    await player.on_unload()

    assert chromecast.socket_client.disconnect_called is True
    assert chromecast.socket_client.is_alive() is True


async def test_unload_while_running_waits_for_the_socket_thread() -> None:
    """Outside of shutdown, disconnecting still blocks for the socket thread to exit."""
    chromecast = MagicMock()
    player = _fake_player(chromecast, closing=False)

    await player.on_unload()

    chromecast.disconnect.assert_called_once_with(10)
