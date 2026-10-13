"""Tests for lyric line parsing."""

from music_assistant.providers.web_kiosk.lyrics import parse_track_lyrics


def test_plain_lines_have_no_timestamp() -> None:
    """Plain text becomes one untimed line per non-empty row."""
    lines = parse_track_lyrics("one\n\ntwo\n", None)

    assert lines == [{"t": None, "text": "one"}, {"t": None, "text": "two"}]


def test_lrc_wins_and_keeps_timestamps() -> None:
    """A synced lyric is used even when plain text is also present."""
    lines = parse_track_lyrics("plain", "[00:01.50]Hello\n[01:02]There\n")

    assert lines == [
        {"t": 1.5, "text": "Hello"},
        {"t": 62.0, "text": "There"},
    ]


def test_plain_text_that_is_lrc_is_timed() -> None:
    """LRC stored in the plain field is still read as timestamps."""
    lines = parse_track_lyrics("[00:03]Only\n", None)

    assert lines == [{"t": 3.0, "text": "Only"}]


def test_empty_lyrics() -> None:
    """Missing text produces no lines."""
    assert parse_track_lyrics(None, None) == []
    assert parse_track_lyrics("  \n", " \n") == []
