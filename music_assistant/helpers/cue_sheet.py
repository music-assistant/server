"""
CUE sheet parser for Music Assistant.

Parses standard CUE sheet format into structured data.
Supports the CATALOG, FILE, TRACK, INDEX, TITLE, PERFORMER, ISRC and REM
directives, plus the non-standard top-level GENRE extension. Other standard
directives (FLAGS, PREGAP, POSTGAP, SONGWRITER, CDTEXTFILE) are accepted
but ignored.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

_LOGGER = logging.getLogger(__name__)


@dataclass
class CueTrack:
    """A single track entry from a CUE sheet."""

    number: int
    title: str | None = None
    performers: list[str] = field(default_factory=list)  # one PERFORMER line per artist
    start_position: float = 0.0  # seconds from INDEX 01
    isrcs: list[str] = field(default_factory=list)  # ISRC directive, repeated for multi-value
    sort_name: str | None = None  # REM TITLESORT
    artist_sort_names: list[str] = field(
        default_factory=list
    )  # REM ARTISTSORT, aligned by index with performers
    musicbrainz_artistids: list[str] = field(
        default_factory=list
    )  # REM MUSICBRAINZ_ARTISTID, aligned by index
    musicbrainz_recordingid: str | None = None  # REM MUSICBRAINZ_RECORDINGID
    musicbrainz_releasetrackid: str | None = (
        None  # REM MUSICBRAINZ_TRACKID (matches Picard's %musicbrainz_trackid%)
    )
    copyright: str | None = None  # REM COPYRIGHT
    grouping: str | None = None  # REM GROUPING
    comment: str | None = None  # REM COMMENT → metadata.description
    explicit: bool | None = None  # REM ITUNESADVISORY ("1"/"0")
    genres: list[str] = field(default_factory=list)  # REM GENRE inside a TRACK block, multi-line


@dataclass
class CueSheet:
    """Parsed CUE sheet data."""

    file_path: str | None = None  # referenced audio file (None for embedded CUE)
    title: str | None = None  # album title
    performers: list[str] = field(default_factory=list)  # album artists, one PERFORMER line each
    sort_title: str | None = None  # REM ALBUMSORT
    album_artist_sort_names: list[str] = field(
        default_factory=list
    )  # REM ALBUMARTISTSORT, aligned by index
    musicbrainz_albumartistids: list[str] = field(
        default_factory=list
    )  # REM MUSICBRAINZ_ALBUMARTISTID, aligned by index
    date: str | None = None  # REM DATE
    genres: list[str] = field(default_factory=list)  # REM GENRE at sheet level
    album_types: list[str] = field(
        default_factory=list
    )  # REM RELEASETYPE (e.g. "album", "compilation")
    barcode: str | None = None  # CATALOG directive (UPC/EAN)
    musicbrainz_albumid: str | None = None  # REM MUSICBRAINZ_ALBUMID
    musicbrainz_releasegroupid: str | None = None  # REM MUSICBRAINZ_RELEASEGROUPID
    tracks: list[CueTrack] = field(default_factory=list)


def _parse_timestamp(timestamp: str) -> float:
    """
    Convert CUE timestamp (MM:SS:FF) to seconds.

    :param timestamp: CUE format timestamp where FF = frames at 75fps.
    """
    match = re.match(r"(\d+):(\d+):(\d+)", timestamp)
    if not match:
        _LOGGER.warning("Invalid CUE timestamp %r, treating as 0", timestamp)
        return 0.0
    minutes, seconds, frames = int(match.group(1)), int(match.group(2)), int(match.group(3))
    return minutes * 60.0 + seconds + frames / 75.0


def _unquote(value: str) -> str:
    """Remove surrounding quotes from a CUE value."""
    value = value.strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1]
    return value


def parse_cue_sheet(cue_content: str) -> CueSheet:
    """
    Parse CUE sheet content into structured data.

    :param cue_content: The raw text content of a CUE sheet.
    """
    sheet = CueSheet()
    current_track: CueTrack | None = None

    for raw_line in cue_content.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        upper_line = line.upper()

        if upper_line.startswith("REM "):
            _parse_rem_line(line, sheet, current_track)

        elif upper_line.startswith("PERFORMER "):
            value = _unquote(line[10:])
            if current_track is not None:
                current_track.performers.append(value)
            else:
                sheet.performers.append(value)

        elif upper_line.startswith("TITLE "):
            value = _unquote(line[6:])
            if current_track is not None:
                current_track.title = value
            else:
                sheet.title = value

        elif upper_line.startswith("FILE "):
            # FILE "filename.flac" WAVE
            # extract filename between quotes, ignore type
            match = re.match(r'FILE\s+"([^"]+)"', line, re.IGNORECASE)
            if match:
                sheet.file_path = match.group(1)
            else:
                # handle unquoted filename: FILE filename.flac WAVE
                parts = line.split(None, 2)
                if len(parts) >= 2:
                    sheet.file_path = parts[1]

        elif upper_line.startswith("TRACK "):
            # TRACK 01 AUDIO
            match = re.match(r"TRACK\s+(\d+)", line, re.IGNORECASE)
            if match:
                current_track = CueTrack(number=int(match.group(1)))
                sheet.tracks.append(current_track)

        elif upper_line.startswith("INDEX "):
            if current_track is None:
                continue
            # INDEX 01 MM:SS:FF, use INDEX 01 as track start
            match = re.match(r"INDEX\s+(\d+)\s+(\d+:\d+:\d+)", line, re.IGNORECASE)
            if match and match.group(1) == "01":
                current_track.start_position = _parse_timestamp(match.group(2))

        elif upper_line.startswith("ISRC "):
            if current_track is not None:
                current_track.isrcs.append(_unquote(line[5:]))

        elif upper_line.startswith("CATALOG "):
            # disc-level UPC/EAN, only valid outside a TRACK block
            if current_track is None:
                sheet.barcode = _unquote(line[8:])

        elif upper_line.startswith("GENRE "):
            # non-standard CD-Text extension (cuetools, foobar2000); same landing
            # as REM GENRE so tools using either form produce the same result
            target = current_track.genres if current_track is not None else sheet.genres
            target.append(_unquote(line[6:]))

    return sheet


def _parse_rem_line(line: str, sheet: CueSheet, current_track: CueTrack | None) -> None:
    """
    Parse a REM line for metadata.

    :param line: The full REM line.
    :param sheet: The CueSheet being built.
    :param current_track: The current track context, if any.
    """
    # REM KEY VALUE, split into at most 3 parts
    parts = line.split(None, 2)
    if len(parts) < 3:
        return

    key = parts[1].upper()
    value = _unquote(parts[2])

    # sheet-level directives (written outside any TRACK block)
    if current_track is None:
        if key == "DATE":
            sheet.date = value
        elif key == "GENRE":
            sheet.genres.append(value)
        elif key == "MUSICBRAINZ_ALBUMID":
            sheet.musicbrainz_albumid = value
        elif key == "MUSICBRAINZ_RELEASEGROUPID":
            sheet.musicbrainz_releasegroupid = value
        elif key == "MUSICBRAINZ_ALBUMARTISTID":
            sheet.musicbrainz_albumartistids.append(value)
        elif key == "ALBUMSORT":
            sheet.sort_title = value
        elif key == "ALBUMARTISTSORT":
            sheet.album_artist_sort_names.append(value)
        elif key == "RELEASETYPE":
            sheet.album_types.append(value)
        return

    # track-level (inside a TRACK block)
    if key == "GENRE":
        current_track.genres.append(value)
    elif key == "MUSICBRAINZ_RECORDINGID":
        current_track.musicbrainz_recordingid = value
    elif key == "MUSICBRAINZ_TRACKID":
        # matches Picard's %musicbrainz_trackid% variable (release-track MBID)
        current_track.musicbrainz_releasetrackid = value
    elif key == "MUSICBRAINZ_ARTISTID":
        current_track.musicbrainz_artistids.append(value)
    elif key == "ARTISTSORT":
        current_track.artist_sort_names.append(value)
    elif key == "TITLESORT":
        current_track.sort_name = value
    elif key == "COPYRIGHT":
        current_track.copyright = value
    elif key == "GROUPING":
        current_track.grouping = value
    elif key == "COMMENT":
        current_track.comment = value
    elif key == "ITUNESADVISORY":
        current_track.explicit = value == "1"
