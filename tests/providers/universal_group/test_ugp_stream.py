"""Tests for the pacing of the Universal Group Player stream."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import patch

from music_assistant_models.enums import ContentType
from music_assistant_models.media_items import AudioFormat

from music_assistant.controllers.streams.constants import PacingProfile, output_pacing_args
from music_assistant.providers.universal_group.ugp_stream import UGPStream


async def test_ugp_stream_is_paced_like_the_flow_stream() -> None:
    """
    The group stream is one continuous stream fanned out to all members.

    Its source is the raw PCM queue flow, which carries no pacing of its own, so this
    ffmpeg is the single pacing point and takes the flow route's NEAR_REALTIME pace.
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
    assert recorded["extra_input_args"] == output_pacing_args(PacingProfile.NEAR_REALTIME)
