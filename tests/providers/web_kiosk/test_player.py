"""Tests for the WebKioskPlayer state machine."""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.enums import PlaybackState, PlayerFeature
from music_assistant_models.player import PlayerMedia

import music_assistant.providers.web_kiosk.player as player_module
from music_assistant.providers.web_kiosk.http_server import WebKioskHTTPServer
from music_assistant.providers.web_kiosk.player import WebKioskPlayer, media_timeline


def _media() -> Mock:
    """Return a minimal PlayerMedia stand-in."""
    media = Mock(spec=PlayerMedia)
    media.uri = "spotify://track/1"
    media.title = "Track"
    media.artist = "Artist"
    media.image_url = None
    media.stream_duration = 120
    media.duration = 120
    return media


async def test_play_media_sets_playing_and_media(player: WebKioskPlayer) -> None:
    """play_media stores the media and moves the player to PLAYING."""
    media = _media()
    await player.play_media(media)

    assert player.playback_state == PlaybackState.PLAYING
    assert player.current_media is media
    assert player.current_stream_url == media.uri


async def test_play_media_notifies_kiosk(player: WebKioskPlayer) -> None:
    """play_media pushes a 'play' broadcast through the HTTP server."""
    player.provider.http_server = Mock()  # type: ignore[attr-defined]
    await player.play_media(_media())

    player.provider.http_server.broadcast_play.assert_called_once()  # type: ignore[attr-defined]


def test_media_timeline_uses_song_length_after_seek() -> None:
    """A shortened stream still belongs to the full song."""
    media = Mock()
    media.duration = 208
    media.stream_duration = 62

    assert media_timeline(media) == (208.0, 146.0)


def test_media_timeline_without_stream_duration() -> None:
    """Missing stream length means the audio starts at the beginning."""
    media = Mock()
    media.duration = 208
    media.stream_duration = None

    assert media_timeline(media) == (208.0, 0.0)


def test_media_timeline_stream_only() -> None:
    """A stream length alone is the only clock the screen can draw."""
    media = Mock()
    media.duration = None
    media.stream_duration = 10

    assert media_timeline(media) == (10.0, 0.0)


def test_media_timeline_equal_durations_have_no_offset() -> None:
    """Equal lengths mean the served audio is the whole song."""
    media = Mock()
    media.duration = 120
    media.stream_duration = 120

    assert media_timeline(media) == (120.0, 0.0)


async def test_play_media_reports_song_length_and_stream_start(player: WebKioskPlayer) -> None:
    """After a seek, the kiosk is told the song length and where the file starts."""
    media = _media()
    media.duration = 208
    media.stream_duration = 62
    server = WebKioskHTTPServer(player.provider, 0)  # type: ignore[arg-type]
    server._ws_clients[player.player_id] = {Mock(closed=False)}
    server._post = Mock()  # type: ignore[method-assign]
    player.provider.http_server = server  # type: ignore[attr-defined]

    await player.play_media(media)

    message = json.loads(server._post.call_args.args[1])
    assert message["duration"] == 208.0
    assert message["start"] == 146.0


async def test_pause_snapshots_elapsed_time(player: WebKioskPlayer) -> None:
    """pause() accumulates elapsed time and moves to PAUSED."""
    player._attr_playback_state = PlaybackState.PLAYING
    player._attr_elapsed_time = 10.0
    player._attr_elapsed_time_last_updated = time.time() - 5.0

    await player.pause()

    assert player.playback_state == PlaybackState.PAUSED
    assert player._attr_elapsed_time is not None
    assert player._attr_elapsed_time >= 15.0


async def test_repeated_pause_keeps_paused_position(player: WebKioskPlayer) -> None:
    """A second pause must not add the time spent paused to the position."""
    player._attr_playback_state = PlaybackState.PLAYING
    player._attr_elapsed_time = 10.0
    player._attr_elapsed_time_last_updated = time.time() - 5.0
    await player.pause()
    paused_position = player._attr_elapsed_time
    assert paused_position is not None

    player._attr_elapsed_time_last_updated = time.time() - 60.0
    await player.pause()

    assert player.playback_state == PlaybackState.PAUSED
    assert player._attr_elapsed_time == paused_position


