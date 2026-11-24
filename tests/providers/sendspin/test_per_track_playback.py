"""Tests for Sendspin playing queue items one after another on one push stream."""

from __future__ import annotations

import asyncio
from functools import partial
from itertools import groupby
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiosendspin.server.audio import AudioFormat as SendspinAudioFormat
from aiosendspin.server.push_stream import MAIN_CHANNEL
from music_assistant_models.dsp import DSPConfig
from music_assistant_models.errors import AudioError, MediaNotFoundError
from music_assistant_models.media_items.audio_format import AudioFormat

from music_assistant.models.player import PlayerMedia
from music_assistant.providers.sendspin.playback import (
    SendspinPlaybackSession,
    _HistoryChunk,
    _PipelineConfig,
)
from music_assistant.providers.sendspin.player import SendspinPlayer
from tests.providers.sendspin.test_dsp_channel_alignment import _FifoFFMpeg

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from uuid import UUID

_SAMPLE_RATE = 48_000
_CHUNK_DURATION_US = 100_000
_PCM_FORMAT = AudioFormat(sample_rate=_SAMPLE_RATE, bit_depth=32, channels=2)
_SENDSPIN_PCM_FORMAT = SendspinAudioFormat(
    sample_rate=_SAMPLE_RATE, bit_depth=32, channels=2, sample_type="float"
)
_CHUNK_BYTES = b"\x00" * (round(_CHUNK_DURATION_US / 1_000_000 * _SAMPLE_RATE) * 4 * 2)
_SEND_AHEAD_US = 500_000
# the sessions here play for well under a second
_LAST_AUDIO_LEAD_US = _CHUNK_DURATION_US
_T0 = 10_000_000
_real_sleep = asyncio.sleep


class _TailHoldingFFMpeg(_FifoFFMpeg):
    """Identity DSP that holds back its last input until stdin closes, like a real ffmpeg."""

    def __init__(self) -> None:
        super().__init__(None, yielding_writes=False)
        self._tail = b""

    async def write(self, data: bytes) -> None:
        tail, self._tail = self._tail, data
        if tail:
            await super().write(tail)

    async def write_eof(self) -> None:
        if self._tail:
            await super().write(self._tail)
            self._tail = b""
        await super().write_eof()


class _FakePushStream:
    """Stands in for aiosendspin's PushStream, committing chunks back to back from _T0."""

    def __init__(self) -> None:
        self._commits = 0
        self.now = _T0 - _SEND_AHEAD_US
        self.prepared: list[tuple[UUID, int]] = []
        self.pcm_cached_channels: set[UUID] = set()
        self.is_stopped = False
        # event loop turns each commit waits for, which lets the producer run ahead
        self.commit_lag = 0

    def set_live_source(self, _live: bool) -> None:
        pass

    def enable_pcm_cache_for_channel(self, channel_id: UUID) -> None:
        self.pcm_cached_channels.add(channel_id)

    def prepare_audio(self, _pcm: bytes, audio_format: SendspinAudioFormat, **kwargs: Any) -> None:
        self.prepared.append((kwargs["channel_id"], audio_format.sample_rate))

    async def commit_audio(self) -> int:
        start_us = _T0 + self._commits * _CHUNK_DURATION_US
        self._commits += 1
        self.now = start_us - _SEND_AHEAD_US
        return start_us

    async def sleep_to_limit_buffer(self, _limit_us: int) -> None:
        for _ in range(self.commit_lag):
            await _real_sleep(0)

    def now_us(self) -> int:
        return self.now

    def stop(self, *, keep_stream: bool = False) -> None:
        self.is_stopped = True

    def clear(self) -> None:
        pass


async def _chunks(count: int) -> AsyncIterator[bytes]:
    for _ in range(count):
        yield _CHUNK_BYTES


def _media(queue_item_id: str) -> PlayerMedia:
    return PlayerMedia(
        uri=f"library://track/{queue_item_id}", source_id="queue", queue_item_id=queue_item_id
    )


def _next_item_in(order: list[str]) -> Callable[[str, str], SimpleNamespace | None]:
    """Build a stand-in for the queue's next item lookup that follows the given item order."""

    def _next_item(_queue_id: str, item_id: str) -> SimpleNamespace | None:
        index = order.index(item_id) + 1
        return SimpleNamespace(queue_item_id=order[index]) if index < len(order) else None

    return _next_item


def _player() -> MagicMock:
    """Build a player whose current media is the attribute the session writes."""
    player = MagicMock()
    type(player).current_media = property(lambda self: self._attr_current_media)
    player.player_id = "leader"
    player.requires_flow_mode = False
    player.mass.config.get_player_dsp_config.return_value = DSPConfig(enabled=False)
    player.mass.config.get_raw_player_config_value.return_value = "stereo"
    player.mass.player_queues.queue_data_or_none.return_value = SimpleNamespace(
        session_id=None, queue=SimpleNamespace(flow_mode=False)
    )
    player.api.group.stop = AsyncMock()
    player.provider.is_silent_client.return_value = False
    player.provider.speaker_output.side_effect = lambda client_id: (client_id, None, False)
    return player


