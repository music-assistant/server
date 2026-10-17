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
from unittest.mock import MagicMock, patch

import pytest
from aiosendspin.models.types import GoodbyeReason
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

    cast_player = MagicMock()
    cast_player.player_id = "cc_player"
    cast_player.display_name = "Bar"
    cast_player.available = True
    cast_player.cc.app_id = app_id

    def update_status(*, callback_function: Any) -> None:
        if not answers:
            return
        cast_player.cc.app_id = reported_app_id
        callback_function(True, {"type": "RECEIVER_STATUS"})

    cast_player.cc.socket_client.receiver_controller.update_status = update_status

    provider = MagicMock()
    provider.mass = mass
    provider.logger = logging.getLogger("test.cast_bridge")
    bridge = SendspinChromecastBridge(provider, cast_player, MagicMock(), "spb_aabbccddeeff")
    client = MagicMock()
    client.is_connected = client_connected
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


async def test_late_app_gone_report_does_not_fail_the_next_playback() -> None:
    """The app-gone handler triggered by the refreshed status leaves the new future alone."""
    bridge, _ = _make_bridge(
        app_id=SENDSPIN_CAST_APP_ID, reported_app_id=None, client_connected=True
    )
    bridge._cast_app_was_active = True

    await bridge.refresh_cast_app_state()
    ready = bridge.reset_cast_app_ready()
    bridge._handle_cast_app_gone(None)

    assert not ready.done()


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
