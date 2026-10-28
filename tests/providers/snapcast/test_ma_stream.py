"""Tests for music_assistant.providers.snapcast.ma_stream._register_tcp_server_source."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any, Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import ContentType

from music_assistant.providers.snapcast.constants import (
    DEFAULT_SNAPCAST_FORMAT,
    snapcast_sampleformat_query,
    snapcast_stream_format,
)
from music_assistant.providers.snapcast.ma_stream import SnapcastMAStream
from music_assistant.providers.snapcast.provider import SnapCastProvider

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from .conftest import FakeSnapserver


def _make_stream(
    provider: MagicMock, name: str = "Music Assistant - testhash (announcement)"
) -> SnapcastMAStream:
    """
    Build a SnapcastMAStream instance directly.

    Bypasses the constructor's media handling (we only test the snapserver
    source registration).
    """
    media = MagicMock()
    return SnapcastMAStream(
        provider=provider,
        media=media,
        stream_name=name,
    )


@pytest.mark.parametrize(
    ("sample_rate", "bit_depth", "expected"),
    [
        (48000, 16, "sampleformat=48000:16:2"),
        (96000, 16, "sampleformat=96000:16:2"),
        (96000, 24, "sampleformat=96000:24:2&packed_s24le=true"),
        (192000, 24, "sampleformat=192000:24:2&packed_s24le=true"),
    ],
)
def test_sampleformat_query_enables_packed_s24le_for_24bit(
    sample_rate: int, bit_depth: int, expected: str
) -> None:
    """24-bit TCP sources must request packed_s24le; 16-bit must not."""
    audio_format = snapcast_stream_format(sample_rate, bit_depth)
    assert snapcast_sampleformat_query(audio_format) == expected


def test_stream_format_maps_bit_depth_to_pcm_content_type() -> None:
    """Bit depth selects the matching packed PCM content type."""
    assert snapcast_stream_format(48000, 16).content_type == ContentType.PCM_S16LE
    assert snapcast_stream_format(96000, 24).content_type == ContentType.PCM_S24LE


def test_transport_format_uses_snapserver_codec() -> None:
    """The reported final format follows the codec configured on Snapserver."""
    provider = MagicMock()
    provider._use_builtin_server = True
    provider._snapcast_server_transport_codec = "opus"
    provider.stream_audio_format = DEFAULT_SNAPCAST_FORMAT
    stream = _make_stream(provider)

    output_format = stream._get_transport_format()

    assert output_format.content_type == ContentType.OPUS
    assert output_format.codec_type == ContentType.OPUS
    assert output_format.sample_rate == 48000
    assert output_format.bit_depth == 16


def test_transport_format_follows_configured_stream_format() -> None:
    """Transport format inherits the configured Snapcast PCM sample rate/bit depth."""
    provider = MagicMock()
    provider._use_builtin_server = True
    provider._snapcast_server_transport_codec = "flac"
    provider.stream_audio_format = snapcast_stream_format(96000, 24)
    stream = _make_stream(provider)

    output_format = stream._get_transport_format()

    assert output_format.content_type == ContentType.FLAC
    assert output_format.sample_rate == 96000
    assert output_format.bit_depth == 24


def test_external_transport_format_reads_uri_codec() -> None:
    """External Snapserver codec is read from the stream URI query."""
    provider = MagicMock()
    provider._use_builtin_server = False
    provider.stream_audio_format = DEFAULT_SNAPCAST_FORMAT
    stream = _make_stream(provider)
    stream.snap_stream = MagicMock(
        _stream={"uri": {"query": {"codec": "flac"}}},
    )

    output_format = stream._get_transport_format()

    assert output_format.content_type == ContentType.FLAC
    assert output_format.codec_type == ContentType.FLAC


class _FakeFFMpeg:
    """FFMpeg stand-in that exits immediately."""

    def __init__(self, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        pass

    async def wait(self) -> int:
        return 0


@pytest.mark.asyncio
async def test_streamer_pokes_players_when_playback_anchor_is_known(
    fake_provider: MagicMock,
) -> None:
    """The stream's players are refreshed once the server buffer delay has passed."""
    fake_provider._use_builtin_server = True
    fake_provider._snapcast_server_buffer_size = 2000
    fake_provider._snapcast_server_host = "127.0.0.1"
    fake_provider.mass.create_task.side_effect = asyncio.ensure_future
    fake_provider.mass.closing = True
    stream = _make_stream(fake_provider, name="music")
    stream.snap_stream = MagicMock(identifier="music-1", path="tcp://0.0.0.0:4953")

    with patch("music_assistant.providers.snapcast.ma_stream.FFMpeg", _FakeFFMpeg):
        await stream._streamer_task_impl()

    fake_provider.mass.call_later.assert_called_once_with(
        2.0, stream._snap_on_stream_update, task_id="snapcast_playback_started_music"
    )


