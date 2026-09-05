"""Tests for the transcript helpers."""

from music_assistant.helpers.transcripts import (
    cues_to_text,
    document_to_text,
    parse_transcript_cues,
)

WEBVTT = """WEBVTT - An episode title

NOTE
This comment is not a cue.

00:00.000 --> 00:04.680
The biggest story in Britain today

1
00:04.980 --> 00:07.320
<v Jane Doe>Were you silent or were you
silenced?

00:08.420 --> 00:12.720
<v Jane Doe>Still Jane speaking.

00:12.980 --> 00:16.060
<v.loud John Smith>Tom &amp; Jerry said <b>no</b>.
"""

SUBRIP = """1
00:00:05,359 --> 00:00:11,439
You're listening to a podcast.

2
01:02:03,500 --> 01:02:04,000
An hour in.
"""


def test_parses_webvtt_cues() -> None:
    """Test that WebVTT cues are parsed with their timings, text and speaker."""
    cues = parse_transcript_cues(WEBVTT)
    assert len(cues) == 4
    assert cues[0].start == 0.0
    assert cues[0].end == 4.68
    assert cues[0].text == "The biggest story in Britain today"
    assert cues[0].speaker is None


def test_parses_webvtt_voice_tags() -> None:
    """Test that a voice span sets the speaker and is kept out of the cue text."""
    cues = parse_transcript_cues(WEBVTT)
    assert cues[1].speaker == "Jane Doe"
    assert cues[1].text == "Were you silent or were you silenced?"
    assert cues[3].speaker == "John Smith"


def test_parses_webvtt_markup_and_entities() -> None:
    """Test that inline markup is dropped and html entities are decoded."""
    assert parse_transcript_cues(WEBVTT)[3].text == "Tom & Jerry said no."


def test_skips_non_cue_blocks() -> None:
    """Test that the header and comment blocks yield no cues."""
    assert all("comment" not in cue.text for cue in parse_transcript_cues(WEBVTT))


def test_parses_subrip_cues() -> None:
    """Test that SubRip sequence numbers and comma separated timings are handled."""
    cues = parse_transcript_cues(SUBRIP)
    assert len(cues) == 2
    assert cues[0].start == 5.359
    assert cues[0].text == "You're listening to a podcast."
    assert cues[1].start == 3723.5


def test_parses_carriage_returns_and_byte_order_mark() -> None:
    """Test that a document with CRLF line endings and a BOM still parses."""
    assert len(parse_transcript_cues("﻿" + SUBRIP.replace("\n", "\r\n"))) == 2


def test_returns_no_cues_for_untimed_document() -> None:
    """Test that a document without timings yields no cues."""
    assert parse_transcript_cues("<p>Just some prose.</p>") == []


def test_cues_to_text_names_each_new_speaker() -> None:
    """Test that a speaker is named on taking over and not repeated per cue."""
    assert cues_to_text(parse_transcript_cues(WEBVTT)) == (
        "The biggest story in Britain today\n"
        "Jane Doe: Were you silent or were you silenced?\n"
        "Still Jane speaking.\n"
        "John Smith: Tom & Jerry said no."
    )


def test_document_to_text_strips_markup() -> None:
    """Test that an html document is rendered as readable text."""
    assert document_to_text("<p>First line.</p>\n<p>Second  &amp; last.</p>") == (
        "First line.\nSecond & last."
    )


def test_document_to_text_drops_blank_lines() -> None:
    """Test that blank and whitespace only lines are dropped."""
    assert document_to_text("first\n\n   \nsecond") == "first\nsecond"


# real cue pairs from generated Pocket Casts and publisher transcripts
ROLLING_WINDOW = """WEBVTT

00:10.000 --> 00:15.000
the radius of the Earth is not a rounding error. It's so big.

00:15.000 --> 00:20.000
of the Earth is not a rounding error. It's so big. Really, what we would be building
"""

REPEATED_CUE = """WEBVTT

00:10.000 --> 00:12.000
Still look great.

00:12.000 --> 00:14.000
Still look great.
"""

UNSPOKEN_REPEAT = """WEBVTT

03:18.540 --> 03:24.040
before Neanderthals even existed, to a 1.7 million year old foot bone.

03:24.040 --> 03:24.260
to a 1.7 million year old foot bone.
"""

SHORT_REPEAT = """WEBVTT

00:10.000 --> 00:12.000
and it goes ping ping

00:12.000 --> 00:14.000
ping ping pin pin and he has the phomo.
"""


def test_trims_text_repeated_from_the_previous_cue() -> None:
    """Test that a rolling-window transcriber's repeated lead-in is dropped."""
    cues = parse_transcript_cues(ROLLING_WINDOW)
    assert cues[1].text == "Really, what we would be building"


def test_keeps_a_cue_that_only_repeats() -> None:
    """Test that a cue adding nothing is kept, since the words may really be said twice."""
    cues = parse_transcript_cues(REPEATED_CUE)
    assert [cue.text for cue in cues] == ["Still look great.", "Still look great."]


def test_drops_a_repeat_too_short_to_have_been_spoken() -> None:
    """Test that a repeat crammed into a fraction of a second is dropped."""
    cues = parse_transcript_cues(UNSPOKEN_REPEAT)
    assert [cue.text for cue in cues] == [
        "before Neanderthals even existed, to a 1.7 million year old foot bone."
    ]


def test_keeps_a_short_repeat() -> None:
    """Test that a short repeat is left alone, since it is usually real speech."""
    cues = parse_transcript_cues(SHORT_REPEAT)
    assert cues[1].text == "ping ping pin pin and he has the phomo."


def test_trimming_never_empties_a_cue() -> None:
    """Test that trimming always leaves text behind."""
    for document in (ROLLING_WINDOW, REPEATED_CUE, SHORT_REPEAT, UNSPOKEN_REPEAT):
        assert all(cue.text.strip() for cue in parse_transcript_cues(document))


def test_parses_a_voice_tag_carrying_classes() -> None:
    """Test that a voice span with classes still names the speaker."""
    document = "WEBVTT\n\n00:00.000 --> 00:02.000\n<v.loud.first Jane Doe>Hello there.\n"
    assert parse_transcript_cues(document)[0].speaker == "Jane Doe"


def test_malformed_voice_tag_does_not_stall() -> None:
    """Test that an unterminated voice span is rejected quickly rather than backtracking."""
    document = "WEBVTT\n\n00:00.000 --> 00:02.000\n<v" + ".!" * 40 + "\n"
    assert parse_transcript_cues(document)[0].speaker is None
