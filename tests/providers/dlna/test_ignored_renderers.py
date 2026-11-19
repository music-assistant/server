"""Tests for the renderers DLNA setup ignores after connecting to them."""

from __future__ import annotations

import asyncio
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

from music_assistant.providers.dlna.player import DLNAPlayer
from tests.common import MockProvider


def _player(manufacturer: str = "Acme", *, has_play_media: bool = True) -> DLNAPlayer:
    """
    Return a player whose (connected) device reports the given manufacturer and capability.

    :param manufacturer: The manufacturer the device reports.
    :param has_play_media: Whether the device can play media.
    """
    device = MagicMock()
    device.name = "Bar"
    device.manufacturer = manufacturer
    device.has_play_media = has_play_media
    return DLNAPlayer(
        MockProvider("dlna", instance_id="dlna_test"),  # type: ignore[arg-type]
        "uuid:dlna-player",
        "http://192.168.1.41/description.xml",
        device=device,
    )


async def test_setup_releases_a_renderer_without_play_capability() -> None:
    """A renderer that cannot play is never attached, so setup ends its event subscriptions."""
    player = _player(has_play_media=False)
    attach = AsyncMock()
    disconnect = AsyncMock()
    with (
        patch.object(player, "_device_connect", AsyncMock()),
        patch.object(player, "_device_disconnect", disconnect),
        patch.object(player.mass.players, "register", attach),
    ):
        assert await player.setup() is False
    attach.assert_not_called()
    disconnect.assert_awaited_once()


async def test_setup_releases_a_passive_sonos_speaker() -> None:
    """A passive speaker of a Sonos stereo pair is never attached, so setup ends its subscriptions."""
    player = _player("Sonos, Inc.")
    attach = AsyncMock()
    disconnect = AsyncMock()
    with (
        patch.object(player, "_device_connect", AsyncMock()),
        patch.object(player, "_device_disconnect", disconnect),
        patch.object(player, "_is_sonos_passive_speaker", AsyncMock(return_value=True)),
        patch.object(player.mass.players, "register", attach),
    ):
        assert await player.setup() is False
    attach.assert_not_called()
    disconnect.assert_awaited_once()


async def test_a_poll_that_runs_while_setup_ignores_the_renderer_ends_quietly() -> None:
    """The poll after the first event of a subscription survives setup dropping the device."""
    player = _player("Sonos, Inc.")
    device = cast("MagicMock", player.device)
    updating = asyncio.Event()
    disconnected = asyncio.Event()

    async def _update(**_: bool) -> None:
        updating.set()
        await disconnected.wait()

    device.async_update = _update
    device.async_unsubscribe_services = AsyncMock(side_effect=disconnected.set)
    poll = asyncio.create_task(player._update_player(poll_first=True))
    await updating.wait()
    with (
        patch.object(player, "_device_connect", AsyncMock()),
        patch.object(player, "_is_sonos_passive_speaker", AsyncMock(return_value=True)),
    ):
        assert await player.setup() is False

    await poll
    assert player.device is None
    assert not player.available
