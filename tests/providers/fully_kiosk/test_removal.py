"""Tests for what the Fully Kiosk provider cleans up for a removed player."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

from music_assistant.providers.fully_kiosk.player import FullyKioskPlayer
from music_assistant.providers.fully_kiosk.provider import CONF_MANUAL_IPS, FullyKioskProvider


async def test_removed_player_prunes_manual_host_config() -> None:
    """A removed player has its host entry taken out of the provider config."""
    provider = FullyKioskProvider.__new__(FullyKioskProvider)
    config_entries = {
        CONF_MANUAL_IPS: SimpleNamespace(
            value=["kitchen.local:2323", "office.local:2324", "office.local:2324", "bedroom.local"]
        )
    }
    provider.config = Mock()
    provider.config.instance_id = "fully_kiosk_test"
    provider.config.values = config_entries
    provider.config.get_value.side_effect = lambda key, default=None: (
        config_entries.get(key, SimpleNamespace(value=default)).value
    )
    provider.mass = Mock()
    provider._host_players = {"fully_kiosk_office.local_2324": Mock(), "kitchen": Mock()}
    player = FullyKioskPlayer.__new__(FullyKioskPlayer)
    player._provider = provider
    player.host = "office.local"
    player.port = 2324

    await player.on_remove()

    provider.mass.config.set_raw_provider_config_value.assert_called_once_with(
        "fully_kiosk_test",
        CONF_MANUAL_IPS,
        ["kitchen.local:2323", "bedroom.local"],
        encrypted=False,
        immediate=False,
    )
    assert list(provider._host_players) == ["kitchen"]
