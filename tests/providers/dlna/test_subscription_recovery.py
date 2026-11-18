"""Tests for a DLNA renderer that failed to renew an event subscription."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from async_upnp_client.exceptions import UpnpConnectionError
from music_assistant_models.errors import PlayerUnavailableError

from music_assistant.providers.dlna.player import DLNAPlayer
from tests.providers.dlna.test_device_state import _mock_device, _player


def _player_after_failed_renewal() -> tuple[DLNAPlayer, MagicMock]:
    """Return a player and its device, which the library marked unavailable on a failed renewal."""
    device = _mock_device()
    device.async_update = AsyncMock()
    device.async_unsubscribe_services = AsyncMock()
    device.async_subscribe_services = AsyncMock()
    device.profile_device.available = False
    return _player(device), device


async def test_device_that_still_responds_is_resubscribed_and_stays_available() -> None:
    """A device that answers the poll after a failed renewal gets new subscriptions."""
    player, device = _player_after_failed_renewal()

    await player.poll()
    await player.set_dynamic_attributes()

    device.async_unsubscribe_services.assert_awaited_once()
    device.async_subscribe_services.assert_awaited_once_with(auto_resubscribe=True)
    assert player.device is device
    assert player.available


async def test_device_that_fails_to_resubscribe_is_unavailable() -> None:
    """A device that answers the poll but can not be subscribed again is reported unavailable."""
    player, device = _player_after_failed_renewal()
    device.async_subscribe_services.side_effect = UpnpConnectionError

    with (
        patch.object(DLNAPlayer, "update_state") as update_state,
        pytest.raises(PlayerUnavailableError),
    ):
        await player.poll()

    assert player.device is None
    assert not player.available
    update_state.assert_called_once()
