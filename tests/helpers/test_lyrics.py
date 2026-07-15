"""Tests for the lyrics helpers."""

from music_assistant.helpers.lyrics import normalize_lrc_lyrics

LRC_WITH_ID_TAGS = """[ar:Chubby Checker oppure  Beatles, The]
[al:Hits Of The 60's - Vol. 2 - Oldies]
[ti:Let's Twist Again]
[au:Written by Kal Mann / Dave Appell, 1961]
[length: 2:23]
[offset:+500]
[by:lrc-author]
[re:some editor]
[ve:1.0]
[#some comment]

[00:12.00]Naku Penda Piya-Naku Taka Piya-Mpenziwe
[00:15.30]Some more lyrics ...
"""

LRC_CLEAN = """[00:12.00]Naku Penda Piya-Naku Taka Piya-Mpenziwe
[00:15.30]Some more lyrics ..."""


def test_normalize_strips_id_tags() -> None:
    """Test that LRC ID tag lines are stripped from LRC lyrics."""
    assert normalize_lrc_lyrics(LRC_WITH_ID_TAGS) == LRC_CLEAN


def test_normalize_no_changes_needed() -> None:
    """Test that already normalized LRC lyrics are returned unmodified."""
    assert normalize_lrc_lyrics(LRC_CLEAN) == LRC_CLEAN


def test_normalize_preserves_untimed_and_blank_lines() -> None:
    """Test that untimed lines and blank lines between timed lines are preserved."""
    lrc = "[00:01.00]First line\n\n[00:05.00]Second line\nuntimed line"
    assert normalize_lrc_lyrics(lrc) == lrc


def test_normalize_empty_input() -> None:
    """Test that None and empty strings pass through unmodified."""
    assert normalize_lrc_lyrics(None) is None
    assert normalize_lrc_lyrics("") == ""


def test_normalize_only_id_tags() -> None:
    """Test that lyrics consisting of only ID tags result in None."""
    assert normalize_lrc_lyrics("[ar:Some Artist]\n[ti:Some Title]") is None


def test_normalize_expands_repeated_lines() -> None:
    """Test that lines with multiple timestamps are expanded into one line per timestamp."""
    lrc = "[00:21.10][00:45.10]Repeating lyrics (e.g. chorus)"
    assert normalize_lrc_lyrics(lrc) == (
        "[00:21.10]Repeating lyrics (e.g. chorus)\n[00:45.10]Repeating lyrics (e.g. chorus)"
    )


def test_normalize_expands_and_sorts_within_line() -> None:
    """Test that expanded repeating lines sort chronologically with surrounding lines."""
    lrc = "[01:45][00:21.10] [00:33:99]Chorus\n[01:00]Second line"
    assert normalize_lrc_lyrics(lrc) == (
        "[00:21.10]Chorus\n[00:33:99]Chorus\n[01:00]Second line\n[01:45]Chorus"
    )


def test_normalize_strips_word_timing_tags() -> None:
    """Test that (unsupported) enhanced LRC word timing tags are stripped."""
    lrc = "[00:12.00]<00:12.10>Lyrics <00:12.50>with <00:13.00>word timing"
    assert normalize_lrc_lyrics(lrc) == "[00:12.00]Lyrics with word timing"


def test_normalize_full_file_with_repeats() -> None:
    """Test a full LRC file with ID tags, repeated lines and enhanced word timing tags."""
    lrc = (
        "[ti:Some Title]\n"
        "[offset:-200]\n"
        "\n"
        "[00:12.00]First verse line\n"
        "[00:21.10][01:45.00]The chorus <00:22.00>with <00:23.00>word timing\n"
        "[00:30.00]Second verse line"
    )
    assert normalize_lrc_lyrics(lrc) == (
        "[00:12.00]First verse line\n"
        "[00:21.10]The chorus with word timing\n"
        "[00:30.00]Second verse line\n"
        "[01:45.00]The chorus with word timing"
    )


def test_normalize_single_timestamp_line_untouched() -> None:
    """Test that a single-timestamp line keeps its original formatting."""
    lrc = "[00:01.00]  spaced text  "
    assert normalize_lrc_lyrics(lrc) == lrc
