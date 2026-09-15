"""Turn beets library rows into Music Assistant media items."""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any, Literal

from music_assistant_models.enums import AlbumType, ContentType, ExternalID, ImageType
from music_assistant_models.errors import InvalidDataError
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

from music_assistant.constants import VARIOUS_ARTISTS_MBID, VARIOUS_ARTISTS_NAME
from music_assistant.helpers.datetime import from_utc_timestamp
from music_assistant.helpers.tags import clean_mbid
from music_assistant.helpers.util import parse_title_and_version

from .constants import (
    ALBUM_ID_PREFIX,
    ALBUM_TYPE_PRIORITY,
    IMAGE_PATH_PREFIX,
    TRACK_ID_PREFIX,
)
from .library import BeetsRow, value_at
from .library import split_multi_value as split_multi_value  # noqa: PLC0414 (re-exported)


@dataclass(frozen=True)
class ParseContext:
    """Provider settings the parsers need to build media items."""

    instance_id: str
    domain: str
    music_directory: str
    beets_directory: str | None
    favorite_rating_threshold: float | None


def track_id_prefix(instance_id: str) -> str:
    """
    Return the prefix a track provider item id of this instance starts with.

    :param instance_id: The provider instance id.
    """
    return f"{TRACK_ID_PREFIX}{instance_id}-"


def album_id_prefix(instance_id: str) -> str:
    """
    Return the prefix an album provider item id of this instance starts with.

    :param instance_id: The provider instance id.
    """
    return f"{ALBUM_ID_PREFIX}{instance_id}-"


def track_item_id(ctx: ParseContext, beets_id: int) -> str:
    """
    Return the provider item id of a beets item.

    :param ctx: The provider parse context.
    :param beets_id: The beets item id.
    """
    return f"{track_id_prefix(ctx.instance_id)}{beets_id}"


def album_item_id(ctx: ParseContext, beets_id: int) -> str:
    """
    Return the provider item id of a beets album.

    :param ctx: The provider parse context.
    :param beets_id: The beets album id.
    """
    return f"{album_id_prefix(ctx.instance_id)}{beets_id}"


def decode_path(value: object) -> str | None:
    """
    Return a beets path column as text, or None when it is empty.

    :param value: The raw BLOB or text value.
    """
    if isinstance(value, bytes):
        return os.fsdecode(value) or None
    if isinstance(value, str):
        return value or None
    return None


def expand_path(value: object, music_directory: str, beets_directory: str | None) -> str | None:
    """
    Return the absolute path, as seen by Music Assistant, of a path stored by beets.

    :param value: The raw path value from beets.
    :param music_directory: Where beets' music directory is mounted for Music Assistant.
    :param beets_directory: beets' own music directory, whose prefix is swapped for
        music_directory on absolute paths.
    """
    path = decode_path(value)
    if path is None:
        return None
    if not posixpath.isabs(path):
        return posixpath.join(music_directory, path)
    if beets_directory:
        prefix = beets_directory.rstrip("/")
        if path == prefix or path.startswith(f"{prefix}/"):
            return f"{music_directory.rstrip('/')}{path[len(prefix) :]}"
    return path


def parse_album_type(albumtypes: object, albumtype: object) -> AlbumType:
    """
    Return the MA album type for beets' albumtypes and albumtype fields.

    :param albumtypes: The beets albumtypes value.
    :param albumtype: The beets albumtype value.
    """
    values = {part.strip().lower() for part in split_multi_value(albumtypes)}
    if isinstance(albumtype, str):
        values.add(albumtype.strip().lower())
    for beets_type, album_type in ALBUM_TYPE_PRIORITY:
        if beets_type in values:
            return album_type
    return AlbumType.UNKNOWN


def loudness_from_gains(
    fields: Mapping[str, Any], level: Literal["track", "album"]
) -> float | None:
    """
    Return integrated loudness in LUFS from beets' R128 or ReplayGain gain fields.

    :param fields: The beets item fields.
    :param level: Whether to read the track or the album gains.
    """
    if (r128_gain := _float(fields.get(f"r128_{level}_gain"))) is not None:
        return -23 - r128_gain
    if (replaygain := _float(fields.get(f"rg_{level}_gain"))) is not None:
        return -18 - replaygain
    return None


def parse_favorite(flex: Mapping[str, Any], threshold: float | None) -> bool:
    """
    Return whether a flexible rating reaches the favorite threshold.

    :param flex: The item's flexible attributes.
    :param threshold: The configured threshold, or None when ratings are ignored.
    """
    if threshold is None:
        return False
    rating = _float(flex.get("rating"))
    return rating is not None and rating >= threshold


