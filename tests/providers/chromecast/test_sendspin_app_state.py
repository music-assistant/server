"""
Tests for how the Chromecast Sendspin bridge judges whether its Cast app is ready.

Playback waits for the Sendspin Cast app before it starts streaming, so the bridge must
tell a running, connected app from one that is gone. A speaker going into standby
(e.g. Raumfeld eco mode) can kill the app without a goodbye, and after an MA restart the
app may still run without a connection to the new server. In both cases the app has to
be launched again, while an app that runs and is connected must be left alone.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiosendspin.models.types import GoodbyeReason
from aiosendspin.server import ClientDisconnectedEvent
from pychromecast.error import NotConnected

from music_assistant.providers.chromecast import sendspin_bridge as bridge_module
from music_assistant.providers.chromecast.constants import SENDSPIN_CAST_APP_ID
from music_assistant.providers.chromecast.sendspin_bridge import SendspinChromecastBridge


def _make_bridge(
    *,
    app_id: str | None,
    reported_app_id: str | None = None,
    answers: bool = True,
    client_connected: bool = False,
) -> tuple[SendspinChromecastBridge, MagicMock]:
    """
    Build a bridge for a Cast device with a cached and a freshly reported app.

    :param app_id: The app the cached Cast status says runs.
    :param reported_app_id: The app the device reports when asked for its status.
    :param answers: Whether the device answers a status request at all.
    :param client_connected: Whether the Sendspin server holds a connection for the device.
    :return: The bridge, and its Sendspin client.
    """
    loop = asyncio.get_running_loop()
    mass = MagicMock()
    mass.loop = loop
    mass.create_task = MagicMock(side_effect=asyncio.ensure_future)
    mass.streams.publish_ip = "192.168.1.10"
    mass.config.get_raw_player_config_value = MagicMock(return_value=None)

    cast_player = MagicMock()
    cast_player.player_id = "cc_player"
    cast_player.display_name = "Bar"
    cast_player.available = True
    cast_player.cc.app_id = app_id

    def update_status(*, callback_function: Any) -> None:
        if not answers:
            return
        cast_player.cc.app_id = reported_app_id
        # as on a real device: the status listener is scheduled before the request's
        # callback, and passes the new app on to the bridge from the event loop
        loop.call_soon_threadsafe(bridge.on_cast_status_changed, reported_app_id)
        callback_function(True, {"type": "RECEIVER_STATUS"})

    cast_player.cc.socket_client.receiver_controller.update_status = update_status

    provider = MagicMock()
    provider.mass = mass
    provider.logger = logging.getLogger("test.cast_bridge")
    bridge = SendspinChromecastBridge(provider, cast_player, MagicMock(), "spb_aabbccddeeff")
    client = MagicMock()
    client.is_connected = client_connected
    client.group.clients = [client]
    client.group.stop = AsyncMock()
    client.ungroup = AsyncMock()
    client.detach_connection = MagicMock(
        side_effect=lambda _reason: setattr(client, "is_connected", False)
    )
    bridge._sendspin_client = client
    return bridge, client


def _block_launches(bridge: SendspinChromecastBridge) -> list[None]:
    """
    Replace the app launch with one that stays underway, and record each launch.

    :param bridge: The bridge whose launches to replace.
    :return: The list that gets an entry per launch started.
    """
    launches: list[None] = []

    async def launch() -> None:
        launches.append(None)
        await asyncio.Event().wait()

    bridge._launch_sendspin_app = launch  # type: ignore[method-assign]
    return launches


async def test_stale_connection_is_dropped_when_the_app_is_gone() -> None:
    """A device reporting no Sendspin app loses its stale connection and needs a launch."""
    bridge, client = _make_bridge(
        app_id=SENDSPIN_CAST_APP_ID, reported_app_id=None, client_connected=True
    )

    await bridge.refresh_cast_app_state()

    client.detach_connection.assert_called_once_with(GoodbyeReason.SHUTDOWN)
    assert not bridge.reset_cast_app_ready().done()


async def test_connection_is_kept_while_the_app_runs() -> None:
    """A device still running the Sendspin app keeps its connection and is ready at once."""
    bridge, client = _make_bridge(
        app_id=SENDSPIN_CAST_APP_ID, reported_app_id=SENDSPIN_CAST_APP_ID, client_connected=True
    )

    await bridge.refresh_cast_app_state()

    client.detach_connection.assert_not_called()
    assert bridge.reset_cast_app_ready().done()


async def test_cached_state_is_kept_when_the_device_does_not_answer() -> None:
    """Without an answer from the device the connection is left as it is."""
    bridge, client = _make_bridge(app_id=SENDSPIN_CAST_APP_ID, answers=False, client_connected=True)

    with patch.object(bridge_module, "CAST_STATUS_REFRESH_TIMEOUT", 0.01):
        await bridge.refresh_cast_app_state()

    client.detach_connection.assert_not_called()


async def test_refresh_finding_the_app_gone_keeps_the_group() -> None:
    """An app found gone by the refresh is not torn down (stopped or ungrouped) as well."""
    bridge, client = _make_bridge(
        app_id=SENDSPIN_CAST_APP_ID, reported_app_id=None, client_connected=True
    )
    bridge._cast_app_was_active = True

    await bridge.refresh_cast_app_state()
    ready = bridge.reset_cast_app_ready()
    for _ in range(5):
        await asyncio.sleep(0)

    client.detach_connection.assert_called_once_with(GoodbyeReason.SHUTDOWN)
    client.ungroup.assert_not_awaited()
    client.group.stop.assert_not_awaited()
    assert not ready.done()


async def test_app_gone_while_the_device_does_not_answer_is_still_handled() -> None:
    """An app reported gone during an unanswered refresh gets the usual app-gone handling."""
    bridge, client = _make_bridge(app_id=SENDSPIN_CAST_APP_ID, answers=False, client_connected=True)
    bridge._cast_app_was_active = True

    async def app_goes_away() -> None:
        await asyncio.sleep(0)
        bridge.cast_player.cc.app_id = None  # type: ignore[misc]
        bridge.on_cast_status_changed(None)

    with patch.object(bridge_module, "CAST_STATUS_REFRESH_TIMEOUT", 0.01):
        await asyncio.gather(bridge.refresh_cast_app_state(), app_goes_away())
    for _ in range(5):
        await asyncio.sleep(0)

    client.detach_connection.assert_called_once_with(GoodbyeReason.SHUTDOWN)
    client.group.stop.assert_awaited_once()


async def test_running_app_without_a_connection_is_not_ready() -> None:
    """An app left running from before an MA restart, but not connected, is launched again."""
    bridge, _ = _make_bridge(app_id=SENDSPIN_CAST_APP_ID, client_connected=False)
    launches = _block_launches(bridge)

    ready = bridge.reset_cast_app_ready()
    bridge.launch_cast_app()
    await asyncio.sleep(0)

    assert not ready.done()
    assert len(launches) == 1


async def test_no_relaunch_when_the_app_is_connected() -> None:
    """An app that runs and is connected is neither launched again nor waited for."""
    bridge, _ = _make_bridge(app_id=SENDSPIN_CAST_APP_ID, client_connected=True)
    launches = _block_launches(bridge)

    ready = bridge.reset_cast_app_ready()
    bridge.launch_cast_app()
    await asyncio.sleep(0)

    assert ready.done()
    assert launches == []


async def test_stream_start_leaves_a_launch_underway_alone() -> None:
    """A stream start during the launch play_media started neither cancels nor repeats it."""
    bridge, _ = _make_bridge(app_id=None)
    launches = _block_launches(bridge)

    bridge.reset_cast_app_ready()
    bridge.launch_cast_app()
    await asyncio.sleep(0)
    launch_task = bridge._launch_task
    bridge._on_stream_start(MagicMock())
    await asyncio.sleep(0)

    assert bridge._launch_task is launch_task
    assert launch_task is not None
    assert not launch_task.cancelled()
    assert len(launches) == 1
    launch_task.cancel()


async def test_stream_start_right_after_connected_does_not_relaunch() -> None:
    """The stream start that follows the app's connected report does not launch it again."""
    bridge, _ = _make_bridge(app_id=SENDSPIN_CAST_APP_ID, client_connected=False)
    launches = _block_launches(bridge)

    ready = bridge.reset_cast_app_ready()
    # the app reports connected just before the Sendspin server sees its connection
    bridge._mark_cast_app_connected()
    bridge._on_stream_start(MagicMock())
    await asyncio.sleep(0)

    assert ready.done()
    assert launches == []


