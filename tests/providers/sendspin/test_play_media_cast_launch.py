"""
Tests for play_media on a Sendspin player bridged to a Cast device.

The stream start used to launch the Sendspin Cast app, which takes several seconds: the
stream ran on meanwhile, and the device missed the start of every track whenever the
app wasn't running yet. play_media must launch the app and wait for it to report ready
before it starts the playback session.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, PropertyMock, patch

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import PlayerCommandFailed
from music_assistant_models.player import PlayerMedia

from music_assistant.providers.sendspin.player import SendspinPlayer


def _player(ready: asyncio.Future[None]) -> tuple[SendspinPlayer, Mock, Mock, list[str]]:
    """
    Build a real (but un-__init__'d) SendspinPlayer bridged to a Cast device.

    :param ready: The future the bridge hands out for the Cast app's readiness.
    :return: The player, its playback session, its bridge, and the call order.
    """
    calls: list[str] = []
    player = object.__new__(SendspinPlayer)
    player._player_id = "p1"
    player.logger = Mock()
    session = Mock(
        cancel=AsyncMock(),
        start=AsyncMock(side_effect=lambda _media: calls.append("start")),
    )
    player.playback_session = session
    bridge = Mock()
    bridge.refresh_cast_app_state = AsyncMock(side_effect=lambda: calls.append("refresh"))

    def reset_cast_app_ready() -> asyncio.Future[None]:
        calls.append("reset")
        return ready

    bridge.reset_cast_app_ready = Mock(side_effect=reset_cast_app_ready)
    bridge.launch_cast_app = Mock(side_effect=lambda: calls.append("launch"))
    manager = Mock()
    manager.get_bridge_by_client_id.return_value = bridge
    player._get_cast_bridge_manager = Mock(return_value=manager)  # type: ignore[method-assign]
    player._state = SimpleNamespace(current_media=None)  # type: ignore[assignment]
    player.update_state = Mock()  # type: ignore[method-assign,misc]
    player.mass = Mock(create_task=Mock(side_effect=lambda coro, **_kwargs: coro.close()))
    return player, session, bridge, calls


def _media() -> PlayerMedia:
    return PlayerMedia(
        uri="track-1",
        media_type=MediaType.TRACK,
        title="Track 1",
        source_id="queue-1",
        queue_item_id="item-1",
    )


async def _play_media(player: SendspinPlayer) -> None:
    with (
        patch.object(SendspinPlayer, "synced_to", new_callable=PropertyMock, return_value=None),
        patch.object(
            SendspinPlayer, "protocol_parent_id", new_callable=PropertyMock, return_value=None
        ),
        patch.object(SendspinPlayer, "display_name", new_callable=PropertyMock, return_value="P"),
        patch.object(
            SendspinPlayer, "translation_owner", new_callable=PropertyMock, return_value="sendspin"
        ),
    ):
        await SendspinPlayer.play_media(player, _media())


async def test_waits_for_the_cast_app_before_starting_the_stream() -> None:
    """A Cast app that isn't running is launched, and the stream starts once it is ready."""
    ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    player, session, _, calls = _player(ready)

    call = asyncio.create_task(_play_media(player))
    for _ in range(5):
        await asyncio.sleep(0)

    assert calls == ["refresh", "reset", "launch"]
    session.start.assert_not_awaited()

    ready.set_result(None)
    await call

    assert calls == ["refresh", "reset", "launch", "start"]


async def test_running_cast_app_is_not_launched_again() -> None:
    """A Cast app that is already running and connected starts the stream right away."""
    ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    ready.set_result(None)
    player, _, bridge, calls = _player(ready)

    await _play_media(player)

    bridge.launch_cast_app.assert_not_called()
    assert calls == ["refresh", "reset", "start"]


async def test_failed_launch_never_starts_the_stream() -> None:
    """A Cast app that fails to start fails the command without streaming to the device."""
    ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    player, session, _, _ = _player(ready)

    call = asyncio.create_task(_play_media(player))
    await asyncio.sleep(0)
    ready.set_exception(PlayerCommandFailed("the Cast receiver refused the launch"))

    with pytest.raises(PlayerCommandFailed, match="refused the launch"):
        await call

    session.start.assert_not_awaited()
    session.cancel.assert_awaited_with("cast app readiness failed")
