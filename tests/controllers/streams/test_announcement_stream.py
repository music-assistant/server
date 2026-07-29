"""Tests for the announcement stream lifecycle in the streams controller."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, Iterator
from contextlib import aclosing
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from music_assistant_models.enums import ContentType, MediaType
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.player import PlayerMedia

from music_assistant.controllers.players.helpers import AnnounceData
from music_assistant.controllers.streams.announcements import (
    ANNOUNCEMENT_PCM_FORMAT,
    AnnouncementRenderer,
)
from music_assistant.controllers.streams.controller import StreamsController

PCM_FORMAT = AudioFormat(
    content_type=ContentType.PCM_S16LE,
    sample_rate=44100,
    bit_depth=16,
    channels=2,
)
ONE_SECOND_CHUNK = b"\x00" * ANNOUNCEMENT_PCM_FORMAT.pcm_sample_size


def _announce_data(
    announce_player_id: str | None = None, pre_announce: bool = False
) -> AnnounceData:
    return AnnounceData(
        announcement_url="http://test/announcement.mp3",
        pre_announce=pre_announce,
        pre_announce_url="http://test/chime.mp3",
        announce_player_id=announce_player_id,
    )


def _announcement(pre_announce: bool = False) -> PlayerMedia:
    """Return the PlayerMedia a player is handed for an announcement."""
    return PlayerMedia(
        uri="http://ma/announcement/player_1.mp3",
        media_type=MediaType.ANNOUNCEMENT,
        custom_data=dict(_announce_data(pre_announce=pre_announce)),
    )


def _fake_ffmpeg_stream(chunks: int) -> Any:
    """Return a get_ffmpeg_stream stand-in yielding the given number of 1-second chunks."""

    def _factory(*_args: Any, **_kwargs: Any) -> AsyncGenerator[bytes]:
        async def _stream() -> AsyncGenerator[bytes]:
            for _ in range(chunks):
                yield ONE_SECOND_CHUNK

        return _stream()

    return _factory


def _controller(renderer: AnnouncementRenderer) -> StreamsController:
    """Return a StreamsController stub that only knows about announcements."""
    controller = StreamsController.__new__(StreamsController)
    controller.announcement_renderer = renderer
    return controller


@pytest.mark.asyncio
async def test_source_is_rendered_once_for_all_consumers() -> None:
    """Every consumer of the same announcement is served from a single render."""
    renderer = AnnouncementRenderer()
    controller = _controller(renderer)
    factory = MagicMock(side_effect=_fake_ffmpeg_stream(3))

    async def _read() -> list[bytes]:
        return [
            chunk
            async for chunk in controller.get_announcement_stream(_announce_data(), PCM_FORMAT)
        ]

    with patch("music_assistant.controllers.streams.announcements.get_ffmpeg_stream", factory):
        # play_announcement holds a reference for the duration of the announcement
        owner_render = renderer.acquire(_announce_data())
        # two group members read at the same time, a HEAD probe read before them
        sequential = await _read()
        concurrent = await asyncio.gather(_read(), _read())
        await renderer.release(owner_render)

    # the source is fetched/decoded once, yet every consumer got the full audio
    assert factory.call_count == 1
    assert all(len(chunks) == 3 for chunks in [sequential, *concurrent])
    assert renderer.active_renders == 0


@pytest.mark.asyncio
async def test_render_is_kept_alive_while_a_consumer_reads() -> None:
    """A render survives its owner releasing it as long as a consumer is still reading."""
    renderer = AnnouncementRenderer()
    controller = _controller(renderer)

    with patch(
        "music_assistant.controllers.streams.announcements.get_ffmpeg_stream",
        side_effect=_fake_ffmpeg_stream(2),
    ):
        owner_render = renderer.acquire(_announce_data())
        stream = controller.get_announcement_stream(_announce_data(), PCM_FORMAT)
        async with aclosing(stream):
            assert await anext(stream) == ONE_SECOND_CHUNK
            # the owner (play_announcement) is done, the consumer is not
            await renderer.release(owner_render)
            assert renderer.active_renders == 1
            assert await anext(stream) == ONE_SECOND_CHUNK

    assert renderer.active_renders == 0


@pytest.mark.asyncio
async def test_duration_is_exact_without_probing_the_source() -> None:
    """The rendered audio yields the exact announcement duration."""
    renderer = AnnouncementRenderer()
    controller = _controller(renderer)

    with patch(
        "music_assistant.controllers.streams.announcements.get_ffmpeg_stream",
        side_effect=_fake_ffmpeg_stream(4),
    ):
        render = renderer.acquire(_announce_data())
        assert await controller.get_announcement_duration(_announcement()) == 4
        assert render.duration == 4.0
        await renderer.release(render)

    # without an active render there is nothing to report
    assert await controller.get_announcement_duration(_announcement()) is None


@pytest.mark.asyncio
async def test_pre_announce_is_part_of_the_render() -> None:
    """The pre-announce chime is decoded into the same render as the announcement."""
    renderer = AnnouncementRenderer()
    controller = _controller(renderer)
    factory = MagicMock(side_effect=_fake_ffmpeg_stream(1))

    with patch("music_assistant.controllers.streams.announcements.get_ffmpeg_stream", factory):
        render = renderer.acquire(_announce_data(pre_announce=True))
        duration = await render.wait_finished()
        await renderer.release(render)

    # chime and announcement are decoded separately but land in one render
    assert factory.call_count == 2
    assert duration == 2.0
    # an announcement with a chime is a different render than the one without
    assert await controller.get_announcement_duration(_announcement()) is None


@pytest.mark.asyncio
async def test_render_is_cancelled_when_released() -> None:
    """Releasing the last reference tears the render (and its ffmpeg chain) down."""
    ffmpeg_stream_closed = asyncio.Event()

    def _endless_stream(*_args: Any, **_kwargs: Any) -> AsyncGenerator[bytes]:
        async def _stream() -> AsyncGenerator[bytes]:
            try:
                while True:
                    yield ONE_SECOND_CHUNK
                    await asyncio.sleep(0)
            finally:
                ffmpeg_stream_closed.set()

        return _stream()

    renderer = AnnouncementRenderer()
    with patch(
        "music_assistant.controllers.streams.announcements.get_ffmpeg_stream",
        side_effect=_endless_stream,
    ):
        render = renderer.acquire(_announce_data())
        await render.wait_ready()
        await renderer.release(render)

    await asyncio.wait_for(ffmpeg_stream_closed.wait(), timeout=1)
    assert renderer.active_renders == 0


@pytest.mark.asyncio
async def test_the_chime_alone_does_not_make_the_clip_ready() -> None:
    """Playback waits for announcement audio, not just the pre-announce chime."""
    tts_answered = asyncio.Event()

    def _fast_chime_slow_tts(*_args: Any, **kwargs: Any) -> AsyncGenerator[bytes]:
        is_chime = "chime" in kwargs["audio_input"]

        async def _stream() -> AsyncGenerator[bytes]:
            if is_chime:
                yield ONE_SECOND_CHUNK
                return
            await tts_answered.wait()
            yield ONE_SECOND_CHUNK

        return _stream()

    renderer = AnnouncementRenderer()
    with patch(
        "music_assistant.controllers.streams.announcements.get_ffmpeg_stream",
        side_effect=_fast_chime_slow_tts,
    ):
        render = renderer.acquire(_announce_data(pre_announce=True))
        waiter = asyncio.ensure_future(render.wait_ready())
        await asyncio.sleep(0)
        # the chime is buffered, but a player started on it would run dry
        assert render.duration == 1.0
        assert not waiter.done()

        tts_answered.set()
        assert await asyncio.wait_for(waiter, timeout=1) is True
        assert render.duration == 2.0
        await renderer.release(render)


@pytest.mark.asyncio
async def test_a_reader_can_start_before_the_render_finished() -> None:
    """A reader that catches up with the render waits for the rest of the clip."""
    released = asyncio.Event()

    def _slow_stream(*_args: Any, **_kwargs: Any) -> AsyncGenerator[bytes]:
        async def _stream() -> AsyncGenerator[bytes]:
            yield ONE_SECOND_CHUNK
            await released.wait()
            yield ONE_SECOND_CHUNK

        return _stream()

    renderer = AnnouncementRenderer()
    controller = _controller(renderer)
    with patch(
        "music_assistant.controllers.streams.announcements.get_ffmpeg_stream",
        side_effect=_slow_stream,
    ):
        render = renderer.acquire(_announce_data())
        stream = controller.get_announcement_stream(_announce_data(), PCM_FORMAT)
        async with aclosing(stream):
            # the reader catches up with the render and has to wait for the rest
            assert await anext(stream) == ONE_SECOND_CHUNK
            reader = asyncio.ensure_future(anext(stream))
            await asyncio.sleep(0)
            assert not reader.done()
            released.set()
            assert await asyncio.wait_for(reader, timeout=1) == ONE_SECOND_CHUNK
        await renderer.release(render)

    assert renderer.active_renders == 0


def test_announcement_http_profile_prefers_fetching_player() -> None:
    """The http profile comes from the player that actually fetches the announcement."""
    controller = StreamsController.__new__(StreamsController)
    controller.mass = mass = MagicMock()
    fetcher = MagicMock()
    fetcher.get_output_config_value.return_value = "forced_content_length"
    parent = MagicMock()
    parent.get_output_config_value.return_value = "chunked"
    players = {"fetcher": fetcher, "parent": parent}
    mass.players.get_player = MagicMock(side_effect=lambda pid: players.get(pid))

    # a known fetcher (e.g. a linked protocol player) provides the profile
    profile = controller._get_announcement_http_profile("parent", _announce_data("fetcher"))
    assert profile == "forced_content_length"

    # without a recorded fetcher, resolve against the visible player's output path
    profile = controller._get_announcement_http_profile("parent", _announce_data(None))
    assert profile == "chunked"

    # a dangling fetcher id falls back to the visible player
    profile = controller._get_announcement_http_profile("parent", _announce_data("ghost"))
    assert profile == "chunked"

    # unknown player resolves to the safe default
    profile = controller._get_announcement_http_profile("unknown", _announce_data(None))
    assert profile == "default"


ANNOUNCEMENT_AUDIO = b"announcement-audio"


def _fake_announcement_stream(*_args: Any, **_kwargs: Any) -> AsyncGenerator[bytes]:
    async def _stream() -> AsyncGenerator[bytes]:
        yield ANNOUNCEMENT_AUDIO

    return _stream()


@pytest.fixture(name="render_mock")
def render_mock_fixture() -> Iterator[MagicMock]:
    """Replace the announcement render chain with a stub that yields a single chunk."""
    with patch.object(
        StreamsController, "get_announcement_stream", side_effect=_fake_announcement_stream
    ) as mocked:
        yield mocked


def _announcement_controller(http_profile: str) -> tuple[StreamsController, MagicMock]:
    """Build a controller serving one pending announcement for player 'player1'."""
    controller = StreamsController.__new__(StreamsController)
    controller.mass = mass = MagicMock()
    controller.logger = logging.getLogger("test.streams.announcement")
    controller.announcements = {"player1": _announce_data(None)}
    player = MagicMock()
    player.display_name = "Player A"
    player.get_output_config_value.return_value = http_profile
    mass.players.get_player = MagicMock(
        side_effect=lambda pid, *_args, **_kwargs: player if pid == "player1" else None
    )
    # no queue is registered for this player: announcements must be served regardless
    mass.player_queues.get = MagicMock(return_value=None)
    return controller, mass


def _mocked_request(method: str, player_id: str = "player1") -> web.Request:
    return make_mocked_request(
        method,
        f"/announcement/{player_id}.mp3",
        match_info={"player_id": player_id, "fmt": "mp3"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("http_profile", ["forced_content_length", "chunked", "default"])
async def test_head_request_does_not_render_announcement(
    render_mock: MagicMock, http_profile: str
) -> None:
    """A HEAD probe answers with headers only, without running the render chain."""
    controller, _ = _announcement_controller(http_profile)

    resp = await controller.serve_announcement_stream(_mocked_request("HEAD"))

    assert resp.status == 200
    assert resp.content_type == "audio/mpeg"
    render_mock.assert_not_called()


@pytest.mark.asyncio
async def test_get_request_renders_announcement_once(render_mock: MagicMock) -> None:
    """A GET on the forced_content_length profile renders the announcement exactly once."""
    controller, _ = _announcement_controller("forced_content_length")

    resp = await controller.serve_announcement_stream(_mocked_request("GET"))

    assert isinstance(resp, web.Response)
    assert resp.body == ANNOUNCEMENT_AUDIO
    render_mock.assert_called_once()


@pytest.mark.asyncio
async def test_announcement_served_to_player_without_queue(
    render_mock: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """The stream resolves the player itself, so a player without a queue is still served."""
    controller, mass = _announcement_controller("default")

    with caplog.at_level(logging.DEBUG, logger="test.streams.announcement"):
        resp = await controller.serve_announcement_stream(_mocked_request("GET"))

    assert resp.status == 200
    mass.player_queues.get.assert_not_called()
    render_mock.assert_called_once()
    # the player is logged by name, not by its playback state
    assert "Player A" in caplog.text


@pytest.mark.asyncio
async def test_unknown_player_raises_not_found(render_mock: MagicMock) -> None:
    """An announcement request for an unknown player is rejected."""
    controller, _ = _announcement_controller("default")

    with pytest.raises(web.HTTPNotFound):
        await controller.serve_announcement_stream(_mocked_request("GET", player_id="ghost"))

    render_mock.assert_not_called()
