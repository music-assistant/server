"""Playback started after an await goes through a playback claim (see CLAUDE.local.md)."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, Mock

from music_assistant_models.enums import PlaybackState
from music_assistant_models.player import PlayerMedia

from music_assistant.providers.web_kiosk.player import WebKioskPlayer
from music_assistant.providers.web_kiosk.provider import WebKioskProvider


def _media(uri: str = "song") -> PlayerMedia:
    """Return media that starts a new playback on the kiosk."""
    return PlayerMedia(uri=uri, duration=180, source_id="wk_test", queue_item_id=uri)


async def test_claim_starts_playback_it_owns(player: WebKioskPlayer, mass_mock: Mock) -> None:
    """With nothing in between, the claim starts the stream and leaves it playing."""
    mass_mock.players.get_player.return_value = player
    claim = player.claim()

    assert await claim.start(lambda: player.play_media(_media()))

    assert player.playback_state == PlaybackState.PLAYING


async def test_claim_does_not_start_after_a_stop(player: WebKioskPlayer, mass_mock: Mock) -> None:
    """A stop during the wait wins: the stream is not started."""
    mass_mock.players.get_player.return_value = player
    await player.play_media(_media())
    claim = player.claim()
    await player.stop()
    begin = AsyncMock()

    assert not await claim.start(begin)

    begin.assert_not_awaited()
    assert player.playback_state == PlaybackState.IDLE


async def test_claim_does_not_start_on_a_replaced_player(
    provider: WebKioskProvider, player: WebKioskPlayer, mass_mock: Mock
) -> None:
    """A player instance replaced during the wait is not the one the stream may reach."""
    mass_mock.players.get_player.return_value = player
    claim = player.claim()
    mass_mock.players.get_player.return_value = WebKioskPlayer(provider, player.player_id)
    begin = AsyncMock()

    assert not await claim.start(begin)

    begin.assert_not_awaited()


async def test_pause_during_the_wait_is_kept(player: WebKioskPlayer, mass_mock: Mock) -> None:
    """A pause issued while playback is prepared leaves the started stream paused."""
    mass_mock.players.get_player.return_value = player
    await player.play_media(_media())
    claim = player.claim()
    await player.pause()

    assert await claim.start(lambda: player.play_media(_media("next")))

    assert player.playback_state == PlaybackState.PAUSED


async def test_play_after_a_pause_at_the_claim_wins(
    player: WebKioskPlayer, mass_mock: Mock
) -> None:
    """A play issued during the wait overrides the pause the player had when it was claimed."""
    mass_mock.players.get_player.return_value = player
    await player.play_media(_media())
    await player.pause()
    claim = player.claim()
    await player.play()

    assert await claim.start(lambda: player.play_media(_media("next")))

    assert player.playback_state == PlaybackState.PLAYING


async def test_released_takeover_without_a_start_goes_idle(
    player: WebKioskPlayer, mass_mock: Mock
) -> None:
    """Playback taken over from an output is dropped when no stream replaced it."""
    mass_mock.players.get_player.return_value = player
    cast("Any", player).set_active_output_protocol = Mock()
    claim = player.claim(take_over=True)

    claim.release()

    assert player.playback_state == PlaybackState.IDLE
    assert player.current_media is None


async def test_released_takeover_keeps_the_stream_it_started(
    player: WebKioskPlayer, mass_mock: Mock
) -> None:
    """Releasing after a start leaves the new stream alone."""
    mass_mock.players.get_player.return_value = player
    cast("Any", player).set_active_output_protocol = Mock()
    claim = player.claim(take_over=True)
    await claim.start(lambda: player.play_media(_media()))

    claim.release()

    assert player.playback_state == PlaybackState.PLAYING
    assert player.current_media is not None