class _ConsumingFFMpeg(_FakeFFMpeg):
    """FFMpeg stand-in that reads all of its input at once, like ffmpeg's -re burst."""

    def __init__(self, audio_input: AsyncGenerator[bytes], **kwargs: Any) -> None:
        self._audio_input = audio_input

    async def wait(self) -> int:
        async for _ in self._audio_input:
            pass
        return 0


def _clip_stream(fake_provider: MagicMock, seconds: float) -> SnapcastMAStream:
    """Build a stream on the built-in server (0.5 s buffer) whose source yields a clip."""
    fake_provider._use_builtin_server = True
    fake_provider._snapcast_server_buffer_size = 500
    fake_provider._snapcast_server_host = "127.0.0.1"
    fake_provider.mass.create_task.side_effect = asyncio.ensure_future
    fake_provider.mass.closing = True

    async def clip() -> AsyncGenerator[bytes]:
        yield b"\0" * int(DEFAULT_SNAPCAST_FORMAT.pcm_sample_size * seconds)

    fake_provider.mass.streams.get_stream = MagicMock(return_value=clip())
    stream = _make_stream(fake_provider)
    stream.snap_stream = MagicMock(identifier="clip-1", path="tcp://0.0.0.0:4953")
    return stream


@pytest.mark.asyncio
async def test_streamer_ends_once_clients_played_the_buffered_audio(
    fake_provider: MagicMock,
) -> None:
    """A short clip is not cut off by switching the group away while clients still play it."""
    stream = _clip_stream(fake_provider, seconds=0.5)

    with patch("music_assistant.providers.snapcast.ma_stream.FFMpeg", _ConsumingFFMpeg):
        task = asyncio.ensure_future(stream._streamer_task_impl())
        # played out after 0.5 s buffer + 0.5 s clip
        await asyncio.sleep(0.7)
        assert not task.done()
        await asyncio.wait_for(task, 1)


@pytest.mark.asyncio
async def test_stop_ends_the_play_out_wait(fake_provider: MagicMock) -> None:
    """A stop request does not wait for the clients to play the buffered audio."""
    stream = _clip_stream(fake_provider, seconds=1.5)

    with patch("music_assistant.providers.snapcast.ma_stream.FFMpeg", _ConsumingFFMpeg):
        task = asyncio.ensure_future(stream._streamer_task_impl())
        await asyncio.sleep(0.1)
        assert not task.done()

        stream.request_stop_stream()
        await asyncio.wait_for(task, 0.5)


@pytest.mark.asyncio
async def test_closing_the_tracked_audio_closes_the_source(fake_provider: MagicMock) -> None:
    """Closing the audio early (stop or restart) still runs the source's cleanup."""
    closed = False

    async def source() -> AsyncGenerator[bytes]:
        nonlocal closed
        try:
            while True:
                yield b"\0" * 4
        finally:
            closed = True

    tracked = _make_stream(fake_provider)._track_sent_audio(source())
    await anext(tracked)
    await tracked.aclose()

    assert closed


@pytest.mark.asyncio
async def test_register_uses_configured_sampleformat(
    fake_provider: MagicMock, fake_snapserver: FakeSnapserver
) -> None:
    """stream_add_stream URI must include the configured sampleformat and packed_s24le."""
    fake_provider.stream_audio_format = snapcast_stream_format(96000, 24)
    fake_snapserver.queue_success(stream_id="hires-1")
    stream = _make_stream(fake_provider)

    await stream._register_tcp_server_source()

    assert len(fake_snapserver.add_stream_calls) == 1
    uri = fake_snapserver.add_stream_calls[0]
    assert "sampleformat=96000:24:2" in uri
    assert "packed_s24le=true" in uri


