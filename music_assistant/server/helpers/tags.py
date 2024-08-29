"""Helpers/utilities to parse ID3 tags from audio files with ffmpeg."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass
from json import JSONDecodeError
from typing import Any

import eyed3

from music_assistant.common.helpers.util import try_parse_int
from music_assistant.common.models.enums import AlbumType
from music_assistant.common.models.errors import InvalidDataError
from music_assistant.common.models.media_items import MediaItemChapter
from music_assistant.constants import MASS_LOGGER_NAME, UNKNOWN_ARTIST
from music_assistant.server.helpers.process import AsyncProcess

LOGGER = logging.getLogger(f"{MASS_LOGGER_NAME}.tags")

# silence the eyed3 logger because it is too verbose
logging.getLogger("eyed3").setLevel(logging.ERROR)


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
        return (x.strip() for x in org_str)
    org_str = org_str.strip()
    if TAG_SPLITTER in org_str:
        return clean_tuple(org_str.split(TAG_SPLITTER))
    if allow_unsafe_splitters and "/" in org_str:
        return clean_tuple(org_str.split("/"))
    if allow_unsafe_splitters and ", " in org_str:
        return clean_tuple(org_str.split(", "))
    return clean_tuple((org_str,))


def split_artists(
    org_artists: str | tuple[str, ...], allow_ampersand: bool = False
) -> tuple[str, ...]:
    """Parse all artists from a string."""
    final_artists = set()
    # when not using the multi artist tag, the artist string may contain
    # multiple artists in freeform, even featuring artists may be included in this
    # string. Try to parse the featuring artists and separate them.
    splitters = ("featuring", " feat. ", " feat ", "feat.")
    if allow_ampersand:
        splitters = (*splitters, " & ")
    artists = split_items(org_artists)
    for item in artists:
        for splitter in splitters:
            if splitter not in item:
                continue
            for subitem in item.split(splitter):
                final_artists.add(subitem.strip())
    if not final_artists:
        # none of the extra splitters was found
        return artists
    return tuple(final_artists)


@dataclass
class AudioTags:
    """Audio metadata parsed from an audio file."""

    raw: dict[str, Any]
    sample_rate: int
    channels: int
    bits_per_sample: int
    format: str
    bit_rate: int
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
            return split_artists(tag)
        # fallback to parsing from filename
        title = self.filename.rsplit(os.sep, 1)[-1].split(".")[0]
        if " - " in title:
            title_parts = title.split(" - ")
            if len(title_parts) >= 2:
                return split_artists(title_parts[0])
        return (UNKNOWN_ARTIST,)

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
                # special case: album artist noted as 2 artists with ampersand
                # but with 2 mb ids so they should be treated as 2 artists
                # example: John Travolta & Olivia Newton John on the Grease album
                return split_artists(tag, allow_ampersand=True)
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
        # handle audiobook/podcast
        if self.filename.endswith("m4b") and len(self.chapters) > 1:
            return AlbumType.AUDIOBOOK
        if "podcast" in self.tags.get("genre", "").lower() and len(self.chapters) > 1:
            return AlbumType.PODCAST
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
            AlbumType.PODCAST,
            AlbumType.AUDIOBOOK,
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
    def chapters(self) -> list[MediaItemChapter]:
        """Return chapters in MediaItem (if any)."""
        chapters: list[MediaItemChapter] = []
        if raw_chapters := self.raw.get("chapters"):
            for chapter_data in raw_chapters:
                chapters.append(
                    MediaItemChapter(
                        chapter_id=chapter_data["id"],
                        position_start=chapter_data["start"],
                        position_end=chapter_data["end"],
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
        tags = {}
        for stream in raw["streams"] + [raw["format"]]:
            for key, value in stream.get("tags", {}).items():
                alt_key = key.lower().replace(" ", "").replace("_", "").replace("-", "")
                tags[alt_key] = value

        return AudioTags(
            raw=raw,
            sample_rate=int(audio_stream.get("sample_rate", 44100)),
            channels=audio_stream.get("channels", 2),
            bits_per_sample=int(
                audio_stream.get("bits_per_raw_sample", audio_stream.get("bits_per_sample")) or 16
            ),
            format=raw["format"]["format_name"],
            bit_rate=int(raw["format"].get("bit_rate", 320)),
            duration=float(raw["format"].get("duration", 0)) or None,
            tags=tags,
            has_cover_image=has_cover_image,
            filename=raw["format"]["filename"],
        )

    def get(self, key: str, default=None) -> Any:
        """Get tag by key."""
        return self.tags.get(key, default)


async def parse_tags(input_file: str, file_size: int | None = None) -> AudioTags:
    """
    Parse tags from a media file (or URL).

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
    async with AsyncProcess(args, stdin=False, stdout=True) as ffmpeg:
        res = await ffmpeg.read(-1)
    try:
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

        if (
            not input_file.startswith("http")
            and input_file.endswith(".mp3")
            and "musicbrainzrecordingid" not in tags.tags
            and await asyncio.to_thread(os.path.isfile, input_file)
        ):
            # eyed3 is able to extract the musicbrainzrecordingid from the unique file id
            # this is actually a bug in ffmpeg/ffprobe which does not expose this tag
            # so we use this as alternative approach for mp3 files
            audiofile = await asyncio.to_thread(eyed3.load, input_file)
            if audiofile is not None and audiofile.tag is not None:
                for uf_id in audiofile.tag.unique_file_ids:
                    if uf_id.owner_id == b"http://musicbrainz.org" and uf_id.uniq_id:
                        tags.tags["musicbrainzrecordingid"] = uf_id.uniq_id.decode()
                        break
            del audiofile
        return tags
    except (KeyError, ValueError, JSONDecodeError, InvalidDataError) as err:
        msg = f"Unable to retrieve info for {input_file}: {err!s}"
        raise InvalidDataError(msg) from err


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