async def test_stop_clears_media_and_stream(player: WebKioskPlayer) -> None:
    """stop() returns the player to IDLE and clears current media."""
    await player.play_media(_media())
    await player.stop()

    assert player.playback_state == PlaybackState.IDLE
    assert player.current_media is None
    assert player.current_stream_url is None


async def test_seek_updates_elapsed(player: WebKioskPlayer) -> None:
    """seek() moves elapsed time and notifies the kiosk."""
    player._attr_playback_state = PlaybackState.PLAYING
    player.provider.http_server = Mock()  # type: ignore[attr-defined]

    await player.seek(30)

    assert player.elapsed_time == 30.0
    player.provider.http_server.broadcast_seek.assert_called_once_with(  # type: ignore[attr-defined]
        "wk_test", 30
    )


def test_update_position_ignored_while_paused(player: WebKioskPlayer) -> None:
    """Position reports are dropped unless the player is PLAYING."""
    player._attr_playback_state = PlaybackState.PAUSED
    player._attr_elapsed_time = 5.0

    player.update_position(99.0)

    assert player.elapsed_time == 5.0


async def test_slow_stream_progress_is_not_capped_in_media_seconds(
    player: WebKioskPlayer, mass_mock: Mock
) -> None:
    """A 0.5x stream can progress past the original media duration."""
    mass_mock.player_queues.get.return_value = Mock(
        current_item=Mock(extra_attributes={"playback_speed": 0.5})
    )
    await player.play_media(
        PlayerMedia(uri="podcast", duration=300, stream_duration=300, source_id="wk_test")
    )
    player.note_seek(400)
    assert player.elapsed_time == 400


async def test_speed_is_read_from_the_dispatched_item_before_queue_advances(
    player: WebKioskPlayer, mass_mock: Mock
) -> None:
    """MA dispatches play_media before replacing the queue's previous current item."""
    mass_mock.player_queues.get.return_value = Mock(
        current_item=Mock(extra_attributes={"playback_speed": 0.5})
    )
    mass_mock.player_queues.get_item.return_value = Mock(extra_attributes={"playback_speed": 2.0})
    await player.play_media(
        PlayerMedia(uri="new-podcast", duration=300, source_id="wk_test", queue_item_id="next")
    )
    assert player.playback_speed == 2.0


def _serve_flow(mass_mock: Mock, item_id: str, position: float) -> None:
    """Resolve queue media to a flow stream whose queue sits at item_id and position."""
    mass_mock.streams.base_url = "http://ma.local:8097"
    mass_mock.streams.resolve_stream_url = AsyncMock(
        return_value="http://ma.local:8097/flow/session/wk_test/item/wk_test.mp3"
    )
    mass_mock.player_queues.get.return_value = Mock(
        current_item=Mock(queue_item_id=item_id, duration=180),
        corrected_elapsed_time=position,
    )


async def test_reconnect_waiting_behind_a_track_change_does_not_seek_it(
    player: WebKioskPlayer, mass_mock: Mock
) -> None:
    """A stream started while a reconnect waits reached every page; it is not rebuilt."""
    _serve_flow(mass_mock, "old", 90)
    await player.play_media(
        PlayerMedia(uri="old", duration=180, source_id="wk_test", queue_item_id="old")
    )
    await player.pause()
    mass_mock.players.cmd_seek = AsyncMock()
    mass_mock.players.cmd_pause = AsyncMock()
    loading = asyncio.Event()
    finish = asyncio.Event()

    async def load_replacement() -> None:
        async with mass_mock.players.get_group_and_player_lock(player.player_id):
            loading.set()
            await finish.wait()
            await player.play_media(
                PlayerMedia(uri="new", duration=180, source_id="wk_test", queue_item_id="new")
            )

    loader = asyncio.create_task(load_replacement())
    await loading.wait()
    refresh = asyncio.create_task(player.refresh_stream())
    await asyncio.sleep(0)
    finish.set()
    await asyncio.gather(loader, refresh)

    mass_mock.players.cmd_seek.assert_not_awaited()
    mass_mock.players.cmd_pause.assert_not_awaited()


