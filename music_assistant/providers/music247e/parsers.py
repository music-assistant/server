"""Parsers for the 24-7 (247e) music backend API responses."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import AlbumType, ContentType, ExternalID, ImageType, MediaType
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    ItemMapping,
    MediaItemImage,
    Playlist,
    ProviderMapping,
    Track,
)

from music_assistant.constants import (
    VARIOUS_ARTISTS_MBID,
    VARIOUS_ARTISTS_NAME,
)
from music_assistant.helpers.lyrics import convert_to_lrc_lyrics
from music_assistant.helpers.util import infer_album_type, parse_title_and_version, try_parse_int
from music_assistant.providers.music247e.constants import (
    VARIOUS_ARTISTS_ID,
)

if TYPE_CHECKING:
    from music_assistant.providers.music247e.api_client import JsonLike
    from music_assistant.providers.music247e.provider import Music247eProvider


def parse_track(provider: Music247eProvider, track_obj: JsonLike) -> Track:
    """Parse track data from the 24-7 (247e) API response."""
    track = Track(
        item_id=track_obj["id"],
        provider=provider.instance_id,
        name=track_obj["title"],
        duration=track_obj.get("duration", 0),
        provider_mappings={
            ProviderMapping(
                item_id=str(track_obj["id"]),
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
                available=track_obj.get("availableToStream", True),
                audio_format=AudioFormat(
                    content_type=ContentType.MP4,
                    bit_rate=try_parse_int(provider.playback_quality),
                ),
                url=track_obj.get("share"),
            )
        },
    )

    if isrc := track_obj.get("isrc"):
        track.external_ids.add((ExternalID.ISRC, isrc))

    if "artist" in track_obj:
        artist = _parse_item_mapping(provider, track_obj["artist"], MediaType.ARTIST)
        track.artists.append(artist)

    for feat_artist_obj in track_obj.get("featuredArtists", {}).get("items", []):
        feat_artist = _parse_item_mapping(provider, feat_artist_obj, MediaType.ARTIST)
        track.artists.append(feat_artist)

    if "album" in track_obj:
        album = _parse_item_mapping(provider, track_obj["album"], MediaType.ALBUM)
        track.album = album

    if track_genre := track_obj.get("genre"):
        track.metadata.genres = {track_genre} if isinstance(track_genre, str) else set(track_genre)

    if track_label := track_obj.get("label"):
        track.metadata.label = track_label

    if track_obj.get("cover"):
        track.metadata.add_image(
            MediaItemImage(
                type=ImageType.THUMB,
                path=track_obj["cover"],
                remotely_accessible=True,
                provider=provider.instance_id,
            )
        )

    return track


def parse_artist(provider: Music247eProvider, artist_obj: JsonLike) -> Artist:
    """Parse artist data from the 24-7 (247e) API response."""
    artist = Artist(
        item_id=artist_obj["id"],
        provider=provider.instance_id,
        name=artist_obj["title"],
        provider_mappings={
            ProviderMapping(
                item_id=str(artist_obj["id"]),
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
                url=artist_obj.get("share"),
            )
        },
    )

    if artist.item_id == VARIOUS_ARTISTS_ID:
        artist.mbid = VARIOUS_ARTISTS_MBID
        artist.name = VARIOUS_ARTISTS_NAME

    if artist_obj.get("cover"):
        artist.metadata.add_image(
            MediaItemImage(
                type=ImageType.THUMB,
                path=artist_obj["cover"],
                remotely_accessible=True,
                provider=provider.instance_id,
            )
        )

    return artist


def parse_album(provider: Music247eProvider, album_obj: JsonLike) -> Album:
    """Parse album data from the 24-7 (247e) API response."""
    name, version = parse_title_and_version(album_obj["title"])
    album = Album(
        item_id=album_obj["id"],
        provider=provider.instance_id,
        name=name,
        version=version,
        provider_mappings={
            ProviderMapping(
                item_id=str(album_obj["id"]),
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
                available=album_obj.get("available", True),
                audio_format=AudioFormat(
                    content_type=ContentType.MP4,
                    bit_rate=try_parse_int(provider.playback_quality),
                ),
                url=album_obj.get("share"),
            )
        },
        is_playable=album_obj.get("available", True),
    )

    if album_upc := album_obj.get("upc"):
        album.external_ids.add((ExternalID.BARCODE, album_upc))

    album.artists.append(_parse_item_mapping(provider, album_obj["artist"], MediaType.ARTIST))

    for feat_artist_obj in album_obj.get("featuredArtists", {}).get("items", []):
        feat_artist = _parse_item_mapping(provider, feat_artist_obj, MediaType.ARTIST)
        album.artists.append(feat_artist)

    if album_genre := album_obj.get("genre"):
        album.metadata.genres = {album_genre} if isinstance(album_genre, str) else set(album_genre)

    if album_obj.get("type") == "COMPILATION":
        album.album_type = AlbumType.COMPILATION
    elif album_obj.get("type") == "SINGLE":
        album.album_type = AlbumType.SINGLE
    elif album_obj.get("type") == "REGULAR":
        album.album_type = AlbumType.ALBUM

    inferred_type = infer_album_type(name, version)
    if inferred_type in (AlbumType.SOUNDTRACK, AlbumType.LIVE):
        album.album_type = inferred_type

    if album_obj.get("cover"):
        album.metadata.add_image(
            MediaItemImage(
                type=ImageType.THUMB,
                path=album_obj["cover"],
                remotely_accessible=True,
                provider=provider.instance_id,
            )
        )

    if album_label := album_obj.get("label"):
        album.metadata.label = album_label

    if album_obj.get("releaseDate"):
        album.year = try_parse_int(album_obj["releaseDate"][:4])

    return album


def parse_playlist(provider: Music247eProvider, playlist_obj: JsonLike) -> Playlist:
    """Parse playlist data from the 24-7 (247e) API response."""
    playlist = Playlist(
        item_id=str(playlist_obj["id"]),
        provider=provider.instance_id,
        name=playlist_obj["title"],
        is_editable=playlist_obj["isOwned"],
        provider_mappings={
            ProviderMapping(
                item_id=str(playlist_obj["id"]),
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
                url=playlist_obj["share"],
                is_unique=playlist_obj["isOwned"],
            )
        },
    )

    if playlist_obj.get("description"):
        playlist.metadata.description = playlist_obj["description"]

    if playlist_obj.get("cover"):
        playlist.metadata.add_image(
            MediaItemImage(
                type=ImageType.THUMB,
                path=playlist_obj["cover"],
                remotely_accessible=True,
                provider=provider.instance_id,
            )
        )

    return playlist


def parse_lyrics(lyrics: list[JsonLike]) -> tuple[str | None, str | None]:
    """
    Parse the 24-7 (247e) lyrics payload and extract the lyric text in two formats if possible.

    :return: Plain lyrics and LRC lyrics, each None if unavailable.
    """
    if not lyrics:
        return None, None

    plain = ""
    timed = [
        (item.get("line", ""), item["startInMs"])
        for item in lyrics
        if item.get("startInMs") is not None
    ]
    lrc = convert_to_lrc_lyrics(timed)

    for item in lyrics:
        line = item.get("line", "")
        plain += line + "\n"

    plain = plain.strip()
    lrc = lrc.strip()

    return plain or None, lrc or None


def _parse_item_mapping(
    provider: Music247eProvider, item_obj: JsonLike, media_type: MediaType
) -> ItemMapping:
    """Parse a compact nested artist or album reference."""
    item_id = str(item_obj["id"])
    name, version = (
        parse_title_and_version(item_obj["title"])
        if media_type == MediaType.ALBUM
        else (item_obj["title"], "")
    )
    if media_type == MediaType.ARTIST and item_id == VARIOUS_ARTISTS_ID:
        name = VARIOUS_ARTISTS_NAME
    mapping = ItemMapping(
        media_type=media_type,
        item_id=item_id,
        provider=provider.instance_id,
        name=name,
        version=version,
        available=item_obj.get("available", True),
    )
    if cover := item_obj.get("cover"):
        mapping.image = MediaItemImage(
            type=ImageType.THUMB,
            path=cover,
            remotely_accessible=True,
            provider=provider.instance_id,
        )
    if media_type == MediaType.ALBUM and (release_date := item_obj.get("releaseDate")):
        mapping.year = try_parse_int(release_date[:4])
    return mapping
