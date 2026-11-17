"""Tests for a Cast device whose power is cut at a power control, and comes back."""

from __future__ import annotations

import asyncio
import logging
import socket
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock, patch

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import PlayerUnavailableError
from music_assistant_models.player import PlayerMedia
from pychromecast.const import CAST_TYPE_AUDIO, CAST_TYPE_GROUP
from pychromecast.error import ChromecastConnectionError
from pychromecast.models import HostServiceInfo
from pychromecast.socket_client import CONNECTION_STATUS_CONNECTED, SocketClient

from music_assistant.constants import DEFAULT_POWER_ON_STARTUP_TIME
from music_assistant.controllers.players.constants import POWER_RESTORE_TIMEOUT
from music_assistant.controllers.players.power import EXTERNAL_POWER_OFF_STOP_DELAY
from music_assistant.providers.chromecast.constants import MASS_APP_ID
from tests.providers.chromecast.test_lifecycle import (
    _cast_info,
    _cast_player,
    _connect_client,
    _mocked_provider,
    _register_cast,
)
from tests.scenarios.harness import Scenario

if TYPE_CHECKING:
    import pathlib
    from collections.abc import AsyncGenerator

    from music_assistant.providers.chromecast.helpers import ChromecastInfo
    from music_assistant.providers.chromecast.player import ChromecastPlayer
    from tests.scenarios.harness import FakePowerControl

# a stream of the server itself, whose address goes to the speaker as it is
MEDIA = PlayerMedia(uri="http://ma.local/stream/track.flac", media_type=MediaType.FLOW_STREAM)


@pytest.fixture
async def scenario(tmp_path: pathlib.Path) -> AsyncGenerator[Scenario]:
    """
    Boot a real server with fake player providers.

    :param tmp_path: Temporary directory for the server's storage.
    """
    scenario = Scenario(tmp_path)
    try:
        await scenario.start()
        yield scenario
    finally:
        await scenario.close()


def _report_connected(client: MagicMock) -> None:
    """Let a device report through its client that the connection is made."""
    client.register_connection_listener.call_args.args[0].new_connection_status(
        MagicMock(status=CONNECTION_STATUS_CONNECTED)
    )


async def _attached(cast_info: ChromecastInfo | None = None) -> tuple[ChromecastPlayer, MagicMock]:
    """
    Attach the protocol of a Cast device to a fake client that is connected.

    :param cast_info: The info of the device, a Google speaker when omitted.
    """
    protocol = _cast_player(_mocked_provider(), cast_info or _cast_info())
    protocol.set_player(MagicMock())
    with patch("pychromecast.get_chromecast_from_cast_info", side_effect=_connect_client):
        await protocol.on_attach(MagicMock())
    client = cast("MagicMock", protocol.cc)
    protocol.update_state = MagicMock()  # type: ignore[misc, method-assign]
    _report_connected(client)
    await asyncio.sleep(0)
    assert protocol.available is True
    return protocol, client


async def _restore(
    protocol: ChromecastPlayer, *, connects: bool = True
) -> tuple[MagicMock, asyncio.Task[None]]:
    """
    Tell a protocol that the power of its device is back, and return its new client.

    :param protocol: The protocol.
    :param connects: Whether the new client reports its connection.
    :return: The new client, and the call that told the protocol, which may still wait.
    """
    with patch(
        "pychromecast.get_chromecast_from_cast_info", side_effect=_connect_client
    ) as connect:
        restoring = asyncio.create_task(protocol.on_power_restored())
        await asyncio.sleep(0.15)
    connect.assert_called_once()
    new_client = cast("MagicMock", protocol.cc)
    if connects:
        assert not restoring.done()
        _report_connected(new_client)
        await asyncio.wait_for(restoring, 1)
    return new_client, restoring


