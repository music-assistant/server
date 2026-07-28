"""Tests for music_assistant.helpers.audio."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from music_assistant_models.enums import ContentType
from music_assistant_models.media_items import AudioFormat

from music_assistant.helpers.audio import (
    build_concat_filelist,
    calculate_content_length,
    clamp_gain_to_true_peak,
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


def test_clamp_gain_to_true_peak_ignores_non_positive_gain() -> None:
    """A negative or zero gain is returned unchanged, regardless of true peak."""
    assert clamp_gain_to_true_peak(-3.0, -1.0) == -3.0
    assert clamp_gain_to_true_peak(0.0, -1.0) == 0.0


def test_clamp_gain_to_true_peak_ignores_unknown_true_peak() -> None:
    """A positive gain is returned unchanged when no true peak was measured."""
    assert clamp_gain_to_true_peak(6.0, None) == 6.0


def test_clamp_gain_to_true_peak_limits_to_available_headroom() -> None:
    """A positive gain is capped to the headroom the measured true peak leaves."""
    assert clamp_gain_to_true_peak(6.0, -3.0) == 2.0


def test_clamp_gain_to_true_peak_never_goes_negative() -> None:
    """A true peak already at or above the ceiling clamps the gain to unity, not below."""
    assert clamp_gain_to_true_peak(6.0, -0.1) == 0.0
