"""Unit tests for the player-control config entries."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from music_assistant_models.constants import PLAYER_CONTROL_FAKE
from music_assistant_models.enums import PlayerFeature, PlayerType

from music_assistant.constants import CONF_MUTE_CONTROL
from music_assistant.mass import MusicAssistant

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry


def _make_player(features: set[PlayerFeature]) -> MagicMock:
    """Return a mock non-group player that supports exactly the given features."""
    player = MagicMock()
    player.state.type = PlayerType.PLAYER
    player.linked_output_protocols = []
    player.supports_feature.side_effect = lambda feature: feature in features
    return player


def _entries_by_key(
    mass: MusicAssistant,
    features: set[PlayerFeature],
    protocol_player: MagicMock | None = None,
) -> dict[str, ConfigEntry]:
    """Build the player-control config entries for a player with the given features."""
    player = _make_player(features)
    if protocol_player is not None:
        player.linked_output_protocols = [
            MagicMock(output_protocol_id="protocol_player_1", protocol_domain="squeezelite")
        ]
    mass.players = MagicMock()
    mass.players.player_controls.return_value = []
    mass.players.get_player.return_value = protocol_player
    entries = mass.config._create_player_control_config_entries(player)
    return {entry.key: entry for entry in entries}


async def test_fake_mute_offered_when_volume_provided_by_protocol_player(
    mass_minimal: MusicAssistant,
) -> None:
    """
    Fake mute is offered when the volume path comes from a linked protocol player.

    Regression test: players whose volume is provided by a linked protocol player
    (e.g. squeezelite wrapped by the universal player) don't report VOLUME_SET
    natively, yet fake mute works fine for them as it drives the configured
    volume control.
    """
    protocol_player = MagicMock()
    protocol_player.available = True
    protocol_player.supports_feature.side_effect = lambda feature: (
        feature == PlayerFeature.VOLUME_SET
    )
    entry = _entries_by_key(mass_minimal, set(), protocol_player)[CONF_MUTE_CONTROL]
    assert any(option.value == PLAYER_CONTROL_FAKE for option in entry.options)


async def test_fake_mute_not_offered_without_any_volume_path(
    mass_minimal: MusicAssistant,
) -> None:
    """Fake mute stays hidden when neither the player nor a protocol player provides volume."""
    entry = _entries_by_key(mass_minimal, {PlayerFeature.VOLUME_MUTE})[CONF_MUTE_CONTROL]
    assert all(option.value != PLAYER_CONTROL_FAKE for option in entry.options)
