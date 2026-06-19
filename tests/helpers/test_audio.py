"""Tests for music_assistant.helpers.audio."""

from __future__ import annotations

from music_assistant.helpers.audio import build_concat_filelist


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
