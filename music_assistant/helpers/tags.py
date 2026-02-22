"""Helpers/utilities to parse tags from audio files with ffmpeg and mutagen."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass
from json import JSONDecodeError
from typing import Any

import mutagen
from music_assistant_models.enums import AlbumType
from music_assistant_models.errors import InvalidDataError
from mutagen._vorbis import VCommentDict
from mutagen.apev2 import APEv2
from mutagen.mp4 import MP4Tags

from music_assistant.constants import MASS_LOGGER_NAME, UNKNOWN_ARTIST
from music_assistant.helpers.json import json_loads
from music_assistant.helpers.process import AsyncProcess
from music_assistant.helpers.util import infer_album_type, try_parse_int

LOGGER = logging.getLogger(f"{MASS_LOGGER_NAME}.tags")


# the only multi-item splitter we accept is the semicolon,
# which is also the default in Musicbrainz Picard.
# the slash is also a common splitter but causes collisions with
# artists actually containing a slash in the name, such as AC/DC
TAG_SPLITTER = ";"


def clean_tuple(values: Iterable[str]) -> tuple[str, ...]:
    """Return a tuple with all empty values removed."""
    return tuple(x.strip() for x in values if x not in (None, "", " "))


def split_items(
    org_str: str | list[str] | tuple[str, ...] | None, allow_unsafe_splitters: bool = False
) -> tuple[str, ...]:
    """Split up a tags string by common splitter."""
    if org_str is None:
        return ()
    if isinstance(org_str, tuple | list):
        final_items: list[str] = []
        for item in org_str:
            final_items.extend(split_items(item, allow_unsafe_splitters))
        return tuple(final_items)
    org_str = org_str.strip()
    if TAG_SPLITTER in org_str:
        return clean_tuple(org_str.split(TAG_SPLITTER))
    if allow_unsafe_splitters and "/" in org_str:
        return clean_tuple(org_str.split("/"))
    if allow_unsafe_splitters and ", " in org_str:
        return clean_tuple(org_str.split(", "))
    return clean_tuple((org_str,))


# Artist splitting logic:
# When not using the multi-artist tag (ARTISTS), the artist string may contain
# multiple artists in freeform. Featuring artists may also be included in this
# string. We parse and separate them based on common splitter patterns.
#
# We use the MusicBrainz Artist ID count as a guide for how many artists to extract:
# - 0 IDs: only split on "featuring" splitters (to capture feat. artists in DB)
# - 1 ID: don't split at all (single artist confirmed)
# - 2+ IDs: split on featuring first, then extra splitters until we reach the target count
#
# TODO: If a MusicBrainz mirror/local database was available, artist names could be
# looked up directly using the MB Artist IDs from the tags, eliminating the need for
# ARTISTS tag parsing or ARTIST tag splitting entirely.
#
# Featuring splitters - always split on these to capture featuring artists in the database
FEATURING_SPLITTERS = [
    " featuring ",
    " Featuring ",
    " feat. ",
    " Feat. ",
    " feat ",
    " Feat ",
    " duet with ",
    " Duet With ",
    " ft. ",
    " Ft. ",
    " vs. ",
    " Vs. ",
]

# Extra splitters - only use these when we have MB ID evidence of multiple artists
EXTRA_SPLITTERS = [" & ", ", ", " + ", " with ", " With "]


def _split_on_featuring(item: str) -> list[str]:
    """Split a string on featuring splitters, returns list of parts."""
    for splitter in FEATURING_SPLITTERS:
        if splitter in item:
            parts = []
            for subitem in item.split(splitter):
                clean_item = subitem.strip()
                if clean_item:
                    # Recursively process each part for nested featuring splitters
                    parts.extend(_split_on_featuring(clean_item))
            return parts
    return [item]


def _split_to_target_count(
    artists: list[str],
    expected_count: int,
    org_artists: str | tuple[str, ...],
) -> list[str]:
    """Split artists on extra splitters to reach expected count.

    :param artists: List of artists after featuring splits.
    :param expected_count: Target number of artists.
    :param org_artists: Original input for logging.
    """
    current_artists = list(artists)
    stopped_early = False

    # Keep iterating until we reach the target or can't split anymore
    while len(current_artists) < expected_count:
        made_progress = False

        for i, item in enumerate(current_artists):
            if len(current_artists) >= expected_count:
                break

            for splitter in EXTRA_SPLITTERS:
                if splitter not in item:
                    continue

                parts = [p.strip() for p in item.split(splitter) if p.strip()]
                if len(parts) <= 1:
                    continue

                potential_count = len(current_artists) - 1 + len(parts)

                if potential_count <= expected_count:
                    # Safe to split fully - replace item with its parts
                    current_artists = current_artists[:i] + parts + current_artists[i + 1 :]
                    made_progress = True
                else:
                    # Splitting would exceed target - do partial split
                    needed = expected_count - len(current_artists) + 1
                    if needed >= 2:
                        new_parts = [*parts[: needed - 1], splitter.join(parts[needed - 1 :])]
                        current_artists = current_artists[:i] + new_parts + current_artists[i + 1 :]
                        made_progress = True
                        stopped_early = True
                break  # Only use first matching splitter for this item

            if made_progress:
                break  # Restart the outer loop with updated list

        if not made_progress:
            break  # No more splitting possible

    # Remove duplicates while preserving order
    seen: set[str] = set()
    final_artists = []
    for artist in current_artists:
        if artist and artist not in seen:
            seen.add(artist)
            final_artists.append(artist)

    if stopped_early:
        LOGGER.warning(
            "Artist splitting stopped early to match expected count %d: '%s'",
            expected_count,
            org_artists,
        )
    elif len(final_artists) < expected_count:
        LOGGER.warning(
            "Could not split artist string to match expected count %d (got %d): '%s'",
            expected_count,
            len(final_artists),
            org_artists,
        )

    return final_artists


def split_artists(
    org_artists: str | tuple[str, ...],
    expected_count: int | None = None,
) -> tuple[str, ...]:
    """Parse artists from a string, guided by expected artist count.

    :param org_artists: The artist string or tuple of strings to parse.
    :param expected_count: Expected number of artists (typically from MB artist IDs).
        If None or 0: only split on "featuring" splitters, no extra splitting.
        If 1: return as-is without any splitting.
        If > 1: split on featuring splitters first, then extra splitters to reach target.
    """
    artists = split_items(org_artists, allow_unsafe_splitters=False)

    # If expected_count is 1, return as-is without any splitting
    if expected_count == 1:
        return artists

    # Step 1: Always split on featuring splitters
    final_artists: list[str] = []
    for item in artists:
        for part in _split_on_featuring(item):
            if part and part not in final_artists:
                final_artists.append(part)

    # Step 2: If no expected_count or already at/above target, we're done
    if not expected_count or expected_count <= 1 or len(final_artists) >= expected_count:
        return tuple(final_artists) if final_artists else artists

    # Step 3: Need more artists - split on extra splitters to reach expected_count
    final_artists = _split_to_target_count(final_artists, expected_count, org_artists)

    return tuple(final_artists) if final_artists else artists


@dataclass
class AudioTagsChapter:
    """Chapter data from an audio file."""

    chapter_id: int
    position_start: float
    position_end: float
    title: str | None


@dataclass
class AudioTags:
    """Audio metadata parsed from an audio file."""

    raw: dict[str, Any]
    sample_rate: int
    channels: int
    bits_per_sample: int
    format: str
    bit_rate: int | None
    duration: float | None
    tags: dict[str, str]
    has_cover_image: bool
    filename: str

    @property
    def title(self) -> str:
        """Return title tag (as-is)."""
        if tag := self.tags.get("title"):
            return tag
        # fallback to parsing from filename
        title = self.filename.rsplit(os.sep, 1)[-1].split(".")[0]
        if " - " in title:
            title_parts = title.split(" - ")
            if len(title_parts) >= 2:
                return title_parts[1].strip()
        return title

    @property
    def version(self) -> str:
        """Return version tag (as-is)."""
        if tag := self.tags.get("version"):
            return tag
        album_type_tag = (
            self.tags.get("musicbrainzalbumtype")
            or self.tags.get("albumtype")
            or self.tags.get("releasetype")
        )
        if album_type_tag and "live" in album_type_tag.lower():
            # yes, this can happen
            return "Live"
        return ""

    @property
    def album(self) -> str | None:
        """Return album tag (as-is) if present."""
        return self.tags.get("album")

    @property
    def artists(self) -> tuple[str, ...]:
        """Return track artists."""
        # prefer multi-artist tag (ARTISTS plural)
        if tag := self.tags.get("artists"):
            artists = split_items(tag)
            # Warn if ARTISTS tag count doesn't match MB Artist ID count
            mb_id_count = len(self.musicbrainz_artistids)
            if mb_id_count and mb_id_count != len(artists):
                LOGGER.warning(
                    "ARTISTS tag count (%d) does not match MusicBrainz Artist ID count (%d): %s",
                    len(artists),
                    mb_id_count,
                    tag,
                )
            return artists
        # fallback to regular artist string
        if tag := self.tags.get("artist"):
            if TAG_SPLITTER in tag:
                return split_items(tag)
            # Use MB artist ID count to guide splitting
            # - 0 IDs: only split on "feat." etc., not on "&" or ","
            # - 1 ID: don't split at all
            # - 2+ IDs: split to match the expected count
            mb_id_count = len(self.musicbrainz_artistids)
            return split_artists(tag, expected_count=mb_id_count if mb_id_count else None)
        # fallback to parsing from filename
        title = self.filename.rsplit(os.sep, 1)[-1].split(".")[0]
        if " - " in title:
            title_parts = title.split(" - ")
            if len(title_parts) >= 2:
                # No MB IDs from filename, only split on featuring splitters
                return split_artists(title_parts[0], expected_count=None)
        return (UNKNOWN_ARTIST,)

    @property
    def writers(self) -> tuple[str, ...]:
        """Return writer(s)."""
        # prefer multi-item tag
        if tag := self.tags.get("writers"):
            return split_items(tag)
        # fallback to regular writer string
        if tag := self.tags.get("writer"):
            if TAG_SPLITTER in tag:
                return split_items(tag)
            # No MB IDs for writers, only split on featuring splitters
            return split_artists(tag, expected_count=None)
        return ()

    @property
    def album_artists(self) -> tuple[str, ...]:
        """Return (all) album artists (if any)."""
        # prefer multi-artist tag (ALBUMARTISTS plural)
        if tag := self.tags.get("albumartists"):
            artists = split_items(tag)
            # Warn if ALBUMARTISTS tag count doesn't match MB Album Artist ID count
            mb_id_count = len(self.musicbrainz_albumartistids)
            if mb_id_count and mb_id_count != len(artists):
                LOGGER.warning(
                    "ALBUMARTISTS tag count (%d) does not match MusicBrainz Album Artist ID "
                    "count (%d): %s",
                    len(artists),
                    mb_id_count,
                    tag,
                )
            return artists
        # fallback to regular album artist string
        if tag := self.tags.get("albumartist"):
            if TAG_SPLITTER in tag:
                return split_items(tag)
            # Use MB album artist ID count to guide splitting
            mb_id_count = len(self.musicbrainz_albumartistids)
            return split_artists(tag, expected_count=mb_id_count if mb_id_count else None)
        return ()

    @property
    def genres(self) -> tuple[str, ...]:
        """Return (all) genres, if any."""
        return split_items(self.tags.get("genre"))

    @property
    def disc(self) -> int | None:
        """Return disc tag if present."""
        if tag := self.tags.get("disc"):
            return try_parse_int(tag.split("/")[0], None)
        return None

    @property
    def track(self) -> int | None:
        """Return track tag if present."""
        if tag := self.tags.get("track"):
            return try_parse_int(tag.split("/")[0], None)
        # fallback to parsing from filename (if present)
        # this can be in the form of 01 - title.mp3
        # or 01-title.mp3
        # or 01.title.mp3
        # or 01 title.mp3
        # or 1. title.mp3
        filename = self.filename.rsplit(os.sep, 1)[-1].split(".")[0]
        for splitpos in (4, 3, 2, 1):
            firstpart = filename[:splitpos].strip()
            if firstpart.isnumeric():
                return try_parse_int(firstpart, None)
        # fallback to parsing from last part of filename (if present)
        # this can be in the form of title 01.mp3
        lastpart = filename.split(" ")[-1]
        if lastpart.isnumeric():
            return try_parse_int(lastpart, None)
        return None

    @property
    def year(self) -> int | None:
        """Return album's year if present, parsed from date."""
        if tag := self.tags.get("originalyear"):
            return try_parse_int(tag.split("-")[0], None)
        if tag := self.tags.get("originaldate"):
            return try_parse_int(tag.split("-")[0], None)
        if tag := self.tags.get("date"):
            return try_parse_int(tag.split("-")[0], None)
        return None

    @property
    def musicbrainz_artistids(self) -> tuple[str, ...]:
        """Return musicbrainz_artistid tag(s) if present."""
        return split_items(self.tags.get("musicbrainzartistid"), True)

    @property
    def musicbrainz_albumartistids(self) -> tuple[str, ...]:
        """Return musicbrainz_albumartistid tag if present."""
        if tag := self.tags.get("musicbrainzalbumartistid"):
            return split_items(tag, True)
        return split_items(self.tags.get("musicbrainzreleaseartistid"), True)

    @property
    def musicbrainz_releasegroupid(self) -> str | None:
        """Return musicbrainz_releasegroupid tag if present."""
        return self.tags.get("musicbrainzreleasegroupid")

    @property
    def musicbrainz_albumid(self) -> str | None:
        """Return musicbrainz_albumid tag if present."""
        return self.tags.get("musicbrainzreleaseid", self.tags.get("musicbrainzalbumid"))

    @property
    def musicbrainz_recordingid(self) -> str | None:
        """Return musicbrainz_recordingid tag if present."""
        if tag := self.tags.get("UFID:http://musicbrainz.org"):
            return tag
        if tag := self.tags.get("musicbrainz.org"):
            return tag
        if tag := self.tags.get("musicbrainzrecordingid"):
            return tag
        return self.tags.get("musicbrainztrackid")

    @property
    def title_sort(self) -> str | None:
        """Return sort title tag (if exists)."""
        if tag := self.tags.get("titlesort"):
            return tag
        return None

    @property
    def album_sort(self) -> str | None:
        """Return album sort title tag (if exists)."""
        if tag := self.tags.get("albumsort"):
            return tag
        return None

    @property
    def artist_sort_names(self) -> tuple[str, ...]:
        """Return artist sort name tag(s) if present."""
        return split_items(self.tags.get("artistsort"), False)

    @property
    def album_artist_sort_names(self) -> tuple[str, ...]:
        """Return artist sort name tag(s) if present."""
        return split_items(self.tags.get("albumartistsort"), False)

    @property
    def album_type(self) -> AlbumType:
        """Return albumtype tag if present."""
        if self.tags.get("compilation", "") == "1":
            return AlbumType.COMPILATION

        tag = (
            self.tags.get("musicbrainzalbumtype")
            or self.tags.get("albumtype")
            or self.tags.get("releasetype")
        )

        if tag is not None:
            # try to parse one in order of preference
            for album_type in (
                AlbumType.LIVE,
                AlbumType.SOUNDTRACK,
                AlbumType.COMPILATION,
                AlbumType.EP,
                AlbumType.SINGLE,
                AlbumType.ALBUM,
            ):
                if album_type.value in tag.lower():
                    return album_type

        # No valid tag found, try inference from album title
        album_title = self.tags.get("album", "")
        return infer_album_type(album_title, "")

    @property
    def isrc(self) -> tuple[str, ...]:
        """Return isrc tag(s)."""
        for tag_name in ("isrc", "tsrc"):
            if tag := self.tags.get(tag_name):
                # sometimes the field contains multiple values
                return split_items(tag, True)
        return ()

    @property
    def barcode(self) -> str | None:
        """Return barcode (upc/ean) tag(s)."""
        for tag_name in ("barcode", "upc", "ean"):
            if tag := self.tags.get(tag_name):
                # sometimes the field contains multiple values
                # we only need one
                for item in split_items(tag, True):
                    if len(item) == 12:
                        # convert UPC barcode to EAN-13
                        return f"0{item}"
                    return item
        return None

    @property
    def chapters(self) -> list[AudioTagsChapter]:
        """Return chapters in MediaItem (if any)."""
        chapters: list[AudioTagsChapter] = []
        if raw_chapters := self.raw.get("chapters"):
            for chapter_data in raw_chapters:
                chapters.append(
                    AudioTagsChapter(
                        chapter_id=chapter_data["id"],
                        position_start=chapter_data["start_time"],
                        position_end=chapter_data["end_time"],
                        title=chapter_data.get("tags", {}).get("title"),
                    )
                )
        return chapters

    @property
    def lyrics(self) -> str | None:
        """Return lyrics tag (if exists)."""
        for key, value in self.tags.items():
            if key.startswith("lyrics"):
                return value
        return None

    @property
    def track_loudness(self) -> float | None:
        """Try to read/calculate the integrated loudness from the tags (track level)."""
        if tag := self.tags.get("r128trackgain"):
            try:
                gain_adjustment = int(tag.split(" ")[0]) / 256
                return -23 - gain_adjustment
            except (ValueError, IndexError) as e:
                LOGGER.warning(f"Invalid r128trackgain tag value: {tag!r} — {e}")

        if tag := self.tags.get("replaygaintrackgain"):
            try:
                gain_adjustment = float(tag.split(" ")[0])
                return -18 - gain_adjustment
            except (ValueError, IndexError) as e:
                LOGGER.warning(f"Invalid replaygaintrackgain tag value: {tag!r} — {e}")

        return None

    @property
    def track_album_loudness(self) -> float | None:
        """Try to read/calculate the integrated loudness from the tags (album level)."""
        if tag := self.tags.get("r128albumgain"):
            try:
                gain_adjustment = int(tag.split(" ")[0]) / 256
                return -23 - gain_adjustment
            except (ValueError, IndexError) as e:
                LOGGER.warning(f"Invalid r128albumgain tag value: {tag!r} — {e}")

        if tag := self.tags.get("replaygainalbumgain"):
            try:
                gain_adjustment = float(tag.split(" ")[0])
                return -18 - gain_adjustment
            except (ValueError, IndexError) as e:
                LOGGER.warning(f"Invalid replaygainalbumgain tag value: {tag!r} — {e}")

        return None

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> AudioTags:
        """Parse instance from raw ffmpeg info output."""
        audio_stream = next((x for x in raw["streams"] if x["codec_type"] == "audio"), None)
        if audio_stream is None:
            msg = "No audio stream found"
            raise InvalidDataError(msg)
        has_cover_image = any(
            x for x in raw["streams"] if x.get("codec_name", "") in ("mjpeg", "png")
        )
        # convert all tag-keys (gathered from all streams) to lowercase without spaces
        # prefer format as that contains the actual ID3 tags
        # append any tags found in streams (but don't overwrite format tags)
        tags = {}
        for stream in [raw["format"]] + raw["streams"]:
            if stream.get("codec_type") == "video":
                continue
            for key, value in stream.get("tags", {}).items():
                alt_key = key.lower()
                for char in [" ", "_", "-", "/"]:
                    alt_key = alt_key.replace(char, "")
                if alt_key in tags:
                    continue
                tags[alt_key] = value

        return AudioTags(
            raw=raw,
            sample_rate=int(audio_stream.get("sample_rate", 44100)),
            channels=audio_stream.get("channels", 2),
            bits_per_sample=int(
                audio_stream.get("bits_per_raw_sample", audio_stream.get("bits_per_sample")) or 16
            ),
            format=raw["format"]["format_name"],
            bit_rate=int(raw["format"].get("bit_rate", 0)) or None,
            duration=float(raw["format"].get("duration", 0)) or None,
            tags=tags,
            has_cover_image=has_cover_image,
            filename=raw["format"]["filename"],
        )

    def get(self, key: str, default: Any | None = None) -> Any:
        """Get tag by key."""
        return self.tags.get(key, default)