def parse_audio_format(fields: Mapping[str, Any]) -> AudioFormat:
    """
    Return the audio format of a beets item.

    :param fields: The beets item fields.
    """
    path = decode_path(fields.get("path"))
    extension = PurePosixPath(path).suffix.removeprefix(".") if path else ""
    beets_format = fields.get("format")
    codec_type = (
        ContentType.try_parse(beets_format)
        if isinstance(beets_format, str) and beets_format
        else ContentType.UNKNOWN
    )
    content_type = ContentType.try_parse(extension) if extension else ContentType.UNKNOWN
    if content_type == ContentType.UNKNOWN:
        content_type = codec_type
    # AudioFormat derives its output format string at construction, so pass every value in
    optional: dict[str, int] = {}
    if sample_rate := fields.get("samplerate"):
        optional["sample_rate"] = int(sample_rate)
    if bit_depth := fields.get("bitdepth"):
        optional["bit_depth"] = int(bit_depth)
    if channels := fields.get("channels"):
        optional["channels"] = int(channels)
    if bitrate := fields.get("bitrate"):
        optional["bit_rate"] = int(bitrate) // 1000
    return AudioFormat(content_type=content_type, codec_type=codec_type, **optional)  # type: ignore[arg-type]


def album_checksum(album: BeetsRow) -> str:
    """
    Return a checksum that changes whenever the album row or its attributes change.

    :param album: The beets album row.
    """
    return _digest([album.fields, album.flex])


def item_checksum(
    item: BeetsRow, album: BeetsRow | None, favorite_rating_threshold: float | None
) -> str:
    """
    Return a checksum that changes whenever the item, its album or its favorite outcome change.

    :param item: The beets item row.
    :param album: The item's album row, or None for singletons.
    :param favorite_rating_threshold: The configured favorite threshold, or None when ratings
        are ignored.
    """
    return _digest(
        [
            item.fields,
            item.flex,
            album.fields if album else None,
            album.flex if album else None,
            parse_favorite(item.flex, favorite_rating_threshold),
        ]
    )


def parse_artist(
    name: str, ctx: ParseContext, sort_name: str | None = None, mbid: str | None = None
) -> Artist:
    """
    Return an artist keyed by name.

    :param name: The artist name.
    :param ctx: The provider parse context.
    :param sort_name: The artist sort name, if beets has one.
    :param mbid: The artist's MusicBrainz id, if beets has one.
    """
    artist = Artist(
        item_id=name,
        provider=ctx.instance_id,
        name=name,
        sort_name=sort_name or None,
        provider_mappings={
            ProviderMapping(
                item_id=name,
                provider_domain=ctx.domain,
                provider_instance=ctx.instance_id,
                in_library=True,
            )
        },
    )
    if cleaned_mbid := clean_mbid(mbid, f"beets artist {name}"):
        artist.mbid = cleaned_mbid
    return artist


def parse_album(album: BeetsRow, ctx: ParseContext) -> Album:
    """
    Return the MA album for a beets album row.

    :param album: The beets album row.
    :param ctx: The provider parse context.
    """
    fields = album.fields
    item_id = album_item_id(ctx, album.id)
    name, version = parse_title_and_version(_text(fields.get("album")) or str(album.id))
    result = Album(
        item_id=item_id,
        provider=ctx.instance_id,
        name=name,
        version=version,
        provider_mappings={
            ProviderMapping(
                item_id=item_id,
                provider_domain=ctx.domain,
                provider_instance=ctx.instance_id,
                in_library=True,
            )
        },
    )
    if fields.get("comp"):
        result.artists = UniqueList(
            [parse_artist(VARIOUS_ARTISTS_NAME, ctx, mbid=VARIOUS_ARTISTS_MBID)]
        )
    else:
        result.artists = _artists_from_fields(
            fields,
            ("albumartists", "albumartists_sort", "mb_albumartistids"),
            ("albumartist", "albumartist_sort", "mb_albumartistid"),
            ctx,
        )
    result.album_type = parse_album_type(fields.get("albumtypes"), fields.get("albumtype"))
    if year := fields.get("original_year") or fields.get("year"):
        result.year = int(year)
    if mbid := clean_mbid(_text(fields.get("mb_albumid")), f"beets album {album.id}"):
        result.mbid = mbid
    if release_group := clean_mbid(
        _text(fields.get("mb_releasegroupid")), f"beets album {album.id}"
    ):
        result.external_ids.add((ExternalID.MB_RELEASEGROUP, release_group))
    if barcode := _text(fields.get("barcode")):
        result.external_ids.add((ExternalID.BARCODE, barcode))
    if asin := _text(fields.get("asin")):
        result.external_ids.add((ExternalID.ASIN, asin))
    result.metadata.label = _text(fields.get("label"))
    result.metadata.genres = _genres(fields)
    if fields.get("artpath"):
        result.metadata.images = UniqueList(
            [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=f"{IMAGE_PATH_PREFIX}{album.id}?cs={album_checksum(album)}",
                    provider=ctx.instance_id,
                    remotely_accessible=False,
                )
            ]
        )
    return result


