"""
Regression tests for control-only players losing their own config entries.

Reproduces music-assistant/support#4766: a player that only handles control
(power/volume/source) and delegates playback to an audio protocol (e.g.
Bose SoundTouch streaming via DLNA) has no native output protocol. The injected
protocol entries replaced the player's own entries entirely, so everything its
``get_config_entries()`` returned disappeared from the config UI.
"""

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType, PlayerFeature, ProtocolRole

from music_assistant.constants import (
    CONF_PREFERRED_OUTPUT_PROTOCOL,
    CONF_PROTOCOL_KEY_SPLITTER,
)
from music_assistant.mass import MusicAssistant
from music_assistant.models.player import Player
from tests.common import MockProtocol

from .helpers import attach_on_config

PLAYER_ID = "pl0123456789ab"
OWN_ENTRY_KEY = "preset_slot"


class _ControlOnlyProtocol(MockProtocol):
    """Protocol that only offers control: playback goes through another protocol."""

    async def get_config_entries(self) -> list[ConfigEntry]:
        """Return the protocol's own config entries."""
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


def _setup_control_only_player(mass: MusicAssistant) -> Player:
    """Attach a control-only protocol and a DLNA protocol of one device."""
    # deliberately no PLAY_MEDIA: this protocol cannot play media itself,
    # which is what makes all of the player's output protocols non-native
    player, _ = attach_on_config(
        mass,
        "soundtouch",
        "soundtouch_123",
        "Kitchen",
        player_id=PLAYER_ID,
        features={PlayerFeature.VOLUME_SET, PlayerFeature.POWER},
        protocol_class=_ControlOnlyProtocol,
    )
    attach_on_config(
        mass,
        "dlna",
        "dlna_AABBCCDDEEFF",
        "Kitchen DLNA",
        role=ProtocolRole.AUDIO,
        player=player,
        features={PlayerFeature.VOLUME_SET, PlayerFeature.PLAY_MEDIA},
    )
    return player


async def test_control_only_player_keeps_own_config_entries(mass_minimal: MusicAssistant) -> None:
    """A player with only non-native output protocols keeps its own config entries."""
    player = _setup_control_only_player(mass_minimal)
    assert not any(protocol.is_native for protocol in player._output_protocols)

    entries = await mass_minimal.config.get_player_config_entries(PLAYER_ID)
    keys = [entry.key for entry in entries]

    # the player's own entries survive alongside the injected protocol entries
    assert OWN_ENTRY_KEY in keys
    assert CONF_PREFERRED_OUTPUT_PROTOCOL in keys
    assert any(CONF_PROTOCOL_KEY_SPLITTER in key for key in keys)


async def test_control_only_player_entries_dedup_by_key(mass_minimal: MusicAssistant) -> None:
    """Own entries that collide with an injected protocol entry are skipped."""
    _setup_control_only_player(mass_minimal)

    entries = await mass_minimal.config.get_player_config_entries(PLAYER_ID)
    keys = [entry.key for entry in entries]

    assert len(keys) == len(set(keys))
    assert keys.count(OWN_ENTRY_KEY) == 1
    # the protocol-owned entry wins over the player's colliding one
    protocol_entry = next(entry for entry in entries if entry.key == CONF_PREFERRED_OUTPUT_PROTOCOL)
    assert protocol_entry.label != "Should be ignored"
    assert protocol_entry.options
