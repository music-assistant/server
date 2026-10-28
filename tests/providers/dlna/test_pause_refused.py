"""Tests for a DLNA renderer that refuses to pause."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from async_upnp_client.exceptions import UpnpActionResponseError

from music_assistant.providers.dlna.player import DLNAPlayer
from tests.common import MockProvider


def _player(device: MagicMock) -> DLNAPlayer:
    """
    Return a player for the given device, with replace_pause_with_stop off.

    :param device: The mocked device to attach to the player.
    """
    player = DLNAPlayer(
        MockProvider("dlna", instance_id="dlna_test"),  # type: ignore[arg-type]
        "uuid:dlna-player",
        "http://192.168.1.10/description.xml",
        device=device,
    )
    player.get_config_value = MagicMock(return_value=False)  # type: ignore[method-assign]
    return player


def _device(pause_error: Exception | None = None) -> MagicMock:
    """
    Return a mocked device that can pause and stop.

    :param pause_error: The error the device answers Pause with, if any.
    """
    device = MagicMock()
    device.profile_device.available = True
    device.can_pause = True
    device.can_stop = True
    device.async_pause = AsyncMock(side_effect=pause_error)
    device.async_stop = AsyncMock()
    return device


async def test_pause_refused_stops_playback() -> None:
    """A renderer that refuses Pause (701 on an endless stream) is stopped instead."""
    device = _device(UpnpActionResponseError(status=500, error_code=701))
    player = _player(device)

    await player.pause()

    device.async_pause.assert_awaited_once()
    device.async_stop.assert_awaited_once()


async def test_pause_accepted_does_not_stop() -> None:
    """A renderer that pauses is not stopped."""
    device = _device()
    player = _player(device)

    await player.pause()

    device.async_pause.assert_awaited_once()
    device.async_stop.assert_not_awaited()