async def async_parse_tags(
    input_file: str, file_size: int | None = None, require_duration: bool = False
) -> AudioTags:
    """Parse tags from a media file (or URL). Async friendly."""
    return await asyncio.to_thread(parse_tags, input_file, file_size, require_duration)


def parse_tags(
    input_file: str, file_size: int | None = None, require_duration: bool = False
) -> AudioTags:
    """
    Parse tags from a media file (or URL). NOT Async friendly.

    Input_file may be a (local) filename or URL accessible by ffmpeg.
    """
    args = (
        "ffprobe",
        "-hide_banner",
        "-loglevel",
        "fatal",
        "-threads",
        "0",
        "-show_error",
        "-show_format",
        "-show_streams",
        "-show_chapters",
        "-print_format",
        "json",
        "-i",
        input_file,
    )
    try:
        res = subprocess.check_output(args)  # noqa: S603
        data = json.loads(res)
        if error := data.get("error"):
            raise InvalidDataError(error["string"])
        if not data.get("streams"):
            msg = "Not an audio file"
            raise InvalidDataError(msg)
        tags = AudioTags.parse(data)
        del res
        del data
        if not tags.duration and file_size and tags.bit_rate:
            # estimate duration from filesize/bitrate
            tags.duration = int((file_size * 8) / tags.bit_rate)
        if not tags.duration and tags.raw.get("format", {}).get("duration"):
            tags.duration = float(tags.raw["format"]["duration"])

        if not tags.duration and require_duration:
            tags.duration = get_file_duration(input_file)

        # we parse all (basic) tags for all file formats using ffmpeg
        # but we also try to extract some extra tags for local files using mutagen
        if not input_file.startswith("http") and os.path.isfile(input_file):
            extra_tags = parse_tags_mutagen(input_file)
            if extra_tags:
                tags.tags.update(extra_tags)
            # APEv2 cover art is not exposed as video streams by FFmpeg
            # For APEv2-only formats (wv, ape, mpc, tak, ofr), assume they might have cover art
            # We avoid calling mutagen here to prevent double file reads (blocking I/O)
            # The actual extraction happens later in get_apev2_image() if needed
            if not tags.has_cover_image and _format_uses_apev2(tags.format):
                tags.has_cover_image = True
        return tags
    except subprocess.CalledProcessError as err:
        error_msg = f"Unable to retrieve info for {input_file}"
        if output := getattr(err, "stdout", None):
            err_details = json_loads(output)
            with suppress(KeyError):
                error_msg = f"{error_msg} ({err_details['error']['string']})"
        raise InvalidDataError(error_msg) from err
    except (KeyError, ValueError, JSONDecodeError, InvalidDataError) as err:
        msg = f"Unable to retrieve info for {input_file}: {err!s}"
        raise InvalidDataError(msg) from err


