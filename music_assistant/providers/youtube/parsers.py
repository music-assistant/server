"""Parsing utilities to convert yt-dlp entries into Music Assistant model objects."""

from __future__ import annotations

from typing import Any

from music_assistant_models.enums import AlbumType, ContentType, ImageType, MediaType
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    ItemMapping,
    MediaItemImage,
    Playlist,
    ProviderMapping,
    Track,
    UniqueList,
)

from .constants import YT_DOMAIN


def parse_track(entry: dict[str, Any], provider_domain: str, instance_id: str) -> Track | None:
    """Parse a yt-dlp video entry into a Track model object.

    :param entry: Raw yt-dlp video info dict.
    :param provider_domain: Domain of the provider instance.
    :param instance_id: Instance ID of the provider.
    """
    video_id = entry.get("id")
    if not video_id:
        return None
    title = entry.get("title") or video_id
    uploader = entry.get("uploader") or entry.get("channel") or "Unknown"
    channel_id = entry.get("channel_id") or entry.get("uploader_id") or uploader
    track = Track(
        item_id=video_id,
        provider=instance_id,
        name=title,
        duration=int(entry.get("duration") or 0),
        provider_mappings={
            ProviderMapping(
                item_id=video_id,
                provider_domain=provider_domain,
                provider_instance=instance_id,
                url=f"{YT_DOMAIN}/watch?v={video_id}",
                audio_format=AudioFormat(content_type=ContentType.M4A),
            )
        },
    )
    track.artists = UniqueList(
        [
            ItemMapping(
                media_type=MediaType.ARTIST,
                item_id=channel_id,
                provider=instance_id,
                name=uploader,
            )
        ]
    )
    apply_thumbnails(track.metadata, entry, instance_id)
    if description := entry.get("description"):
        track.metadata.description = description
    return track


def parse_channel_as_artist(
    entry: dict[str, Any], provider_domain: str, instance_id: str
) -> Artist:
    """Parse a yt-dlp channel entry into an Artist model object.

    :param entry: Raw yt-dlp channel info dict.
    :param provider_domain: Domain of the provider instance.
    :param instance_id: Instance ID of the provider.
    """
    channel_id = entry.get("channel_id") or entry.get("uploader_id") or entry.get("id", "")
    name = entry.get("channel") or entry.get("uploader") or entry.get("title", channel_id)
    artist = Artist(
        item_id=channel_id,
        provider=instance_id,
        name=name,
        provider_mappings={
            ProviderMapping(
                item_id=channel_id,
                provider_domain=provider_domain,
                provider_instance=instance_id,
                url=f"{YT_DOMAIN}/channel/{channel_id}",
            )
        },
    )
    apply_thumbnails(artist.metadata, entry, instance_id)
    return artist


def parse_playlist_as_album(entry: dict[str, Any], provider_domain: str, instance_id: str) -> Album:
    """Parse a YouTube playlist entry into an Album model object.

    :param entry: Raw yt-dlp or API playlist info dict.
    :param provider_domain: Domain of the provider instance.
    :param instance_id: Instance ID of the provider.
    """
    playlist_id = entry.get("id", "")
    title = entry.get("title") or playlist_id
    channel = entry.get("channel") or entry.get("uploader") or "Unknown"
    channel_id = entry.get("channel_id") or entry.get("uploader_id") or channel
    album = Album(
        item_id=playlist_id,
        provider=instance_id,
        name=title,
        album_type=AlbumType.COMPILATION,
        provider_mappings={
            ProviderMapping(
                item_id=playlist_id,
                provider_domain=provider_domain,
                provider_instance=instance_id,
                url=f"{YT_DOMAIN}/playlist?list={playlist_id}",
            )
        },
    )
    album.artists = UniqueList(
        [
            ItemMapping(
                media_type=MediaType.ARTIST,
                item_id=channel_id,
                provider=instance_id,
                name=channel,
            )
        ]
    )
    apply_thumbnails(album.metadata, entry, instance_id)
    if description := entry.get("description"):
        album.metadata.description = description
    return album


def parse_playlist(entry: dict[str, Any], provider_domain: str, instance_id: str) -> Playlist:
    """Parse a YouTube playlist entry into a Playlist model object.

    :param entry: Raw yt-dlp or API playlist info dict.
    :param provider_domain: Domain of the provider instance.
    :param instance_id: Instance ID of the provider.
    """
    playlist_id = entry.get("id", "")
    title = entry.get("title") or playlist_id
    channel = entry.get("channel") or entry.get("uploader") or "Unknown"
    playlist = Playlist(
        item_id=playlist_id,
        provider=instance_id,
        name=title,
        owner=channel,
        provider_mappings={
            ProviderMapping(
                item_id=playlist_id,
                provider_domain=provider_domain,
                provider_instance=instance_id,
                url=f"{YT_DOMAIN}/playlist?list={playlist_id}",
            )
        },
    )
    apply_thumbnails(playlist.metadata, entry, instance_id)
    if description := entry.get("description"):
        playlist.metadata.description = description
    return playlist


def apply_thumbnails(metadata: Any, entry: dict[str, Any], instance_id: str) -> None:
    """Populate metadata.images from a yt-dlp entry's thumbnail fields.

    :param metadata: MediaItemMetadata object to populate.
    :param entry: Raw yt-dlp info dict containing thumbnail data.
    :param instance_id: Instance ID of the provider.
    """
    if thumbnails := entry.get("thumbnails"):
        metadata.images = parse_thumbnails(thumbnails, instance_id)
    elif thumbnail := entry.get("thumbnail"):
        if thumbnail.startswith("//"):
            thumbnail = f"https:{thumbnail}"
        metadata.images = [
            MediaItemImage(
                type=ImageType.THUMB,
                path=thumbnail,
                provider=instance_id,
                remotely_accessible=True,
            )
        ]


def parse_thumbnails(thumbnails: list[dict[str, Any]], instance_id: str) -> list[MediaItemImage]:
    """Parse a yt-dlp thumbnail list into MediaItemImage objects.

    :param thumbnails: List of thumbnail dicts from yt-dlp.
    :param instance_id: Instance ID of the provider.
    """
    result: list[MediaItemImage] = []
    seen: set[str] = set()
    for img in sorted(thumbnails, key=lambda t: t.get("width") or 0, reverse=True):
        url: str | None = img.get("url")
        if not url or url in seen:
            continue
        if url.startswith("//"):
            url = f"https:{url}"
        seen.add(url)
        width: int = img.get("width") or 0
        height: int = img.get("height") or 0
        image_type = ImageType.LANDSCAPE if height > 0 and width / height > 2.0 else ImageType.THUMB
        result.append(
            MediaItemImage(
                type=image_type,
                path=url,
                provider=instance_id,
                remotely_accessible=True,
            )
        )
    return result
