"""Tests for player icon defaults and overrides."""

from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import PlayerType
from music_assistant_models.player import DeviceInfo

from music_assistant.constants import CONF_ICON, CONF_PLAYERS
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
        (PlayerType.PLAYER, "test", "Monitor Audio", "Creator 90", "speaker"),
        (PlayerType.PLAYER, "test", "Unknown", "Studio Monitor", "speaker"),
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
    cast("MagicMock", player.mass.config.get_raw_player_config_value).return_value = None
    player._cache.clear()

    assert player.icon == expected


def test_explicit_player_icon_overrides_default() -> None:
    """An explicitly configured icon always wins over the computed default."""
    player = MockPlayer(MockProvider("airplay"), "apple_tv", "Apple TV")
    player._attr_device_info = DeviceInfo(manufacturer="Apple", model="Apple TV 4K")
    cast("MagicMock", player.mass.config.get_raw_player_config_value).return_value = "living-room"
    player._cache.clear()

    assert player.icon == "living-room"


def test_automatic_player_icon_refreshes_with_device_info() -> None:
    """An automatic icon follows device information learned after initialization."""
    player = MockPlayer(MockProvider("test"), "late_metadata", "Late metadata")
    cast("MagicMock", player.mass.config.get_raw_player_config_value).return_value = None

    assert player.icon == "speaker"

    player._attr_device_info = DeviceInfo(manufacturer="Sonos", model="Era 100")
    player.update_state(signal_event=False)

    assert player.icon == "sonos"


async def test_unset_icon_config_uses_player_default(mass_minimal: MusicAssistant) -> None:
    """An unset stored icon resolves to the player's computed config default."""
    player = MagicMock()
    player.player_id = "test_player"
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


async def test_unset_icon_config_exposes_automatic_state(
    mass_minimal: MusicAssistant,
) -> None:
    """An absent stored icon stays None while the computed default is provided separately."""
    _set_up_config_player(mass_minimal)

    config = await mass_minimal.config.get_player_config("test_player")

    assert config.values[CONF_ICON].value is None
    assert config.values[CONF_ICON].default_value == "sonos"


async def test_explicit_default_icon_remains_explicit(mass_minimal: MusicAssistant) -> None:
    """A stored icon remains explicit even when it equals the computed default."""
    mass_minimal.config.set(
        f"{CONF_PLAYERS}/test_player",
        {
            "player_id": "test_player",
            "provider": "sonos",
            "player_type": PlayerType.PLAYER,
            "values": {CONF_ICON: "sonos"},
        },
    )
    _set_up_config_player(mass_minimal)

    config = await mass_minimal.config.get_player_config("test_player")

    assert config.values[CONF_ICON].value == "sonos"
    assert config.values[CONF_ICON].default_value == "sonos"

    await mass_minimal.config.save_player_config(
        "test_player",
        {CONF_ICON: "sonos", "hide_in_ui": True},
    )
    stored_values = mass_minimal.config.get(f"{CONF_PLAYERS}/test_player/values")
    assert stored_values[CONF_ICON] == "sonos"


async def test_automatic_icon_stays_unset_on_unrelated_save(
    mass_minimal: MusicAssistant,
) -> None:
    """A stale automatic icon cannot become an explicit choice during a config save."""
    _set_up_config_player(mass_minimal)

    await mass_minimal.config.save_player_config(
        "test_player",
        {CONF_ICON: None, "hide_in_ui": True},
    )

    stored_values = mass_minimal.config.get(f"{CONF_PLAYERS}/test_player/values")
    assert CONF_ICON not in stored_values


async def test_explicit_icon_lifecycle(mass_minimal: MusicAssistant) -> None:
    """Explicit icons survive unrelated saves, can change to the default, and can reset."""
    mass_minimal.config.set(
        f"{CONF_PLAYERS}/test_player",
        {
            "player_id": "test_player",
            "provider": "sonos",
            "player_type": PlayerType.PLAYER,
            "values": {CONF_ICON: "speaker"},
        },
    )
    _set_up_config_player(mass_minimal)

    await mass_minimal.config.save_player_config(
        "test_player",
        {CONF_ICON: "speaker", "hide_in_ui": True},
    )
    stored_values = mass_minimal.config.get(f"{CONF_PLAYERS}/test_player/values")
    assert stored_values[CONF_ICON] == "speaker"

    await mass_minimal.config.save_player_config("test_player", {CONF_ICON: "sonos"})
    stored_values = mass_minimal.config.get(f"{CONF_PLAYERS}/test_player/values")
    assert stored_values[CONF_ICON] == "sonos"

    await mass_minimal.config.save_player_config("test_player", {CONF_ICON: None})
    stored_values = mass_minimal.config.get(f"{CONF_PLAYERS}/test_player/values")
    assert CONF_ICON not in stored_values
    callback = cast("AsyncMock", mass_minimal.players.on_player_config_change)
    assert callback.await_args is not None
    callback_config = callback.await_args.args[0]
    assert callback_config.values[CONF_ICON].value is None


def _set_up_config_player(mass: MusicAssistant) -> None:
    """Set up a player mock that drives the real player config parsing path."""
    if not mass.config.get(f"{CONF_PLAYERS}/test_player"):
        mass.config.set(
            f"{CONF_PLAYERS}/test_player",
            {
                "player_id": "test_player",
                "provider": "sonos",
                "player_type": PlayerType.PLAYER,
                "values": {},
            },
        )
    player = MagicMock()
    player.player_id = "test_player"
    player.state.type = PlayerType.PLAYER
    player.state.name = "Test Player"
    player.provider.instance_id = "sonos"
    player.translation_owner = "provider.sonos"
    player.default_icon = "sonos"
    player.hidden_by_default = False
    player.expose_to_ha_by_default = True
    player.linked_output_protocols = []
    player.output_protocols = []
    player.supported_features = set()
    player.supports_feature.return_value = False
    player.get_config_entries = AsyncMock(return_value=[])
    mass.players = MagicMock()
    mass.players.get_player.return_value = player
    mass.players.player_controls.return_value = []
    mass.players.on_player_config_change = AsyncMock()
