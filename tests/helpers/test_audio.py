"""Tests for music_assistant.helpers.audio."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import ContentType
from music_assistant_models.media_items import AudioFormat

from music_assistant.helpers.audio import (
    build_concat_filelist,
    calculate_content_length,
    parse_loudnorm,
    realtime_pcm_pacer,
    resolve_output_player_ids,
)
from music_assistant.helpers.ffmpeg import DEFAULT_MP3_BIT_RATE


def test_resolve_output_player_ids_resolves_parents_and_duplicates() -> None:
    """Output destinations use visible protocol parents without duplicates."""
    mass = MagicMock()
    players = {
        "leader": SimpleNamespace(protocol_parent_id=None),
        "protocol-child": SimpleNamespace(protocol_parent_id="child"),
        "child": SimpleNamespace(protocol_parent_id=None),
    }
    mass.players.get_player.side_effect = players.get

    result = resolve_output_player_ids(
        mass,
        ("leader", "leader", "protocol-child", "child", "missing", "missing"),
    )

    assert result == {"leader", "child", "missing"}


def test_mp3_content_length_uses_encoder_bitrate() -> None:
    """MP3 size estimation uses the bitrate configured for FFmpeg encoding."""
    seconds = 2
    assert calculate_content_length(
        AudioFormat(content_type=ContentType.MP3),
        seconds,
    ) == int(((DEFAULT_MP3_BIT_RATE * 1000) / 8) * seconds)


def test_build_concat_filelist_plain_paths() -> None:
    """Paths without special characters are wrapped verbatim, one per line."""
    result = build_concat_filelist(["/music/a.mp3", "/music/b.mp3"])
    assert result == "file '/music/a.mp3'\nfile '/music/b.mp3'\n"


def test_build_concat_filelist_escapes_apostrophes() -> None:
    r"""
    A single quote in the path is escaped as '\'' for the concat demuxer.

    Regression test for multipart playback failing on paths such as
    "Amelia Bedelia's", where the demuxer truncated the path at the apostrophe.
    """
    path = "/audiobooks/Herman Parish - Young Amelia Bedelia's Audio Collection/01.mp3"
    result = build_concat_filelist([path])
    assert (
        result
        == "file '/audiobooks/Herman Parish - Young Amelia Bedelia'\\''s Audio Collection/01.mp3'\n"
    )
    # The original apostrophe must survive once the escaping is unwrapped.
    assert path in result.replace("'\\''", "'")


def test_build_concat_filelist_escapes_multiple_apostrophes() -> None:
    """Every apostrophe in a path is escaped, not just the first."""
    result = build_concat_filelist(["/x/it's a, b's & c's.mp3"])
    assert result == "file '/x/it'\\''s a, b'\\''s & c'\\''s.mp3'\n"


# verbatim ffmpeg 7.1 output: the report is a block below the marker line, not inline
FFMPEG_LOUDNORM_OUTPUT = b"""[out#0/null @ 0x93b] Output stream
[Parsed_loudnorm_0 @ 0x93b41d440] \n{
\t"input_i" : "-17.86",
\t"input_tp" : "-1.89",
\t"input_lra" : "0.40",
\t"input_thresh" : "-27.86",
\t"output_i" : "-23.92",
\t"normalization_type" : "dynamic",
\t"target_offset" : "-0.08"
}
size=N/A time=00:00:03.20 bitrate=N/A speed=  86x
"""


def test_parse_loudnorm_reads_the_measurement_ffmpeg_actually_prints() -> None:
    """The integrated loudness is read from loudnorm's own JSON report block."""
    assert parse_loudnorm(FFMPEG_LOUDNORM_OUTPUT) == -17.86


def test_parse_loudnorm_accepts_a_decoded_string() -> None:
    """Callers that already decoded the output get the same measurement."""
    assert parse_loudnorm(FFMPEG_LOUDNORM_OUTPUT.decode()) == -17.86


def test_parse_loudnorm_without_a_report_returns_none() -> None:
    """Output from a run that never reached the filter carries no measurement."""
    assert parse_loudnorm(b"ffmpeg: Invalid data found when processing input") is None


def test_parse_loudnorm_with_a_truncated_report_returns_none() -> None:
    """A report cut off mid-object is not mistaken for a measurement."""
    assert parse_loudnorm(b'[Parsed_loudnorm_0 @ 0x1] \n{\n\t"input_i" : "-17.8') is None


def test_parse_loudnorm_treats_digital_silence_as_no_measurement() -> None:
    """A silent clip reports -inf, which is the absence of a level, not a level."""
    silent = FFMPEG_LOUDNORM_OUTPUT.replace(b'"-17.86"', b'"-inf"')
    assert parse_loudnorm(silent) is None


def test_parse_loudnorm_reads_a_report_from_further_down_the_filter_chain() -> None:
    """The marker carries the filter's position, which is not zero behind another filter."""
    chained = FFMPEG_LOUDNORM_OUTPUT.replace(b"[Parsed_loudnorm_0 @", b"[Parsed_loudnorm_1 @")
    assert parse_loudnorm(chained) == -17.86


@pytest.mark.asyncio
async def test_realtime_pcm_pacer_grants_bounded_initial_burst() -> None:
    """The pacer lets a bounded head start through unpaced, then enforces realtime."""
    # tiny format keeps the test fast: 16000 B/s, so 1s of audio is 16000 bytes
    pcm_format = AudioFormat(
        content_type=ContentType.PCM_S16LE,
        sample_rate=8000,
        bit_depth=16,
        channels=1,
    )

    async def _instant_producer() -> AsyncGenerator[bytes]:
        for _ in range(10):
            yield b"\x00" * 1600  # 0.1s of audio per chunk, produced instantly

    loop = asyncio.get_running_loop()
    start = loop.time()
    async for _ in realtime_pcm_pacer(_instant_producer(), pcm_format):
        pass
    elapsed = loop.time() - start

    # 1.0s of audio with a 0.5s burst allowance should take ~0.5s: clearly less
    # than realtime (burst granted) but still paced (not instant). Bounds are
    # deliberately wide to stay robust on loaded CI runners.
    assert 0.3 < elapsed < 0.9
