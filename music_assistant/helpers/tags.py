"""Helpers/utilities to parse ID3 tags from audio files with ffmpeg."""

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

from music_assistant.constants import MASS_LOGGER_NAME, UNKNOWN_ARTIST
from music_assistant.helpers.json import json_loads
from music_assistant.helpers.process import AsyncProcess
from music_assistant.helpers.util import try_parse_int

LOGGER = logging.getLogger(f"{MASS_LOGGER_NAME}.tags")


# the only multi-item splitter we accept is the semicolon,
# which is also the default in Musicbrainz Picard.
# the slash is also a common splitter but causes collisions with
# artists actually containing a slash in the name, such as AC/DC
TAG_SPLITTER = ";"


def clean_tuple(values: Iterable[str]) -> tuple:
    """Return a tuple with all empty values removed."""
    return tuple(x.strip() for x in values if x not in (None, "", " "))


def split_items(org_str: str, allow_unsafe_splitters: bool = False) -> tuple[str, ...]:
    """Split up a tags string by common splitter."""
    if org_str is None:
        return ()
    if isinstance(org_str, list):
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


def split_artists(
    org_artists: str | tuple[str, ...], allow_extra_splitters: bool = False
) -> tuple[str, ...]:
    """Parse all artists from a string."""
    final_artists: list[str] = []
    # when not using the multi artist tag, the artist string may contain
    # multiple artists in freeform, even featuring artists may be included in this
    # string. Try to parse the featuring artists and separate them.
    splitters = [
        " featuring ",
        " feat. ",
        " feat ",
        " duet with ",
        " with ",
        " ft. ",
        " vs. ",
    ]
    splitters += [x.title() for x in splitters]
    if allow_extra_splitters:
        splitters += [" & ", ", ", " + "]
    artists = split_items(org_artists, allow_unsafe_splitters=False)
    for item in artists:
        for splitter in splitters:
            if splitter not in item:
                continue
            for subitem in item.split(splitter):
                clean_item = subitem.strip()
                if clean_item and clean_item not in final_artists:
                    final_artists.append(subitem.strip())
    if not final_artists:
        # none of the extra splitters was found
        return artists
    return tuple(final_artists)


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
        # prefer multi-artist tag
        if tag := self.tags.get("artists"):
            return split_items(tag)
        # fallback to regular artist string
        if tag := self.tags.get("artist"):
            if TAG_SPLITTER in tag:
                return split_items(tag)
            if len(self.musicbrainz_artistids) > 1:
                # special case: artist noted as 2 artists with ampersand or other splitter
                # but with 2 mb ids so they should be treated as 2 artists
                # example: John Travolta & Olivia Newton John on the Grease album
                return split_artists(tag, allow_extra_splitters=True)
            return split_artists(tag)
        # fallback to parsing from filename
        title = self.filename.rsplit(os.sep, 1)[-1].split(".")[0]
        if " - " in title:
            title_parts = title.split(" - ")
            if len(title_parts) >= 2:
                return split_artists(title_parts[0])
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
            return split_artists(tag)
        return ()

    @property
    def album_artists(self) -> tuple[str, ...]:
        """Return (all) album artists (if any)."""
        # prefer multi-artist tag
        if tag := self.tags.get("albumartists"):
            return split_items(tag)
        # fallback to regular artist string
        if tag := self.tags.get("albumartist"):
            if TAG_SPLITTER in tag:
                return split_items(tag)
            if len(self.musicbrainz_albumartistids) > 1:
                # special case: album artist noted as 2 artists with ampersand or other splitter
                # but with 2 mb ids so they should be treated as 2 artists
                # example: John Travolta & Olivia Newton John on the Grease album
                return split_artists(tag, allow_extra_splitters=True)
            return split_artists(tag)
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
        if tag is None:
            return AlbumType.UNKNOWN
        # the album type tag is messy within id3 and may even contain multiple types
        # try to parse one in order of preference
        for album_type in (
            AlbumType.COMPILATION,
            AlbumType.EP,
            AlbumType.SINGLE,
            AlbumType.ALBUM,
        ):
            if album_type.value in tag.lower():
                return album_type

        return AlbumType.UNKNOWN

    @property
    def isrc(self) -> tuple[str]:
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
        """Try to read/calculate the integrated loudness from the tags."""
        if (tag := self.tags.get("r128trackgain")) is not None:
            return -23 - float(int(tag.split(" ")[0]) / 256)
        if (tag := self.tags.get("replaygaintrackgain")) is not None:
            return -18 - float(tag.split(" ")[0])
        return None

    @property
    def track_album_loudness(self) -> float | None:
        """Try to read/calculate the integrated loudness from the tags (album level)."""
        if tag := self.tags.get("r128albumgain"):
            return -23 - float(int(tag.split(" ")[0]) / 256)
        if (tag := self.tags.get("replaygainalbumgain")) is not None:
            return -18 - float(tag.split(" ")[0])
        return None

    @classmethod
    def parse(cls, raw: dict) -> AudioTags:
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
                alt_key = key.lower().replace(" ", "").replace("_", "").replace("-", "")
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

    def get(self, key: str, default=None) -> Any:
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
        duration = 0
        for part in duration_parts:
            duration = duration * 60 + float(part)
        return duration
    except Exception as err:
        error_msg = f"Unable to retrieve duration for {input_file}"
        raise InvalidDataError(error_msg) from err


