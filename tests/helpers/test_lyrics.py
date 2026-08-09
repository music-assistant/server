"""Tests for the lyrics helpers."""

from music_assistant.helpers.lyrics import (
    convert_to_lrc_lyrics,
    extract_lrc_lyrics,
    normalize_lrc_lyrics,
)

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


def test_convert_single_line() -> None:
    """Test conversion of single line."""
    assert convert_to_lrc_lyrics([("Lyrics", 0)]) == "[00:00.00]Lyrics"
    assert convert_to_lrc_lyrics([("Lyrics", 1234)]) == "[00:01.23]Lyrics"
    assert convert_to_lrc_lyrics([("My lyrics", 1234)]) == "[00:01.23]My lyrics"
    assert convert_to_lrc_lyrics([("  My lyrics  ", 1000)]) == ("[00:01.00]My lyrics")
    assert convert_to_lrc_lyrics([("   ", 1000)]) == "[00:01.00]"
    assert convert_to_lrc_lyrics([]) == ""


def test_convert_single_line_special_characters() -> None:
    """Test conversion of single line with special characters."""
    assert convert_to_lrc_lyrics([("My\nlyrics", 1000)]) == ("[00:01.00]My lyrics")
    assert convert_to_lrc_lyrics([("My\rlyrics", 1000)]) == ("[00:01.00]My lyrics")
    assert convert_to_lrc_lyrics([("My\r\nlyrics", 1000)]) == ("[00:01.00]My lyrics")
    assert convert_to_lrc_lyrics([("\nMy lyrics", 1000)]) == ("[00:01.00]My lyrics")
    assert convert_to_lrc_lyrics([("My lyrics\r\n", 1000)]) == ("[00:01.00]My lyrics")


def test_convert_single_line_special_timestamps() -> None:
    """Test conversion of single line with special timestamps."""
    assert convert_to_lrc_lyrics([("One centisecond", 10)]) == ("[00:00.01]One centisecond")
    assert convert_to_lrc_lyrics([("One second", 1000)]) == ("[00:01.00]One second")
    assert convert_to_lrc_lyrics([("One minute", 60_000)]) == ("[01:00.00]One minute")
    assert convert_to_lrc_lyrics([("One hour", 3_600_000)]) == ("[60:00.00]One hour")
    assert convert_to_lrc_lyrics([("Too small", 1)]) == ("[00:00.00]Too small")
    assert convert_to_lrc_lyrics([("Truncate", 1239)]) == ("[00:01.23]Truncate")
    assert convert_to_lrc_lyrics([("Maximum", 99 * 60_000 + 59_999)]) == "[99:59.99]Maximum"
    assert convert_to_lrc_lyrics([("Too large", 100 * 60_000)]) == ""
    assert convert_to_lrc_lyrics([("Too large", 999_999_999)]) == ""
    assert convert_to_lrc_lyrics([("Invalid", -1)]) == ""


def test_convert_multiple_lines() -> None:
    """Test conversion of multiple lines."""
    lyrics = [
        ("First line", 0),
        ("Second line", 1000),
        ("Third line", 2000),
    ]

    assert convert_to_lrc_lyrics(lyrics) == (
        "[00:00.00]First line\n[00:01.00]Second line\n[00:02.00]Third line"
    )


def test_convert_multiple_lines_all_invalid() -> None:
    """Test conversion of multiple lines that are all invalid."""
    lyrics = [
        ("Negative", -100),
        ("Too large", 100 * 60_000),
    ]

    assert convert_to_lrc_lyrics(lyrics) == ""


def test_convert_multiple_lines_partially_invalid() -> None:
    """Test conversion of multiple lines that are partially invalid."""
    lyrics = [
        ("First line", 0),
        ("Negative", -1),
        ("Second line", 1000),
        ("Negative", -2),
        ("Third line", 2000),
        ("Too large", 100 * 60_000),
    ]

    assert convert_to_lrc_lyrics(lyrics) == (
        "[00:00.00]First line\n[00:01.00]Second line\n[00:02.00]Third line"
    )


def test_extract_detects_lrc_content() -> None:
    """Test that LRC formatted text in the plain lyrics tag is detected as-is."""
    assert extract_lrc_lyrics(LRC_CLEAN) == LRC_CLEAN
    assert extract_lrc_lyrics(LRC_WITH_ID_TAGS) == LRC_WITH_ID_TAGS
    assert extract_lrc_lyrics("[2:30]Short timestamp style") == "[2:30]Short timestamp style"


def test_extract_rejects_plain_lyrics() -> None:
    """Test that regular unsynced lyrics text is not detected as LRC."""
    assert extract_lrc_lyrics("Just some lyrics\nspread over\nmultiple lines") is None
    assert extract_lrc_lyrics(None) is None
    assert extract_lrc_lyrics("") is None
    assert extract_lrc_lyrics("\n\n") is None
    # ID tag lines alone do not count as synced lyrics content
    assert extract_lrc_lyrics("[ar:Some Artist]\n[ti:Some Title]") is None


def test_extract_rejects_timestamp_mention_in_text() -> None:
    """Test that a timestamp-like mention within plain lyrics does not trigger detection."""
    lyrics = (
        "First plain line\nat [2:30] the beat drops\nanother plain line\nand yet another plain line"
    )
    assert extract_lrc_lyrics(lyrics) is None


def test_extract_tolerates_some_untimed_lines() -> None:
    """Test that LRC content with a minority of untimed lines is still detected."""
    lyrics = "[00:12.00]First line\n[00:15.30]Second line\nuntimed line"
    assert extract_lrc_lyrics(lyrics) == lyrics
