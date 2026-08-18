"""
Tests for the opt-in "volume re-apply step" player config entry.

The entry is what makes the workaround reachable at all: without it the feature is
configured nowhere and the code reading it can never fire.
"""

import logging
from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.constants import PLAYER_CONTROL_FAKE
from music_assistant_models.enums import (
    ConfigEntryType,
    PlayerFeature,
    PlayerType,
    ProviderType,
)

from music_assistant.constants import CONF_REAPPLY_VOLUME_STEP, REAPPLY_VOLUME_STEP_MAX
from music_assistant.mass import MusicAssistant
from music_assistant.models.player import DeviceInfo, LinkedOutputProtocol, Player

PLAYER_ID = "test_player_1"
PROTOCOL_ID = "test_protocol_1"


class _TestProvider:
    """Minimal PlayerProvider stand-in backed by the real MusicAssistant."""

    def __init__(self, mass: MusicAssistant, domain: str) -> None:
        """Initialize the test provider."""
        self.mass = mass
        self.domain = domain
        self.instance_id = domain
        self.translation_owner = f"provider.{domain}"
        self.name = f"{domain.title()} Provider"
        self.available = True
        self.logger = logging.getLogger(f"test.{domain}")
        self.manifest = MagicMock()
        self.manifest.domain = domain
        self.manifest.name = self.name
        self.manifest.type = ProviderType.PLAYER
        self.type = ProviderType.PLAYER
        self.players: list[Player] = []

    async def unload(self, is_removed: bool = False) -> None:
        """Unload the provider (nothing to clean up)."""


class _SimplePlayer(Player):
    """Player whose type and features the test picks."""

    def __init__(
        self,
        provider: _TestProvider,
        player_id: str,
        player_type: PlayerType,
        features: set[PlayerFeature],
    ) -> None:
        """Initialize the test player."""
        super().__init__(provider, player_id)  # type: ignore[arg-type]
        self._attr_name = "Test Player"
        self._attr_type = player_type
        self._attr_available = True
        self._attr_powered = True
        self._attr_supported_features = features
        self._attr_device_info = DeviceInfo(model="Test Model", manufacturer="Test Manufacturer")
        self._cache.clear()
        self.update_state(signal_event=False)

    async def stop(self) -> None:
        """Stop playback - required abstract method."""


async def _register_player(
    mass: MusicAssistant,
    player_type: PlayerType = PlayerType.PLAYER,
    features: set[PlayerFeature] | None = None,
) -> _SimplePlayer:
    """Register a single player and return it."""
    provider = _TestProvider(mass, "testprov")
    mass._providers[provider.instance_id] = provider  # type: ignore[assignment]
    mass._provider_manifests[provider.domain] = provider.manifest
    if features is None:
        features = {PlayerFeature.VOLUME_SET, PlayerFeature.PLAY_MEDIA}
    player = _SimplePlayer(provider, PLAYER_ID, player_type, features)
    mass.players._players[PLAYER_ID] = player
    mass.players.on_player_config_change = AsyncMock()  # type: ignore[method-assign]
    return player


async def test_entry_offered_to_a_player_that_can_set_volume(mass: MusicAssistant) -> None:
    """The workaround is configurable on a normal player with volume control."""
    await _register_player(mass)

    entries = await mass.config.get_player_config_entries(PLAYER_ID)
    entry = next(entry for entry in entries if entry.key == CONF_REAPPLY_VOLUME_STEP)

    assert entry.type == ConfigEntryType.FLOAT
    assert entry.advanced
    # unset is the off switch, so the entry must not arrive pre-filled or demand a value
    assert entry.default_value is None
    assert not entry.required
    assert entry.range == (0, int(REAPPLY_VOLUME_STEP_MAX))


async def test_entry_hidden_from_a_player_without_volume_control(mass: MusicAssistant) -> None:
    """A player that cannot set volume has nothing to re-apply."""
    await _register_player(mass, features={PlayerFeature.PLAY_MEDIA})

    entries = await mass.config.get_player_config_entries(PLAYER_ID)

    assert not any(entry.key == CONF_REAPPLY_VOLUME_STEP for entry in entries)


async def test_entry_hidden_from_group_players(mass: MusicAssistant) -> None:
    """
    A group reports VOLUME_SET but holds no volume level of its own.

    Offering the knob there would render a setting that can never do anything; the
    workaround belongs on the real players the group renders through.
    """
    await _register_player(mass, player_type=PlayerType.GROUP)

    entries = await mass.config.get_player_config_entries(PLAYER_ID)

    assert not any(entry.key == CONF_REAPPLY_VOLUME_STEP for entry in entries)


async def test_entry_offered_when_volume_lives_on_a_linked_protocol_player(
    mass: MusicAssistant,
) -> None:
    """
    A wrapped device carries its volume on a linked protocol player, not natively.

    A generic (non-Google) Cast receiver is exposed as a protocol player and wrapped in a
    universal player that has no volume feature of its own; its volume resolves to the
    protocol player. This is the exact device the workaround exists for, so gating on native
    VOLUME_SET (which the wrapper never has) would hide the knob from it entirely.
    """
    provider = _TestProvider(mass, "testprov")
    mass._providers[provider.instance_id] = provider  # type: ignore[assignment]
    mass._provider_manifests[provider.domain] = provider.manifest

    # the visible player has no native volume - only the linked protocol player does
    visible = _SimplePlayer(provider, PLAYER_ID, PlayerType.PLAYER, {PlayerFeature.POWER})
    protocol = _SimplePlayer(
        provider,
        PROTOCOL_ID,
        PlayerType.PROTOCOL,
        {PlayerFeature.VOLUME_SET, PlayerFeature.PLAY_MEDIA},
    )
    mass.players._players[PLAYER_ID] = visible
    mass.players._players[PROTOCOL_ID] = protocol
    visible.set_linked_output_protocols(
        [
            LinkedOutputProtocol(
                output_protocol_id=PROTOCOL_ID,
                protocol_domain="chromecast",
                priority=50,
            )
        ]
    )
    visible._cache.clear()
    mass.players.on_player_config_change = AsyncMock()  # type: ignore[method-assign]

    # the old gate would have hidden it: the wrapper has no native volume feature
    assert not visible.supports_feature(PlayerFeature.VOLUME_SET)

    entries = await mass.config.get_player_config_entries(PLAYER_ID)

    assert any(entry.key == CONF_REAPPLY_VOLUME_STEP for entry in entries)


async def test_entry_hidden_when_volume_control_resolves_to_no_player(
    mass: MusicAssistant,
) -> None:
    """
    Fake or external volume control has no device volume to re-apply.

    volume_control can be set but still resolve to no player - a "fake" simulated volume, or
    an external player-control id. The workaround only acts when volume resolves to a real
    player, so the knob stays hidden there instead of rendering a setting that never fires.
    """
    player = await _register_player(mass, features={PlayerFeature.PLAY_MEDIA})
    # seed the cached volume_control with a value that is not a player id: fake volume is
    # simulated by MA, so there is no device volume for the detour to act on. volume_control
    # is a propcache under_cached_property, so its store is _cache, not __dict__
    player._cache["volume_control"] = PLAYER_CONTROL_FAKE

    entries = await mass.config.get_player_config_entries(PLAYER_ID)

    assert not any(entry.key == CONF_REAPPLY_VOLUME_STEP for entry in entries)
