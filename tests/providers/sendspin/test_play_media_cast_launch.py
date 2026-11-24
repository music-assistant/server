"""
Tests for play_media on a Sendspin player bridged to a Cast device.

The stream start used to launch the Sendspin Cast app, which takes several seconds: the
stream ran on meanwhile, and the device missed the start of every track whenever the
app wasn't running yet. play_media must launch the app and wait for it to report ready
before it starts the playback session.

The same goes for the Cast-bridged members of the group a player leads: an app that is
only launched by the stream start misses a short clip altogether (support#7069).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, PropertyMock, patch

import pytest
from aiosendspin.models.types import PlaybackStateType
from music_assistant_models.enums import MediaType, PlaybackState
from music_assistant_models.errors import PlayerCommandFailed
from music_assistant_models.player import PlayerMedia

from music_assistant.providers.sendspin import player as player_module
from music_assistant.providers.sendspin.player import SendspinPlayer


def _player(ready: asyncio.Future[None]) -> tuple[SendspinPlayer, Mock, Mock, list[str]]:
    """
    Build a real (but un-__init__'d) SendspinPlayer bridged to a Cast device.

    :param ready: The future the bridge hands out for the Cast app's readiness.
    :return: The player, its playback session, its bridge, and the call order.
    """
    calls: list[str] = []
    player = object.__new__(SendspinPlayer)
    player._protocol_id = "p1"
    player.logger = Mock()

    async def cancel(_reason: str, *, keep_stream: bool = False) -> None:
        calls.append("cancel (keep stream)" if keep_stream else "cancel")

    session = Mock(
        cancel=AsyncMock(side_effect=cancel),
        start=AsyncMock(side_effect=lambda _media: calls.append("start")),
    )
    player.playback_session = session
    player.api = Mock(client_id="p1")
    player.api.group.has_active_stream = False
    player.api.group.clients = [player.api]
    bridge = Mock()

    async def refresh_cast_app_state() -> None:
        current = player._attr_current_media
        calls.append(f"refresh ({current.uri})" if current else "refresh")

    bridge.refresh_cast_app_state = AsyncMock(side_effect=refresh_cast_app_state)

    def reset_cast_app_ready() -> asyncio.Future[None]:
        calls.append("reset")
        return ready

    bridge.reset_cast_app_ready = Mock(side_effect=reset_cast_app_ready)
    bridge.launch_cast_app = Mock(side_effect=lambda: calls.append("launch"))
    manager = Mock()
    bridges = {"p1": bridge}
    manager.bridges = bridges
    manager.get_bridge_by_client_id = bridges.get
    player._get_cast_bridge_manager = Mock(return_value=manager)  # type: ignore[method-assign]
    player._player = SimpleNamespace(  # type: ignore[assignment]
        current_media=None, playback_state=PlaybackState.IDLE
    )
    player._attr_current_media = None
    player.update_state = Mock()  # type: ignore[method-assign,misc]
    player.mass = Mock(create_task=Mock(side_effect=lambda coro, **_kwargs: coro.close()))
    return player, session, bridge, calls


def _add_member(
    player: SendspinPlayer,
    client_id: str,
    ready: asyncio.Future[None],
    *,
    available: bool = True,
) -> Mock:
    """
    Add a Cast-bridged member to the group of the player.

    :param player: The player leading the group.
    :param client_id: The Sendspin client id of the member.
    :param ready: The future its bridge hands out for the Cast app's readiness.
    :param available: Whether the Cast device of the member is available.
    :return: The bridge of the member.
    """
    bridge = Mock()
    bridge.cast_player.available = available
    bridge.cast_player.display_name = client_id
    bridge.refresh_cast_app_state = AsyncMock()
    bridge.reset_cast_app_ready = Mock(return_value=ready)
    manager = player._get_cast_bridge_manager()
    assert manager is not None
    manager.bridges[client_id] = bridge  # type: ignore[attr-defined]
    player.api.group.clients.append(SimpleNamespace(client_id=client_id))  # type: ignore[arg-type]
    return bridge


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

    # the current playback is ended before the launch, not kept running meanwhile
    assert calls == ["refresh", "reset", "cancel", "launch"]
    session.start.assert_not_awaited()

    ready.set_result(None)
    await call

    assert calls == ["refresh", "reset", "cancel", "launch", "cancel (keep stream)", "start"]


async def test_running_cast_app_is_not_launched_again() -> None:
    """A Cast app that is already running and connected starts the stream right away."""
    ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    ready.set_result(None)
    player, _, bridge, calls = _player(ready)

    await _play_media(player)

    bridge.launch_cast_app.assert_not_called()
    assert calls == ["refresh", "reset", "cancel (keep stream)", "start"]


async def test_failed_launch_never_starts_the_stream() -> None:
    """A Cast app that fails to start fails the command without streaming to the device."""
    ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    player, session, _, calls = _player(ready)

    call = asyncio.create_task(_play_media(player))
    for _ in range(5):
        await asyncio.sleep(0)
    ready.set_exception(PlayerCommandFailed("the Cast receiver refused the launch"))

    with pytest.raises(PlayerCommandFailed, match="refused the launch"):
        await call

    session.start.assert_not_awaited()
    # the playback being replaced is ended, not kept for a successor that never comes
    assert calls == ["refresh", "reset", "cancel", "launch", "cancel"]
    session.cancel.assert_awaited_with("cast app readiness failed")


async def test_new_media_is_set_only_after_the_status_refresh() -> None:
    """The playback still running during the refresh keeps reporting under its own media."""
    ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    ready.set_result(None)
    player, _, _, calls = _player(ready)
    previous = _media()
    previous.uri = "track-0"
    player._attr_current_media = previous

    await _play_media(player)

    assert calls[0] == "refresh (track-0)"
    assert player._attr_current_media is not None
    assert player._attr_current_media.uri == "track-1"


async def test_stop_during_the_launch_does_not_start_playback() -> None:
    """A stop while the Cast app launches ends the wait without starting playback."""
    ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    player, session, _, calls = _player(ready)

    call = asyncio.create_task(_play_media(player))
    for _ in range(5):
        await asyncio.sleep(0)
    assert calls[-1] == "launch"

    SendspinPlayer._abort_pending_cast_start(player)
    await call

    session.start.assert_not_awaited()
    assert ready.cancelled()


async def test_stop_as_the_app_reports_ready_does_not_start_playback() -> None:
    """A stop that lands right as the app reports ready still keeps playback from starting."""
    ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    player, session, _, _ = _player(ready)

    call = asyncio.create_task(_play_media(player))
    for _ in range(5):
        await asyncio.sleep(0)
    ready.set_result(None)
    SendspinPlayer._abort_pending_cast_start(player)
    await call

    session.start.assert_not_awaited()


async def test_newer_play_media_replaces_one_waiting_for_the_app() -> None:
    """A play_media replaced while it waits returns quietly and leaves the session alone."""
    ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    player, session, _, calls = _player(ready)

    call = asyncio.create_task(_play_media(player))
    for _ in range(5):
        await asyncio.sleep(0)
    # what a newer play_media does: bump the generation, and reset_cast_app_ready
    # cancels the future the older call waits on
    player._play_generation += 1
    ready.cancel()
    await call

    session.start.assert_not_awaited()
    assert "cancel" not in calls[calls.index("launch") :]


async def test_previous_playback_ending_during_the_refresh_keeps_the_new_start() -> None:
    """The outgoing playback ending by itself (which stops the group) doesn't drop the new start."""
    ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    ready.set_result(None)
    player, session, bridge, _ = _player(ready)
    session.playback_task = None

    async def refresh_while_the_previous_track_ends() -> None:
        with patch.object(
            SendspinPlayer, "synced_to", new_callable=PropertyMock, return_value=None
        ):
            SendspinPlayer._on_group_stopped(player)

    bridge.refresh_cast_app_state = AsyncMock(side_effect=refresh_while_the_previous_track_ends)

    await _play_media(player)

    session.start.assert_awaited_once()


async def test_group_stop_during_the_launch_does_not_start_playback() -> None:
    """A group stop while the Cast app launches ends the wait without starting playback."""
    ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    player, session, _, _ = _player(ready)
    session.playback_task = None

    call = asyncio.create_task(_play_media(player))
    for _ in range(5):
        await asyncio.sleep(0)
    with patch.object(SendspinPlayer, "synced_to", new_callable=PropertyMock, return_value=None):
        SendspinPlayer._on_group_stopped(player)
    await call

    session.start.assert_not_awaited()
    assert ready.cancelled()


async def test_regrouping_during_the_launch_does_not_start_playback() -> None:
    """A player moved to another group while its Cast app launches doesn't start there."""
    ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    player, session, _, _ = _player(ready)

    call = asyncio.create_task(_play_media(player))
    for _ in range(5):
        await asyncio.sleep(0)
    with patch.object(
        SendspinPlayer, "_controller_role", new_callable=PropertyMock, return_value=None
    ):
        SendspinPlayer._on_group_changed(player, Mock(state=PlaybackStateType.STOPPED))
    await call

    session.start.assert_not_awaited()
    assert ready.cancelled()


async def test_regrouping_while_the_old_playback_is_cancelled_launches_nothing() -> None:
    """A request invalidated while the outgoing playback is cancelled doesn't launch the app."""
    ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    player, session, bridge, _ = _player(ready)

    async def cancel(_reason: str, *, keep_stream: bool = False) -> None:
        if not keep_stream:
            # what a regroup does while the cancel is awaited
            SendspinPlayer._abort_pending_cast_start(player)

    session.cancel = AsyncMock(side_effect=cancel)

    await _play_media(player)

    bridge.launch_cast_app.assert_not_called()
    session.start.assert_not_awaited()


async def test_members_are_launched_along_and_waited_for() -> None:
    """The apps of all Cast members launch together, and the stream starts once all are ready."""
    loop = asyncio.get_running_loop()
    ready: asyncio.Future[None] = loop.create_future()
    member_ready: asyncio.Future[None] = loop.create_future()
    player, session, _, calls = _player(ready)
    member = _add_member(player, "member", member_ready)

    call = asyncio.create_task(_play_media(player))
    for _ in range(5):
        await asyncio.sleep(0)
    assert calls[-1] == "launch"
    member.launch_cast_app.assert_called_once_with()

    ready.set_result(None)
    for _ in range(5):
        await asyncio.sleep(0)
    session.start.assert_not_awaited()

    member_ready.set_result(None)
    await call
    session.start.assert_awaited_once()


async def test_leader_without_cast_app_waits_for_its_cast_member() -> None:
    """A player that needs no Cast app itself still waits for the app of its member."""
    member_ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    player, session, _, calls = _player(asyncio.get_running_loop().create_future())
    member = _add_member(player, "member", member_ready)
    player._get_cast_bridge_manager().bridges.pop("p1")  # type: ignore[union-attr]

    call = asyncio.create_task(_play_media(player))
    for _ in range(5):
        await asyncio.sleep(0)
    member.launch_cast_app.assert_called_once_with()
    session.start.assert_not_awaited()

    member_ready.set_result(None)
    await call
    assert calls == ["cancel", "cancel (keep stream)", "start"]


async def test_running_member_is_not_launched_again() -> None:
    """A member whose app runs and is connected does not hold back the start."""
    loop = asyncio.get_running_loop()
    ready: asyncio.Future[None] = loop.create_future()
    member_ready: asyncio.Future[None] = loop.create_future()
    ready.set_result(None)
    member_ready.set_result(None)
    player, _, _, calls = _player(ready)
    member = _add_member(player, "member", member_ready)

    await _play_media(player)

    member.launch_cast_app.assert_not_called()
    assert calls == ["refresh", "reset", "cancel (keep stream)", "start"]


async def test_member_that_never_gets_ready_does_not_keep_the_group_silent() -> None:
    """Playback starts without a member whose app does not come up in time."""
    loop = asyncio.get_running_loop()
    ready: asyncio.Future[None] = loop.create_future()
    ready.set_result(None)
    player, session, _, _ = _player(ready)
    _add_member(player, "late member", loop.create_future())

    with patch.object(player_module, "CAST_MEMBERS_READY_TIMEOUT", 0.01):
        await asyncio.wait_for(_play_media(player), timeout=1)

    session.start.assert_awaited_once()
    assert "late member" in str(player.logger.warning.call_args)  # type: ignore[attr-defined]


async def test_member_that_fails_does_not_keep_the_group_silent() -> None:
    """Playback starts without a member whose device refuses the app."""
    loop = asyncio.get_running_loop()
    ready: asyncio.Future[None] = loop.create_future()
    ready.set_result(None)
    member_ready: asyncio.Future[None] = loop.create_future()
    player, session, _, _ = _player(ready)
    _add_member(player, "member", member_ready)

    call = asyncio.create_task(_play_media(player))
    for _ in range(5):
        await asyncio.sleep(0)
    member_ready.set_exception(PlayerCommandFailed("the Cast receiver refused the launch"))
    # as the bridge does: nobody else retrieves it
    member_ready.exception()
    await call

    session.start.assert_awaited_once()


async def test_member_whose_app_turns_out_to_be_gone_is_launched() -> None:
    """A member's app that the device no longer runs is launched, whatever was known before."""
    loop = asyncio.get_running_loop()
    ready: asyncio.Future[None] = loop.create_future()
    ready.set_result(None)
    running: asyncio.Future[None] = loop.create_future()
    running.set_result(None)
    gone: asyncio.Future[None] = loop.create_future()
    player, session, _, _ = _player(ready)
    member = _add_member(player, "member", running)

    async def app_turns_out_to_be_gone() -> None:
        member.reset_cast_app_ready.return_value = gone

    member.refresh_cast_app_state = AsyncMock(side_effect=app_turns_out_to_be_gone)

    call = asyncio.create_task(_play_media(player))
    for _ in range(5):
        await asyncio.sleep(0)
    member.launch_cast_app.assert_called_once_with()
    session.start.assert_not_awaited()

    gone.set_result(None)
    await call
    session.start.assert_awaited_once()


async def test_members_are_not_waited_for_while_the_group_plays() -> None:
    """A track change or seek does not wait for a member that is not there."""
    loop = asyncio.get_running_loop()
    ready: asyncio.Future[None] = loop.create_future()
    ready.set_result(None)
    player, session, _, _ = _player(ready)
    member = _add_member(player, "member", loop.create_future())
    player.api.group.has_active_stream = True  # type: ignore[misc]

    await _play_media(player)

    member.launch_cast_app.assert_not_called()
    session.start.assert_awaited_once()


async def test_unavailable_member_is_not_waited_for() -> None:
    """A member whose Cast device is off does not hold back the start."""
    loop = asyncio.get_running_loop()
    ready: asyncio.Future[None] = loop.create_future()
    ready.set_result(None)
    player, session, _, _ = _player(ready)
    member = _add_member(player, "member", loop.create_future(), available=False)

    await _play_media(player)

    member.launch_cast_app.assert_not_called()
    session.start.assert_awaited_once()


async def test_stop_while_waiting_for_a_member_does_not_start_playback() -> None:
    """A stop while a member's app launches ends the wait without starting playback."""
    loop = asyncio.get_running_loop()
    ready: asyncio.Future[None] = loop.create_future()
    ready.set_result(None)
    player, session, _, _ = _player(ready)
    _add_member(player, "member", loop.create_future())

    call = asyncio.create_task(_play_media(player))
    for _ in range(5):
        await asyncio.sleep(0)
    assert not call.done()

    SendspinPlayer._abort_pending_cast_start(player)
    await asyncio.wait_for(call, timeout=1)

    session.start.assert_not_awaited()
    assert player._pending_cast_app_ready is None


async def test_newer_play_media_replaces_one_waiting_for_a_member() -> None:
    """A play_media replaced while it waits for a member leaves the start to its successor."""
    loop = asyncio.get_running_loop()
    ready: asyncio.Future[None] = loop.create_future()
    ready.set_result(None)
    member_ready: asyncio.Future[None] = loop.create_future()
    player, session, _, calls = _player(ready)
    _add_member(player, "member", member_ready)

    call = asyncio.create_task(_play_media(player))
    for _ in range(5):
        await asyncio.sleep(0)
    # what a newer play_media does: bump the generation, and reset_cast_app_ready
    # cancels the future the older call waits on
    player._play_generation += 1
    member_ready.cancel()
    await asyncio.wait_for(call, timeout=1)

    session.start.assert_not_awaited()
    assert calls == ["refresh", "reset", "cancel"]