def get_file_duration(input_file: str) -> float:
    """
    Parse file/stream duration from an audio file using ffmpeg.

    NOT Async friendly.
    """
    args = (
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "info",
        "-i",
        input_file,
        "-f",
        "null",
        "-",
    )
    try:
        res = subprocess.check_output(args, stderr=subprocess.STDOUT).decode()  # noqa: S603
        # extract duration from ffmpeg output
        duration_str = res.split("time=")[-1].split(" ")[0].strip()
        duration_parts = duration_str.split(":")
        duration = 0.0
        for part in duration_parts:
            duration = duration * 60 + float(part)
        return duration
    except Exception as err:
        error_msg = f"Unable to retrieve duration for {input_file}"
        raise InvalidDataError(error_msg) from err


def _decode_mp4_freeform_single(values: list[Any]) -> str:
    """Decode a single-value MP4 freeform tag (bytes to string).

    :param values: List of MP4FreeForm values (typically contains one item).
    """
    if not values:
        return ""
    val = values[0]
    if isinstance(val, bytes):
        return val.decode("utf-8", errors="replace")
    return str(val)


def _decode_mp4_freeform_list(values: list[Any]) -> list[str]:
    """Decode a multi-value MP4 freeform tag (bytes to strings).

    :param values: List of MP4FreeForm values.
    """
    result = []
    for val in values:
        if isinstance(val, bytes):
            result.append(val.decode("utf-8", errors="replace"))
        else:
            result.append(str(val))
    return result


