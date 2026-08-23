"""
Tests for the live external source published in a player's source list.

While a source has taken a player over there is no queue to read its ordering from,
so the source entry itself has to carry what the source can do and what it reports
being in — otherwise a client has nothing to drive its shuffle/repeat controls off.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import RepeatMode
from music_assistant_models.media_items import AudioSource, ProviderMapping

from music_assistant.controllers.players.audio_sources import AudioSourceSession
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
    )


def _live_source(player: MockPlayer, uri: str = SOURCE_URI) -> PlayerSource | None:
    """Return the live external source from the player's FINAL source list."""
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
