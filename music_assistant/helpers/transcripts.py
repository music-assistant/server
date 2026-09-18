"""Helpers to turn transcript documents into readable text and timed cues."""

from __future__ import annotations

import html
import json
import re
from dataclasses import replace
from typing import Any

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
_HTML_LINE_BREAK = re.compile(r"<br\s*/?>|</(?:p|div|h[1-6]|li|tr|cite|time)\s*>", re.IGNORECASE)
_BLOCK_SEPARATOR = re.compile(r"\n[ \t]*\n")
# WebVTT blocks that never contain dialogue
_NON_CUE_BLOCKS = ("WEBVTT", "NOTE", "STYLE", "REGION")
# a Podcasting 2.0 JSON transcript is often word by word, so runs of segments are joined
# until a sentence ends, capped so a stretch without punctuation still breaks into lines
_SENTENCE_END = (".", "!", "?", "\u2026")
_MAX_JOINED_SEGMENT_CHARS = 200
# speech recognition working on a rolling window repeats the tail of one cue at the start
# of the next. Short repeats are left alone because they are usually really said twice.
_MIN_REPEATED_PREFIX = 15
# a rolling window never echoes more than a sentence or so, and bounding the search keeps
# one enormous cue in a broken document from tying up the event loop
_MAX_REPEATED_PREFIX = 300
# such a repeat can only run into the cue that directly follows, in the same voice. A
# repeat after a real pause or by another speaker is someone genuinely saying it again.
_MAX_REPEAT_GAP = 1.0
# words a cue could not have been spoken in its own time span did not come from the audio.
# Fast speech runs to roughly 25 characters a second, so 40 leaves headroom for real speech.
_MAX_SPOKEN_CHARS_PER_SECOND = 40


def parse_transcript_cues(raw: str) -> list[MediaItemTranscriptCue]:
    """
    Parse a WebVTT, SubRip or Podcasting 2.0 JSON document into timed cues, in document order.

    Returns an empty list when the document carries no recognisable cues.

    :param raw: The transcript document.
    """
    document = _normalize_newlines(raw)
    if (segments := _json_segments(document)) is not None:
        return _without_repeated_text(_cues_from_json_segments(segments))
    cues: list[MediaItemTranscriptCue] = []
    for block in _BLOCK_SEPARATOR.split(document):
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

    A JSON document is never prose, so it yields an empty string.

    :param raw: The transcript document.
    """
    document = _normalize_newlines(raw)
    if _json_segments(document) is not None:
        return ""
    lines = _HTML_LINE_BREAK.sub("\n", document)
    stripped = html.unescape(_MARKUP_TAG.sub("", lines))
    return "\n".join(line for raw_line in stripped.split("\n") if (line := _collapse(raw_line)))


def _without_repeated_text(
    cues: list[MediaItemTranscriptCue],
) -> list[MediaItemTranscriptCue]:
    """Drop the text a cue repeats from the end of the one before it."""
    result: list[MediaItemTranscriptCue] = []
    for cue in cues:
        repeated = _repeated_prefix_length(result[-1], cue) if result else 0
        if repeated == len(cue.text):
            # a cue that repeats and adds nothing is kept, since the words may really be
            # said twice, unless it is too short for them to have been spoken at all
            if not _is_unspoken(cue):
                result.append(cue)
            continue
        result.append(replace(cue, text=cue.text[repeated:].lstrip()) if repeated else cue)
    return result


def _repeated_prefix_length(
    previous: MediaItemTranscriptCue, current: MediaItemTranscriptCue
) -> int:
    """Return how much of a cue's text repeats the end of the one before it."""
    if not _follows_directly(previous, current):
        return 0
    tail = previous.text[-_MAX_REPEATED_PREFIX:]
    head = current.text[:_MAX_REPEATED_PREFIX]
    probe = head[:_MIN_REPEATED_PREFIX]
    if len(probe) < _MIN_REPEATED_PREFIX:
        return 0
    # every overlap starts with the probe, so only its occurrences in the tail need checking,
    # earliest first so the longest overlap wins
    position = tail.find(probe)
    while position != -1:
        if head.startswith(tail[position:]):
            return len(tail) - position
        position = tail.find(probe, position + 1)
    return 0


def _follows_directly(previous: MediaItemTranscriptCue, current: MediaItemTranscriptCue) -> bool:
    """Whether a cue carries straight on from the one before it, in the same voice."""
    if previous.speaker != current.speaker or previous.end is None:
        return False
    return current.start - previous.end <= _MAX_REPEAT_GAP


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


def _json_segments(document: str) -> list[Any] | None:
    """Return the segments of a JSON transcript, or None when the document is not JSON."""
    if not document.startswith("{"):
        return None
    try:
        parsed = json.loads(document)
    except ValueError:
        return None
    segments = parsed.get("segments") if isinstance(parsed, dict) else None
    return segments if isinstance(segments, list) else []


def _cues_from_json_segments(segments: list[Any]) -> list[MediaItemTranscriptCue]:
    """Build cues from Podcasting 2.0 JSON segments, joining word-level ones into sentences."""
    cues: list[MediaItemTranscriptCue] = []
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        if (start := _as_seconds(segment.get("startTime"))) is None:
            continue
        if not (text := _collapse(html.unescape(str(segment.get("body") or "")))):
            continue
        end = _as_seconds(segment.get("endTime"))
        speaker = _collapse(html.unescape(str(segment.get("speaker") or ""))) or None
        if cues and _continues_sentence(cues[-1], speaker, text):
            previous = cues[-1]
            cues[-1] = replace(
                previous,
                end=previous.end if end is None else end,
                text=f"{previous.text} {text}",
            )
            continue
        cues.append(MediaItemTranscriptCue(start=start, end=end, text=text, speaker=speaker))
    return cues


def _continues_sentence(previous: MediaItemTranscriptCue, speaker: str | None, text: str) -> bool:
    """Whether a segment belongs to the sentence the previous cue left unfinished."""
    return (
        previous.speaker == speaker
        and not previous.text.endswith(_SENTENCE_END)
        and len(previous.text) + len(text) < _MAX_JOINED_SEGMENT_CHARS
    )


def _as_seconds(value: Any) -> float | None:
    """Read a JSON timing as seconds, or None when it is missing or unreadable."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except TypeError, ValueError:
        return None


def _normalize_newlines(raw: str) -> str:
    """Strip any byte order mark and normalise all line endings to newlines."""
    return raw.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n").strip()


def _collapse(value: str) -> str:
    """Collapse all runs of whitespace into single spaces."""
    return " ".join(value.split())