def _parse_mp4_tags(tags: MP4Tags) -> dict[str, Any]:  # noqa: PLR0915
    """Parse MP4/M4A/AAC tags from mutagen MP4Tags object.

    :param tags: The MP4Tags object from mutagen.
    """
    result: dict[str, Any] = {}

    # Basic text tags (single value - extract first element)
    if "©nam" in tags:
        result["title"] = tags["©nam"][0]
    if "©ART" in tags:
        result["artist"] = tags["©ART"][0]
    if "aART" in tags:
        result["albumartist"] = tags["aART"][0]
    if "©alb" in tags:
        result["album"] = tags["©alb"][0]

    # Genre can have multiple values
    if "©gen" in tags:
        result["genre"] = list(tags["©gen"])

    # Sort tags (single value)
    if "sonm" in tags:
        result["titlesort"] = tags["sonm"][0]
    if "soal" in tags:
        result["albumsort"] = tags["soal"][0]
    # Sort tags (multi-value to match ID3 behavior)
    if "soar" in tags:
        result["artistsort"] = list(tags["soar"])
    if "soaa" in tags:
        result["albumartistsort"] = list(tags["soaa"])

    # iTunes freeform tags for MusicBrainz IDs
    # Single value tags
    if "----:com.apple.iTunes:MusicBrainz Album Id" in tags:
        result["musicbrainzalbumid"] = _decode_mp4_freeform_single(
            tags["----:com.apple.iTunes:MusicBrainz Album Id"]
        )
    if "----:com.apple.iTunes:MusicBrainz Release Group Id" in tags:
        result["musicbrainzreleasegroupid"] = _decode_mp4_freeform_single(
            tags["----:com.apple.iTunes:MusicBrainz Release Group Id"]
        )
    if "----:com.apple.iTunes:MusicBrainz Track Id" in tags:
        result["musicbrainztrackid"] = _decode_mp4_freeform_single(
            tags["----:com.apple.iTunes:MusicBrainz Track Id"]
        )
    if "----:com.apple.iTunes:MusicBrainz Recording Id" in tags:
        result["musicbrainzrecordingid"] = _decode_mp4_freeform_single(
            tags["----:com.apple.iTunes:MusicBrainz Recording Id"]
        )

    # Multi-value tags (return as list to match ID3 behavior)
    if "----:com.apple.iTunes:MusicBrainz Artist Id" in tags:
        result["musicbrainzartistid"] = _decode_mp4_freeform_list(
            tags["----:com.apple.iTunes:MusicBrainz Artist Id"]
        )
    if "----:com.apple.iTunes:MusicBrainz Album Artist Id" in tags:
        result["musicbrainzalbumartistid"] = _decode_mp4_freeform_list(
            tags["----:com.apple.iTunes:MusicBrainz Album Artist Id"]
        )

    # Additional freeform tags
    if "----:com.apple.iTunes:ARTISTS" in tags:
        result["artists"] = _decode_mp4_freeform_list(tags["----:com.apple.iTunes:ARTISTS"])
    if "----:com.apple.iTunes:BARCODE" in tags:
        result["barcode"] = _decode_mp4_freeform_list(tags["----:com.apple.iTunes:BARCODE"])
    if "----:com.apple.iTunes:ISRC" in tags:
        result["tsrc"] = _decode_mp4_freeform_list(tags["----:com.apple.iTunes:ISRC"])

    # Track and disc numbers (MP4 stores as tuple: [(number, total)])
    # NOTE: type ignores needed because MP4Tags from mutagen uses an untyped get() internally
    if tags.get("trkn"):  # type: ignore[no-untyped-call]
        track_info = tags["trkn"][0]
        if track_info[0]:
            result["track"] = str(track_info[0])
        if len(track_info) > 1 and track_info[1]:
            result["tracktotal"] = str(track_info[1])
    if tags.get("disk"):  # type: ignore[no-untyped-call]
        disc_info = tags["disk"][0]
        if disc_info[0]:
            result["disc"] = str(disc_info[0])
        if len(disc_info) > 1 and disc_info[1]:
            result["disctotal"] = str(disc_info[1])

    # Date
    if "©day" in tags:
        result["date"] = tags["©day"][0]

    # Lyrics
    if "©lyr" in tags:
        result["lyrics"] = tags["©lyr"][0]

    # Compilation flag
    if tags.get("cpil"):  # type: ignore[no-untyped-call]
        result["compilation"] = "1" if tags["cpil"] else "0"

    # Album type (MusicBrainz)
    if "----:com.apple.iTunes:MusicBrainz Album Type" in tags:
        result["musicbrainzalbumtype"] = _decode_mp4_freeform_single(
            tags["----:com.apple.iTunes:MusicBrainz Album Type"]
        )

    # ReplayGain tags
    if "----:com.apple.iTunes:REPLAYGAIN_TRACK_GAIN" in tags:
        result["replaygaintrackgain"] = _decode_mp4_freeform_single(
            tags["----:com.apple.iTunes:REPLAYGAIN_TRACK_GAIN"]
        )
    if "----:com.apple.iTunes:REPLAYGAIN_ALBUM_GAIN" in tags:
        result["replaygainalbumgain"] = _decode_mp4_freeform_single(
            tags["----:com.apple.iTunes:REPLAYGAIN_ALBUM_GAIN"]
        )

    return result


