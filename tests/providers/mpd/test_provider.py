"""Tests for the MPD player provider."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from music_assistant.constants import (
    CONF_ENTRY_PREFER_WAV_FOR_LIVE_SOURCES_DEFAULT_ENABLED,
    CONF_PREFER_WAV_FOR_LIVE_SOURCES,
)
from music_assistant.providers.mpd.player import MPDPlayer
from music_assistant.providers.mpd.provider import MPDPlayerProvider


async def test_mpd_prefers_wav_for_live_sources_by_default() -> None:
    """MPD players default to the known-compatible low-latency WAV path."""
    player = MPDPlayer.__new__(MPDPlayer)

    entries = await player.get_config_entries()
    entry = next(entry for entry in entries if entry.key == CONF_PREFER_WAV_FOR_LIVE_SOURCES)

    assert (
        entry.default_value == CONF_ENTRY_PREFER_WAV_FOR_LIVE_SOURCES_DEFAULT_ENABLED.default_value
    )


async def test_remove_player_prunes_manual_host_config() -> None:
    """Removing an MPD player should also remove its host entry from provider config."""
    provider = MPDPlayerProvider.__new__(MPDPlayerProvider)
    config_entries = {
        "manual_discovery_ip_addresses": SimpleNamespace(
            value=[
                "kitchen.local:6600",
                "office.local:6601",
                "office.local:6601",
                "bedroom.local",
            ]
        )
    }
    provider.config = Mock()
    provider.config.instance_id = "mpd_test"
    provider.config.values = config_entries
    provider.config.get_value.side_effect = lambda key, default=None: (
        config_entries.get(key, SimpleNamespace(value=default)).value
    )
    provider.mass = Mock()
    provider.mass.config = Mock()
    provider.mass.players = Mock()
    provider.mass.players.get_player.return_value = None
    provider.mass.players.unregister = AsyncMock()

    await provider.remove_player("mpd_office.local_6601")

    # the config controller stores the pruned list (and updates the in-place config copy)
    provider.mass.config.set_raw_provider_config_value.assert_called_once_with(
        "mpd_test",
        "manual_discovery_ip_addresses",
        ["kitchen.local:6600", "bedroom.local"],
        encrypted=False,
        immediate=False,
    )
    provider.mass.players.unregister.assert_awaited_once_with("mpd_office.local_6601", True)