def parse_tags_mutagen(input_file: str) -> dict[str, Any]:
    """
    Parse tags from an audio file using Mutagen.

    NOT Async friendly.
    """
    result = {}
    try:
        # TODO: extend with more tags and file types!
        tags = mutagen.File(input_file)
        if tags is None or not tags.tags:
            return result
        tags = dict(tags.tags)
        # ID3 tags
        if "TIT2" in tags:
            result["title"] = tags["TIT2"].text[0]
        if "TPE1" in tags:
            result["artist"] = tags["TPE1"].text[0]
        if "TPE2" in tags:
            result["albumartist"] = tags["TPE2"].text[0]
        if "TALB" in tags:
            result["album"] = tags["TALB"].text[0]
        if "TCON" in tags:
            result["genre"] = tags["TCON"].text
        if "TXXX:ARTISTS" in tags:
            result["artists"] = tags["TXXX:ARTISTS"].text
        if "TXXX:MusicBrainz Album Id" in tags:
            result["musicbrainzalbumid"] = tags["TXXX:MusicBrainz Album Id"].text[0]
        if "TXXX:MusicBrainz Album Artist Id" in tags:
            result["musicbrainzalbumartistid"] = tags["TXXX:MusicBrainz Album Artist Id"].text
        if "TXXX:MusicBrainz Artist Id" in tags:
            result["musicbrainzartistid"] = tags["TXXX:MusicBrainz Artist Id"].text
        if "TXXX:MusicBrainz Release Group Id" in tags:
            result["musicbrainzreleasegroupid"] = tags["TXXX:MusicBrainz Release Group Id"].text[0]
        if "UFID:http://musicbrainz.org" in tags:
            result["musicbrainzrecordingid"] = tags["UFID:http://musicbrainz.org"].data.decode()
        if "TXXX:MusicBrainz Track Id" in tags:
            result["musicbrainztrackid"] = tags["TXXX:MusicBrainz Track Id"].text[0]
        if "TXXX:BARCODE" in tags:
            result["barcode"] = tags["TXXX:BARCODE"].text
        if "TXXX:TSRC" in tags:
            result["tsrc"] = tags["TXXX:TSRC"].text
        if "TSOP" in tags:
            result["artistsort"] = tags["TSOP"].text
        if "TSO2" in tags:
            result["albumartistsort"] = tags["TSO2"].text
        if tags.get("TSOT"):
            result["titlesort"] = tags["TSOT"].text[0]
        if tags.get("TSOA"):
            result["albumsort"] = tags["TSOA"].text[0]

        del tags
        return result
    except Exception as err:
        LOGGER.debug(f"Error parsing mutagen tags for {input_file}: {err}")
        return result


async def get_embedded_image(input_file: str) -> bytes | None:
    """Return embedded image data.

    Input_file may be a (local) filename or URL accessible by ffmpeg.
    """
    args = (
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
    )
    async with AsyncProcess(
        args, stdin=False, stdout=True, stderr=None, name="ffmpeg_image"
    ) as ffmpeg:
        return await ffmpeg.read(-1)