def _parse_id3_tags(tags: dict[str, Any]) -> dict[str, Any]:
    """Parse ID3 tags (MP3 files) from mutagen tags dict.

    :param tags: Dictionary of ID3 tags from mutagen.
    """
    result: dict[str, Any] = {}

    # Basic tags (single value)
    if "TIT2" in tags:
        result["title"] = tags["TIT2"].text[0]
    if "TPE1" in tags:
        result["artist"] = tags["TPE1"].text[0]
    if "TPE2" in tags:
        result["albumartist"] = tags["TPE2"].text[0]
    if "TALB" in tags:
        result["album"] = tags["TALB"].text[0]

    # Genre (multi-value)
    if "TCON" in tags:
        result["genre"] = tags["TCON"].text

    # Multi-value artist tag
    if "TXXX:ARTISTS" in tags:
        result["artists"] = tags["TXXX:ARTISTS"].text

    # MusicBrainz tags (single value)
    if "TXXX:MusicBrainz Album Id" in tags:
        result["musicbrainzalbumid"] = tags["TXXX:MusicBrainz Album Id"].text[0]
    if "TXXX:MusicBrainz Release Group Id" in tags:
        result["musicbrainzreleasegroupid"] = tags["TXXX:MusicBrainz Release Group Id"].text[0]
    if "UFID:http://musicbrainz.org" in tags:
        result["musicbrainzrecordingid"] = tags["UFID:http://musicbrainz.org"].data.decode()
    if "TXXX:MusicBrainz Track Id" in tags:
        result["musicbrainztrackid"] = tags["TXXX:MusicBrainz Track Id"].text[0]

    # MusicBrainz tags (multi-value)
    if "TXXX:MusicBrainz Album Artist Id" in tags:
        result["musicbrainzalbumartistid"] = tags["TXXX:MusicBrainz Album Artist Id"].text
    if "TXXX:MusicBrainz Artist Id" in tags:
        result["musicbrainzartistid"] = tags["TXXX:MusicBrainz Artist Id"].text

    # Additional tags
    if "TXXX:BARCODE" in tags:
        result["barcode"] = tags["TXXX:BARCODE"].text
    if "TXXX:TSRC" in tags:
        result["tsrc"] = tags["TXXX:TSRC"].text

    # Sort tags (multi-value to support multiple artists)
    if "TSOP" in tags:
        result["artistsort"] = tags["TSOP"].text
    if "TSO2" in tags:
        result["albumartistsort"] = tags["TSO2"].text

    # Sort tags (single value)
    if tags.get("TSOT"):
        result["titlesort"] = tags["TSOT"].text[0]
    if tags.get("TSOA"):
        result["albumsort"] = tags["TSOA"].text[0]

    return result


