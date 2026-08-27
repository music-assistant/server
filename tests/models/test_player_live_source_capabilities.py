"""
Tests for the external sources published in a player's source list.

While a source has taken a player over there is no queue to read its ordering from,
so the source entry itself has to carry what the source can do and what it reports
being in — otherwise a client has nothing to drive its shuffle/repeat controls off.
Sources a plugin has bound to the player get a standing entry even without a live
session, so they are selectable straight from the source menu.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import PlayerType, RepeatMode
from music_assistant_models.media_items import AudioSource, ProviderMapping

from music_assistant.controllers.players.audio_sources import AudioSourceSession
from music_assistant.models.plugin import PluginProvider
from tests.common import MockPlayer, MockProvider

if TYPE_CHECKING:
    from music_assistant_models.player import PlayerSource

PLAYER_ID = "player_1"
SOURCE_URI = "spotify_connect--test://audio_source/main"


@pytest.fixture
def mock_mass() -> MagicMock:
    """Create a mock MusicAssistant instance."""
    mass = MagicMock()
    mass.config.get_raw_player_config_value = MagicMock(
        side_effect=lambda _player_id, _key, default=None: default
    )
    mass.players.get_audio_source_session = MagicMock(return_value=None)
    return mass


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


def _bound_plugin(mock_mass: MagicMock, source: AudioSource, player_id: str = PLAYER_ID) -> None:
    """Expose the given source as bound to the given player via a plugin provider."""
    plugin = _BoundSourcePlugin(source, player_id)
    mock_mass.get_providers_supporting_feature = MagicMock(return_value=[plugin])


def _live_source(player: MockPlayer, uri: str = SOURCE_URI) -> PlayerSource | None:
    """Return the external source entry from the player's FINAL source list."""
    sources = player._Player__final_source_list  # type: ignore[attr-defined]
    return next((x for x in sources if x.id == uri), None)


def _playing(mock_mass: MagicMock, source: AudioSource, **state: object) -> None:
    """Put the given source on the player as its live session."""
    session = AudioSourceSession(
        player_id=PLAYER_ID,
        source=source,
        provider_instance_id="spotify_connect--test",
        **state,  # type: ignore[arg-type]
    )
    mock_mass.players.get_audio_source_session = MagicMock(return_value=session)


def test_ordering_source_advertises_its_controls(
    mock_mass: MagicMock, provider: MockProvider
) -> None:
    """A source that orders its own session says so, so clients can offer the controls."""
    player = MockPlayer(provider, PLAYER_ID, "Player")
    _playing(mock_mass, _audio_source(can_shuffle=True, can_repeat=True))

    source = _live_source(player)

    assert source is not None
    assert source.can_shuffle is True
    assert source.can_repeat is True


def test_transport_only_source_advertises_no_ordering(
    mock_mass: MagicMock, provider: MockProvider
) -> None:
    """A source that cannot be reordered says so, so clients grey the controls out."""
    player = MockPlayer(provider, PLAYER_ID, "Player")
    _playing(mock_mass, _audio_source(can_play_pause=True))

    source = _live_source(player)

    assert source is not None
    # pins that this source really was published, so the two flags below are a
    # read of it rather than of an absent entry
    assert source.can_play_pause is True
    assert source.can_shuffle is False
    assert source.can_repeat is False


def test_reported_ordering_reaches_the_source_list(
    mock_mass: MagicMock, provider: MockProvider
) -> None:
    """The state the session reports is what clients render."""
    player = MockPlayer(provider, PLAYER_ID, "Player")
    _playing(
        mock_mass,
        _audio_source(can_shuffle=True, can_repeat=True),
        shuffle_enabled=True,
        repeat_mode=RepeatMode.ALL,
    )

    source = _live_source(player)

    assert source is not None
    assert source.shuffle_enabled is True
    assert source.repeat_mode is RepeatMode.ALL


def test_unreported_ordering_stays_unknown(mock_mass: MagicMock, provider: MockProvider) -> None:
    """A session that has not reported its ordering must not read as shuffle off."""
    player = MockPlayer(provider, PLAYER_ID, "Player")
    _playing(mock_mass, _audio_source(can_shuffle=True, can_repeat=True))

    source = _live_source(player)

    assert source is not None
    assert source.shuffle_enabled is None
    assert source.repeat_mode is None


def test_reported_off_is_not_unreported(mock_mass: MagicMock, provider: MockProvider) -> None:
    """A session reporting shuffle off is distinct from one that has not reported."""
    player = MockPlayer(provider, PLAYER_ID, "Player")
    _playing(
        mock_mass,
        _audio_source(can_shuffle=True, can_repeat=True),
        shuffle_enabled=False,
        repeat_mode=RepeatMode.OFF,
    )

    source = _live_source(player)

    assert source is not None
    assert source.shuffle_enabled is False
    assert source.repeat_mode is RepeatMode.OFF


def test_a_shuffle_report_tells_subscribers(mock_mass: MagicMock, provider: MockProvider) -> None:
    """
    A source reporting new ordering reaches clients, not just the next poll.

    The published state is only half the contract: the source list is part of the
    player's change fingerprint, so a report that does not move it leaves every
    subscriber on the old value until something unrelated happens to fire.
    """
    player = MockPlayer(provider, PLAYER_ID, "Player")
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
    player = MockPlayer(provider, PLAYER_ID, "Player")
    _bound_plugin(mock_mass, _audio_source(can_initiate=True, can_play_pause=True))

    source = _live_source(player)

    assert source is not None
    assert source.passive is False
    assert source.can_play_pause is True


def test_source_bound_to_another_player_is_not_listed(
    mock_mass: MagicMock, provider: MockProvider
) -> None:
    """Another player's bound source stays out of this player's source menu."""
    player = MockPlayer(provider, PLAYER_ID, "Player")
    _bound_plugin(mock_mass, _audio_source(can_initiate=True), player_id="other_player")

    assert _live_source(player) is None


def test_standing_entry_passive_follows_can_initiate(
    mock_mass: MagicMock, provider: MockProvider
) -> None:
    """A source MA cannot start on demand is listed as passive."""
    player = MockPlayer(provider, PLAYER_ID, "Player")
    _bound_plugin(mock_mass, _audio_source(can_initiate=False))

    source = _live_source(player)

    assert source is not None
    assert source.passive is True


def test_live_session_entry_wins_over_the_standing_entry(
    mock_mass: MagicMock, provider: MockProvider
) -> None:
    """While the source is live, the session entry with its live ordering state is the one listed."""
    player = MockPlayer(provider, PLAYER_ID, "Player")
    source = _audio_source(can_initiate=True, can_shuffle=True, can_repeat=True)
    _playing(mock_mass, source, shuffle_enabled=True, repeat_mode=RepeatMode.ALL)
    _bound_plugin(mock_mass, source)

    sources = player._Player__final_source_list  # type: ignore[attr-defined]
    entries = [x for x in sources if x.id == SOURCE_URI]

    assert len(entries) == 1
    assert entries[0].shuffle_enabled is True
    assert entries[0].repeat_mode is RepeatMode.ALL


def test_protocol_player_gets_no_standing_entry(
    mock_mass: MagicMock, provider: MockProvider
) -> None:
    """Protocol players keep their bare source list, without bound-source entries."""
    player = MockPlayer(provider, PLAYER_ID, "Player", player_type=PlayerType.PROTOCOL)
    _bound_plugin(mock_mass, _audio_source(can_initiate=True))

    assert _live_source(player) is None