async def test_a_power_cut_stops_the_client_and_reads_unavailable() -> None:
    """A device whose power is cut has its client stopped at once, and reads unavailable."""
    protocol, client = await _attached()
    handler = MagicMock()
    protocol.register_client_handler(handler)
    app_status = MagicMock()
    protocol.on_app_status_changed = app_status
    update_state = cast("MagicMock", protocol.update_state)
    update_state.reset_mock()

    await protocol.on_power_cut()

    client.disconnect.assert_called_once_with(0)
    assert protocol.available is False
    update_state.assert_called_once_with()
    app_status.assert_called_once_with(None)
    # the controllers are taken off the client before it stops and tears them down
    calls = [name for name, _, _ in client.mock_calls]
    assert calls.index("disconnect") > max(
        index for index, name in enumerate(calls) if name == "unregister_handler"
    )
    client.unregister_handler.assert_any_call(handler)
    # what the stopped client still reports does not reach the protocol
    _report_connected(client)
    await asyncio.sleep(0)
    assert protocol.available is False
    # told once more, nothing changes
    await protocol.on_power_cut()
    client.disconnect.assert_called_once_with(0)
    app_status.assert_called_once_with(None)


async def test_a_device_whose_power_is_back_gets_a_new_client_and_reads_available() -> None:
    """A device is connected to with a new client, and reads available once that reports it."""
    protocol, client = await _attached()
    await protocol.on_power_cut()

    new_client, restoring = await _restore(protocol, connects=False)

    assert new_client is not client
    new_client.start.assert_called_once_with()
    # it waits for the new client to report the connection, and reads unavailable until then
    assert (restoring.done(), protocol.available) == (False, False)
    _report_connected(new_client)
    await asyncio.wait_for(restoring, 1)
    assert protocol.available is True
    # the client of before the cut was stopped once, when the power was cut
    client.disconnect.assert_called_once_with(0)
    new_client.disconnect.assert_not_called()


async def test_a_power_that_is_back_without_a_cut_before_it_replaces_the_client() -> None:
    """
    A client from before the power went is stopped, and the device reads unavailable meanwhile.

    A play within the first seconds of a power cut switches the power on again before the
    device was told of the cut: its client still takes the connection for alive.
    """
    protocol, client = await _attached()
    app_status = MagicMock()
    protocol.on_app_status_changed = app_status

    new_client, restoring = await _restore(protocol, connects=False)

    client.disconnect.assert_called_once_with(0)
    assert new_client is not client
    new_client.start.assert_called_once_with()
    assert (restoring.done(), protocol.available) == (False, False)
    app_status.assert_called_once_with(None)
    _report_connected(new_client)
    await asyncio.wait_for(restoring, 1)
    assert protocol.available is True


async def test_a_power_that_is_back_twice_leaves_one_client() -> None:
    """A device told twice that its power is back ends up with one client that runs."""
    protocol, _ = await _attached()
    await protocol.on_power_cut()

    first_client, _ = await _restore(protocol)
    second_client, _ = await _restore(protocol)

    first_client.disconnect.assert_called_once_with(0)
    assert second_client is not first_client
    second_client.disconnect.assert_not_called()
    assert protocol.available is True


async def test_a_handler_of_another_part_moves_to_the_new_client() -> None:
    """A controller registered on the client of the device is registered on its new client."""
    protocol, client = await _attached()
    handler = MagicMock()
    protocol.register_client_handler(handler)
    client.register_handler.assert_called_with(handler)

    new_client, _ = await _restore(protocol)

    client.unregister_handler.assert_any_call(handler)
    new_client.register_handler.assert_any_call(handler)
    protocol.unregister_client_handler(handler)
    new_client.unregister_handler.assert_called_with(handler)
    assert protocol._client_handlers == []


async def test_a_device_that_does_not_answer_yet_keeps_its_new_client_trying() -> None:
    """
    A device that is not up when its start-up time is over reads unavailable.

    The call waits until the players controller ends it, and the new client tries on.
    """
    protocol, _ = await _attached()
    await protocol.on_power_cut()

    with (
        patch("pychromecast.get_chromecast_from_cast_info", side_effect=_connect_client),
        pytest.raises(TimeoutError),
    ):
        async with asyncio.timeout(0.3):
            await protocol.on_power_restored()

    new_client = cast("MagicMock", protocol.cc)
    new_client.start.assert_called_once_with()
    new_client.disconnect.assert_not_called()
    assert protocol.available is False
    # once the device is up, the client that kept trying reports it
    _report_connected(new_client)
    await asyncio.sleep(0)
    assert protocol.available is True