def _vorbis_get_single(tags: VCommentDict, key: str) -> str | None:
    """Get single value from Vorbis comments (first item if multiple exist).

    :param tags: VCommentDict from mutagen.
    :param key: Tag name (case insensitive).
    """
    values = tags.get(key)  # type: ignore[no-untyped-call]
    return values[0] if values else None


def _vorbis_get_multi(tags: VCommentDict, key: str) -> list[str] | None:
    """Get all values from Vorbis comments as a list.

    :param tags: VCommentDict from mutagen.
    :param key: Tag name (case insensitive).
    """
    values = tags.get(key)  # type: ignore[no-untyped-call]
    return list(values) if values else None


def _parse_vorbis_artist_tags(tags: VCommentDict, result: dict[str, Any]) -> None:
    """Parse artist-related tags from Vorbis comments into result dict.

    Handles multiple ARTIST/ALBUMARTIST fields per Vorbis spec, as well as
    explicit ARTISTS tag which take precedence.

    :param tags: VCommentDict from mutagen.
    :param result: Dictionary to store parsed tags.
    """
    # Artist tags - check for multiple values (per Vorbis spec recommendation)
    # Multiple ARTIST fields are treated the same as an ARTISTS tag
    artist_values = _vorbis_get_multi(tags, "ARTIST")
    if artist_values:
        if len(artist_values) > 1:
            # Multiple ARTIST fields - treat as authoritative list (like ARTISTS tag)
            result["artists"] = artist_values
        else:
            # Single ARTIST field - use normal parsing logic
            result["artist"] = artist_values[0]

    # Album artist tags - same logic for multiple values
    albumartist_values = _vorbis_get_multi(tags, "ALBUMARTIST")
    if albumartist_values:
        if len(albumartist_values) > 1:
            # Multiple ALBUMARTIST fields - treat as authoritative list
            result["albumartists"] = albumartist_values
        else:
            result["albumartist"] = albumartist_values[0]

    # Explicit ARTISTS tag takes precedence if present
    if artists := _vorbis_get_multi(tags, "ARTISTS"):
        result["artists"] = artists