def _session(
    monkeypatch: pytest.MonkeyPatch, player: MagicMock
) -> tuple[SendspinPlaybackSession, _FakePushStream]:
    """Build a session on a fake push stream whose clock moves on while playback waits."""
    session = SendspinPlaybackSession(player)
    push_stream = _FakePushStream()
    monkeypatch.setattr(
        session, "_select_session_pcm_formats", lambda: (_PCM_FORMAT, _SENDSPIN_PCM_FORMAT)
    )
    monkeypatch.setattr(session, "_create_push_stream", lambda: push_stream)
    monkeypatch.setattr(session, "_get_start_mixes", lambda _media: (False, False))
    monkeypatch.setattr(
        "music_assistant.providers.sendspin.playback.import_module_in_thread", AsyncMock()
    )
    monkeypatch.setattr(
        "music_assistant.providers.sendspin.playback._LAST_AUDIO_LEAD_US", _LAST_AUDIO_LEAD_US
    )
    # the fake push stream counts each commit as one chunk, also the first
    monkeypatch.setattr("music_assistant.providers.sendspin.playback._START_QUEUE_US", 0)
    real_sleep = asyncio.sleep

    async def _sleep(delay: float) -> None:
        push_stream.now += round(delay * 1_000_000)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", _sleep)
    return session, push_stream