def test_output_plan_is_registered_for_all_snapcast_members() -> None:
    """Every client consuming a shared Snapcast stream gets the same output path."""
    provider = MagicMock()
    provider.stream_audio_format = DEFAULT_SNAPCAST_FORMAT
    stream = _make_stream(provider)
    stream.media.source_id = "queue-1"
    stream.media.queue_session_id = "session-1"
    stream.snap_stream = MagicMock(identifier="stream-1")
    group = MagicMock(
        stream="stream-1",
        clients=["snap-child"],
    )
    provider._snapserver.groups = [group]
    provider._get_ma_id.return_value = "child"
    output_details = MagicMock(player_ids=["leader"])
    stream._output_plan = MagicMock(output_details=output_details)

    stream._register_output_plan()

    registered_ids = {
        call.args[0] for call in provider.mass.streams.audio_processing.update_output.call_args_list
    }
    assert registered_ids == {"leader", "child"}


@pytest.mark.asyncio
async def test_happy_path_register_succeeds_first_attempt(
    fake_provider: MagicMock, fake_snapserver: FakeSnapserver
) -> None:
    """Regression: a clean snapserver returns id on first try, MA registers the stream."""
    fake_snapserver.queue_success(stream_id="ok-1")
    stream = _make_stream(fake_provider)

    await stream._register_tcp_server_source()

    assert stream.snap_stream is not None
    assert stream.snap_stream.identifier == "ok-1"
    assert len(fake_snapserver.add_stream_calls) == 1
    assert "sampleformat=48000:16:2" in fake_snapserver.add_stream_calls[0]
    assert "packed_s24le" not in fake_snapserver.add_stream_calls[0]


@pytest.mark.asyncio
async def test_real_port_conflict_retries_with_different_port(
    fake_provider: MagicMock, fake_snapserver: FakeSnapserver
) -> None:
    """Regression: a non-name error (e.g. port already bound) keeps the loop going."""
    fake_snapserver.queue_other_error("bind: Address already in use")
    fake_snapserver.queue_success(stream_id="ok-after-retry")

    stream = _make_stream(fake_provider)
    await stream._register_tcp_server_source()

    assert stream.snap_stream is not None
    assert stream.snap_stream.identifier == "ok-after-retry"
    # Two add_stream calls were made — first failed, second succeeded
    assert len(fake_snapserver.add_stream_calls) == 2
    # The two URIs must use different ports
    port_1 = fake_snapserver.add_stream_calls[0].split("0.0.0.0:")[1].split("?")[0]
    port_2 = fake_snapserver.add_stream_calls[1].split("0.0.0.0:")[1].split("?")[0]
    assert port_1 != port_2


@pytest.mark.asyncio
async def test_all_attempts_exhausted_raises_with_honest_message(
    fake_provider: MagicMock, fake_snapserver: FakeSnapserver
) -> None:
    """
    When all 50 retries fail the error must reference 'after retries' or similar.

    Must NOT say 'No free port found' which lies about the cause.
    """
    # Queue 51 errors so even after 50 attempts the loop hits the raise
    for _ in range(51):
        fake_snapserver.queue_other_error("Some persistent error")

    stream = _make_stream(fake_provider)

    with pytest.raises(RuntimeError) as exc_info:
        await stream._register_tcp_server_source()

    message = str(exc_info.value)
    assert "No free port" not in message, (
        f"Error message still claims port shortage when the real cause may differ: {message}"
    )
    assert "attempts" in message.lower() or "register" in message.lower()


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (
            {
                "code": -32603,
                "data": 'Stream with name "x" already exists',
                "message": "Internal error",
            },
            True,
        ),
        (
            {
                "code": -32603,
                "data": 'Stream with name "abc" already exists in registry',
                "message": "Internal error",
            },
            True,
        ),
        # negative cases — these must NOT match
        (
            {"code": -32603, "data": "bind: Address already in use", "message": "Internal error"},
            False,
        ),
        (
            {
                "code": -32603,
                "data": "Some other error already exists somewhere",
                "message": "Internal error",
            },
            False,
        ),
        ({}, False),
        (None, False),
        ("plain string", False),
        ({"data": 12345}, False),
    ],
)
def test_is_name_collision_error_matches_only_specific_pattern(
    result: object, expected: bool
) -> None:
    """_is_name_collision_error must NOT false-positive on unrelated errors."""
    assert SnapcastMAStream._is_name_collision_error(result) is expected


