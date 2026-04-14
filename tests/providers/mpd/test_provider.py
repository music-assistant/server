"""Tests for the MPD player provider."""

from typing import Any, cast
from unittest.mock import AsyncMock, Mock

from music_assistant.providers.mpd.provider import MPDPlayerProvider


async def test_remove_player_prunes_manual_host_config() -> None:
    """Removing an MPD player should also remove its host entry from provider config."""
    provider = cast("Any", MPDPlayerProvider.__new__(MPDPlayerProvider))
    provider.config = Mock()
    provider.config.instance_id = "mpd_test"
    provider.config.get_value.return_value = [
        "kitchen.local:6600",
        "office.local:6601",
        "office.local:6601",
        "bedroom.local",
    ]
    provider.mass = Mock()
    provider.mass.config = Mock()
    provider.mass.players = Mock()
    provider.mass.players.get_player.return_value = None
    provider.mass.players.unregister = AsyncMock()

    await provider.remove_player("mpd_office.local_6601")

    provider.mass.config.set_raw_provider_config_value.assert_called_once_with(
        "mpd_test",
        "manual_discovery_ip_addresses",
        ["kitchen.local:6600", "bedroom.local"],
    )
    provider.mass.players.unregister.assert_awaited_once_with("mpd_office.local_6601", True)