async def test_running_app_that_lost_its_connection_is_launched_again() -> None:
    """An app that reported connected and then lost its connection is relaunched for playback."""
    bridge, client = _make_bridge(app_id=SENDSPIN_CAST_APP_ID, client_connected=True)
    launches = _block_launches(bridge)
    bridge._mark_cast_app_connected()
    client.is_connected = False

    ready = bridge.reset_cast_app_ready()
    bridge.launch_cast_app()
    await asyncio.sleep(0)

    assert not ready.done()
    assert len(launches) == 1


async def test_unexpected_status_refresh_error_is_raised() -> None:
    """Only a timeout or a Cast transport error falls back to the cached state."""
    bridge, _ = _make_bridge(app_id=SENDSPIN_CAST_APP_ID, client_connected=True)
    receiver = bridge.cast_player.cc.socket_client.receiver_controller
    receiver.update_status = MagicMock(side_effect=AttributeError("no such method"))  # type: ignore[method-assign]

    with pytest.raises(AttributeError):
        await bridge.refresh_cast_app_state()


async def test_cast_transport_error_keeps_the_cached_state() -> None:
    """A Cast device that can't be reached leaves its connection as it is."""
    bridge, client = _make_bridge(app_id=SENDSPIN_CAST_APP_ID, client_connected=True)
    receiver = bridge.cast_player.cc.socket_client.receiver_controller
    receiver.update_status = MagicMock(side_effect=NotConnected("connecting"))  # type: ignore[method-assign]

    await bridge.refresh_cast_app_state()

    client.detach_connection.assert_not_called()