async def test_pause_during_a_reconnect_seek_is_kept(
    player: WebKioskPlayer, mass_mock: Mock
) -> None:
    """A pause issued while the stream is rebuilt is not overwritten by the new stream."""
    _serve_flow(mass_mock, "item", 60)
    await player.play_media(
        PlayerMedia(uri="song", duration=180, source_id="wk_test", queue_item_id="item")
    )
    mass_mock.players.get_player.return_value = player

    async def seek_with_a_pause(_player_id: str, _position: int) -> None:
        # Queue pause takes no playback lock; it lands while the seek prepares.
        await player.pause()
        await player.play_media(
            PlayerMedia(uri="song@60", duration=180, source_id="wk_test", queue_item_id="item")
        )

    mass_mock.players.cmd_seek = AsyncMock(side_effect=seek_with_a_pause)

    assert await player.refresh_stream()

    assert player.current_media is not None
    assert player.current_media.uri == "song@60"
    assert player.playback_state == PlaybackState.PAUSED


@pytest.mark.parametrize("volume_control", [True, False])
def test_kiosk_advertises_mute_independent_of_volume_control(
    player: WebKioskPlayer, volume_control: bool
) -> None:
    """Muting the media element works even where the browser cannot change its volume."""
    player.set_volume_control_supported(volume_control)
    assert PlayerFeature.VOLUME_MUTE in player.supported_features
    assert player.volume_muted is False


async def test_volume_mute_keeps_volume_and_notifies_kiosk(player: WebKioskPlayer) -> None:
    """Mute and unmute toggle the muted state and leave the volume level alone."""
    server = Mock()
    player.provider.http_server = server  # type: ignore[attr-defined]
    player._attr_volume_level = 40

    def state() -> tuple[bool | None, int | None]:
        return player.volume_muted, player.volume_level

    await player.volume_mute(True)
    assert state() == (True, 40)
    server.broadcast_mute.assert_called_with("wk_test", True)

    await player.volume_mute(False)
    assert state() == (False, 40)
    server.broadcast_mute.assert_called_with("wk_test", False)


class _Clock:
    """Wall and monotonic clock for the player module, moved by the test."""

    def __init__(self) -> None:
        self.now = 1_000_000.0

    def time(self) -> float:
        return self.now

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    """Drive the kiosk player's clock by hand."""
    fake = _Clock()
    monkeypatch.setattr(player_module, "time", fake)
    return fake


async def _reporting_at(player: WebKioskPlayer, clock: _Clock, position: float) -> None:
    """Start a 300 s track and let the page report the given position."""
    await player.play_media(PlayerMedia(uri="track", duration=300))
    clock.now += position
    player.update_position(position)


async def test_clock_holds_when_the_page_stops_reporting(
    player: WebKioskPlayer, clock: _Clock
) -> None:
    """Without reports the position runs on for at most 10 s, then holds."""
    await _reporting_at(player, clock, 100)

    clock.now += 30
    await player.poll()
    held = player.elapsed_time
    clock.now += 30
    await player.poll()

    assert held is not None
    assert 100 <= held <= 110
    assert player.elapsed_time == held
    assert player.elapsed_time_last_updated == clock.now


async def test_clock_resumes_from_a_report_after_a_hold(
    player: WebKioskPlayer, clock: _Clock
) -> None:
    """A report after a hold sets the position again."""
    await _reporting_at(player, clock, 100)
    clock.now += 30
    await player.poll()

    clock.now += 5
    player.update_position(135)

    assert player.elapsed_time == 135


async def test_clock_does_not_run_past_the_served_audio(
    player: WebKioskPlayer, clock: _Clock
) -> None:
    """Between reports the position stops at the end of the served audio."""
    await _reporting_at(player, clock, 299)

    clock.now += 8
    await player.poll()

    assert player.elapsed_time == 300


async def test_pause_after_silence_keeps_the_held_position(
    player: WebKioskPlayer, clock: _Clock
) -> None:
    """Pausing a page that stopped reporting does not add the silent time."""
    await _reporting_at(player, clock, 100)
    clock.now += 60

    await player.pause()

    assert player.elapsed_time == 110
