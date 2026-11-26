"""Tests for the pacing of the Universal Group Player stream."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import patch

import pytest
from music_assistant_models.enums import ContentType
from music_assistant_models.errors import AudioError
from music_assistant_models.media_items import AudioFormat

from music_assistant.controllers.streams.constants import output_pacing_args
from music_assistant.models.media_capabilities import AudioSourceRefusedError
from music_assistant.providers.universal_group.ugp_stream import UGPStream


async def test_ugp_stream_is_paced_like_the_flow_stream() -> None:
    """
    The group stream is one continuous stream fanned out to all members.

    Its source is the raw PCM queue flow, which carries no pacing of its own, so this
    ffmpeg is the single pacing point and takes the flow route's realtime pace.
    """
    recorded: dict[str, Any] = {}
    pcm_format = AudioFormat(
        content_type=ContentType.PCM_F32LE,
        sample_rate=44100,
        bit_depth=32,
        channels=2,
    )

    async def fake_ffmpeg_stream(**kwargs: Any) -> AsyncGenerator[bytes]:
        recorded.update(kwargs)
        yield b"\x00" * 16

    async def audio_source() -> AsyncGenerator[bytes]:
        yield b"\x00" * 16

    stream = UGPStream(
        audio_source=audio_source(),
        audio_format=pcm_format,
        base_pcm_format=pcm_format,
        queue_id="q",
        session_id="s",
    )
    with patch(
        "music_assistant.providers.universal_group.ugp_stream.get_ffmpeg_stream",
        fake_ffmpeg_stream,
    ):
        chunks = [chunk async for chunk in stream.subscribe_raw()]

    assert chunks == [b"\x00" * 16]
    assert recorded["extra_input_args"] == output_pacing_args()


@pytest.mark.parametrize(
    ("error", "logged"),
    [
        (AudioSourceRefusedError("No AirPlay client is streaming to this receiver"), False),
        (AudioError("Error while feeding audio to FFmpeg"), True),
    ],
    ids=["refused", "failed"],
)
async def test_only_a_source_that_fails_is_logged_as_an_error(
    error: AudioError, logged: bool
) -> None:
    """Only a failure is logged here: the streams controller logs a source refusing the group."""
    pcm_format = AudioFormat(
        content_type=ContentType.PCM_F32LE,
        sample_rate=44100,
        bit_depth=32,
        channels=2,
    )
    no_chunks: list[bytes] = []

    async def failing_ffmpeg_stream(**_kwargs: Any) -> AsyncGenerator[bytes]:
        for chunk in no_chunks:
            yield chunk
        raise error

    async def audio_source() -> AsyncGenerator[bytes]:
        yield b"\x00" * 16

    stream = UGPStream(
        audio_source=audio_source(),
        audio_format=pcm_format,
        base_pcm_format=pcm_format,
        queue_id="q",
        session_id="s",
    )
    with (
        patch(
            "music_assistant.providers.universal_group.ugp_stream.get_ffmpeg_stream",
            failing_ffmpeg_stream,
        ),
        patch("music_assistant.providers.universal_group.ugp_stream.LOGGER") as logger,
    ):
        chunks = [chunk async for chunk in stream.subscribe_raw()]

    # the members still get the end of the stream
    assert chunks == []
    assert logger.error.called is logged