async def test_the_player_switches_to_the_next_item_when_its_audio_plays(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The next item becomes current once its audio plays, not when it is sent ahead."""
    first, second = _media("item-1"), _media("item-2")
    player = _player()
    player._attr_current_media = first
    player.mass.player_queues.get_next_item.side_effect = _next_item_in(["item-1", "item-2"])
    sources = {"item-1": 3, "item-2": 2}
    player.mass.streams.get_stream.side_effect = lambda media, *_args, **_kwargs: _chunks(
        sources[media.queue_item_id]
    )
    seen_while_sending: list[tuple[str | None, float]] = []
    player.update_state.side_effect = lambda **_kwargs: seen_while_sending.append(
        (player._attr_current_media.queue_item_id, player._attr_elapsed_time)
    )

    session, push_stream = _session(monkeypatch, player)

    async def _play_until_the_second_item_starts(_self: SendspinPlaybackSession) -> None:
        # the item start timer fires once the second item plays
        call_later = cast("MagicMock", player.mass.call_later)
        apply_item_start = next(
            call.args[1]
            for call in reversed(call_later.call_args_list)
            if call.kwargs["task_id"].startswith("sendspin_item_start_")
        )
        second_start_us = _self.item_start_us("item-2", _T0 + 10_000_000)
        assert second_start_us is not None
        push_stream.now = second_start_us + 50_000
        await apply_item_start()

    monkeypatch.setattr(
        SendspinPlaybackSession, "_wait_for_buffer_drain", _play_until_the_second_item_starts
    )
    assert session.set_next_media(second)

    await session._run_playback(first)

    played = [call.args[0].queue_item_id for call in player.mass.streams.get_stream.call_args_list]
    assert played == ["item-1", "item-2"]
    # all five chunks were sent before the second item started playing 0.3 s into the session
    # audio, and it took over once it played, 0.05 s in
    assert seen_while_sending == [("item-1", 0.0), ("item-2", 0.05)]
    assert player._attr_current_media is second


async def test_an_item_added_while_the_last_one_plays_out_still_plays(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An item the queue hands over after the last item's audio was all sent plays next."""
    first, added = _media("item-1"), _media("item-2")
    player = _player()
    player._attr_current_media = first
    queue_order = ["item-1"]
    player.mass.player_queues.get_next_item.side_effect = _next_item_in(queue_order)
    player.mass.streams.get_stream.side_effect = lambda *_args, **_kwargs: _chunks(2)
    session, _ = _session(monkeypatch, player)
    monkeypatch.setattr(SendspinPlaybackSession, "_wait_for_buffer_drain", AsyncMock())
    clock_sleep = asyncio.sleep

    async def _add_while_playing_out(delay: float) -> None:
        if session._next_media is None and len(session._item_starts) == 1:
            queue_order.append("item-2")
            session.set_next_media(added)
        await clock_sleep(delay)

    monkeypatch.setattr(asyncio, "sleep", _add_while_playing_out)

    await session._run_playback(first)

    played = [call.args[0].queue_item_id for call in player.mass.streams.get_stream.call_args_list]
    assert played == ["item-1", "item-2"]


async def test_the_last_audio_is_sent_while_the_players_can_still_play_it_in_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session nothing follows ends its audio ahead of its end, and takes no item after that."""
    last = _media("item-1")
    player = _player()
    player._attr_current_media = last
    player.mass.player_queues.get_next_item.return_value = None
    player.mass.streams.get_stream.side_effect = lambda *_args, **_kwargs: _chunks(2)
    session, push_stream = _session(monkeypatch, player)
    ended: list[tuple[int, bool]] = []

    async def _record_the_end(_self: SendspinPlaybackSession) -> None:
        ended.append((push_stream.now, _self.set_next_media(_media("item-2"))))

    monkeypatch.setattr(SendspinPlaybackSession, "_wait_for_buffer_drain", _record_the_end)

    await session._run_playback(last)

    audio_end_us = _T0 + 2 * _CHUNK_DURATION_US
    assert ended == [(audio_end_us - _LAST_AUDIO_LEAD_US, False)]


async def test_a_player_that_requires_flow_mode_plays_no_next_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session of such a player ends with its stream, also when a next item was handed over."""
    first = _media("item-1")
    player = _player()
    player.requires_flow_mode = True
    player._attr_current_media = first
    player.mass.player_queues.get_next_item.side_effect = _next_item_in(["item-1", "item-2"])
    player.mass.streams.get_stream.side_effect = lambda *_args, **_kwargs: _chunks(2)
    session, push_stream = _session(monkeypatch, player)
    ended_at: list[int] = []

    async def _record_the_end(_self: SendspinPlaybackSession) -> None:
        ended_at.append(push_stream.now)

    monkeypatch.setattr(SendspinPlaybackSession, "_wait_for_buffer_drain", _record_the_end)
    assert session.set_next_media(_media("item-2"))

    await session._run_playback(first)

    played = [call.args[0].queue_item_id for call in player.mass.streams.get_stream.call_args_list]
    assert played == ["item-1"]
    # it did not wait for its audio to play out either
    assert ended_at == [_T0 + _CHUNK_DURATION_US - _SEND_AHEAD_US]


async def test_a_queue_that_started_something_else_plays_no_next_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stream the queue ended for a new play request is not followed by the item sent along."""
    first = _media("item-1")
    player = _player()
    player._attr_current_media = first
    player.mass.player_queues.get_next_item.side_effect = _next_item_in(["item-1", "item-2"])
    queue_data = player.mass.player_queues.queue_data_or_none.return_value

    async def _stream_ended_by_a_new_session(
        *_args: object, **_kwargs: object
    ) -> AsyncIterator[bytes]:
        async for chunk in _chunks(2):
            yield chunk
        queue_data.session_id = "next-session"

    player.mass.streams.get_stream.side_effect = _stream_ended_by_a_new_session
    session, _ = _session(monkeypatch, player)
    monkeypatch.setattr(SendspinPlaybackSession, "_wait_for_buffer_drain", AsyncMock())
    assert session.set_next_media(_media("item-2"))

    await asyncio.wait_for(session._run_playback(first), timeout=5)

    played = [call.args[0].queue_item_id for call in player.mass.streams.get_stream.call_args_list]
    assert played == ["item-1"]


async def test_an_item_that_no_longer_follows_the_playing_one_is_not_played(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An item handed over before the queue stopped playing it next is dropped."""
    first, handed_over = _media("item-1"), _media("item-2")
    player = _player()
    player._attr_current_media = first
    player.mass.player_queues.get_next_item.side_effect = _next_item_in(["item-1"])
    player.mass.streams.get_stream.side_effect = lambda *_args, **_kwargs: _chunks(2)
    session, _ = _session(monkeypatch, player)
    monkeypatch.setattr(SendspinPlaybackSession, "_wait_for_buffer_drain", AsyncMock())
    assert session.set_next_media(handed_over)

    await session._run_playback(first)

    played = [call.args[0].queue_item_id for call in player.mass.streams.get_stream.call_args_list]
    assert played == ["item-1"]


async def test_the_item_that_follows_after_a_queue_change_plays_after_a_stale_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An item handed over after a stale one, once the queue changed, still plays."""
    first, stale, following = _media("item-1"), _media("item-2"), _media("item-3")
    player = _player()
    player._attr_current_media = first
    player.mass.player_queues.get_next_item.side_effect = _next_item_in(["item-1", "item-3"])
    player.mass.streams.get_stream.side_effect = lambda *_args, **_kwargs: _chunks(2)
    session, _ = _session(monkeypatch, player)
    monkeypatch.setattr(SendspinPlaybackSession, "_wait_for_buffer_drain", AsyncMock())
    clock_sleep = asyncio.sleep

    async def _hand_over_while_playing_out(delay: float) -> None:
        await clock_sleep(delay)
        if len(session._item_starts) == 1:
            session.set_next_media(following)

    monkeypatch.setattr(asyncio, "sleep", _hand_over_while_playing_out)
    assert session.set_next_media(stale)

    await session._run_playback(first)

    played = [call.args[0].queue_item_id for call in player.mass.streams.get_stream.call_args_list]
    assert played == ["item-1", "item-3"]


async def test_the_item_after_a_short_one_follows_after_a_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A next item the queue hands over only after the audio played out still plays."""
    short, following = _media("item-1"), _media("item-2")
    player = _player()
    player._attr_current_media = short
    player.mass.player_queues.get_next_item.side_effect = lambda _queue_id, item_id: (
        SimpleNamespace(queue_item_id="item-2") if item_id == "item-1" else None
    )
    player.mass.streams.get_stream.side_effect = lambda *_args, **_kwargs: _chunks(2)
    session, push_stream = _session(monkeypatch, player)
    monkeypatch.setattr(SendspinPlaybackSession, "_wait_for_buffer_drain", AsyncMock())
    clock_sleep = asyncio.sleep
    audio_end_us = _T0 + 2 * _CHUNK_DURATION_US

    async def _hand_over_after_play_out(delay: float) -> None:
        await clock_sleep(delay)
        if push_stream.now > audio_end_us and len(session._item_starts) == 1:
            session.set_next_media(following)

    monkeypatch.setattr(asyncio, "sleep", _hand_over_after_play_out)

    await session._run_playback(short)

    played = [call.args[0].queue_item_id for call in player.mass.streams.get_stream.call_args_list]
    assert played == ["item-1", "item-2"]


async def test_an_item_does_not_play_before_its_source_sends_audio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An item whose source is late does not play yet, also once the audio before it ran out."""
    first, late = _media("item-1"), _media("item-2")
    player = _player()
    player._attr_current_media = first
    player.mass.player_queues.get_next_item.side_effect = _next_item_in(["item-1", "item-2"])
    source_ready = asyncio.Event()

    async def _source(
        media: PlayerMedia, *_args: object, **_kwargs: object
    ) -> AsyncIterator[bytes]:
        if media is late:
            await source_ready.wait()
        async for chunk in _chunks(2):
            yield chunk

    player.mass.streams.get_stream.side_effect = _source
    session, push_stream = _session(monkeypatch, player)
    after_first_us = _T0 + 3 * _CHUNK_DURATION_US
    playing_once_sent: list[tuple[int, PlayerMedia | None]] = []

    async def _record_what_plays(_self: SendspinPlaybackSession) -> None:
        playing_once_sent.append(_self._playing_item(_T0, after_first_us))

    monkeypatch.setattr(SendspinPlaybackSession, "_wait_for_buffer_drain", _record_what_plays)
    assert session.set_next_media(late)
    playback = asyncio.create_task(session._run_playback(first))
    while push_stream._commits < 2 or len(session._item_starts) < 2:
        await asyncio.sleep(0)

    assert session._playing_item(_T0, after_first_us) == (0, first)
    assert session.upcoming_item() is None
    source_ready.set()
    await asyncio.wait_for(playback, timeout=5)

    assert playing_once_sent == [(2 * _CHUNK_DURATION_US, late)]


async def test_an_item_handed_over_without_stream_details_still_plays(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An item the queue hands over before its stream details are loaded gets them first."""
    first, second = _media("item-1"), _media("item-2")
    player = _player()
    player._attr_current_media = first
    player.mass.player_queues.get_next_item.side_effect = _next_item_in(["item-1", "item-2"])
    queue_items = {
        "item-1": SimpleNamespace(streamdetails=MagicMock()),
        "item-2": SimpleNamespace(streamdetails=None),
    }
    player.mass.player_queues.get_item.side_effect = lambda _queue_id, item_id: queue_items[item_id]
    resolved = MagicMock()

    async def _get_stream_details(queue_item: SimpleNamespace) -> object:
        queue_item.streamdetails = resolved
        return resolved

    player.mass.streams.audio.get_stream_details = _get_stream_details
    streamed_with: list[object] = []

    def _get_stream(media: PlayerMedia, *_args: object, **_kwargs: object) -> AsyncIterator[bytes]:
        streamed_with.append(queue_items[cast("str", media.queue_item_id)].streamdetails)
        return _chunks(2)

    player.mass.streams.get_stream.side_effect = _get_stream
    session, _ = _session(monkeypatch, player)
    monkeypatch.setattr(SendspinPlaybackSession, "_wait_for_buffer_drain", AsyncMock())
    assert session.set_next_media(second)

    await session._run_playback(first)

    assert streamed_with == [queue_items["item-1"].streamdetails, resolved]


async def test_an_item_whose_stream_details_fail_counts_as_a_failed_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The queue stops after enough items in a row that never got stream details."""
    first, failing = _media("item-1"), _media("item-2")
    player = _player()
    player._attr_current_media = first
    player.mass.player_queues.get_next_item.side_effect = _next_item_in(["item-1", "item-2"])
    queue_items = {
        "item-1": SimpleNamespace(streamdetails=MagicMock()),
        "item-2": SimpleNamespace(streamdetails=None),
    }
    player.mass.player_queues.get_item.side_effect = lambda _queue_id, item_id: queue_items[item_id]
    player.mass.streams.audio.get_stream_details = AsyncMock(side_effect=MediaNotFoundError("gone"))
    player.mass.streams.get_stream.side_effect = lambda *_args, **_kwargs: _chunks(2)
    session, _ = _session(monkeypatch, player)
    monkeypatch.setattr(SendspinPlaybackSession, "_wait_for_buffer_drain", AsyncMock())
    assert session.set_next_media(failing)

    await session._run_playback(first)

    player.mass.player_queues.report_source_failure.assert_called_once_with(
        queue_items["item-2"], failing.queue_session_id
    )


async def test_an_item_that_fails_after_another_one_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing item keeps the audio sent before it and the queue moves on past it."""
    first, failing, following = _media("item-1"), _media("item-2"), _media("item-3")
    player = _player()
    player._attr_current_media = first
    player.mass.player_queues.get_next_item.side_effect = _next_item_in(
        ["item-1", "item-2", "item-3"]
    )

    async def _broken_source() -> AsyncIterator[bytes]:
        async for chunk in _chunks(0):
            yield chunk
        raise AudioError("stream gone")

    player.mass.streams.get_stream.side_effect = lambda media, *_args, **_kwargs: (
        _broken_source() if media is failing else _chunks(2)
    )
    shown: list[str | None] = []
    player.update_state.side_effect = lambda **_kwargs: shown.append(
        player._attr_current_media.queue_item_id
    )
    session, push_stream = _session(monkeypatch, player)
    monkeypatch.setattr(SendspinPlaybackSession, "_wait_for_buffer_drain", AsyncMock())
    clock_sleep = asyncio.sleep
    call_later = cast("MagicMock", player.mass.call_later)
    loaded = cast("MagicMock", player.mass.player_queues.track_loaded_in_buffer)

    async def _hand_over_the_item_after_the_failed_one(delay: float) -> None:
        await clock_sleep(delay)
        if len(session._item_starts) != 2 or player._attr_current_media is failing:
            return
        # the item start timer makes the failed item current
        await next(
            call.args[1]
            for call in reversed(call_later.call_args_list)
            if call.kwargs["task_id"].startswith("sendspin_item_start_")
        )()
        # the queue hands over the next item once the failed one loaded and plays
        if player._attr_current_media is failing and loaded.call_args == (("queue", "item-2"),):
            # the audio ran out meanwhile, so the next item starts a second later than planned
            push_stream._commits += 1_000_000 // _CHUNK_DURATION_US
            assert session.set_next_media(following)

    monkeypatch.setattr(asyncio, "sleep", _hand_over_the_item_after_the_failed_one)
    assert session.set_next_media(failing)

    await session._run_playback(first)

    played = [call.args[0].queue_item_id for call in player.mass.streams.get_stream.call_args_list]
    assert played == ["item-1", "item-2", "item-3"]
    # the next item's audio is all sent before it plays, and the player never goes back to an
    # item it moved past meanwhile
    assert [item for item, _ in groupby(shown)] == ["item-1", "item-2"]


@pytest.mark.parametrize(
    ("elapsed", "position"), [(42.7, 42), (216.4, 215)], ids=["mid-track", "past the duration"]
)
async def test_a_next_item_that_replaces_one_sent_ahead_restarts_at_the_position(
    elapsed: float, position: int
) -> None:
    """The player restarts the queue where it plays when the item sent ahead is replaced."""
    player = MagicMock()
    player.playback_session.set_next_media.return_value = False
    player.mass.player_queues.get.return_value = SimpleNamespace(
        queue_id="queue",
        corrected_elapsed_time=elapsed,
        current_item=SimpleNamespace(duration=215),
    )

    await SendspinPlayer.enqueue_next_media(player, _media("item-3"))

    player.mass.player_queues.seek.assert_called_once_with("queue", position)


def test_beats_of_a_track_resumed_mid_way_start_before_its_audio() -> None:
    """A track resumed at a position has its file start that far before its first sample."""
    player = MagicMock()
    player.requires_flow_mode = False
    player.playback_session.item_start_us.return_value = _T0
    item = SimpleNamespace(queue_item_id="item-1", streamdetails=SimpleNamespace(seek_position=5.0))

    player._timeline_track_start_us = partial(SendspinPlayer._timeline_track_start_us, player)

    anchor_us = SendspinPlayer._timeline_track_anchor_us(
        player, cast("Any", SimpleNamespace(flow_mode=False)), cast("Any", item), _T0
    )

    assert anchor_us == _T0 - 5_000_000


async def test_the_next_leader_follows_the_items_of_the_session_it_takes_over() -> None:
    """A dynamic leader switch hands the playing item over with the session."""
    playing = _media("item-1")
    leader, next_leader = _player(), _player()
    leader._attr_current_media = playing
    next_leader._attr_current_media = None
    session = SendspinPlaybackSession(leader)

    await session.transfer_to(next_leader)

    assert next_leader._attr_current_media is playing


def test_a_next_item_replaces_one_sent_ahead_only_by_restarting() -> None:
    """Once another item was sent ahead, only that same item can follow without a restart."""
    playing, sent_ahead = _media("item-1"), _media("item-2")
    player = _player()
    player._attr_current_media = playing
    session = SendspinPlaybackSession(player)
    session._item_starts = [(0, playing), (3_000_000, sent_ahead)]

    assert session.set_next_media(_media("item-2")) is True
    assert session.set_next_media(_media("item-3")) is False
    cast("Any", session)._item_starts = [(0, playing)]
    assert session.set_next_media(_media("item-3")) is True


def test_the_upcoming_item_is_the_one_after_the_item_playing_now() -> None:
    """Each item is upcoming until it plays, from before the first one starts."""
    first, second, third = _media("item-1"), _media("item-2"), _media("item-3")
    session = SendspinPlaybackSession(_player())
    push_stream = _FakePushStream()
    session_state = cast("Any", session)
    session_state._push_stream = push_stream
    session_state._timeline_start_us = _T0
    session._item_starts = [(0, first), (3_000_000, second), (5_000_000, third)]

    assert session.upcoming_item() == (first, second, _T0 + 3_000_000)
    push_stream.now = _T0 + 3_000_000
    assert session.upcoming_item() == (second, third, _T0 + 5_000_000)
    push_stream.now = _T0 + 5_000_000
    assert session.upcoming_item() is None


def test_a_repeated_item_follows_its_own_pass_sent_ahead() -> None:
    """With repeat one, each pass sent ahead is followed by another until the repeat ends."""
    playing, sent_ahead = _media("item-1"), _media("item-1")
    player = _player()
    player._attr_current_media = playing
    player.mass.player_queues.get_next_item.return_value = SimpleNamespace(queue_item_id="item-1")
    session = SendspinPlaybackSession(player)
    session._item_starts = [(0, playing), (3_000_000, sent_ahead)]
    again = _media("item-1")

    assert session.set_next_media(again) is True
    assert session._next_media is again
    # once the repeat ends, the pass sent ahead has to make way for the next item
    player.mass.player_queues.get_next_item.return_value = SimpleNamespace(queue_item_id="item-2")
    assert session.set_next_media(_media("item-2")) is False


def _formats(sample_rate: int) -> tuple[AudioFormat, SendspinAudioFormat]:
    return (
        AudioFormat(sample_rate=sample_rate, bit_depth=32, channels=2),
        SendspinAudioFormat(sample_rate=sample_rate, bit_depth=32, channels=2, sample_type="float"),
    )


def _two_rates_session(
    monkeypatch: pytest.MonkeyPatch,
    player: MagicMock,
    *,
    leader_rate: int | None = None,
) -> tuple[SendspinPlaybackSession, _FakePushStream, list[tuple[int, int, bool]]]:
    """
    Build a session that plays a 48 kHz item-1, then a 44.1 kHz item-2.

    :param leader_rate: A rate the leader is pinned at, instead of following each item.
    :return: The session, its push stream, and each format follow as
        (commits so far, rate, whether it follows a later item).
    """
    player.mass.player_queues.get_next_item.side_effect = _next_item_in(["item-1", "item-2"])
    player.mass.streams.get_stream.side_effect = lambda media, *_args, **_kwargs: _chunks(
        3 if media.queue_item_id == "item-1" else 2
    )
    session, push_stream = _session(monkeypatch, player)
    push_stream.commit_lag = 3
    monkeypatch.setattr(SendspinPlaybackSession, "_wait_for_buffer_drain", AsyncMock())
    rates = {"item-1": 48_000, "item-2": 44_100}
    monkeypatch.setattr(
        session,
        "_get_item_streamdetails",
        lambda media: SimpleNamespace(sample_rate=rates[media.queue_item_id], seek_position=0),
    )
    monkeypatch.setattr(
        session,
        "_select_session_pcm_formats",
        lambda: _formats(leader_rate or rates["item-1"]),
    )
    monkeypatch.setattr(
        session,
        "_later_item_sample_rate",
        AsyncMock(side_effect=lambda streamdetails: leader_rate or streamdetails.sample_rate),
    )
    followed: list[tuple[int, int, bool]] = []

    async def _follow(_player_ids: object, *, later_item: bool = False) -> None:
        item = cast("Any", session._item_streamdetails)
        followed.append((push_stream._commits, item.sample_rate, later_item))

    monkeypatch.setattr(session, "_follow_session_sample_rate", _follow)
    return session, push_stream, followed


async def test_a_later_item_is_sent_in_its_own_format_from_its_first_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An item at another rate is sent in its own format from its first chunk on."""
    first, second = _media("item-1"), _media("item-2")
    player = _player()
    player._attr_current_media = first
    session, push_stream, _ = _two_rates_session(monkeypatch, player)
    assert session.set_next_media(second)

    await session._run_playback(first)

    requested = [call.args[1].sample_rate for call in player.mass.streams.get_stream.call_args_list]
    assert requested == [48_000, 44_100]
    sent_rates = [rate for _, rate in push_stream.prepared]
    assert sent_rates[:3] == [48_000] * 3
    assert set(sent_rates[3:]) == {44_100}


async def test_a_later_item_switches_format_after_a_start_that_joined_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later item switches format once a first commit that joined the start queue is sent."""
    first, second = _media("item-1"), _media("item-2")
    player = _player()
    player._attr_current_media = first
    session, push_stream, followed = _two_rates_session(monkeypatch, player)
    monkeypatch.setattr(
        "music_assistant.providers.sendspin.playback._START_QUEUE_US", 10 * _CHUNK_DURATION_US
    )
    assert session.set_next_media(second)

    await asyncio.wait_for(session._run_playback(first), timeout=5)

    rates = [rate for _, rate in push_stream.prepared]
    # item-1's 3 chunks go out in the first commit, item-2 follows in commits of its own
    assert rates[0] == 48_000
    assert set(rates[1:]) == {44_100}
    assert followed == [(0, 48_000, False), (1, 44_100, True)]


async def test_players_follow_a_later_item_only_once_its_first_chunk_is_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Members follow the next item's rate at its first chunk, also while the leader's is pinned."""
    first, second = _media("item-1"), _media("item-2")
    player = _player()
    player._attr_current_media = first
    session, _, followed = _two_rates_session(monkeypatch, player, leader_rate=48_000)
    assert session.set_next_media(second)

    await session._run_playback(first)

    # item-2 opens while item-1 is still being sent, and its rate applies after item-1's 3 chunks
    assert followed == [(0, 48_000, False), (3, 44_100, True)]


async def test_each_item_reports_its_own_output_once_the_players_follow_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later item's output is reported under that item, leaving the playing item's as it is."""
    first, second = _media("item-1"), _media("item-2")
    player = _player()
    player._attr_current_media = first
    player.api.group.clients = [SimpleNamespace(client_id="leader")]
    session, _, _ = _two_rates_session(monkeypatch, player)
    # players take the rate of the item the session sends once they follow it
    monkeypatch.setattr(
        session,
        "_get_member_output_format",
        lambda _player_id: _formats(cast("Any", session._item_streamdetails).sample_rate)[0],
    )
    assert session.set_next_media(second)

    await session._run_playback(first)

    reports = [
        (call.kwargs["queue_item_id"], call.args[1].sample_rate, call.args[2].sample_rate)
        for call in player.mass.streams.audio.get_player_output_plan.call_args_list
    ]
    assert {report for report in reports if report[0] == "item-1"} == {("item-1", 48_000, 48_000)}
    assert [report for report in reports if report[0] == "item-2"][-1] == (
        "item-2",
        44_100,
        44_100,
    )


async def test_a_flow_stream_reports_one_output_for_all_its_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A flow stream's output is reported for the whole session, not under its first item."""
    player = _player()
    player._attr_current_media = _media("item-1")
    player.mass.player_queues.queue_data_or_none.return_value.queue.flow_mode = True
    player.mass.streams.get_stream.side_effect = lambda *_args, **_kwargs: _chunks(2)
    session, _ = _session(monkeypatch, player)
    monkeypatch.setattr(SendspinPlaybackSession, "_wait_for_buffer_drain", AsyncMock())

    await session._run_playback(player.current_media)

    reports = player.mass.streams.audio.get_player_output_plan.call_args_list
    assert reports
    assert {call.kwargs["queue_item_id"] for call in reports} == {None}


async def test_member_dsp_restarts_at_the_rate_of_a_later_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A member's DSP process is flushed and replaced at the next item's rate, losing no chunk."""
    first, second = _media("item-1"), _media("item-2")
    player = _player()
    player._attr_current_media = first
    session, push_stream, _ = _two_rates_session(monkeypatch, player)
    monkeypatch.setattr(
        session,
        "_read_pipeline_config",
        lambda _player_id: _PipelineConfig(requires_transform=True, filter_params=("volume=1.0",)),
    )
    ffmpegs: list[tuple[_TailHoldingFFMpeg, int]] = []
    flushed_when_replaced: list[bool] = []

    def _create_ffmpeg(_params: object) -> _TailHoldingFFMpeg:
        flushed_when_replaced.extend(ffmpeg._eof for ffmpeg, _ in ffmpegs)
        ffmpeg = _TailHoldingFFMpeg()
        ffmpegs.append((ffmpeg, session._pcm_format.sample_rate))
        return ffmpeg

    monkeypatch.setattr(session, "_create_member_ffmpeg", _create_ffmpeg)
    assert session.set_next_media(second)

    await asyncio.wait_for(session._run_playback(first), timeout=5)

    assert [rate for _, rate in ffmpegs] == [48_000, 44_100]
    assert flushed_when_replaced == [True]
    channels: dict[UUID, list[int]] = {}
    for channel_id, rate in push_stream.prepared:
        channels.setdefault(channel_id, []).append(rate)
    main, dsp = channels.values()
    assert dsp == main
    # the cache holds the audio from where each player's new format starts
    assert push_stream.pcm_cached_channels == {next(iter(channels.keys() - {MAIN_CHANNEL}))}


async def test_member_dsp_sends_the_end_of_a_short_item_before_a_late_next_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A member's DSP process does not hold back the end of an item while the next one is late."""
    short, following = _media("item-1"), _media("item-2")
    player = _player()
    player._attr_current_media = short
    player.mass.player_queues.get_next_item.side_effect = _next_item_in(["item-1", "item-2"])
    player.mass.streams.get_stream.side_effect = lambda *_args, **_kwargs: _chunks(2)
    session, push_stream = _session(monkeypatch, player)
    monkeypatch.setattr(SendspinPlaybackSession, "_wait_for_buffer_drain", AsyncMock())
    monkeypatch.setattr(
        session,
        "_read_pipeline_config",
        lambda _player_id: _PipelineConfig(requires_transform=True, filter_params=("volume=1.0",)),
    )
    monkeypatch.setattr(session, "_create_member_ffmpeg", lambda _params: _TailHoldingFFMpeg())
    clock_sleep = asyncio.sleep
    audio_end_us = _T0 + 2 * _CHUNK_DURATION_US
    committed_at_handover: list[int] = []

    async def _hand_over_after_play_out(delay: float) -> None:
        await clock_sleep(delay)
        if push_stream.now > audio_end_us and len(session._item_starts) == 1:
            committed_at_handover.append(push_stream._commits)
            session.set_next_media(following)

    monkeypatch.setattr(asyncio, "sleep", _hand_over_after_play_out)

    await asyncio.wait_for(session._run_playback(short), timeout=5)

    assert committed_at_handover == [2]
    assert push_stream._commits == 4


async def test_member_dsp_sends_the_end_of_the_last_item_before_the_session_waits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A member's DSP process does not hold back the end of the audio while nothing follows it."""
    last = _media("item-1")
    player = _player()
    player._attr_current_media = last
    player.mass.player_queues.get_next_item.return_value = None
    player.mass.streams.get_stream.side_effect = lambda *_args, **_kwargs: _chunks(4)
    session, push_stream = _session(monkeypatch, player)
    monkeypatch.setattr(SendspinPlaybackSession, "_wait_for_buffer_drain", AsyncMock())
    monkeypatch.setattr(
        session,
        "_read_pipeline_config",
        lambda _player_id: _PipelineConfig(requires_transform=True, filter_params=("volume=1.0",)),
    )
    monkeypatch.setattr(session, "_create_member_ffmpeg", lambda _params: _TailHoldingFFMpeg())
    clock_sleep = asyncio.sleep
    committed_while_waiting: list[int] = []

    async def _record_the_commits(delay: float) -> None:
        committed_while_waiting.append(push_stream._commits)
        await clock_sleep(delay)

    monkeypatch.setattr(asyncio, "sleep", _record_the_commits)

    await asyncio.wait_for(session._run_playback(last), timeout=5)

    assert committed_while_waiting[0] == 4


async def test_a_dsp_member_joining_after_a_format_switch_replays_older_audio_converted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audio sent before a format switch reaches a joining member's DSP in the new format."""
    old_format, new_format = _formats(48_000)[0], _formats(44_100)[0]
    session = SendspinPlaybackSession(_player())
    session_state = cast("Any", session)
    session_state._playback_running = True
    session_state._push_stream = _FakePushStream()
    session._pcm_format = new_format
    session._history.extend(
        [
            _HistoryChunk(_T0, _CHUNK_DURATION_US, b"a", 0, old_format),
            # the schedule moved forward by one chunk here
            _HistoryChunk(_T0 + 2 * _CHUNK_DURATION_US, _CHUNK_DURATION_US, b"b", 0, old_format),
            _HistoryChunk(_T0 + 3 * _CHUNK_DURATION_US, _CHUNK_DURATION_US, b"new", 0, new_format),
        ]
    )
    pipeline = SimpleNamespace(config=_PipelineConfig(requires_transform=True, filter_params=()))
    session_state._sync_member_pipeline = AsyncMock(return_value=pipeline)
    session_state._create_member_ffmpeg = lambda _params: _FifoFFMpeg(None, yielding_writes=False)
    resampled: list[tuple[bytes, int, int]] = []

    async def _resample(
        pcm: bytes, input_format: AudioFormat, output_format: AudioFormat
    ) -> AsyncIterator[bytes]:
        resampled.append((pcm, input_format.sample_rate, output_format.sample_rate))
        yield b"converted"

    monkeypatch.setattr("music_assistant.providers.sendspin.playback.resample_pcm_audio", _resample)
    replayed: list[bytes] = []

    async def _enqueue(_state: object, pcm: bytes) -> None:
        replayed.append(pcm)

    session_state._enqueue_join_pcm = _enqueue
    session.pending_join_members.add("member")

    await session._start_join_catchup("member")
    state = session_state._join_catchup["member"]
    await state.snapshot_task
    first_history_start_us = state.first_history_start_us
    await session._clear_join_catchup()

    assert resampled == [(b"a" + bytes(4800 * 8) + b"b", 48_000, 44_100)]
    assert replayed == [b"converted", b"new"]
    assert first_history_start_us == _T0


@pytest.mark.parametrize(
    ("change", "expected_rate"), [("format_switch", 44_100), ("member_left", None)]
)
async def test_a_dsp_catch_up_that_went_stale_while_starting_is_not_kept(
    change: str, expected_rate: int | None
) -> None:
    """A catch-up whose processor started across a format switch restarts, or ends on a leave."""
    old_format, new_format = _formats(48_000)[0], _formats(44_100)[0]
    session = SendspinPlaybackSession(_player())
    session_state = cast("Any", session)
    session_state._playback_running = True
    session_state._push_stream = _FakePushStream()
    session._pcm_format = old_format
    session.pending_join_members.add("member")
    pipeline = SimpleNamespace(config=_PipelineConfig(requires_transform=True, filter_params=()))
    session_state._sync_member_pipeline = AsyncMock(return_value=pipeline)
    session_state._feed_join_history = AsyncMock()
    ffmpegs: list[_FifoFFMpeg] = []

    async def _change_while_starting() -> None:
        if len(ffmpegs) > 1:
            return
        if change == "format_switch":
            session._pcm_format = new_format
        else:
            session.pending_join_members.discard("member")

    def _create_ffmpeg(_params: object) -> _FifoFFMpeg:
        ffmpegs.append(_FifoFFMpeg(_change_while_starting, yielding_writes=False))
        return ffmpegs[-1]

    session_state._create_member_ffmpeg = _create_ffmpeg

    await session._start_join_catchup("member")

    state = session_state._join_catchup.get("member")
    assert (state and state.processor._sample_rate) == expected_rate
    assert ffmpegs[0]._closed
    await session._clear_join_catchup()


@pytest.mark.parametrize("restart_fails", [False, True])
async def test_a_dsp_member_catching_up_at_a_format_switch_starts_over_in_the_new_format(
    monkeypatch: pytest.MonkeyPatch, restart_fails: bool
) -> None:
    """A member's DSP catch-up running when the format switches restarts at the new rate."""
    first, second = _media("item-1"), _media("item-2")
    player = _player()
    player._attr_current_media = first
    session, _, _ = _two_rates_session(monkeypatch, player)
    session_state = cast("Any", session)
    later_item_sample_rate = session._later_item_sample_rate
    done = asyncio.get_running_loop().create_future()
    done.set_result(None)

    async def _later_item_rate_while_a_member_catches_up(streamdetails: Any) -> int:
        session_state._join_catchup["member"] = SimpleNamespace(
            processor=AsyncMock(),
            writer_task=done,
            drainer_task=done,
            snapshot_task=None,
            write_lock=asyncio.Lock(),
            first_history_start_us=None,
            fed_until_us=None,
            history_end_us=None,
            promotion_target_end_us=None,
            promotion_armed_monotonic_s=None,
        )
        return await later_item_sample_rate(streamdetails)

    monkeypatch.setattr(
        session, "_later_item_sample_rate", _later_item_rate_while_a_member_catches_up
    )
    restarted: list[tuple[str, int]] = []

    async def _start_join_catchup(player_id: str) -> None:
        restarted.append((player_id, session._pcm_format.sample_rate))
        if restart_fails:
            raise AudioError("ffmpeg failed to start")

    monkeypatch.setattr(session, "_start_join_catchup", _start_join_catchup)
    assert session.set_next_media(second)

    await session._run_playback(first)

    assert restarted == [("member", 44_100)]
    # a failed restart leaves that member without a catch-up, not the group without the item
    played = [call.args[0].queue_item_id for call in player.mass.streams.get_stream.call_args_list]
    assert played == ["item-1", "item-2"]


async def test_stopping_a_dsp_catch_up_passes_on_a_cancel_of_its_caller() -> None:
    """A cancel that lands while a catch-up stops still stops the task that stopped it."""
    session = SendspinPlaybackSession(_player())
    release = asyncio.Event()

    async def _slow_to_stop() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()
            raise

    writer = asyncio.create_task(_slow_to_stop())
    done = asyncio.get_running_loop().create_future()
    done.set_result(None)
    cast("Any", session)._join_catchup["member"] = SimpleNamespace(
        processor=AsyncMock(), writer_task=writer, drainer_task=done, snapshot_task=None
    )
    await asyncio.sleep(0)
    stopper = asyncio.create_task(session._stop_join_catchup("member"))
    await asyncio.sleep(0)

    stopper.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await stopper
