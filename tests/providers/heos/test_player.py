"""Tests for the HEOS protocol."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.enums import MediaType, PlaybackState
from pyheos import PlayState as HeosPlayState
from pyheos import const as heos_const

from music_assistant.models.player import PlayerMedia
from music_assistant.providers.heos.player import HeosPlayer


def _url_stream_now_playing() -> MagicMock:
    """HEOS now-playing for a generic URL stream it cannot parse (MA's own source)."""
    now_playing = MagicMock()
    now_playing.source_id = heos_const.MUSIC_SOURCE_LOCAL_MUSIC
    now_playing.type = "song"
    now_playing.song = "Url Stream"
    now_playing.album = "Url Stream"
    now_playing.artist = "Url Stream"
    now_playing.image_url = ""
    now_playing.media_id = "1"
    now_playing.album_id = "1"
    now_playing.current_position = None
    now_playing.current_position_updated = None
    now_playing.duration = None
    return now_playing


def _external_now_playing(current_position: int | None, duration: int | None = 245000) -> MagicMock:
    """HEOS now-playing for an external source, with millisecond progress values."""
    now_playing = _url_stream_now_playing()
    now_playing.source_id = 9999
    now_playing.song = "External Track"
    now_playing.current_position = current_position
    now_playing.current_position_updated = datetime(2026, 1, 1, tzinfo=UTC)
    now_playing.duration = duration
    return now_playing


def _make_player(now_playing: MagicMock) -> HeosPlayer:
    """Build a HeosPlayer backed by a mocked device/controller."""
    provider = MagicMock()
    provider._heos_queue = MagicMock()
    device = MagicMock()
    device.player_id = "1"
    device.name = "Kitchen"
    device.heos = MagicMock()
    device.state = HeosPlayState.PLAY
    device.now_playing_media = now_playing
    return HeosPlayer(provider, device)


def test_url_stream_now_playing_preserves_ma_media_while_ma_controls() -> None:
    """
    Preserve MA's current_media against a HEOS "Url Stream" report.

    HEOS cannot parse metadata from the generic URL stream MA serves, so it
    reports ``Url Stream``. When MA controls playback that report must be
    ignored (even if ``active_source`` is momentarily stale from the
    ``play_url`` race) so MA's own, correct metadata is preserved.

    See https://github.com/music-assistant/support/issues/5614
    """
    protocol = _make_player(_url_stream_now_playing())
    correct_media = PlayerMedia(
        uri="http://ma/stream/foo",
        media_type=MediaType.TRACK,
        title="Real Track",
        artist="Real Artist",
        album="Real Album",
    )
    protocol._attr_current_media = correct_media
    protocol._ma_controls_playback = True
    # Stale: a previous external source is still the recorded active source.
    protocol._attr_active_source = "external_radio"

    protocol._update_player_current_media()

    # MA's correct media is preserved; the bogus "Url Stream" did not clobber it.
    assert protocol._attr_current_media is correct_media
    assert protocol._attr_current_media.title == "Real Track"
    # Playback control was not latched away to the phantom external source.
    assert protocol._ma_controls_playback is True


def test_external_source_now_playing_still_updates_media() -> None:
    """A genuinely external (non-local) source must still update current_media."""
    now_playing = _url_stream_now_playing()
    now_playing.source_id = 9999  # an external source, not MA's local stream
    now_playing.song = "External Track"
    now_playing.artist = "External Artist"
    protocol = _make_player(now_playing)
    protocol._attr_current_media = PlayerMedia(
        uri="http://ma/stream/foo", media_type=MediaType.TRACK, title="MA Track"
    )
    protocol._ma_controls_playback = True
    protocol._attr_active_source = protocol.protocol_id

    protocol._update_player_current_media()

    assert protocol._attr_current_media.title == "External Track"


def test_external_source_is_ignored_during_ma_playback_transition() -> None:
    """Suppress external-source media updates while MA playback is starting."""
    protocol = _make_player(_external_now_playing(None))
    protocol._ma_playback_starting = True
    protocol._ma_controls_playback = True
    current_media = PlayerMedia(
        uri="http://ma/stream/foo", media_type=MediaType.TRACK, title="MA Track"
    )
    protocol._attr_current_media = current_media

    protocol._update_player_current_media()

    assert protocol._ma_playback_starting is True
    assert protocol._ma_controls_playback is True
    assert protocol._attr_current_media.title == "MA Track"

    protocol._finish_ma_playback_transition()

    assert protocol._attr_current_media.title == "External Track"
    assert protocol._ma_controls_playback is False


async def test_new_ma_playback_cancels_previous_transition() -> None:
    """Keep a new MA playback transition protected from the previous timer."""
    protocol = _make_player(_url_stream_now_playing())
    mass = cast("MagicMock", protocol.mass)
    device = cast("MagicMock", protocol._device)
    transition_pending = True

    def cancel_timer(task_id: str) -> None:
        nonlocal transition_pending
        assert task_id == f"heos_playback_transition_{protocol.protocol_id}"
        transition_pending = False

    async def play_url(_: str) -> None:
        if transition_pending:
            protocol._finish_ma_playback_transition()

    mass.cancel_timer.side_effect = cancel_timer
    mass.streams.resolve_stream_url = AsyncMock(return_value="http://ma/stream/new")
    device.play_url = AsyncMock(side_effect=play_url)
    protocol._ma_playback_starting = True

    await protocol.play_media(
        PlayerMedia(
            uri="library://track/2",
            media_type=MediaType.TRACK,
            title="New MA Track",
        )
    )

    assert protocol._ma_playback_starting is True
    mass.cancel_timer.assert_called_once_with(f"heos_playback_transition_{protocol.protocol_id}")


async def test_select_source_ends_ma_playback_transition() -> None:
    """Allow an explicit source change to update HEOS media immediately."""
    protocol = _make_player(_url_stream_now_playing())
    mass = cast("MagicMock", protocol.mass)
    device = cast("MagicMock", protocol._device)
    protocol._ma_playback_starting = True

    async def play_input_source(_: str) -> None:
        assert protocol._ma_playback_starting is False

    device.play_input_source = AsyncMock(side_effect=play_input_source)

    await protocol.select_source("aux_in_1")

    mass.cancel_timer.assert_called_once_with(f"heos_playback_transition_{protocol.protocol_id}")


async def test_detach_cancels_playback_transition() -> None:
    """Cancel the playback transition when the protocol is detached."""
    protocol = _make_player(_url_stream_now_playing())
    mass = cast("MagicMock", protocol.mass)
    protocol._ma_playback_starting = True

    await protocol.on_attach(MagicMock())
    await protocol.on_detach()

    mass.cancel_timer.assert_any_call(f"heos_playback_transition_{protocol.protocol_id}")
    assert protocol._ma_playback_starting is False


async def _start_ma_playback(protocol: HeosPlayer) -> dict[str, Callable[[], None]]:
    """Start MA playback on a playing player and return its pending timers by task id."""
    mass = cast("MagicMock", protocol.mass)
    device = cast("MagicMock", protocol._device)
    timers: dict[str, Callable[[], None]] = {}

    def call_later(
        _delay: float, target: Callable[..., None], *args: Any, task_id: str, **kwargs: Any
    ) -> None:
        timers[task_id] = partial(target, *args, **kwargs)

    mass.call_later.side_effect = call_later
    mass.cancel_timer.side_effect = lambda task_id: timers.pop(task_id, None)
    mass.streams.resolve_stream_url = AsyncMock(return_value="http://ma/stream/next")
    device.play_url = AsyncMock()
    protocol.get_config_value = MagicMock(return_value=5)  # type: ignore[method-assign]
    protocol.set_dynamic_attributes()
    assert protocol.playback_state == PlaybackState.PLAYING

    await protocol.play_media(
        PlayerMedia(uri="library://track/2", media_type=MediaType.TRACK, title="Next")
    )
    return timers


async def _set_device_state(protocol: HeosPlayer, state: HeosPlayState) -> None:
    """Feed a HEOS state change event into the protocol."""
    cast("MagicMock", protocol._device).state = state
    await protocol._player_event_received(heos_const.EVENT_PLAYER_STATE_CHANGED)


async def test_transient_stop_while_restarting_ma_playback_keeps_playing() -> None:
    """
    Keep reporting playing while HEOS restarts on a new MA stream.

    HEOS reports stop for several seconds when switching to the next track's stream.
    Publishing that as idle drops the group's queue and can dissolve a sync group.

    See https://github.com/music-assistant/support/issues/6373
    """
    protocol = _make_player(_url_stream_now_playing())
    timers = await _start_ma_playback(protocol)
    transition_timer = f"heos_playback_transition_{protocol.protocol_id}"

    await _set_device_state(protocol, HeosPlayState.STOP)
    assert protocol.playback_state == PlaybackState.PLAYING

    # transition timeout passes while the device still restarts
    timers.pop(transition_timer)()
    assert protocol.playback_state == PlaybackState.PLAYING
    assert transition_timer in timers

    await _set_device_state(protocol, HeosPlayState.PLAY)
    timers.pop(transition_timer)()
    assert protocol.playback_state == PlaybackState.PLAYING
    assert protocol._ma_playback_starting is False


async def test_queue_cleanup_waits_for_device_to_play_during_restart() -> None:
    """Schedule the HEOS queue cleanup only once the device plays the new stream."""
    protocol = _make_player(_url_stream_now_playing())
    timers = await _start_ma_playback(protocol)
    cleanup_timer = f"heos_queue_cleanup_timer_{protocol.protocol_id}"

    await _set_device_state(protocol, HeosPlayState.STOP)
    await protocol._player_event_received(heos_const.EVENT_PLAYER_QUEUE_CHANGED)
    assert cleanup_timer not in timers

    await _set_device_state(protocol, HeosPlayState.PLAY)
    assert cleanup_timer in timers


async def test_ma_playback_that_never_starts_becomes_idle() -> None:
    """Report idle once HEOS keeps reporting stop past the playback transition."""
    protocol = _make_player(_url_stream_now_playing())
    timers = await _start_ma_playback(protocol)
    transition_timer = f"heos_playback_transition_{protocol.protocol_id}"

    await _set_device_state(protocol, HeosPlayState.STOP)
    timers.pop(transition_timer)()
    timers.pop(transition_timer)()

    assert protocol.playback_state == PlaybackState.IDLE
    assert protocol._ma_playback_starting is False


async def test_stop_during_ma_playback_transition_reports_idle() -> None:
    """A stop requested during the playback transition is reported right away."""
    protocol = _make_player(_url_stream_now_playing())
    await _start_ma_playback(protocol)
    cast("MagicMock", protocol._device).stop = AsyncMock()

    await protocol.stop()
    await _set_device_state(protocol, HeosPlayState.STOP)

    assert protocol.playback_state == PlaybackState.IDLE


def test_media_position_and_duration_reported_in_seconds() -> None:
    """HEOS reports milliseconds; the media must carry seconds."""
    protocol = _make_player(_external_now_playing(113000))
    protocol._attr_active_source = "9999"

    protocol._update_player_current_media()

    assert protocol._attr_current_media is not None
    assert protocol._attr_current_media.elapsed_time == 113
    assert protocol._attr_current_media.duration == 245


def test_media_and_player_position_agree() -> None:
    """The media-level and player-level positions must use the same unit."""
    protocol = _make_player(_external_now_playing(113000))
    protocol._attr_active_source = "9999"

    protocol.set_dynamic_attributes(update_media=True)

    assert protocol._attr_current_media is not None
    assert protocol._attr_current_media.elapsed_time == protocol._attr_elapsed_time == 113


def test_position_zero_is_reported_not_dropped() -> None:
    """Position 0 is a real position at the start of a track, not a missing one."""
    protocol = _make_player(_external_now_playing(0))
    protocol._attr_active_source = "9999"

    protocol.set_dynamic_attributes(update_media=True)

    assert protocol._attr_current_media is not None
    assert protocol._attr_current_media.elapsed_time == 0
    assert protocol._attr_elapsed_time == 0


def test_missing_progress_stays_none() -> None:
    """Without progress info, no position or duration is reported."""
    protocol = _make_player(_external_now_playing(None, duration=None))
    protocol._attr_active_source = "9999"

    protocol.set_dynamic_attributes(update_media=True)

    assert protocol._attr_current_media is not None
    assert protocol._attr_current_media.elapsed_time is None
    assert protocol._attr_current_media.duration is None
    assert protocol._attr_elapsed_time is None