def _parse_vorbis_tags(tags: VCommentDict) -> dict[str, Any]:
    """Parse Vorbis comment tags (FLAC, OGG Vorbis, OGG Opus, etc.).

    Vorbis comments support multiple values for the same field name per the spec.
    For example, multiple ARTIST fields can be used instead of a single ARTISTS field.
    See: https://xiph.org/vorbis/doc/v-comment.html

    :param tags: VCommentDict from mutagen (FLAC, OGG, etc.).
    """
    result: dict[str, Any] = {}

    # Basic tags
    if title := _vorbis_get_single(tags, "TITLE"):
        result["title"] = title
    if album := _vorbis_get_single(tags, "ALBUM"):
        result["album"] = album

    # Artist tags (handles multiple ARTIST/ALBUMARTIST fields per Vorbis spec)
    _parse_vorbis_artist_tags(tags, result)

    # Genre (multi-value)
    if genre := _vorbis_get_multi(tags, "GENRE"):
        result["genre"] = genre

    # MusicBrainz tags (single value)
    if mb_album := _vorbis_get_single(tags, "MUSICBRAINZ_ALBUMID"):
        result["musicbrainzalbumid"] = mb_album
    if mb_rg := _vorbis_get_single(tags, "MUSICBRAINZ_RELEASEGROUPID"):
        result["musicbrainzreleasegroupid"] = mb_rg
    if mb_track := _vorbis_get_single(tags, "MUSICBRAINZ_TRACKID"):
        result["musicbrainzrecordingid"] = mb_track
    if mb_reltrack := _vorbis_get_single(tags, "MUSICBRAINZ_RELEASETRACKID"):
        result["musicbrainztrackid"] = mb_reltrack

    # MusicBrainz tags (multi-value)
    if mb_aa_ids := _vorbis_get_multi(tags, "MUSICBRAINZ_ALBUMARTISTID"):
        result["musicbrainzalbumartistid"] = mb_aa_ids
    if mb_a_ids := _vorbis_get_multi(tags, "MUSICBRAINZ_ARTISTID"):
        result["musicbrainzartistid"] = mb_a_ids

    # Additional tags
    if barcode := _vorbis_get_multi(tags, "BARCODE"):
        result["barcode"] = barcode
    if isrc := _vorbis_get_multi(tags, "ISRC"):
        result["isrc"] = isrc

    # Date
    if date := _vorbis_get_single(tags, "DATE"):
        result["date"] = date

    # Lyrics
    if lyrics := _vorbis_get_single(tags, "LYRICS"):
        result["lyrics"] = lyrics

    # Compilation flag
    if compilation := _vorbis_get_single(tags, "COMPILATION"):
        result["compilation"] = compilation

    # Album type (MusicBrainz)
    if albumtype := _vorbis_get_single(tags, "MUSICBRAINZ_ALBUMTYPE"):
        result["musicbrainzalbumtype"] = albumtype
    # Also check RELEASETYPE which is an alternative tag name
    if not result.get("musicbrainzalbumtype"):
        if releasetype := _vorbis_get_single(tags, "RELEASETYPE"):
            result["musicbrainzalbumtype"] = releasetype

    # ReplayGain tags
    if rg_track := _vorbis_get_single(tags, "REPLAYGAIN_TRACK_GAIN"):
        result["replaygaintrackgain"] = rg_track
    if rg_album := _vorbis_get_single(tags, "REPLAYGAIN_ALBUM_GAIN"):
        result["replaygainalbumgain"] = rg_album

    # Sort tags
    if artistsort := _vorbis_get_multi(tags, "ARTISTSORT"):
        result["artistsort"] = artistsort
    if albumartistsort := _vorbis_get_multi(tags, "ALBUMARTISTSORT"):
        result["albumartistsort"] = albumartistsort
    if titlesort := _vorbis_get_single(tags, "TITLESORT"):
        result["titlesort"] = titlesort
    if albumsort := _vorbis_get_single(tags, "ALBUMSORT"):
        result["albumsort"] = albumsort

    return result


def _apev2_get_values(tags: APEv2, key: str) -> list[str]:
    """Get values from an APEv2 tag, splitting on null bytes for multi-value fields.

    :param tags: APEv2 tags object.
    :param key: Tag key (case-insensitive in APEv2).
    """
    if key not in tags:
        return []
    val = str(tags[key])
    # APEv2 uses null byte as separator for multiple values
    if "\x00" in val:
        return [v.strip() for v in val.split("\x00") if v.strip()]
    return [val] if val else []


def _apev2_get_single(tags: APEv2, key: str) -> str | None:
    """Get a single value from an APEv2 tag.

    :param tags: APEv2 tags object.
    :param key: Tag key.
    """
    values = _apev2_get_values(tags, key)
    return values[0] if values else None


def _apev2_get_multi(tags: APEv2, key: str) -> list[str] | None:
    """Get multiple values from an APEv2 tag.

    :param tags: APEv2 tags object.
    :param key: Tag key.
    """
    values = _apev2_get_values(tags, key)
    return values if values else None


def _parse_apev2_tags(tags: APEv2) -> dict[str, Any]:  # noqa: PLR0915
    r"""Parse APEv2 tags into a normalized dictionary.

    APEv2 tags are used by WavPack, Musepack, Monkey's Audio, OptimFROG, and TAK.
    Multi-value fields use null byte (\x00) as separator.

    :param tags: APEv2 tags object from mutagen.
    """
    result: dict[str, Any] = {}

    # Basic text tags
    if title := _apev2_get_single(tags, "Title"):
        result["title"] = title
    if artist := _apev2_get_single(tags, "Artist"):
        result["artist"] = artist
    if albumartist := _apev2_get_single(tags, "Album Artist"):
        result["albumartist"] = albumartist
    if album := _apev2_get_single(tags, "Album"):
        result["album"] = album

    # Genre (can be multi-value)
    if genre := _apev2_get_multi(tags, "Genre"):
        result["genre"] = genre

    # Multi-artist support (ARTISTS tag)
    if artists := _apev2_get_multi(tags, "Artists"):
        result["artists"] = artists

    # MusicBrainz IDs - single value
    if mb_albumid := _apev2_get_single(tags, "MUSICBRAINZ_ALBUMID"):
        result["musicbrainzalbumid"] = mb_albumid
    if mb_releasegroupid := _apev2_get_single(tags, "MUSICBRAINZ_RELEASEGROUPID"):
        result["musicbrainzreleasegroupid"] = mb_releasegroupid
    if mb_trackid := _apev2_get_single(tags, "MUSICBRAINZ_TRACKID"):
        # MUSICBRAINZ_TRACKID in APEv2 is actually the recording ID
        result["musicbrainzrecordingid"] = mb_trackid
    if mb_releasetrackid := _apev2_get_single(tags, "MUSICBRAINZ_RELEASETRACKID"):
        result["musicbrainztrackid"] = mb_releasetrackid

    # MusicBrainz IDs - multi-value (can have multiple artist IDs)
    if mb_artistid := _apev2_get_multi(tags, "MUSICBRAINZ_ARTISTID"):
        result["musicbrainzartistid"] = mb_artistid
    if mb_albumartistid := _apev2_get_multi(tags, "MUSICBRAINZ_ALBUMARTISTID"):
        result["musicbrainzalbumartistid"] = mb_albumartistid

    # Additional tags
    if barcode := _apev2_get_single(tags, "Barcode"):
        result["barcode"] = barcode
    if isrc := _apev2_get_multi(tags, "ISRC"):
        result["isrc"] = isrc

    # Track and disc numbers
    if track := _apev2_get_single(tags, "Track"):
        result["track"] = track
    if disc := _apev2_get_single(tags, "Disc"):
        result["disc"] = disc

    # Date
    if date := _apev2_get_single(tags, "Year"):
        result["date"] = date

    # Lyrics
    if lyrics := _apev2_get_single(tags, "Lyrics"):
        result["lyrics"] = lyrics

    # Compilation
    if compilation := _apev2_get_single(tags, "Compilation"):
        result["compilation"] = compilation

    # Album type
    if albumtype := _apev2_get_single(tags, "MUSICBRAINZ_ALBUMTYPE"):
        result["musicbrainzalbumtype"] = albumtype

    # ReplayGain tags
    if rg_track := _apev2_get_single(tags, "REPLAYGAIN_TRACK_GAIN"):
        result["replaygaintrackgain"] = rg_track
    if rg_album := _apev2_get_single(tags, "REPLAYGAIN_ALBUM_GAIN"):
        result["replaygainalbumgain"] = rg_album

    # Sort tags
    if artistsort := _apev2_get_multi(tags, "ARTISTSORT"):
        result["artistsort"] = artistsort
    if albumartistsort := _apev2_get_multi(tags, "ALBUMARTISTSORT"):
        result["albumartistsort"] = albumartistsort
    if titlesort := _apev2_get_single(tags, "TITLESORT"):
        result["titlesort"] = titlesort
    if albumsort := _apev2_get_single(tags, "ALBUMSORT"):
        result["albumsort"] = albumsort

    return result