@pytest.mark.asyncio
async def test_name_collision_with_local_stream_cached_adopts_it(
    fake_provider: MagicMock, fake_snapserver: FakeSnapserver
) -> None:
    """
    When snapserver reports the name as already-registered MA must adopt the orphan.

    The stream must be in the local snapserver cache; MA must adopt it instead of
    looping until retries exhaust.
    """
    target_name = "Music Assistant - 590b15 (announcement)"
    fake_snapserver.cache_stream_directly("orphan-id", target_name)
    fake_snapserver.queue_name_collision()

    stream = _make_stream(fake_provider, name=target_name)
    await stream._register_tcp_server_source()

    assert stream.snap_stream is not None
    assert stream.snap_stream.identifier == "orphan-id"
    # Adoption must NOT spend further retries
    assert len(fake_snapserver.add_stream_calls) == 1


@pytest.mark.asyncio
async def test_name_collision_with_incompatible_format_recreates_stream(
    fake_provider: MagicMock, fake_snapserver: FakeSnapserver
) -> None:
    """Orphan streams with a different sample format must be removed and recreated."""
    target_name = "Music Assistant - hires"
    fake_provider.stream_audio_format = snapcast_stream_format(96000, 24)
    fake_snapserver.cache_stream_directly("orphan-id", target_name)
    fake_snapserver.queue_name_collision()
    fake_snapserver.queue_success(stream_id="recreated-id")

    stream = _make_stream(fake_provider, name=target_name)
    await stream._register_tcp_server_source()

    assert stream.snap_stream is not None
    assert stream.snap_stream.identifier == "recreated-id"
    assert fake_snapserver.stream_remove_stream.await_count == 1
    assert "sampleformat=96000:24:2" in fake_snapserver.add_stream_calls[-1]
    assert "packed_s24le=true" in fake_snapserver.add_stream_calls[-1]


@pytest.mark.asyncio
async def test_orphan_stream_after_ma_restart_gets_adopted_via_resync(
    fake_provider: MagicMock, fake_snapserver: FakeSnapserver
) -> None:
    """
    Bug B core scenario: MA restarted while a stream was registered.

    The local snapserver cache is empty, but the server still holds the orphan.
    MA must call status() + synchronize() to re-discover it before adopting.
    """
    target_name = "Music Assistant - 590b15 (announcement)"
    # Stream visible only via status(), NOT in the local cache yet
    fake_snapserver.stage_orphan_stream("orphan-id", target_name)
    fake_snapserver.queue_name_collision()

    # Sanity: the local cache is empty before
    assert fake_snapserver._streams_by_id == {}

    stream = _make_stream(fake_provider, name=target_name)
    await stream._register_tcp_server_source()

    assert stream.snap_stream is not None
    assert stream.snap_stream.identifier == "orphan-id"
    # The status() round-trip must have happened
    assert fake_snapserver.status.await_count >= 1


@pytest.mark.asyncio
async def test_second_register_call_is_idempotent_when_stream_already_set(
    fake_provider: MagicMock, fake_snapserver: FakeSnapserver
) -> None:
    """
    Calling _register_tcp_server_source after self.snap_stream is set is a no-op.

    This is the early-return guard at the top of the method. Real concurrency
    safety is provided by `_lifecycle_lock` in setup(); that path is exercised
    by integration tests, not unit tests.
    """
    fake_snapserver.queue_success(stream_id="single-stream")
    fake_snapserver.queue_success(stream_id="should-not-happen")

    stream = _make_stream(fake_provider)

    await asyncio.gather(
        stream._register_tcp_server_source(),
        stream._register_tcp_server_source(),
    )

    assert stream.snap_stream is not None
    assert stream.snap_stream.identifier == "single-stream"
    # Only ONE add_stream call was issued
    assert len(fake_snapserver.add_stream_calls) == 1


def test_pick_port_avoids_already_tried() -> None:
    """Within a single retry loop, _pick_port_avoiding must not return a tried port."""
    # Build a stream stub that exposes only what _pick_port_avoiding needs
    stream = SnapcastMAStream.__new__(SnapcastMAStream)

    used: set[int] = set()
    for _ in range(100):
        port = stream._pick_port_avoiding(used)
        assert port is not None, "Helper unexpectedly returned None when ports remain"
        assert 4953 <= port <= 4953 + 200
        assert port not in used
        used.add(port)