async def test_a_device_detached_while_its_new_client_connects_ends_the_wait() -> None:
    """A device taken off its player while its new client connects is left without a client."""
    protocol, _ = await _attached()
    await protocol.on_power_cut()
    new_client, restoring = await _restore(protocol, connects=False)
    assert not restoring.done()

    with patch("pychromecast.get_chromecast_from_cast_info") as connect:
        protocol.set_player(None)
        await protocol.on_detach()
        await asyncio.wait_for(restoring, 1)
        # told after the detach, it gets no client
        await asyncio.wait_for(protocol.on_power_restored(), 1)
        await protocol.on_power_cut()

    new_client.disconnect.assert_called_once_with(10)
    connect.assert_not_called()


async def test_a_restore_that_runs_while_the_device_is_detached_starts_no_client() -> None:
    """
    A restore that runs while its device is being detached leaves the detach its client.

    The detach has stopped following the client and waits for the bridge of the device,
    before it disconnects the client. A restore that was queued before the detach runs
    then: it starts no client, so the detach disconnects the one the device had.
    """
    protocol, client = await _attached()
    bridge_ends = asyncio.Event()

    async def _bridge() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await bridge_ends.wait()
            raise

    protocol._bridge_task = asyncio.create_task(_bridge())
    await asyncio.sleep(0)
    protocol.set_player(None)
    detaching = asyncio.create_task(protocol.on_detach())
    await asyncio.sleep(0.01)
    assert protocol.status_listener is None
    assert not detaching.done()

    with patch(
        "pychromecast.get_chromecast_from_cast_info", side_effect=_connect_client
    ) as connect:
        restoring = asyncio.create_task(protocol.on_power_restored())
        await asyncio.sleep(0.01)
        bridge_ends.set()
        await detaching
    try:
        # how often the client the device had was disconnected
        assert client.disconnect.call_count == 1
        client.disconnect.assert_called_once_with(10)
        connect.assert_not_called()
        assert protocol.status_listener is None
        assert restoring.done()
    finally:
        restoring.cancel()
        await asyncio.gather(restoring, return_exceptions=True)


async def test_a_device_that_is_not_attached_keeps_its_client_when_the_power_is_cut() -> None:
    """A device taken off its player, and not detached yet, is not touched by a power cut."""
    protocol, client = await _attached()
    listener = protocol.status_listener
    protocol.set_player(None)

    await protocol.on_power_cut()

    client.disconnect.assert_not_called()
    assert protocol.status_listener is listener
    await protocol.on_detach()
    client.disconnect.assert_called_once_with(10)


async def test_a_cast_group_is_left_alone() -> None:
    """A cast group, which is no device of its own, keeps its client when the power is cut."""
    protocol, client = await _attached(_cast_info(cast_type=CAST_TYPE_GROUP))

    with patch("pychromecast.get_chromecast_from_cast_info") as connect:
        await protocol.on_power_cut()
        await protocol.on_power_restored()

    client.disconnect.assert_not_called()
    connect.assert_not_called()
    assert protocol.cc is client
    assert protocol.available is True