def parse_tags_mutagen(input_file: str) -> dict[str, Any]:
    """Parse tags from an audio file using Mutagen.

    Supports Vorbis comments (FLAC, OGG), ID3 tags (MP3), MP4 tags (AAC/M4A/ALAC),
    and APEv2 tags (WavPack, Musepack, Monkey's Audio).

    :param input_file: Path to the audio file.
    """
    result: dict[str, Any] = {}
    try:
        audio = mutagen.File(input_file)  # type: ignore[attr-defined]
        if audio is None or not audio.tags:
            return result

        # Check if MP4/M4A/AAC file (uses MP4Tags)
        if isinstance(audio.tags, MP4Tags):
            result = _parse_mp4_tags(audio.tags)
        # Check if Vorbis comments (FLAC, OGG Vorbis, OGG Opus, etc.)
        elif isinstance(audio.tags, VCommentDict):
            result = _parse_vorbis_tags(audio.tags)
        # Check if APEv2 tags (WavPack, Musepack, Monkey's Audio, etc.)
        elif isinstance(audio.tags, APEv2):
            result = _parse_apev2_tags(audio.tags)
        else:
            # ID3 tags (MP3) and other formats
            tags_dict = dict(audio.tags)
            result = _parse_id3_tags(tags_dict)

        return result
    except Exception as err:
        LOGGER.debug(f"Error parsing mutagen tags for {input_file}: {err}")
        return result


def _format_uses_apev2(format_name: str) -> bool:
    """Check if an audio format exclusively uses APEv2 tags.

    These formats ONLY use APEv2 tags and cannot have cover art detected by ffprobe's
    video stream detection (unlike ID3's APIC which shows as mjpeg/png stream).

    Formats checked: WavPack, Musepack, Monkey's Audio, OptimFROG, TAK.
    Note: MP3 is NOT included as MP3 files almost always use ID3 tags, which are
    already handled by ffprobe. Checking all MP3 files would impact performance.

    :param format_name: The format name from ffprobe (e.g., "wv", "ape", "mpc").
    """
    # Map ffprobe format names to our check
    # wv = WavPack, ape = Monkey's Audio, mpc/mpc8 = Musepack
    # tak = TAK, ofr = OptimFROG
    apev2_only_formats = {"wv", "ape", "mpc", "mpc8", "tak", "ofr"}
    return format_name.lower() in apev2_only_formats


def get_apev2_image(input_file: str) -> bytes | None:
    """Extract cover art from APEv2 tags using mutagen.

    APEv2 tags (used by WavPack, Musepack, etc.) store cover art differently
    than ID3 tags. FFmpeg does not expose these as video streams, so we use
    mutagen for direct extraction.

    :param input_file: Path to the local audio file.
    """
    audio = mutagen.File(input_file)  # type: ignore[attr-defined]
    if audio is None or not hasattr(audio, "tags") or audio.tags is None:
        return None

    # APEv2 cover art can use various tag names
    cover_tag_names = [
        "Cover Art (Front)",
        "COVER ART (FRONT)",
        "Cover Art (front)",
        "cover art (front)",
        "COVERART",
        "coverart",
    ]

    for tag_name in cover_tag_names:
        if tag_name in audio.tags:
            cover_data = audio.tags[tag_name].value
            if isinstance(cover_data, bytes):
                # APEv2 cover art format: description\x00image_data
                null_index = cover_data.find(b"\x00")
                if null_index != -1:
                    # Extract image data after the null-terminated description
                    return cover_data[null_index + 1 :]
                # No description field, return entire data as image
                return cover_data
    return None


async def get_embedded_image(input_file: str) -> bytes | None:
    """Return embedded image data.

    Input_file may be a (local) filename or URL accessible by ffmpeg.
    """
    # For APEv2-only formats, use mutagen since FFmpeg cannot extract APEv2 cover art
    # Only check files with extensions that exclusively use APEv2 tags to avoid
    # unnecessary blocking I/O for MP3/FLAC/OGG/etc files
    if not input_file.startswith(("http://", "https://")) and os.path.isfile(input_file):
        # Check file extension to determine if it's an APEv2-only format
        ext = input_file.lower().rsplit(".", 1)[-1] if "." in input_file else ""
        if _format_uses_apev2(ext):
            if img_data := await asyncio.to_thread(get_apev2_image, input_file):
                return img_data

    # Use FFmpeg for all other cases (URLs, ID3 tags, Vorbis comments, etc.)
    args = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        input_file,
        "-an",
        "-vcodec",
        "mjpeg",
        "-f",
        "mjpeg",
        "-",
    ]
    async with AsyncProcess(
        args, stdin=False, stdout=True, stderr=None, name="ffmpeg_image"
    ) as ffmpeg:
        return await ffmpeg.read(-1)