def _displaced_stream(provider: MagicMock, stream_id: str = "music-1") -> SnapcastMAStream:
    """Build a registered stream and point every snapserver group somewhere else."""
    stream = _make_stream(provider, name="Music Assistant - music")
    stream.snap_stream = MagicMock(identifier=stream_id)
    provider._snapcast_ma_streams = {stream_id: stream}
    provider._snapserver.groups = [MagicMock(stream="announcement-1")]
    return stream


def test_displaced_stream_is_scheduled_for_stop(fake_provider: MagicMock) -> None:
    """A stream no group plays anymore is stopped after the idle delay."""
    stream = _displaced_stream(fake_provider)

    SnapCastProvider.update_stream_usage(fake_provider)

    fake_provider.mass.loop.call_later.assert_called_once_with(3.0, stream.request_stop_stream)


def test_pinned_stream_survives_being_displaced(fake_provider: MagicMock) -> None:
    """A media change on a playing stream kills the old run without draining it."""
    stream = _displaced_stream(fake_provider)
    stream.pin("player-1")

    SnapCastProvider.update_stream_usage(fake_provider)

    fake_provider.mass.loop.call_later.assert_not_called()


def test_one_member_finishing_does_not_release_another_members_pin(
    fake_provider: MagicMock,
) -> None:
    """
    Regression: announcements on group members run concurrently and share one stream.

    The member that finishes first must not hand the stream back while the others still
    have every group displaced from it.
    """
    stream = _displaced_stream(fake_provider)
    stream.pin("player-1")
    stream.pin("player-2")

    stream.unpin("player-1")
    SnapCastProvider.update_stream_usage(fake_provider)

    fake_provider.mass.loop.call_later.assert_not_called()

    stream.unpin("player-2")
    SnapCastProvider.update_stream_usage(fake_provider)

    fake_provider.mass.loop.call_later.assert_called_once_with(3.0, stream.request_stop_stream)


async def test_replay_after_stop_waits_for_the_previous_run(fake_provider: MagicMock) -> None:
    """
    Regression: replaying right after a stop must not return while the old run drains.

    The caller points the group at the stream once this returns; snapserver still plays
    the previous session's audio until the stopping run has finished.
    """
    stream = _make_stream(fake_provider)
    stream._setup_done = True
    fake_provider.mass.create_task = asyncio.ensure_future
    new_run = AsyncMock()
    stream._streamer_task_impl = new_run  # type: ignore[method-assign]
    old_run_done = asyncio.Event()

    async def old_run() -> None:
        await old_run_done.wait()

    stream._streamer_task = asyncio.ensure_future(old_run())
    stream._streamer_task.add_done_callback(stream._on_streamer_done)
    stream.request_stop_stream()

    start = asyncio.ensure_future(stream.start_stream(allow_restart=True))
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(asyncio.shield(start), 0.05)

    old_run_done.set()
    await asyncio.wait_for(start, 1)
    await asyncio.sleep(0.01)
    new_run.assert_awaited_once()


async def test_restart_of_a_running_stream_does_not_wait(fake_provider: MagicMock) -> None:
    """A media change on a playing stream returns at once; the group is already on it."""
    stream = _make_stream(fake_provider)
    stream._setup_done = True
    stream._streamer_task = asyncio.ensure_future(asyncio.sleep(10))

    stream.update_media(MagicMock())
    await asyncio.wait_for(stream.start_stream(allow_restart=True), 0.05)

    assert stream._restart_requested
    stream._streamer_task.cancel()


async def test_restart_drops_the_playback_anchor_of_the_old_run(
    fake_provider: MagicMock,
) -> None:
    """
    Regression: a restarted stream must not report when the replaced run started.

    The group can be put on the stream before the old run has finished, so players would
    anchor their elapsed time to the old run's start.
    """
    fake_provider._snapcast_server_buffer_size = 1000
    stream = _make_stream(fake_provider)
    stream._setup_done = True
    stream._streamer_task = asyncio.ensure_future(asyncio.sleep(10))
    stream._streaming_started_at = time.time() - 20

    await stream.start_stream(allow_restart=True)

    assert stream.playback_started_at is None
    stream._streamer_task.cancel()