def _closed_port() -> int:
    """Return a loopback port that nothing listens on."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _real_socket_client(protocol: ChromecastPlayer, client: MagicMock, port: int) -> Any:
    """
    Give a fake client a real pychromecast socket client that is not connected.

    :param protocol: The protocol, whose status listener follows the socket client.
    :param client: The fake client of the protocol.
    :param port: The loopback port of the one service it tries.
    """
    socket_client = SocketClient(
        cast_type=CAST_TYPE_AUDIO,
        tries=1,
        timeout=1,
        retry_wait=1,
        services={HostServiceInfo("127.0.0.1", port)},
        zconf=None,
    )
    socket_client.register_connection_listener(cast("Any", protocol.status_listener))
    client.socket_client = socket_client
    return socket_client


def _close_socket_client(socket_client: Any) -> None:
    """Close what a real socket client holds."""
    socket_client.stop.set()
    if socket_client.socket is not None:
        socket_client.socket.close()
    for end in socket_client.socketpair:
        end.close()
    socket_client.selector.close()


async def test_a_client_that_is_stopped_logs_no_error(caplog: pytest.LogCaptureFixture) -> None:
    """
    The worker of a client that is stopped ends its connect without an error in the log.

    pychromecast logs the end of a connect that was stopped as an error.
    """
    protocol, client = await _attached()
    socket_client = _real_socket_client(protocol, client, _closed_port())
    try:
        with caplog.at_level(logging.DEBUG, logger="pychromecast"):
            await protocol.on_power_cut()
            # what its worker does once the client is stopped
            socket_client.stop.set()
            with pytest.raises(ChromecastConnectionError):
                await asyncio.to_thread(socket_client.initialize_connection)
    finally:
        _close_socket_client(socket_client)

    assert [record.getMessage() for record in caplog.records] == []


def _client_of_a_device_that_is_up(*_args: object) -> MagicMock:
    """Return a fake client that reports its connection once it is started."""
    client = _connect_client()
    # the receiver app runs already, so a play needs no launch
    client.app_id = MASS_APP_ID
    client.start.side_effect = lambda: _report_connected(client)
    return client


async def _cast_on_a_plug(
    scenario: Scenario,
) -> tuple[ChromecastPlayer, MagicMock, FakePowerControl, str]:
    """
    Attach a Google speaker on a smart plug that cuts its power, and cut it.

    :return: The protocol of the speaker, its client, the plug and the id of its player.
    """
    protocol = await _register_cast(scenario, _cast_info())
    client = cast("MagicMock", protocol.cc)
    _report_connected(client)
    await scenario.settle()
    assert protocol.player is not None
    player_id = protocol.player.player_id
    plug = await scenario.add_power_control("switch.plug", "Plug", powered=True)
    await scenario.configure_player(player_id, power_control=plug.control_id)
    # this setting is offered once the plug is the power control of the player
    await scenario.configure_player(player_id, power_control_cuts_power=True)
    await scenario.switch(plug, False)
    await scenario.advance(EXTERNAL_POWER_OFF_STOP_DELAY)
    client.disconnect.assert_called_once_with(0)
    assert protocol.available is False
    return protocol, client, plug, player_id


async def test_a_start_after_the_startup_time_plays_through_the_new_client(
    scenario: Scenario,
) -> None:
    """
    A play of a speaker whose power was cut plays through Cast once its new client connected.

    The plug of a Google speaker, which cuts its power, is off, and the client of the
    speaker was stopped. A play switches the plug on and waits for the start-up time. Then
    the speaker gets a new client, which connects, and the music is loaded through it.
    """
    protocol, client, plug, player_id = await _cast_on_a_plug(scenario)

    with patch(
        "pychromecast.get_chromecast_from_cast_info", side_effect=_client_of_a_device_that_is_up
    ) as connect:
        play = await scenario.command(scenario.mass.players.play_media(player_id, MEDIA))
        assert plug.commands == [True]
        await scenario.advance(DEFAULT_POWER_ON_STARTUP_TIME - 1)
        connect.assert_not_called()
        assert not play.done(), play.exception()
        await scenario.advance(2)
        assert play.done()
        await play

    connect.assert_called_once()
    new_client = cast("MagicMock", protocol.cc)
    assert new_client is not client
    assert protocol.available is True
    load = new_client.media_controller.send_message
    load.assert_called_once()
    assert load.call_args.kwargs["data"]["type"] == "LOAD"
    client.media_controller.send_message.assert_not_called()


async def test_a_start_fails_with_the_hint_when_the_new_client_does_not_connect(
    scenario: Scenario,
) -> None:
    """
    A play of a speaker that is not up after its start-up time fails with the hint.

    The plug of a Google speaker, which cuts its power, is off. A play switches it on and
    waits for the start-up time and for the new client of the speaker, which reaches
    nothing: the play fails and tells the user to raise the start-up time, and nothing was
    sent to the speaker.
    """
    protocol, _, plug, player_id = await _cast_on_a_plug(scenario)

    with patch("pychromecast.get_chromecast_from_cast_info", side_effect=_connect_client):
        play = await scenario.command(scenario.mass.players.play_media(player_id, MEDIA))
        await scenario.advance(DEFAULT_POWER_ON_STARTUP_TIME + POWER_RESTORE_TIMEOUT + 1)

    assert plug.commands == [True]
    with pytest.raises(PlayerUnavailableError, match="could not be reached 30s after"):
        await play
    new_client = cast("MagicMock", protocol.cc)
    new_client.start.assert_called_once_with()
    new_client.media_controller.send_message.assert_not_called()
    assert protocol.available is False
