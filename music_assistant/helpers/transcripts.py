"""Helpers to turn transcript documents into readable text and timed cues."""

from __future__ import annotations

import html
import re
from dataclasses import replace

from music_assistant_models.media_items import MediaItemTranscriptCue

from music_assistant.helpers.util import try_parse_duration

# WebVTT writes [HH:]MM:SS.mmm and SubRip writes HH:MM:SS,mmm, and documents in the wild
# are loose about zero padding, so accept either separator and an optional hour part
_CUE_TIMINGS = re.compile(
    r"^(\d{1,4}:(?:\d{1,2}:)?\d{1,2}[.,]\d{1,3})\s*-->\s*(\d{1,4}:(?:\d{1,2}:)?\d{1,2}[.,]\d{1,3})"
)
# a WebVTT voice span names the speaker and may carry classes, as in <v.loud Jane Doe>.
# The class itself cannot contain a dot, and excluding it keeps the repetition
# unambiguous so a malformed document cannot send the match exponential.
_VOICE_TAG = re.compile(r"<v(?:\.[^\s.>]+)*\s+([^>]+)>")
_MARKUP_TAG = re.compile(r"</?[^>]*>")
# html block boundaries, which read as a line break where a cue timing is not available
_HTML_LINE_BREAK = re.compile(r"<br\s*/?>|</(?:p|div|h[1-6]|li|tr)\s*>", re.IGNORECASE)
_BLOCK_SEPARATOR = re.compile(r"\n[ \t]*\n")
# WebVTT blocks that never contain dialogue
_NON_CUE_BLOCKS = ("WEBVTT", "NOTE", "STYLE", "REGION")
# speech recognition working on a rolling window repeats the tail of one cue at the start
# of the next. Short repeats are left alone because they are usually really said twice.
_MIN_REPEATED_PREFIX = 15
# words a cue could not have been spoken in its own time span did not come from the audio.
# Fast speech runs to roughly 25 characters a second.
_MAX_SPOKEN_CHARS_PER_SECOND = 40


def parse_transcript_cues(raw: str) -> list[MediaItemTranscriptCue]:
    """
    Parse a WebVTT or SubRip document into timed cues, in document order.

    Returns an empty list when the document carries no recognisable cues.

    :param raw: The transcript document.
    """
    cues: list[MediaItemTranscriptCue] = []
    for block in _BLOCK_SEPARATOR.split(_normalize_newlines(raw)):
        lines = block.strip().splitlines()
        if not lines or lines[0].split(maxsplit=1)[0] in _NON_CUE_BLOCKS:
            continue
        # the timings are preceded by an optional cue identifier or SubRip sequence number
        for index, line in enumerate(lines):
            if not (timings := _CUE_TIMINGS.match(line.strip())):
                continue
            if cue := _build_cue(timings, lines[index + 1 :]):
                cues.append(cue)
            break
    return _without_repeated_text(cues)


def cues_to_text(cues: list[MediaItemTranscriptCue]) -> str:
    """
    Render timed cues as readable text, naming each speaker as they take over.

    :param cues: The cues to render, in playback order.
    """
    lines: list[str] = []
    previous_speaker: str | None = None
    for cue in cues:
        names_speaker = cue.speaker is not None and cue.speaker != previous_speaker
        lines.append(f"{cue.speaker}: {cue.text}" if names_speaker else cue.text)
        previous_speaker = cue.speaker
    return "\n".join(lines)


def document_to_text(raw: str) -> str:
    """
    Render an untimed transcript document (plain text or HTML) as readable text.

    :param raw: The transcript document.
    """
    lines = _HTML_LINE_BREAK.sub("\n", _normalize_newlines(raw))
    stripped = html.unescape(_MARKUP_TAG.sub("", lines))
    return "\n".join(line for raw_line in stripped.split("\n") if (line := _collapse(raw_line)))


def _without_repeated_text(
    cues: list[MediaItemTranscriptCue],
) -> list[MediaItemTranscriptCue]:
    """Drop the text a cue repeats from the end of the one before it."""
    result: list[MediaItemTranscriptCue] = []
    for cue in cues:
        repeated = _repeated_prefix_length(result[-1].text, cue.text) if result else 0
        if repeated == len(cue.text):
            # a cue that repeats and adds nothing is kept, since the words may really be
            # said twice, unless it is too short for them to have been spoken at all
            if not _is_unspoken(cue):
                result.append(cue)
            continue
        result.append(replace(cue, text=cue.text[repeated:].lstrip()) if repeated else cue)
    return result


def _repeated_prefix_length(previous: str, current: str) -> int:
    """Return how much of the current text repeats the end of the previous one."""
    for length in range(min(len(previous), len(current)), _MIN_REPEATED_PREFIX - 1, -1):
        if previous[-length:] == current[:length]:
            return length
    return 0


def _is_unspoken(cue: MediaItemTranscriptCue) -> bool:
    """Whether a cue holds more words than its own time span could carry."""
    if cue.end is None:
        return False
    duration: float = cue.end - cue.start
    return duration > 0 and len(cue.text) / duration > _MAX_SPOKEN_CHARS_PER_SECOND


def _build_cue(timings: re.Match[str], text_lines: list[str]) -> MediaItemTranscriptCue | None:
    """Build a cue from its timings line and the text lines that follow it."""
    if not (text := _collapse(html.unescape(_MARKUP_TAG.sub("", " ".join(text_lines))))):
        return None
    speaker = None
    if voice := _VOICE_TAG.search(text_lines[0]):
        speaker = _collapse(html.unescape(voice.group(1))) or None
    return MediaItemTranscriptCue(
        start=try_parse_duration(timings.group(1)),
        end=try_parse_duration(timings.group(2)),
        text=text,
        speaker=speaker,
    )


def _normalize_newlines(raw: str) -> str:
    """Strip any byte order mark and normalise all line endings to newlines."""
    return raw.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n").strip()


def _collapse(value: str) -> str:
    """Collapse all runs of whitespace into single spaces."""
    return " ".join(value.split())
