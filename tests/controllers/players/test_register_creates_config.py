"""Tests for the player config being created when a player registers."""

import logging
from unittest.mock import MagicMock

from music_assistant_models.enums import PlayerFeature, PlayerType, ProviderType

from music_assistant.constants import CONF_PLAYERS
from music_assistant.mass import MusicAssistant
from music_assistant.models.player import Player

PLAYER_ID = "test_player"


class _TestProvider:
    """Minimal player provider stand-in backed by the real MusicAssistant."""

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize the test provider."""
        self.mass = mass
        self.domain = "test"
        self.instance_id = "test--instance"
        self.translation_owner = "provider.test"
        self.name = "Test"
        self.available = True
        self.unloading = False
        self.logger = logging.getLogger("test.test")
        self.manifest = MagicMock()
        self.manifest.domain = self.domain
        self.manifest.type = ProviderType.PLAYER
        self.type = ProviderType.PLAYER
        self.endpoints: list[Player] = []

    async def unload(self, is_removed: bool = False) -> None:
        """Unload the provider (nothing to clean up)."""


class _TestPlayer(Player):
    """Minimal concrete player that sets its attributes after the base constructor."""

    def __init__(self, provider: _TestProvider, enabled_by_default: bool = True) -> None:
        """Initialize the test player."""
        super().__init__(provider, PLAYER_ID)  # type: ignore[arg-type]
        self._attr_name = "Kitchen"
        self._attr_type = PlayerType.PLAYER
        self._attr_enabled_by_default = enabled_by_default
        self._attr_available = True
        self._attr_supported_features = {PlayerFeature.PLAY_MEDIA}

    async def stop(self) -> None:
        """Stop playback, required abstract method."""


def _register_provider(mass: MusicAssistant) -> _TestProvider:
    provider = _TestProvider(mass)
    mass._providers[provider.instance_id] = provider  # type: ignore[assignment]
    return provider


async def test_constructing_a_player_does_not_create_its_config(mass: MusicAssistant) -> None:
    """A player object that is never registered leaves no config behind."""
    player = _TestPlayer(_register_provider(mass))

    assert mass.config.get(f"{CONF_PLAYERS}/{PLAYER_ID}") is None
    assert player.config.player_id == PLAYER_ID


async def test_registering_a_player_creates_its_default_config(mass: MusicAssistant) -> None:
    """Registration stores the config with the attributes the player ended up with."""
    player = _TestPlayer(_register_provider(mass))

    await mass.players.register(player)

    raw_conf = mass.config.get(f"{CONF_PLAYERS}/{PLAYER_ID}")
    assert raw_conf["provider"] == "test--instance"
    assert raw_conf["enabled"] is True
    assert raw_conf["default_name"] == "Kitchen"
    assert raw_conf["name"] is None
    assert raw_conf["player_type"] == PlayerType.PLAYER
    assert player.state.enabled is True


async def test_registering_a_player_disabled_by_default(mass: MusicAssistant) -> None:
    """A new player that is disabled by default gets a disabled config on registration."""
    player = _TestPlayer(_register_provider(mass), enabled_by_default=False)

    await mass.players.register(player)

    assert mass.config.get(f"{CONF_PLAYERS}/{PLAYER_ID}/enabled") is False
    assert player.state.enabled is False
