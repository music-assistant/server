"""Tests for MilkDrop visualizer target resolution."""

from __future__ import annotations

from unittest.mock import Mock

from music_assistant_models.enums import PlaybackState

from music_assistant.providers.milkdrop_visualizer.relay import MilkdropRelay


def _relay(players: list[Mock], queues: dict[str, Mock] | None = None) -> MilkdropRelay:
    """Return a relay whose mass exposes the given players and their active queues."""
    provider = Mock()
    provider.mass.players.__iter__ = lambda _self: iter(players)
    provider.mass.players.get_player = Mock(
        side_effect={player.player_id: player for player in players}.get
    )
    provider.mass.player_queues.get_active_queue = Mock(side_effect=(queues or {}).get)
    provider.logger.getChild.return_value = Mock()
    return MilkdropRelay(provider)


def _player(player_id: str) -> Mock:
    """Build a player-shaped mock."""
    player = Mock()
    player.player_id = player_id
    player.display_name = player_id
    return player


def _queue(playing: bool) -> Mock:
    """Build a queue-shaped mock in the given playback state."""
    queue = Mock()
    queue.state = PlaybackState.PLAYING if playing else PlaybackState.IDLE
    return queue


def test_resolves_the_named_player() -> None:
    """Any player id resolves to that player, whatever protocol it plays over."""
    chromecast = _player("chromecast-kitchen")
    relay = _relay([_player("dlna_box"), chromecast])
    assert relay._resolve_target("chromecast-kitchen") is chromecast


def test_unknown_player_resolves_to_none() -> None:
    """A query naming no player at all is reported back rather than guessed at."""
    relay = _relay([_player("dlna_box")])
    assert relay._resolve_target("gone-away") is None


def test_no_query_auto_picks_a_playing_player() -> None:
    """With no query, a player that is playing wins over an idle one."""
    idle = _player("dlna_box")
    playing = _player("chromecast-kitchen")
    relay = _relay(
        [idle, playing],
        {"dlna_box": _queue(playing=False), "chromecast-kitchen": _queue(playing=True)},
    )
    assert relay._resolve_target(None) is playing


def test_no_query_and_nothing_playing_resolves_to_none() -> None:
    """With nothing playing there is no audio to pick up, so no target."""
    relay = _relay([_player("dlna_box")], {"dlna_box": _queue(playing=False)})
    assert relay._resolve_target(None) is None
