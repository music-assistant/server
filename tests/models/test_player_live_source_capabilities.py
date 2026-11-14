"""
Tests for the external sources published in a player's source list.

While a source has taken a player over there is no queue to read its ordering from,
so the source entry itself has to carry what the source can do and what it reports
being in — otherwise a client has nothing to drive its shuffle/repeat controls off.
Sources a plugin has bound to the player, and the inputs of the player's own device,
get a standing entry even without a live session, so they are selectable straight
from the source menu.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import ProtocolRole, RepeatMode
from music_assistant_models.media_items import AudioSource, ProviderMapping

from music_assistant.controllers.players.audio_sources import AudioSourceSession
from music_assistant.models.player import Player
from music_assistant.models.plugin import PluginProvider
from tests.common import MockProvider, attach_mock, mock_player_mass

if TYPE_CHECKING:
    from music_assistant_models.player import PlayerSource

PLAYER_ID = "player_1"
SOURCE_URI = "spotify_connect--test://audio_source/main"


@pytest.fixture
def mock_mass() -> MagicMock:
    """Create a mock MusicAssistant instance."""
    return mock_player_mass()


@pytest.fixture
def provider(mock_mass: MagicMock) -> MockProvider:
    """Create a mock provider."""
    return MockProvider("test_provider", mass=mock_mass)


def _audio_source(
    *,
    can_play_pause: bool = False,
    can_shuffle: bool = False,
    can_repeat: bool = False,
    can_initiate: bool = False,
) -> AudioSource:
    """Return a live source as a plugin declares it."""
    return AudioSource(
        item_id="main",
        provider="spotify_connect--test",
        name="Spotify Connect",
        provider_mappings={
            ProviderMapping(
                item_id="main",
                provider_domain="spotify_connect",
                provider_instance="spotify_connect--test",
            )
        },
        can_play_pause=can_play_pause,
        can_shuffle=can_shuffle,
        can_repeat=can_repeat,
        can_initiate=can_initiate,
    )


class _BoundSourcePlugin(PluginProvider):
    """Plugin stub that binds one AudioSource to one player."""

    def __init__(self, source: AudioSource, player_id: str) -> None:
        self._source = source
        self._bound_player_id = player_id

    def get_player_audio_sources(self, player_id: str) -> list[AudioSource]:
        """Return the bound source for its own player only."""
        return [self._source] if player_id == self._bound_player_id else []


class _InputSourcePlugin(PluginProvider):
    """Plugin stub that reports one AudioSource as an input of one player's device."""

    def __init__(self, source: AudioSource, player_id: str) -> None:
        self._source = source
        self._device_player_id = player_id

    def get_player_input_sources(self, player_id: str) -> list[AudioSource]:
        """Return the input for the device's own player only."""
        return [self._source] if player_id == self._device_player_id else []


def _input_plugin(mock_mass: MagicMock, source: AudioSource, player_id: str) -> None:
    """Expose the given source as an input of the given player via a plugin provider."""
    plugin = _InputSourcePlugin(source, player_id)
    mock_mass.get_providers_supporting_feature = MagicMock(return_value=[plugin])


def _bound_plugin(mock_mass: MagicMock, source: AudioSource, player_id: str = PLAYER_ID) -> None:
    """Expose the given source as bound to the given player via a plugin provider."""
    plugin = _BoundSourcePlugin(source, player_id)
    mock_mass.get_providers_supporting_feature = MagicMock(return_value=[plugin])


def _live_source(player: Player, uri: str = SOURCE_URI) -> PlayerSource | None:
    """Return the external source entry from the source list the player publishes."""
    player.refresh_state(signal_event=False)
    return next((x for x in player.source_list if x.id == uri), None)


def _playing(mock_mass: MagicMock, source: AudioSource, **state: object) -> None:
    """Put the given source on the player as its live session."""
    session = AudioSourceSession(
        player_id=PLAYER_ID,
        source=source,
        provider_instance_id="spotify_connect--test",
        **state,  # type: ignore[arg-type]
    )
    mock_mass.players.get_audio_source_session = MagicMock(return_value=session)


def test_a_shuffle_report_tells_subscribers(mock_mass: MagicMock, provider: MockProvider) -> None:
    """
    A source reporting new ordering reaches clients, not just the next poll.

    The published state is only half the contract: the source list is part of the
    player's change fingerprint, so a report that does not move it leaves every
    subscriber on the old value until something unrelated happens to fire.
    """
    player, _ = attach_mock(provider, "protocol_1", "Player", player_id=PLAYER_ID)
    source = _audio_source(can_shuffle=True, can_repeat=True)
    _playing(mock_mass, source)
    # settle the baseline, so the next update only carries the report
    player.update_state()
    mock_mass.players.signal_player_state_update.reset_mock()

    _playing(mock_mass, source, shuffle_enabled=True, repeat_mode=RepeatMode.ALL)
    # the session lives outside the player's own attributes, so the real path
    # (update_source_options -> trigger_player_update) marks it dirty first
    player.mark_state_dirty()
    player.update_state()

    mock_mass.players.signal_player_state_update.assert_called()


def test_bound_source_gets_a_standing_entry(mock_mass: MagicMock, provider: MockProvider) -> None:
    """A source bound to this player is selectable without a session being active."""
    player, _ = attach_mock(provider, "protocol_1", "Player", player_id=PLAYER_ID)
    _bound_plugin(mock_mass, _audio_source(can_initiate=True, can_play_pause=True))

    source = _live_source(player)

    assert source is not None
    assert source.passive is False
    assert source.can_play_pause is True


def test_source_bound_to_another_player_is_not_listed(
    mock_mass: MagicMock, provider: MockProvider
) -> None:
    """Another player's bound source stays out of this player's source menu."""
    player, _ = attach_mock(provider, "protocol_1", "Player", player_id=PLAYER_ID)
    _bound_plugin(mock_mass, _audio_source(can_initiate=True), player_id="other_player")

    assert _live_source(player) is None


def test_device_input_gets_a_standing_entry(mock_mass: MagicMock, provider: MockProvider) -> None:
    """An input of the player's own device is selectable without a session being active."""
    player, _ = attach_mock(provider, "protocol_1", "Player", player_id=PLAYER_ID)
    _input_plugin(mock_mass, _audio_source(can_initiate=True), PLAYER_ID)

    assert _live_source(player) is not None


def test_input_reported_on_a_linked_protocol_lands_on_the_visible_player(
    mock_mass: MagicMock, provider: MockProvider
) -> None:
    """An input the plugin reports for an audio protocol is listed on the player it belongs to."""
    player, _ = attach_mock(provider, "protocol_1", "Player", player_id=PLAYER_ID)
    attach_mock(
        MockProvider("sendspin", mass=mock_mass),
        "sendspin_x",
        role=ProtocolRole.AUDIO,
        player=player,
    )
    _input_plugin(mock_mass, _audio_source(can_initiate=True), "sendspin_x")

    assert _live_source(player) is not None


def test_input_of_another_device_is_not_listed(
    mock_mass: MagicMock, provider: MockProvider
) -> None:
    """Another device's input stays out of this player's source menu."""
    player, _ = attach_mock(provider, "protocol_1", "Player", player_id=PLAYER_ID)
    _input_plugin(mock_mass, _audio_source(can_initiate=True), "other_player")

    assert _live_source(player) is None
