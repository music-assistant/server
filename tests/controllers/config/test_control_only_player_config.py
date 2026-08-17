"""
Regression tests for control-only players losing their own config entries.

Reproduces music-assistant/support#4766: a player that only handles control
(power/volume/source) and delegates playback to a linked protocol player (e.g.
Bose SoundTouch streaming via DLNA) has no native output protocol. The injected
protocol entries replaced the player's own entries entirely, so everything its
``get_config_entries()`` returned disappeared from the config UI.
"""

import logging
from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType, PlayerFeature, PlayerType, ProviderType

from music_assistant.constants import (
    CONF_PREFERRED_OUTPUT_PROTOCOL,
    CONF_PROTOCOL_KEY_SPLITTER,
)
from music_assistant.mass import MusicAssistant
from music_assistant.models.player import DeviceInfo, LinkedOutputProtocol, Player

PARENT_ID = "soundtouch_123"
CHILD_ID = "dlna_AABBCCDDEEFF"
OWN_ENTRY_KEY = "preset_slot"


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
        # registered in mass._providers, so the stand-in must survive the provider
        # bookkeeping that mass.stop()/unload_provider applies to every provider
        self.type = ProviderType.PLAYER
        self.players: list[Player] = []

    async def unload(self, is_removed: bool = False) -> None:
        """Unload the provider (nothing to clean up)."""


class _ControlOnlyPlayer(Player):
    """Player that only offers control and delegates playback to a protocol player."""

    def __init__(self, provider: _TestProvider, player_id: str, name: str) -> None:
        """Initialize the test player."""
        super().__init__(provider, player_id)  # type: ignore[arg-type]
        self._attr_name = name
        self._attr_type = PlayerType.PLAYER
        self._attr_available = True
        self._attr_powered = True
        # deliberately no PLAY_MEDIA: this player cannot play media itself,
        # which is what makes all of its output protocols non-native
        self._attr_supported_features = {PlayerFeature.VOLUME_SET, PlayerFeature.POWER}
        self._attr_device_info = DeviceInfo(model="Test Model", manufacturer="Test Manufacturer")
        self._cache.clear()
        self.update_state(signal_event=False)

    async def get_config_entries(self) -> list[ConfigEntry]:
        """Return the player's own config entries."""
        return [
            ConfigEntry(
                key=OWN_ENTRY_KEY,
                type=ConfigEntryType.STRING,
                label="Preset slot",
                required=False,
            ),
            # collides with an entry the protocol block already emits: must be deduped
            ConfigEntry(
                key=CONF_PREFERRED_OUTPUT_PROTOCOL,
                type=ConfigEntryType.STRING,
                label="Should be ignored",
                default_value="ignored",
                required=False,
            ),
        ]

    async def stop(self) -> None:
        """Stop playback - required abstract method."""


class _ProtocolPlayer(Player):
    """Minimal protocol player that provides the actual playback output."""

    def __init__(self, provider: _TestProvider, player_id: str, name: str) -> None:
        """Initialize the test player."""
        super().__init__(provider, player_id)  # type: ignore[arg-type]
        self._attr_name = name
        self._attr_type = PlayerType.PROTOCOL
        self._attr_available = True
        self._attr_powered = True
        self._attr_supported_features = {PlayerFeature.VOLUME_SET, PlayerFeature.PLAY_MEDIA}
        self._attr_device_info = DeviceInfo(model="Test Model", manufacturer="Test Manufacturer")
        self._cache.clear()
        self.update_state(signal_event=False)

    async def stop(self) -> None:
        """Stop playback - required abstract method."""


async def _setup_control_only_player(mass: MusicAssistant) -> _ControlOnlyPlayer:
    """Register a control-only player linked to a DLNA protocol player."""
    soundtouch_provider = _TestProvider(mass, "soundtouch")
    dlna_provider = _TestProvider(mass, "dlna")
    mass._providers[soundtouch_provider.instance_id] = soundtouch_provider  # type: ignore[assignment]
    mass._providers[dlna_provider.instance_id] = dlna_provider  # type: ignore[assignment]
    mass._provider_manifests[soundtouch_provider.domain] = soundtouch_provider.manifest
    mass._provider_manifests[dlna_provider.domain] = dlna_provider.manifest

    parent = _ControlOnlyPlayer(soundtouch_provider, PARENT_ID, "Kitchen")
    child = _ProtocolPlayer(dlna_provider, CHILD_ID, "Kitchen DLNA")

    mass.players._players[PARENT_ID] = parent
    mass.players._players[CHILD_ID] = child

    parent.set_linked_output_protocols(
        [
            LinkedOutputProtocol(
                output_protocol_id=CHILD_ID,
                protocol_domain="dlna",
                priority=50,
            )
        ]
    )
    # output_protocols is a cached property populated during __init__'s update_state
    # (empty at that point); drop the cache so the freshly linked protocol shows up
    parent._cache.clear()

    # avoid driving the full player-manager reload machinery
    mass.players.on_player_config_change = AsyncMock()  # type: ignore[method-assign]
    return parent


async def test_control_only_player_keeps_own_config_entries(mass: MusicAssistant) -> None:
    """A player with only non-native output protocols keeps its own config entries."""
    parent = await _setup_control_only_player(mass)
    assert not any(protocol.is_native for protocol in parent.output_protocols)

    entries = await mass.config.get_player_config_entries(PARENT_ID)
    keys = [entry.key for entry in entries]

    # the player's own entries survive alongside the injected protocol entries
    assert OWN_ENTRY_KEY in keys
    assert CONF_PREFERRED_OUTPUT_PROTOCOL in keys
    assert any(CONF_PROTOCOL_KEY_SPLITTER in key for key in keys)


async def test_control_only_player_entries_dedup_by_key(mass: MusicAssistant) -> None:
    """Own entries that collide with an injected protocol entry are skipped."""
    await _setup_control_only_player(mass)

    entries = await mass.config.get_player_config_entries(PARENT_ID)
    keys = [entry.key for entry in entries]

    assert len(keys) == len(set(keys))
    assert keys.count(OWN_ENTRY_KEY) == 1
    # the protocol-owned entry wins over the player's colliding one
    protocol_entry = next(entry for entry in entries if entry.key == CONF_PREFERRED_OUTPUT_PROTOCOL)
    assert protocol_entry.label != "Should be ignored"
    assert protocol_entry.options
