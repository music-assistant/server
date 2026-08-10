"""Tests for MilkDrop visualizer target resolution."""

from __future__ import annotations

from unittest.mock import Mock

from music_assistant_models.enums import PlaybackState

from music_assistant.providers.milkdrop_visualizer.relay import MilkdropRelay
from music_assistant.providers.sendspin.player import SendspinBasePlayer


def _relay(players: list[Mock]) -> MilkdropRelay:
    """Return a relay whose mass exposes the given players."""
    provider = Mock()
    provider.mass.players = players
    provider.logger.getChild.return_value = Mock()
    return MilkdropRelay(provider)


def _player(
    player_id: str,
    *,
    underlying: str | None = None,
    playing: bool = False,
) -> Mock:
    """Build a SendspinBasePlayer-shaped mock the resolver will accept."""
    player = Mock(spec=SendspinBasePlayer)
    player.player_id = player_id
    player.underlying_player_id = underlying
    player.display_name = player_id
    player.api = Mock()
    player.playback_state = PlaybackState.PLAYING if playing else PlaybackState.IDLE
    return player


def test_resolves_exact_player_id() -> None:
    """A query equal to a player id resolves to that player."""
    target = _player("20:F8:3B:09:00:F6")
    relay = _relay([_player("aa:bb:cc:dd:ee:ff"), target])
    assert relay._resolve_target("20:F8:3B:09:00:F6") is target


def test_resolves_universal_wrapper_id_by_suffix() -> None:
    """A universal player id ('up' + normalized MAC) matches the Sendspin device it wraps."""
    target = _player("20:F8:3B:09:00:F6")
    relay = _relay([target])
    assert relay._resolve_target("up20f83b0900f6") is target


def test_unmatched_player_returns_none_rather_than_guessing() -> None:
    """A named player with no Sendspin group resolves to None instead of an arbitrary tap."""
    relay = _relay([_player("20:F8:3B:09:00:F6", playing=True)])
    assert relay._resolve_target("some-chromecast-id") is None


def test_no_query_auto_picks_the_playing_player() -> None:
    """With no query, the currently-playing Sendspin player wins over idle ones."""
    idle = _player("aa:bb:cc:dd:ee:ff")
    playing = _player("20:F8:3B:09:00:F6", playing=True)
    relay = _relay([idle, playing])
    assert relay._resolve_target(None) is playing


def test_no_players_returns_none() -> None:
    """With no Sendspin players at all, resolution yields None."""
    relay = _relay([])
    assert relay._resolve_target(None) is None
