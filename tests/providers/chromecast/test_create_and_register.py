"""Tests for setting up and registering a discovered Chromecast."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from music_assistant_models.config_entries import ProviderConfig
from music_assistant_models.enums import ProviderType
from music_assistant_models.provider import ProviderManifest
from pychromecast.const import CAST_TYPE_CHROMECAST

from music_assistant.constants import CONF_PLAYERS
from music_assistant.providers.chromecast.helpers import ChromecastInfo
from music_assistant.providers.chromecast.provider import ChromecastProvider

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

PLAYER_ID = "cast-uuid"


@pytest.mark.parametrize("registers", [True, False], ids=["registered", "skipped"])
async def test_a_skipped_registration_releases_the_device(registers: bool) -> None:
    """A device that registration skips is disconnected again and gets no bridge."""
    provider = ChromecastProvider.__new__(ChromecastProvider)
    provider.mass = MagicMock()
    provider._pending_discoveries = {PLAYER_ID}
    provider.bridge_manager = MagicMock(evaluate_bridge=AsyncMock())
    castplayer = MagicMock(async_setup=AsyncMock(), on_unload=AsyncMock())
    provider.mass.players.register_or_update = AsyncMock()
    provider.mass.players.get_player.return_value = castplayer if registers else None

    with patch(
        "music_assistant.providers.chromecast.provider.ChromecastPlayer", return_value=castplayer
    ):
        await provider._create_and_register_player(PLAYER_ID, MagicMock(), MagicMock())

    assert castplayer.on_unload.await_count == (0 if registers else 1)
    assert provider.bridge_manager.evaluate_bridge.await_count == (1 if registers else 0)
    assert PLAYER_ID not in provider._pending_discoveries


async def test_a_new_tv_is_stored_disabled_and_released(mass: MusicAssistant) -> None:
    """A newly discovered TV gets a disabled config and its connection is closed again."""
    provider = ChromecastProvider.__new__(ChromecastProvider)
    provider.mass = mass
    provider.manifest = ProviderManifest(
        type=ProviderType.PLAYER,
        domain="chromecast",
        name="Chromecast",
        description="Chromecast",
        codeowners=["@music-assistant"],
    )
    provider.config = ProviderConfig(
        values={},
        type=ProviderType.PLAYER,
        domain="chromecast",
        instance_id="chromecast--test",
        name="Chromecast",
    )
    provider.logger = logging.getLogger("test.chromecast")
    provider.available = True
    provider.unloading = False
    provider.mz_mgr = MagicMock()
    provider._pending_discoveries = {PLAYER_ID}
    provider.bridge_manager = MagicMock(evaluate_bridge=AsyncMock())
    mass._providers[provider.instance_id] = provider
    cast_info = ChromecastInfo(
        services=set(),
        uuid=uuid4(),
        model_name="Chromecast",
        friendly_name="Living Room TV",
        host="10.0.0.10",
        port=8009,
        cast_type=CAST_TYPE_CHROMECAST,
        manufacturer="Google Inc.",
    )
    chromecast = MagicMock()

    with patch("music_assistant.providers.chromecast.player.CastStatusListener"):
        await provider._create_and_register_player(PLAYER_ID, cast_info, chromecast)

    assert mass.config.get(f"{CONF_PLAYERS}/{PLAYER_ID}/enabled") is False
    assert mass.players.get_player(PLAYER_ID) is None
    chromecast.disconnect.assert_called_once()
    provider.bridge_manager.evaluate_bridge.assert_not_awaited()