def parse_track(item: BeetsRow, album: BeetsRow | None, ctx: ParseContext, checksum: str) -> Track:
    """
    Return the MA track for a beets item row.

    :param item: The beets item row.
    :param album: The item's album row, or None for singletons.
    :param ctx: The provider parse context.
    :param checksum: The item checksum to store in the provider mapping.
    :raises InvalidDataError: If neither the item nor its album names an artist.
    """
    fields = item.fields
    item_id = track_item_id(ctx, item.id)
    path = decode_path(fields.get("path"))
    title = _text(fields.get("title")) or (PurePosixPath(path).stem if path else str(item.id))
    name, version = parse_title_and_version(title)
    track = Track(
        item_id=item_id,
        provider=ctx.instance_id,
        name=name,
        version=version,
        provider_mappings={
            ProviderMapping(
                item_id=item_id,
                provider_domain=ctx.domain,
                provider_instance=ctx.instance_id,
                audio_format=parse_audio_format(fields),
                details=checksum,
                in_library=True,
            )
        },
        disc_number=int(fields.get("disc") or 0),
        track_number=int(fields.get("track") or 0),
        date_added=from_utc_timestamp(float(fields["added"])) if fields.get("added") else None,
    )
    track.duration = int(fields.get("length") or 0)
    track.favorite = parse_favorite(item.flex, ctx.favorite_rating_threshold)
    if album is not None and _text(album.fields.get("album")):
        track.album = parse_album(album, ctx)
    track.artists = _artists_from_fields(
        fields,
        ("artists", "artists_sort", "mb_artistids"),
        ("artist", "artist_sort", "mb_artistid"),
        ctx,
    )
    if isinstance(track.album, Album):
        if not track.artists:
            track.artists = UniqueList(track.album.artists)
        elif not track.album.artists:
            track.album.artists = UniqueList(track.artists)
    if not track.artists:
        msg = f"beets item {item.id} has no artist"
        raise InvalidDataError(msg)
    if mbid := clean_mbid(_text(fields.get("mb_trackid")), f"beets item {item.id}"):
        track.mbid = mbid
    if isrc := _text(fields.get("isrc")):
        track.external_ids.add((ExternalID.ISRC, isrc))
    if acoustid := _text(fields.get("acoustid_id")):
        track.external_ids.add((ExternalID.ACOUSTID, acoustid))
    track.metadata.genres = _genres(fields)
    track.metadata.style = _text(fields.get("style"))
    track.metadata.grouping = _text(fields.get("grouping"))
    track.metadata.lyrics = _text(fields.get("lyrics"))
    track.metadata.label = _text(fields.get("label"))
    track.metadata.description = _text(fields.get("comments"))
    track.metadata.release_date = _release_date(fields)
    track.metadata.mood = _text(item.flex.get("mood"))
    return track


def _artists_from_fields(
    fields: Mapping[str, Any],
    multi_keys: tuple[str, str, str],
    single_keys: tuple[str, str, str],
    ctx: ParseContext,
) -> UniqueList[Artist | ItemMapping]:
    """Return artists from beets' list fields, falling back to the single-valued fields."""
    result: UniqueList[Artist | ItemMapping] = UniqueList()
    names = split_multi_value(fields.get(multi_keys[0]))
    if any(name.strip() for name in names):
        sort_names = split_multi_value(fields.get(multi_keys[1]))
        mbids = split_multi_value(fields.get(multi_keys[2]))
        for index, raw_name in enumerate(names):
            if name := raw_name.strip():
                result.append(
                    parse_artist(name, ctx, value_at(sort_names, index), value_at(mbids, index))
                )
        return result
    if single_name := _text(fields.get(single_keys[0])):
        result.append(
            parse_artist(
                single_name,
                ctx,
                _text(fields.get(single_keys[1])),
                _text(fields.get(single_keys[2])),
            )
        )
    return result


def _genres(fields: Mapping[str, Any]) -> set[str] | None:
    """Return the genres of a row from the genres list or the legacy genre column."""
    raw = fields.get("genres") or fields.get("genre")
    genres = {genre.strip() for genre in split_multi_value(raw) if genre.strip()}
    return genres or None


def _release_date(fields: Mapping[str, Any]) -> datetime | None:
    """Return the release date from beets' year, month and day fields."""
    year = int(fields.get("year") or 0)
    if not year:
        return None
    try:
        return datetime(
            year, int(fields.get("month") or 1), int(fields.get("day") or 1), tzinfo=UTC
        )
    except ValueError:
        return None


def _text(value: object) -> str | None:
    """Return a stripped non-empty string, or None."""
    if value is None:
        return None
    return str(value).strip() or None


def _float(value: object) -> float | None:
    """Return value as a float, or None when it is missing or not numeric."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except TypeError, ValueError:
        return None


def _digest(payload: object) -> str:
    """Return a stable SHA-1 hex digest of a JSON-serializable payload."""
    encoded = json.dumps(payload, sort_keys=True, default=_json_default)
    return hashlib.sha1(encoded.encode(), usedforsecurity=False).hexdigest()


def _json_default(value: object) -> str:
    """Serialize the values beets rows hold that JSON does not support."""
    if isinstance(value, bytes):
        return value.hex()
    return str(value)