def test_releasing_the_pin_schedules_the_stop_again(fake_provider: MagicMock) -> None:
    """A stream that stays unused after its pin is released is stopped as usual."""
    stream = _displaced_stream(fake_provider)
    stream.pin("player-1")
    SnapCastProvider.update_stream_usage(fake_provider)

    stream.unpin("player-1")
    SnapCastProvider.update_stream_usage(fake_provider)

    fake_provider.mass.loop.call_later.assert_called_once_with(3.0, stream.request_stop_stream)


class _RunningFFMpeg(_FakeFFMpeg):
    """FFMpeg stand-in that runs until it is killed."""

    def __init__(self, **kwargs: Any) -> None:
        self.kill = AsyncMock()
        _running_ffmpegs.append(self)

    async def wait(self) -> int:
        await asyncio.Event().wait()
        return 0


_running_ffmpegs: list[_RunningFFMpeg] = []


async def test_restart_kills_the_old_run_without_draining(fake_provider: MagicMock) -> None:
    """
    Regression: a media change on a playing stream must not drain the old track.

    Clients play everything the old run still sends, so a graceful close and the wait for
    snapserver to go idle put seconds of the old track before the new one.
    """
    _running_ffmpegs.clear()
    fake_provider._snapcast_server_host = "127.0.0.1"
    fake_provider.mass.create_task.side_effect = asyncio.ensure_future
    stream = _make_stream(fake_provider, name="music")
    stream.snap_stream = MagicMock(identifier="music-1", path="tcp://0.0.0.0:4953")
    stream._wait_stream_idle = AsyncMock()  # type: ignore[method-assign]

    with patch("music_assistant.providers.snapcast.ma_stream.FFMpeg", _RunningFFMpeg):
        stream._streamer_task = asyncio.ensure_future(stream._streamer_task_impl())
        await asyncio.sleep(0.01)
        stream.update_media(MagicMock())
        await asyncio.wait_for(stream._streamer_task, 1)

    _running_ffmpegs[0].kill.assert_awaited_once()


async def test_replay_during_an_explicit_stop_still_drains(fake_provider: MagicMock) -> None:
    """A replay requested while an explicit stop drains must not skip the idle wait."""
    _running_ffmpegs.clear()
    fake_provider._snapcast_server_host = "127.0.0.1"
    fake_provider.mass.create_task.side_effect = asyncio.ensure_future
    stream = _make_stream(fake_provider, name="music")
    stream.snap_stream = MagicMock(identifier="music-1", path="tcp://0.0.0.0:4953")
    stream._wait_stream_idle = AsyncMock()  # type: ignore[method-assign]

    with patch("music_assistant.providers.snapcast.ma_stream.FFMpeg", _RunningFFMpeg):
        stream._streamer_task = asyncio.ensure_future(stream._streamer_task_impl())
        await asyncio.sleep(0.01)
        stream.request_stop_stream()
        # what start_stream sets when a replay finds the run still stopping
        stream._restart_requested = True
        await asyncio.wait_for(stream._streamer_task, 1)

    _running_ffmpegs[0].kill.assert_not_awaited()
    stream._wait_stream_idle.assert_awaited_once()


async def test_run_stopped_while_starting_does_not_signal_started(fake_provider: MagicMock) -> None:
    """A restart during FFmpeg startup leaves the wait for the replacement run pending."""
    _running_ffmpegs.clear()
    fake_provider._snapcast_server_host = "127.0.0.1"
    fake_provider.mass.create_task.side_effect = asyncio.ensure_future
    stream = _make_stream(fake_provider, name="music")
    stream.snap_stream = MagicMock(identifier="music-1", path="tcp://0.0.0.0:4953")
    stream._wait_stream_idle = AsyncMock()  # type: ignore[method-assign]
    started_at_kill: list[bool] = []

    class _RestartedWhileStarting(_RunningFFMpeg):
        async def __aenter__(self) -> Self:
            if len(_running_ffmpegs) == 1:
                stream.update_media(MagicMock())
            self.kill.side_effect = lambda: started_at_kill.append(
                stream._streamer_started_evt.is_set()
            )
            return self

    with patch("music_assistant.providers.snapcast.ma_stream.FFMpeg", _RestartedWhileStarting):
        stream._streamer_task = asyncio.ensure_future(stream._streamer_task_impl())
        await asyncio.wait_for(stream._streamer_task, 1)

    assert started_at_kill == [False]
