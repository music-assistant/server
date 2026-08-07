"""
CUE sheet integration for the filesystem_local provider.

A CUE sheet describes multiple logical tracks within a single audio file
(typically a whole-album rip). This module provides:

- Synthetic ``item_id`` construction/parsing for CUE-derived tracks.
- A :class:`CueSheetHandler` that turns a CUE sheet + its audio file into
  :class:`Track` objects, produces :class:`StreamDetails` for playback of a
  single logical track, and streams the requested segment via FFmpeg.
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import (
    ContentType,
    ExternalID,
    ImageType,
    MediaType,
    StreamType,
)
from music_assistant_models.errors import InvalidDataError, MediaNotFoundError
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    ItemMapping,
    MediaItemImage,
    ProviderMapping,
    Track,
    UniqueList,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.constants import UNKNOWN_ARTIST, UNKNOWN_ARTIST_ID_MBID
from music_assistant.helpers.cue_sheet import CueSheet, CueTrack, parse_cue_sheet
from music_assistant.helpers.ffmpeg import get_ffmpeg_stream
from music_assistant.helpers.tags import AudioTags, async_parse_tags, clean_mbid
from music_assistant.helpers.util import detect_charset

from .constants import CACHE_CATEGORY_CUE_SHEETS, TRACK_EXTENSIONS
from .helpers import FileSystemItem

if TYPE_CHECKING:
    from . import FileSystemProvider

CUE_TRACK_ID_DELIMITER = "::track"


@dataclass(frozen=True, slots=True)
class _TrackBuildContext:
    """Shared state used to build each Track from a CUE sheet."""

    audio_format: AudioFormat
    disc_number: int
    date_added: datetime | None
    embedded_image: MediaItemImage | None
    track_genres: set[str] | None  # fallback genres from the audio file
    album: Album | None
    album_performers: tuple[str, ...]  # sheet-level PERFORMER values, fallback for track artists


def make_cue_track_id(cue_relative_path: str, track_number: int) -> str:
    """Build the synthetic provider item_id for a CUE-derived track."""
    return f"{cue_relative_path}{CUE_TRACK_ID_DELIMITER}{track_number:02d}"


def cue_referenced_audio_stem(cue_item: FileSystemItem, cue_sheet: CueSheet) -> str | None:
    """
    Return the absolute path (without extension) of the CUE's referenced audio file.

    Returns None if the CUE names no file. The named file may differ from the CUE's
    own filename.

    :param cue_item: The CUE file's FileSystemItem.
    :param cue_sheet: The parsed CUE sheet.
    """
    if not cue_sheet.file_path:
        return None
    companion = os.path.join(os.path.dirname(cue_item.absolute_path), cue_sheet.file_path)
    return companion.rsplit(".", 1)[0]


def parse_cue_track_id(item_id: str) -> tuple[str, int] | None:
    """Return (cue_relative_path, track_number) if item_id is a CUE track id, else None."""
    if CUE_TRACK_ID_DELIMITER not in item_id:
        return None
    cue_path, track_num_str = item_id.rsplit(CUE_TRACK_ID_DELIMITER, 1)
    if not cue_path:
        return None
    try:
        track_num = int(track_num_str)
    except ValueError:
        return None
    return cue_path, track_num


def _cue_sheet_from_dict(data: dict[str, Any]) -> CueSheet:
    """Rebuild a :class:`CueSheet` from its ``asdict`` representation."""
    tracks = [CueTrack(**track_data) for track_data in data.get("tracks", [])]
    return CueSheet(**{**data, "tracks": tracks})


class CueSheetHandler:
    """CUE sheet integration bound to a :class:`FileSystemProvider` instance."""

    def __init__(self, provider: FileSystemProvider) -> None:
        """
        Initialize the handler.

        :param provider: The provider that owns this handler; used for shared state
            (cache, logger, mass) and helpers (``_parse_album``, ``_parse_artist``).
        """
        self.provider = provider

    async def read_cue_file(self, cue_item: FileSystemItem) -> str:
        """
        Read CUE file content, decoded with the detected charset.

        :param cue_item: The CUE file's FileSystemItem.
        """
        # route through provider._read_file so non-mounted providers (WebDAV)
        # use their own transport instead of aiofiles
        raw = await self.provider._read_file(cue_item.relative_path)
        encoding = await detect_charset(raw)
        return raw.decode(encoding, errors="replace")

    async def load_cue_sheet(self, cue_item: FileSystemItem) -> CueSheet:
        """
        Return the parsed CUE sheet for the given file.

        :param cue_item: The CUE file's FileSystemItem.
        """
        # cached by (path, checksum) so unchanged CUE files skip the file read
        provider = self.provider
        cached = await provider.mass.cache.get(
            key=cue_item.relative_path,
            provider=provider.instance_id,
            category=CACHE_CATEGORY_CUE_SHEETS,
            checksum=cue_item.checksum,
            default=None,
        )
        if cached is not None:
            return _cue_sheet_from_dict(cached)
        content = await self.read_cue_file(cue_item)
        sheet = parse_cue_sheet(content)
        await provider.mass.cache.set(
            key=cue_item.relative_path,
            data=asdict(sheet),
            provider=provider.instance_id,
            category=CACHE_CATEGORY_CUE_SHEETS,
            checksum=cue_item.checksum,
            expiration=3600 * 24 * 365,
        )
        return sheet

    async def find_audio_file(self, cue_item: FileSystemItem, cue_sheet: CueSheet) -> str | None:
        """
        Locate the audio file referenced by a CUE sheet.

        Returns the provider-relative path of the audio file, or ``None`` if it
        cannot be located. Routing through ``provider.exists`` keeps this working
        for every filesystem provider (local, SMB/NFS mounts, WebDAV).

        :param cue_item: The CUE file's FileSystemItem.
        :param cue_sheet: The parsed CUE sheet data.
        """
        cue_dir = os.path.dirname(cue_item.relative_path)

        def _join(name: str) -> str:
            return os.path.join(cue_dir, name) if cue_dir else name

        # 1. try the filename from the CUE FILE command
        if cue_sheet.file_path:
            candidate = _join(cue_sheet.file_path)
            if await self.provider.exists(candidate):
                return candidate

        # 2. same-name matching: album.cue -> album.{flac,mp3,...}
        cue_stem = cue_item.filename.rsplit(".", 1)[0]
        for ext in TRACK_EXTENSIONS:
            candidate = _join(f"{cue_stem}.{ext}")
            if await self.provider.exists(candidate):
                return candidate

        return None

    async def parse_tracks(self, cue_item: FileSystemItem) -> list[Track]:
        """
        Parse a CUE sheet and return individual :class:`Track` objects.

        :param cue_item: The CUE file's FileSystemItem.
        """
        provider = self.provider
        logger = provider.logger
        cue_sheet = await self.load_cue_sheet(cue_item)

        if not cue_sheet.tracks:
            msg = f"CUE sheet has no tracks: {cue_item.relative_path}"
            raise InvalidDataError(msg)

        audio_relative_path = await self.find_audio_file(cue_item, cue_sheet)
        if audio_relative_path is None:
            msg = f"Audio file not found for CUE sheet: {cue_item.relative_path}"
            raise MediaNotFoundError(msg)

        audio_item = await provider.resolve(audio_relative_path)
        tags = await async_parse_tags(audio_item.absolute_path, audio_item.file_size)
        total_duration = tags.duration or 0.0
        if total_duration <= 0:
            msg = f"Could not determine duration for audio file of CUE sheet: {cue_item.relative_path}"
            raise InvalidDataError(msg)

        self._apply_cue_overrides(tags, cue_sheet)

        album: Album | None = None
        if tags.album:
            album = await provider._parse_album(
                track_path=audio_relative_path,
                track_tags=tags,
                track_created_at=cue_item.created_at,
            )
        else:
            logger.warning(
                "CUE sheet %s has no TITLE and audio file has no album tag",
                cue_item.relative_path,
            )

        # embedded cover art is shared across all CUE tracks from this audio file
        embedded_image = (
            MediaItemImage(
                type=ImageType.THUMB,
                path=audio_relative_path,
                provider=provider.instance_id,
                remotely_accessible=False,
            )
            if tags.has_cover_image
            else None
        )
        # if the album lacks its own image, adopt the embedded one
        if album and embedded_image and not album.image:
            album.metadata.images = UniqueList([embedded_image])

        ctx = _TrackBuildContext(
            audio_format=self._audio_format_from_tags(audio_relative_path, tags),
            # honor audio file's DISCNUMBER (CUE does not carry disc info); defaults to 1
            disc_number=tags.disc or 1,
            date_added=(
                datetime.fromtimestamp(cue_item.created_at, tz=UTC) if cue_item.created_at else None
            ),
            embedded_image=embedded_image,
            track_genres=set(tags.genres) if tags.genres else None,
            album=album,
            album_performers=tuple(cue_sheet.performers),
        )

        sorted_tracks = sorted(cue_sheet.tracks, key=lambda t: t.start_position)
        tracks: list[Track] = []
        for i, cue_track in enumerate(sorted_tracks):
            if i + 1 < len(sorted_tracks):
                duration = sorted_tracks[i + 1].start_position - cue_track.start_position
            else:
                duration = total_duration - cue_track.start_position

            if duration <= 0:
                logger.warning(
                    "CUE sheet %s track %d has non-positive duration (%.2fs); skipping",
                    cue_item.relative_path,
                    cue_track.number,
                    duration,
                )
                continue
            if not cue_track.title:
                logger.warning(
                    "CUE sheet %s track %d has no TITLE; skipping",
                    cue_item.relative_path,
                    cue_track.number,
                )
                continue

            tracks.append(await self._build_track(cue_track, cue_item, duration, ctx))

        return tracks

    async def get_stream_details(self, item_id: str) -> StreamDetails:
        """
        Return the streamdetails for a CUE-sheet-derived track.

        :param item_id: Track ID in format "path/to/file.cue::trackNN".
        """
        parsed = parse_cue_track_id(item_id)
        if parsed is None:
            msg = f"Invalid CUE track id: {item_id}"
            raise InvalidDataError(msg)
        cue_path, track_number = parsed

        # audio format + duration were persisted at sync time; reuse them here
        provider = self.provider
        library_track = await provider.mass.music.tracks.get_library_item_by_prov_id(
            item_id, provider.instance_id
        )
        if library_track is None:
            msg = f"CUE track not in library: {item_id}"
            raise MediaNotFoundError(msg)
        prov_mapping = next(x for x in library_track.provider_mappings if x.item_id == item_id)
        original_format = prov_mapping.audio_format

        # re-parse to read the track's start offset
        cue_item = await provider.resolve(cue_path)
        cue_sheet = await self.load_cue_sheet(cue_item)
        cue_track = next((t for t in cue_sheet.tracks if t.number == track_number), None)
        if cue_track is None:
            msg = f"Track {track_number} not found in CUE sheet: {cue_path}"
            raise MediaNotFoundError(msg)

        audio_relative_path = await self.find_audio_file(cue_item, cue_sheet)
        if audio_relative_path is None:
            msg = f"Audio file not found for CUE sheet: {cue_path}"
            raise MediaNotFoundError(msg)

        # CUE tracks need StreamType.CUSTOM: they are a segment of a larger file and
        # require -ss/-t at the track offset. Core appends its own -ss for user seeks
        # after extra_input_args, and a second input -ss overrides the first, so
        # LOCAL_FILE with a base offset cannot coexist with user seeking.
        output_format = AudioFormat(
            content_type=ContentType.PCM_F32LE,
            sample_rate=original_format.sample_rate,
            bit_depth=32,
            channels=original_format.channels,
        )
        # store the relative path so get_audio_stream re-resolves at stream time;
        # keeps WebDAV auth fresh and keeps credentials out of persisted StreamDetails
        return StreamDetails(
            provider=provider.instance_id,
            item_id=item_id,
            audio_format=output_format,
            media_type=MediaType.TRACK,
            stream_type=StreamType.CUSTOM,
            duration=library_track.duration,
            can_seek=True,
            allow_seek=True,
            data={
                "audio_relative_path": audio_relative_path,
                "start_seconds": cue_track.start_position,
                "original_format": original_format.to_dict(),
            },
        )

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes]:
        """
        Yield the segment of the underlying audio file for a CUE-derived track.

        :param streamdetails: Streamdetails previously built by :meth:`get_stream_details`.
        :param seek_position: Position (seconds) within the track to start from.
        """
        # streamdetails was built by get_stream_details; asserts narrow for mypy
        assert streamdetails.data is not None
        assert streamdetails.duration is not None
        audio_relative_path: str = streamdetails.data["audio_relative_path"]
        base_start: float = streamdetails.data["start_seconds"]
        original_format = AudioFormat.from_dict(streamdetails.data["original_format"])

        # actual seek position within the full audio file
        actual_seek = base_start + seek_position
        remaining_duration = streamdetails.duration - seek_position
        if remaining_duration <= 0:
            return

        # re-resolve to get a current absolute path (e.g. fresh auth for WebDAV)
        audio_item = await self.provider.resolve(audio_relative_path)
        async for chunk in get_ffmpeg_stream(
            audio_input=audio_item.absolute_path,
            input_format=original_format,
            output_format=streamdetails.audio_format,
            extra_input_args=["-ss", str(actual_seek), "-t", str(remaining_duration)],
        ):
            yield chunk

    @staticmethod
    def _audio_format_from_tags(audio_path: str, tags: AudioTags) -> AudioFormat:
        """
        Build an AudioFormat for an audio file from its tags.

        :param audio_path: Path to the audio file (used for extension fallback).
        :param tags: Parsed audio tags.
        """
        return AudioFormat(
            content_type=ContentType.try_parse(audio_path.rsplit(".", 1)[-1] or tags.format),
            sample_rate=tags.sample_rate,
            bit_depth=tags.bits_per_sample,
            channels=tags.channels,
            bit_rate=tags.bit_rate,
        )

    @staticmethod
    def _apply_cue_overrides(tags: AudioTags, cue_sheet: CueSheet) -> None:
        """Overwrite album-level audio tags with values from the CUE sheet."""
        if cue_sheet.title:
            tags.tags["album"] = cue_sheet.title
        if cue_sheet.sort_title:
            tags.tags["albumsort"] = cue_sheet.sort_title
        if cue_sheet.performers:
            # plural form so AudioTags.album_artists sees every value
            tags.tags.pop("albumartist", None)
            tags.tags["albumartists"] = list(cue_sheet.performers)
        if cue_sheet.album_artist_sort_names:
            tags.tags["albumartistsort"] = ";".join(cue_sheet.album_artist_sort_names)
        if cue_sheet.musicbrainz_albumartistids:
            tags.tags["musicbrainzalbumartistid"] = ";".join(cue_sheet.musicbrainz_albumartistids)
        if cue_sheet.date:
            tags.tags["date"] = cue_sheet.date
        if cue_sheet.genres:
            # AudioTags.genres splits on ";" so we join multi-line values that way
            tags.tags["genre"] = ";".join(cue_sheet.genres)
        if cue_sheet.album_types:
            # album_type reads "releasetype" as a single string and substring-matches
            tags.tags["releasetype"] = " ".join(cue_sheet.album_types)
        if cue_sheet.barcode:
            tags.tags["barcode"] = cue_sheet.barcode
        if cue_sheet.musicbrainz_albumid:
            tags.tags["musicbrainzalbumid"] = cue_sheet.musicbrainz_albumid
        if cue_sheet.musicbrainz_releasegroupid:
            tags.tags["musicbrainzreleasegroupid"] = cue_sheet.musicbrainz_releasegroupid

    async def _build_track(
        self,
        cue_track: CueTrack,
        cue_item: FileSystemItem,
        duration: float,
        ctx: _TrackBuildContext,
    ) -> Track:
        """Construct a single Track from a CUE track entry."""
        provider = self.provider
        track_id = make_cue_track_id(cue_item.relative_path, cue_track.number)

        # per-track PERFORMER wins over sheet-level. Multi-artist uses one PERFORMER
        # line per artist (Vorbis convention); never delimiter-split, would mangle "AC/DC".
        performer_names = cue_track.performers or list(ctx.album_performers)
        track_artists: UniqueList[Artist | ItemMapping] = UniqueList()
        for idx, artist_name in enumerate(performer_names):
            artist = await provider._parse_artist(
                name=artist_name,
                sort_name=(
                    cue_track.artist_sort_names[idx]
                    if idx < len(cue_track.artist_sort_names)
                    else None
                ),
                mbid=(
                    cue_track.musicbrainz_artistids[idx]
                    if idx < len(cue_track.musicbrainz_artistids)
                    else None
                ),
            )
            if artist:
                track_artists.append(artist)
        if not track_artists:
            # neither the track nor the sheet declared a PERFORMER; fall back to
            # the [unknown] artist rather than leaving the track artist-less
            unknown = await provider._parse_artist(name=UNKNOWN_ARTIST, mbid=UNKNOWN_ARTIST_ID_MBID)
            if unknown:
                track_artists.append(unknown)

        track = Track(
            item_id=track_id,
            provider=provider.instance_id,
            name=cue_track.title or f"Track {cue_track.number}",
            sort_name=cue_track.sort_name,
            provider_mappings={
                ProviderMapping(
                    item_id=track_id,
                    provider_domain=provider.domain,
                    provider_instance=provider.instance_id,
                    audio_format=ctx.audio_format,
                    details=cue_item.checksum,
                    in_library=True,
                )
            },
            track_number=cue_track.number,
            disc_number=ctx.disc_number,
            duration=round(duration),
            date_added=ctx.date_added,
        )

        if track_artists:
            track.artists = track_artists
        if ctx.album:
            track.album = ctx.album
        for isrc in cue_track.isrcs:
            track.external_ids.add((ExternalID.ISRC, isrc))
        if recording_mbid := clean_mbid(cue_track.musicbrainz_recordingid, cue_item.relative_path):
            # the setter keeps external_ids in sync
            track.mbid = recording_mbid
        if cue_track.musicbrainz_releasetrackid:
            track.external_ids.add((ExternalID.MB_TRACK, cue_track.musicbrainz_releasetrackid))
        if ctx.embedded_image is not None:
            track.metadata.images = UniqueList([ctx.embedded_image])
        if cue_track.genres:
            # per-track REM GENRE wins over the shared audio-file/album genres
            track.metadata.genres = set(cue_track.genres)
        elif ctx.track_genres is not None:
            track.metadata.genres = ctx.track_genres
        if cue_track.copyright:
            track.metadata.copyright = cue_track.copyright
        if cue_track.grouping:
            track.metadata.grouping = cue_track.grouping
        if cue_track.comment:
            track.metadata.description = cue_track.comment
        if cue_track.explicit is not None:
            track.metadata.explicit = cue_track.explicit
        return track