async def test_stream_start_after_a_lost_connection_launches_the_app() -> None:
    """A connected report from before the reset does not keep a stream start from launching."""
    bridge, client = _make_bridge(app_id=SENDSPIN_CAST_APP_ID, client_connected=True)
    launches = _block_launches(bridge)
    bridge._mark_cast_app_connected()
    client.is_connected = False

    ready = bridge.reset_cast_app_ready()
    bridge._on_stream_start(MagicMock())
    await asyncio.sleep(0)

    assert not ready.done()
    assert len(launches) == 1


async def test_idle_status_during_a_relaunch_does_not_fail_playback() -> None:
    """The idle status a force-launch passes through is not taken for the app going away."""
    bridge, client = _make_bridge(app_id=SENDSPIN_CAST_APP_ID, client_connected=False)
    bridge._cast_app_was_active = True
    loop = asyncio.get_running_loop()
    cc: Any = bridge.cast_player.cc

    def launch_app(app_id: str, *, force_launch: bool, callback_function: Any) -> None:  # noqa: ARG001
        cc.app_id = None
        loop.call_soon_threadsafe(bridge.on_cast_status_changed, None)
        cc.app_id = app_id
        callback_function(True, {"type": "RECEIVER_STATUS"})

    cc.socket_client.receiver_controller.launch_app = launch_app

    ready = bridge.reset_cast_app_ready()
    bridge.launch_cast_app()
    assert bridge._launch_task is not None
    await bridge._launch_task
    for _ in range(5):
        await asyncio.sleep(0)

    assert not ready.done()
    client.group.stop.assert_not_awaited()
    client.ungroup.assert_not_awaited()
    assert bridge._cast_app_was_active is True


