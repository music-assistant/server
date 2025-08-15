"""Parsing utilities to convert Spotify API responses into Music Assistant model objects."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

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
from music_assistant_models.unique_list import UniqueList

from music_assistant.helpers.util import parse_title_and_version

if TYPE_CHECKING:
    from .provider import SpotifyProvider


def parse_artist(artist_obj: dict[str, Any], provider: SpotifyProvider) -> Artist:
    """Parse spotify artist object to generic layout."""
    artist = Artist(
        item_id=artist_obj["id"],
        provider=provider.lookup_key,
        name=artist_obj["name"] or artist_obj["id"],
        provider_mappings={
            ProviderMapping(
                item_id=artist_obj["id"],
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
                url=artist_obj["external_urls"]["spotify"],
            )
        },
    )
    if "genres" in artist_obj:
        artist.metadata.genres = set(artist_obj["genres"])
    if artist_obj.get("images"):
        for img in artist_obj["images"]:
            img_url = img["url"]
            if "2a96cbd8b46e442fc41c2b86b821562f" not in img_url:
                artist.metadata.images = UniqueList(
                    [
                        MediaItemImage(
                            type=ImageType.THUMB,
                            path=img_url,
                            provider=provider.lookup_key,
                            remotely_accessible=True,
                        )
                    ]
                )
                break
    return artist


def parse_album(album_obj: dict[str, Any], provider: SpotifyProvider) -> Album:
    """Parse spotify album object to generic layout."""
    name, version = parse_title_and_version(album_obj["name"])
    album = Album(
        item_id=album_obj["id"],
        provider=provider.lookup_key,
        name=name,
        version=version,
        provider_mappings={
            ProviderMapping(
                item_id=album_obj["id"],
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
                audio_format=AudioFormat(content_type=ContentType.OGG, bit_rate=320),
                url=album_obj["external_urls"]["spotify"],
            )
        },
    )
    if "external_ids" in album_obj and album_obj["external_ids"].get("upc"):
        album.external_ids.add((ExternalID.BARCODE, "0" + album_obj["external_ids"]["upc"]))
    if "external_ids" in album_obj and album_obj["external_ids"].get("ean"):
        album.external_ids.add((ExternalID.BARCODE, album_obj["external_ids"]["ean"]))

    for artist_obj in album_obj["artists"]:
        if not artist_obj.get("name") or not artist_obj.get("id"):
            continue
        album.artists.append(parse_artist(artist_obj, provider))

    with contextlib.suppress(ValueError):
        album.album_type = AlbumType(album_obj["album_type"])

    if "genres" in album_obj:
        album.metadata.genres = set(album_obj["genres"])
    if album_obj.get("images"):
        album.metadata.images = UniqueList(
            [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=album_obj["images"][0]["url"],
                    provider=provider.lookup_key,
                    remotely_accessible=True,
                )
            ]
        )
    if "label" in album_obj:
        album.metadata.label = album_obj["label"]
    if album_obj.get("release_date"):
        album.year = int(album_obj["release_date"].split("-")[0])
    if album_obj.get("copyrights"):
        album.metadata.copyright = album_obj["copyrights"][0]["text"]
    if album_obj.get("explicit"):
        album.metadata.explicit = album_obj["explicit"]
    return album


def parse_track(
    track_obj: dict[str, Any],
    provider: SpotifyProvider,
    artist: Artist | None = None,
) -> Track:
    """Parse spotify track object to generic layout."""
    name, version = parse_title_and_version(track_obj["name"])
    track = Track(
        item_id=track_obj["id"],
        provider=provider.lookup_key,
        name=name,
        version=version,
        duration=track_obj["duration_ms"] / 1000,
        provider_mappings={
            ProviderMapping(
                item_id=track_obj["id"],
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
                audio_format=AudioFormat(
                    content_type=ContentType.OGG,
                    bit_rate=320,
                ),
                url=track_obj["external_urls"]["spotify"],
                available=not track_obj["is_local"] and track_obj["is_playable"],
            )
        },
        disc_number=track_obj.get("disc_number", 0),
        track_number=track_obj.get("track_number", 0),
    )
    if isrc := track_obj.get("external_ids", {}).get("isrc"):
        track.external_ids.add((ExternalID.ISRC, isrc))

    if artist:
        track.artists.append(artist)
    for track_artist in track_obj.get("artists", []):
        if not track_artist.get("name") or not track_artist.get("id"):
            continue
        artist_parsed = parse_artist(track_artist, provider)
        if artist_parsed and artist_parsed.item_id not in {x.item_id for x in track.artists}:
            track.artists.append(artist_parsed)

    track.metadata.explicit = track_obj["explicit"]
    if "preview_url" in track_obj:
        track.metadata.preview = track_obj["preview_url"]
    if "album" in track_obj:
        track.album = parse_album(track_obj["album"], provider)
        if track_obj["album"].get("images"):
            track.metadata.images = UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=track_obj["album"]["images"][0]["url"],
                        provider=provider.lookup_key,
                        remotely_accessible=True,
                    )
                ]
            )
    if track_obj.get("copyright"):
        track.metadata.copyright = track_obj["copyright"]
    if track_obj.get("explicit"):
        track.metadata.explicit = True
    if track_obj.get("popularity"):
        track.metadata.popularity = track_obj["popularity"]
    return track


def parse_playlist(playlist_obj: dict[str, Any], provider: SpotifyProvider) -> Playlist:
    """Parse spotify playlist object to generic layout."""
    is_editable = (
        playlist_obj["owner"]["id"] == provider._sp_user["id"] or playlist_obj["collaborative"]
    )
    playlist = Playlist(
        item_id=playlist_obj["id"],
        provider=provider.instance_id if is_editable else provider.lookup_key,
        name=playlist_obj["name"],
        owner=playlist_obj["owner"]["display_name"],
        provider_mappings={
            ProviderMapping(
                item_id=playlist_obj["id"],
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
                url=playlist_obj["external_urls"]["spotify"],
            )
        },
        is_editable=is_editable,
    )
    if playlist_obj.get("images"):
        playlist.metadata.images = UniqueList(
            [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=playlist_obj["images"][0]["url"],
                    provider=provider.lookup_key,
                    remotely_accessible=True,
                )
            ]
        )
    if playlist.owner is None:
        playlist.owner = provider._sp_user["display_name"]
    playlist.cache_checksum = str(playlist_obj["snapshot_id"])
    return playlist
