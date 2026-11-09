"""Tests for the player config being created when a player registers."""

from unittest.mock import MagicMock

from music_assistant_models.config_entries import ProviderConfig
from music_assistant_models.enums import IdentifierType, PlayerFeature, PlayerType, ProviderType
from music_assistant_models.provider import ProviderManifest

from music_assistant.constants import CONF_PLAYERS
from music_assistant.mass import MusicAssistant
from music_assistant.models.player import Player
from music_assistant.models.player_provider import PlayerProvider

PLAYER_ID = "test_player"


class _TestPlayer(Player):
    """Minimal concrete player that sets its attributes after the base constructor."""

    def __init__(
        self,
        provider: PlayerProvider,
        enabled_by_default: bool = True,
        player_type: PlayerType = PlayerType.STEREO_PAIR,
    ) -> None:
        """Initialize the test player."""
        super().__init__(provider, PLAYER_ID)
        self._attr_name = "Kitchen"
        # not the class default, so a stored type can only come from the final attributes
        self._attr_type = player_type
        self._attr_enabled_by_default = enabled_by_default
        self._attr_available = True
        self._attr_supported_features = {PlayerFeature.PLAY_MEDIA}
        self._attr_device_info.add_identifier(IdentifierType.MAC_ADDRESS, "AA:BB:CC:DD:EE:FF")

    async def stop(self) -> None:
        """Stop playback, required abstract method."""


def _register_provider(mass: MusicAssistant) -> PlayerProvider:
    """
    Load a bare player provider into the given Music Assistant instance.

    :param mass: The Music Assistant instance to load the provider into.
    """
    provider = PlayerProvider(
        mass,
        manifest=ProviderManifest(
            type=ProviderType.PLAYER,
            domain="test",
            name="Test",
            description="Test player provider",
            codeowners=["@music-assistant"],
        ),
        config=ProviderConfig(
            values={},
            type=ProviderType.PLAYER,
            domain="test",
            instance_id="test--instance",
            name="Test",
        ),
    )
    provider.available = True
    # enabling a player starts a discovery, which has nothing to find here
    provider.on_player_enabled = MagicMock()  # type: ignore[method-assign]
    mass._providers[provider.instance_id] = provider
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
    assert raw_conf["player_type"] == PlayerType.STEREO_PAIR
    assert player.state.enabled is True


async def test_a_new_player_disabled_by_default_is_not_registered(
    mass: MusicAssistant,
) -> None:
    """A new player that is disabled by default only gets its disabled config stored."""
    player = _TestPlayer(
        _register_provider(mass), enabled_by_default=False, player_type=PlayerType.PROTOCOL
    )

    await mass.players.register(player)

    assert mass.config.get(f"{CONF_PLAYERS}/{PLAYER_ID}/enabled") is False
    assert mass.players.get_player(PLAYER_ID) is None
    assert not player.initialized.is_set()
    assert mass.player_queues.get(PLAYER_ID) is None
    # protocol linking never saw it, so no universal player is (or will be) created for it
    assert PLAYER_ID not in mass.players._pending_protocol_evaluations
    assert player.protocol_parent_id is None
    assert not [
        player_id
        for player_id, raw_conf in mass.config.get(CONF_PLAYERS).items()
        if raw_conf.get("provider") == "universal_player"
    ]


async def test_a_player_with_a_stored_disabled_config_is_registered_and_enabled_in_place(
    mass: MusicAssistant,
) -> None:
    """A player whose stored config is disabled registers disabled and is enabled in place."""
    provider = _register_provider(mass)
    mass.config.create_default_player_config(
        PLAYER_ID, provider.instance_id, PlayerType.STEREO_PAIR, enabled=False
    )
    player = _TestPlayer(provider, enabled_by_default=False)

    await mass.players.register(player)
    assert mass.players.get_player(PLAYER_ID) is player
    assert player.initialized.is_set()
    assert player.state.enabled is False

    await mass.config.save_player_config(PLAYER_ID, {"enabled": True})

    assert mass.players.get_player(PLAYER_ID) is player
    assert player.state.enabled is True