async def test_cast_disconnect_during_the_refresh_is_handled() -> None:
    """A Cast connection lost during the refresh counts as the app going away."""
    bridge, client = _make_bridge(app_id=SENDSPIN_CAST_APP_ID, client_connected=True)
    bridge._cast_app_was_active = True
    loop = asyncio.get_running_loop()
    cast_player: Any = bridge.cast_player

    def update_status(*, callback_function: Any) -> None:
        # a lost connection leaves the cached app id and fails the pending request
        cast_player.available = False
        loop.call_soon_threadsafe(bridge.on_cast_status_changed, None)
        callback_function(False, None)

    receiver = cast_player.cc.socket_client.receiver_controller
    receiver.update_status = update_status

    await bridge.refresh_cast_app_state()
    ready = bridge.reset_cast_app_ready()
    for _ in range(5):
        await asyncio.sleep(0)

    client.detach_connection.assert_called_once_with(GoodbyeReason.SHUTDOWN)
    assert not ready.done()


async def test_stream_start_after_a_disconnect_without_reset_launches_the_app() -> None:
    """A group start that doesn't reset readiness still relaunches an app that lost its connection."""
    bridge, client = _make_bridge(app_id=SENDSPIN_CAST_APP_ID, client_connected=True)
    launches = _block_launches(bridge)
    bridge.reset_cast_app_ready()
    bridge._mark_cast_app_connected()

    client.is_connected = False
    bridge._on_sendspin_event(
        MagicMock(), ClientDisconnectedEvent(client_id="spb_aabbccddeeff", goodbye_reason=None)
    )
    bridge._on_stream_start(MagicMock())
    await asyncio.sleep(0)

    assert len(launches) == 1


async def test_another_clients_disconnect_keeps_the_connected_report() -> None:
    """Only this device's own disconnect cancels its connected report."""
    bridge, _ = _make_bridge(app_id=SENDSPIN_CAST_APP_ID, client_connected=False)
    launches = _block_launches(bridge)
    bridge.reset_cast_app_ready()
    bridge._mark_cast_app_connected()

    bridge._on_sendspin_event(
        MagicMock(), ClientDisconnectedEvent(client_id="spb_ffeeddccbbaa", goodbye_reason=None)
    )
    bridge._on_stream_start(MagicMock())
    await asyncio.sleep(0)

    assert launches == []


async def test_launch_finishing_during_a_refresh_keeps_the_app_tracked() -> None:
    """A launch completing while a refresh waits stays tracked, so the app exiting later is handled."""
    bridge, client = _make_bridge(app_id=None, reported_app_id=SENDSPIN_CAST_APP_ID)
    loop = asyncio.get_running_loop()
    receiver = bridge.cast_player.cc.socket_client.receiver_controller

    def update_status(*, callback_function: Any) -> None:
        # the launch started earlier completes while the device answers the refresh
        bridge.cast_player.cc.app_id = SENDSPIN_CAST_APP_ID  # type: ignore[misc]
        loop.call_soon_threadsafe(setattr, bridge, "_cast_app_was_active", True)
        callback_function(True, {"type": "RECEIVER_STATUS"})

    receiver.update_status = update_status  # type: ignore[method-assign,assignment]

    await bridge.refresh_cast_app_state()
    assert bridge._cast_app_was_active is True

    client.is_connected = True
    bridge.cast_player.cc.app_id = None  # type: ignore[misc]
    bridge.on_cast_status_changed(None)
    for _ in range(5):
        await asyncio.sleep(0)

    client.detach_connection.assert_called_once_with(GoodbyeReason.SHUTDOWN)
    client.group.stop.assert_awaited_once()
