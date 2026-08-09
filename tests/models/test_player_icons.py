"""Tests for player icon defaults and overrides."""

from typing import cast
from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import PlayerType
from music_assistant_models.player import DeviceInfo

from music_assistant.constants import CONF_ICON
from music_assistant.mass import MusicAssistant
from tests.common import MockPlayer, MockProvider


@pytest.mark.parametrize(
    ("player_type", "provider_domain", "manufacturer", "model", "expected"),
    [
        (PlayerType.GROUP, "sonos", "Sonos", "Arc", "speakers"),
        (PlayerType.STEREO_PAIR, "test", "Unknown", "Unknown", "speakers"),
        (PlayerType.PLAYER, "sonos", "Unknown", "Unknown", "sonos"),
        (PlayerType.PLAYER, "hass_players", "Sonos", "Era 100", "sonos"),
        (PlayerType.PLAYER, "wiim", "Linkplay", "Unknown", "wiim"),
        (PlayerType.PLAYER, "airplay", "Apple", "Apple TV 4K Gen2", "apple-tv"),
        (PlayerType.PLAYER, "airplay", "Apple", "HomePod", "homepod-mini"),
        (PlayerType.PLAYER, "airplay", "Apple", "Mac14,3", "mac"),
        (
            PlayerType.PLAYER,
            "chromecast",
            "Google Inc.",
            "Google Nest Mini",
            "google-nest",
        ),
        (PlayerType.PLAYER, "chromecast", "Google Inc.", "Chromecast Audio", "cast"),
        (
            PlayerType.PLAYER,
            "hass_players",
            "Nabu Casa",
            "Home Assistant Voice Preview Edition",
            "voice-pe",
        ),
        (PlayerType.PLAYER, "fully_kiosk", "Samsung", "SM-X700", "tablet"),
        (PlayerType.PLAYER, "roku_media_assistant", "Roku", "Ultra", "tv"),
        (PlayerType.PLAYER, "test", "Samsung", "HW-Q990 Soundbar", "soundbar"),
        (PlayerType.PLAYER, "test", "Unknown", "Bluetooth Speaker", "bluetooth"),
        (PlayerType.DISPLAY, "sendspin", "Unknown", "Unknown", "monitor"),
        (PlayerType.VISUALIZER, "sendspin", "Unknown", "Unknown", "monitor"),
        (PlayerType.LIGHT, "hue_entertainment", "Signify", "Hue", "sun"),
        (PlayerType.PLAYER, "test", "Unknown", "Unknown", "speaker"),
    ],
)
def test_default_player_icon(
    player_type: PlayerType,
    provider_domain: str,
    manufacturer: str,
    model: str,
    expected: str,
) -> None:
    """The player type, provider, and device metadata select a useful default icon."""
    player = MockPlayer(
        MockProvider(provider_domain),
        "test_player",
        "Test Player",
        player_type=player_type,
    )
    player._attr_device_info = DeviceInfo(manufacturer=manufacturer, model=model)
    cast("MagicMock", player.config.get_value).return_value = None
    player._cache.clear()

    assert player.icon == expected


def test_explicit_player_icon_overrides_default() -> None:
    """An explicitly configured icon always wins over the computed default."""
    player = MockPlayer(MockProvider("airplay"), "apple_tv", "Apple TV")
    player._attr_device_info = DeviceInfo(manufacturer="Apple", model="Apple TV 4K")
    cast("MagicMock", player.config.get_value).return_value = "living-room"
    player._cache.clear()

    assert player.icon == "living-room"


async def test_unset_icon_config_uses_player_default(mass_minimal: MusicAssistant) -> None:
    """An unset stored icon resolves to the player's computed config default."""
    player = MagicMock()
    player.state.type = PlayerType.PLAYER
    player.default_icon = "sonos"
    player.hidden_by_default = False
    player.expose_to_ha_by_default = True
    player.linked_output_protocols = []
    player.supports_feature.return_value = False
    mass_minimal.players = MagicMock()
    mass_minimal.players.player_controls.return_value = []

    entries = mass_minimal.config._get_default_player_config_entries(player)
    icon_entry = next(entry for entry in entries if entry.key == CONF_ICON)

    assert icon_entry.default_value == "sonos"
    assert icon_entry.parse_value(None) == "sonos"
