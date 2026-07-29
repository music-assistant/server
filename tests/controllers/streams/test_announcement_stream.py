"""Tests for the announcement stream lifecycle in the streams controller."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, Iterator
from contextlib import aclosing
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from music_assistant_models.enums import ContentType
from music_assistant_models.media_items import AudioFormat

from music_assistant.controllers.players.helpers import AnnounceData
from music_assistant.controllers.streams.controller import StreamsController

PCM_FORMAT = AudioFormat(
    content_type=ContentType.PCM_S16LE,
    sample_rate=44100,
    bit_depth=16,
    channels=2,
)


@pytest.mark.asyncio
async def test_fetch_task_cancelled_when_consumer_abandons() -> None:
    """The background fetch task and its ffmpeg stream stop when the consumer goes away."""
    ffmpeg_stream_closed = asyncio.Event()
    fetch_tasks: list[asyncio.Task[None]] = []

    def _fake_ffmpeg_stream(*_args: Any, **_kwargs: Any) -> AsyncGenerator[bytes]:
        async def _stream() -> AsyncGenerator[bytes]:
            try:
                while True:
                    yield b"\x00" * 1024
            finally:
                ffmpeg_stream_closed.set()

        return _stream()

    def _create_task(coro: Any) -> asyncio.Task[None]:
        task = asyncio.get_running_loop().create_task(coro)
        fetch_tasks.append(task)
        return task

    fake_self = MagicMock()
    fake_self.mass.create_task = MagicMock(side_effect=_create_task)

    with patch(
        "music_assistant.controllers.streams.controller.get_ffmpeg_stream",
        side_effect=_fake_ffmpeg_stream,
    ):
        stream = StreamsController.get_announcement_stream(
            cast("StreamsController", fake_self),
            announcement_url="http://test/announcement.mp3",
            output_format=PCM_FORMAT,
            pre_announce=False,
        )
        # consume a few chunks, then abandon the stream (player disconnected)
        async with aclosing(stream):
            chunk_count = 0
            async for _chunk in stream:
                chunk_count += 1
                if chunk_count == 3:
                    break

    assert len(fetch_tasks) == 1
    # closing the consumer must cancel the fetch task and finalize the ffmpeg stream
    await asyncio.wait_for(ffmpeg_stream_closed.wait(), timeout=1)
    with pytest.raises(asyncio.CancelledError):
        await fetch_tasks[0]


def _announce_data(announce_player_id: str | None) -> AnnounceData:
    return AnnounceData(
        announcement_url="http://test/announcement.mp3",
        pre_announce=False,
        pre_announce_url="",
        announce_player_id=announce_player_id,
    )


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
