"""Parsers for YouSee Musik API responses."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import AlbumType, ContentType, ExternalID, ImageType
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    MediaItemImage,
    Playlist,
    ProviderMapping,
    Track,
)

from music_assistant.constants import (
    VARIOUS_ARTISTS_MBID,
    VARIOUS_ARTISTS_NAME,
)
from music_assistant.helpers.util import infer_album_type, parse_title_and_version, try_parse_int
from music_assistant.providers.yousee.constants import (
    CONF_QUALITY,
    VARIOUS_ARTISTS_ID,
)

if TYPE_CHECKING:
    from music_assistant.providers.yousee.api_client import JsonLike
    from music_assistant.providers.yousee.provider import YouSeeMusikProvider


async def parse_track(provider: YouSeeMusikProvider, track_obj: JsonLike) -> Track:
    """Parse track data from YouSee API response."""
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
                    bit_rate=try_parse_int(provider.config.get_value(CONF_QUALITY)),
                ),
                url=track_obj.get("share"),
            )
        },
    )

    if isrc := track_obj.get("isrc"):
        track.external_ids.add((ExternalID.ISRC, isrc))

    if "artist" in track_obj:
        artist = parse_artist(provider, track_obj["artist"])
        track.artists.append(artist)

    for feat_artist_obj in track_obj.get("featuredArtists", {}).get("items", []):
        feat_artist = parse_artist(provider, feat_artist_obj)
        track.artists.append(feat_artist)

    if "album" in track_obj:
        album = await parse_album(provider, track_obj["album"])
        track.album = album

    if track_genre := track_obj.get("genre"):
        track.metadata.genres = set(track_genre)

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


def parse_artist(provider: YouSeeMusikProvider, artist_obj: JsonLike) -> Artist:
    """Parse artist data from YouSee API response."""
    artist = Artist(
        item_id=artist_obj["id"],
        provider=provider.instance_id,
        name=artist_obj["title"],
        uri=artist_obj.get("share"),
        provider_mappings={
            ProviderMapping(
                item_id=str(artist_obj["id"]),
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
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


async def parse_album(provider: YouSeeMusikProvider, album_obj: JsonLike) -> Album:
    """Parse album data from YouSee API response."""
    if "artist" not in album_obj:
        return await provider.get_album(str(album_obj["id"]))

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
                audio_format=AudioFormat(
                    content_type=ContentType.MP4,
                    bit_rate=try_parse_int(provider.config.get_value(CONF_QUALITY)),
                ),
                url=album_obj.get("share"),
            )
        },
        is_playable=album_obj.get("available", True),
    )

    if album_upc := album_obj.get("upc"):
        album.external_ids.add((ExternalID.BARCODE, album_upc))

    album.artists.append(parse_artist(provider, album_obj["artist"]))

    for feat_artist_obj in album_obj.get("featuredArtists", {}).get("items", []):
        feat_artist = parse_artist(provider, feat_artist_obj)
        album.artists.append(feat_artist)

    if album_genre := album_obj.get("genre"):
        album.metadata.genres = set(album_genre)

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


async def parse_playlist(provider: YouSeeMusikProvider, playlist_obj: JsonLike) -> Playlist:
    """Parse playlist data from YouSee API response."""
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


async def parse_lyrics(lyrics: list[JsonLike]) -> tuple[str | None, str | None]:
    """Parse the YouSee lyrics payload and extract the lyric text in two formats if possible.

    Returns:
        Tuple[str | None, str | None]: lyrics (plain) and lyrics_lrc, if present.
    """
    if not lyrics:
        return None, None

    plain = ""
    lrc = ""

    for item in lyrics:
        line = item.get("line", "")
        if (start_ms := item.get("startInMs")) is not None:
            minutes = start_ms // 60000
            seconds = (start_ms % 60000) // 1000
            milliseconds = start_ms % 1000
            lrc += f"[{minutes:02}:{seconds:02}.{milliseconds:02}] {line}\n"

        plain += line + "\n"

    plain = plain.strip()
    lrc = lrc.strip()

    return plain if plain else None, lrc if lrc else None
