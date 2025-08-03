"""
Parsers for the Niconico provider in Music Assistant.

This module contains functions to parse various Niconico objects such as playlists,
tracks, and artists into Music Assistant media items.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from music_assistant_models.enums import (
    ImageType,
    LinkType,
)
from music_assistant_models.media_items import (
    Album,
    Artist,
    MediaItemImage,
    MediaItemLink,
    MediaItemMetadata,
    Playlist,
    ProviderMapping,
    Track,
)
from music_assistant_models.unique_list import UniqueList
from niconico.objects.nvapi import SeriesData
from niconico.objects.user import NicoUser, UserMylistItem, UserSeriesItem
from niconico.objects.video import EssentialVideo, Mylist, Owner, VideoThumbnail
from niconico.objects.video.search import EssentialMylist, EssentialSeries, SnapshotVideoItem

from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers.niconico.helpers import AlbumWithTracks, PlaylistWithTracks

if TYPE_CHECKING:
    from niconico.objects.user import (
        UserMylistItem,
        UserSeriesItem,
    )
    from niconico.objects.video import Mylist


def parse_playlist_by_mylist(
    provider: MusicProvider, mylist: UserMylistItem | Mylist | EssentialMylist
) -> Playlist:
    """Parse a NicoNico UserMylistItem into a Playlist."""
    playlist = Playlist(
        item_id=str(mylist.id_),
        provider=provider.lookup_key,
        name=(mylist.title if isinstance(mylist, EssentialMylist) else mylist.name),
        owner=mylist.owner.id_ or "",
        is_editable=True,  # Own mylists are editable by default
        metadata=MediaItemMetadata(
            description=getattr(mylist, "description", ""),
            links={
                MediaItemLink(
                    type=LinkType.WEBSITE,
                    url=f"https://www.nicovideo.jp/mylist/{mylist.id_}",
                )
            },
        ),
        provider_mappings=_create_provider_mapping(
            item_id=str(mylist.id_),
            provider=provider,
            available=True,
            url_path="mylist",
        ),
    )

    if mylist.owner.icon_url:
        if not playlist.metadata.images:
            playlist.metadata.images = UniqueList()
        playlist.metadata.images.append(
            MediaItemImage(
                type=ImageType.THUMB,
                path=mylist.owner.icon_url,
                provider=provider.lookup_key,
                remotely_accessible=True,
            )
        )
    return playlist


def parse_following_playlist_by_mylist(
    provider: MusicProvider, mylist: UserMylistItem | Mylist | EssentialMylist
) -> Playlist:
    """Parse a NicoNico UserMylistItem from following users into a read-only Playlist."""
    playlist = parse_playlist_by_mylist(provider, mylist)
    # Mark following mylists as non-editable
    playlist.is_editable = False
    return playlist


def parse_album_by_series(
    provider: MusicProvider, series: SeriesData | UserSeriesItem | EssentialSeries
) -> Album:
    """Parse a NicoNico SeriesData, UserSeriesItem, or EssentialSeries into an Album."""
    # Extract common data based on series type
    if isinstance(series, SeriesData):
        item_id = str(series.detail.id_)
        name = series.detail.title
        description = series.detail.description or ""
        thumbnail_url = series.detail.thumbnail_url
        series_owner = series.detail.owner
        owner_id = series_owner.id_ if series_owner else None
        owner_name = None
        if series_owner:
            if series_owner.type_ == "user" and series_owner.user:
                owner_name = series_owner.user.nickname
            elif series_owner.type_ == "channel" and series_owner.channel:
                owner_name = series_owner.channel.name
    elif isinstance(series, EssentialSeries):
        item_id = str(series.id_)
        name = series.title
        description = series.description or ""
        thumbnail_url = series.thumbnail_url
        essential_owner = series.owner
        owner_id = essential_owner.id_ if essential_owner else None
        owner_name = essential_owner.name if essential_owner else None
    else:  # UserSeriesItem
        item_id = str(series.id_)
        name = series.title
        description = series.description or ""
        thumbnail_url = series.thumbnail_url
        user_owner = series.owner
        owner_id = user_owner.id_ if user_owner else None
        owner_name = None  # UserSeriesItem doesn't seem to have owner name

    # Create album with common structure
    album = Album(
        item_id=item_id,
        provider=provider.lookup_key,
        name=name,
        metadata=MediaItemMetadata(
            description=description,
            links={
                MediaItemLink(
                    type=LinkType.WEBSITE,
                    url=f"https://www.nicovideo.jp/series/{item_id}",
                )
            },
        ),
        provider_mappings=_create_provider_mapping(
            item_id=item_id,
            provider=provider,
            url_path="series",
        ),
    )

    # Add artist (series owner) if available
    if owner_id:
        artist = Artist(
            item_id=str(owner_id),
            provider=provider.lookup_key,
            name=owner_name if owner_name else "",
            provider_mappings=_create_provider_mapping(
                item_id=str(owner_id),
                provider=provider,
                url_path="user",
            ),
        )
        album.artists = UniqueList([artist])

    # Add thumbnail image if available
    if thumbnail_url:
        album.metadata.images = UniqueList(
            [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=thumbnail_url,
                    provider=provider.lookup_key,
                    remotely_accessible=True,
                )
            ]
        )

    return album


def parse_playlist_with_tracks_by_mylist(
    provider: MusicProvider, mylist: Mylist
) -> PlaylistWithTracks:
    """Parse a NicoNico UserMylistItem into a PlaylistWithTracks."""
    playlist = parse_playlist_by_mylist(provider, mylist)
    tracks = []
    for item in mylist.items:
        track = parse_track_by_essential_video(provider, item.video)
        if track:
            tracks.append(track)
    return PlaylistWithTracks(playlist, tracks)


def parse_track_by_essential_video(provider: MusicProvider, video: EssentialVideo) -> Track | None:
    """Parse an EssentialVideo object into a Track."""
    # Skip muted videos
    if video.is_muted:
        return None

    # Calculate popularity using standard formula
    popularity = _calculate_popularity(
        mylist_count=video.count.mylist,
        like_count=video.count.like,
    )

    # Create base track with enhanced metadata
    track = Track(
        item_id=video.id_,
        provider=provider.lookup_key,
        name=video.title,
        duration=video.duration,
        artists=UniqueList([parse_artist(provider, video.owner)]),
        is_playable=video.duration > 0 and not video.is_payment_required,
        metadata=_create_track_metadata(
            description=video.short_description,
            explicit=video.require_sensitive_masking,
            release_date_str=video.registered_at,
            popularity=popularity,
            thumbnail=video.thumbnail,
            video_id=video.id_,
            provider=provider,
        ),
        provider_mappings=_create_provider_mapping(
            item_id=video.id_,
            provider=provider,
            available=not video.is_payment_required and not video.is_muted,
        ),
    )

    # Trigger async tag caching for this video (fire-and-forget)
    if hasattr(provider, "tag_manager"):
        provider.tag_manager.trigger_update(video.id_)

    return track


def _parse_video_thumbnails(
    provider: MusicProvider, thumbnail: VideoThumbnail
) -> UniqueList[MediaItemImage]:
    """Parse video thumbnails into multiple image sizes."""
    images: UniqueList[MediaItemImage] = UniqueList()

    # nhd_url is the largest size, use it as primary
    if thumbnail.nhd_url:
        images.append(
            MediaItemImage(
                type=ImageType.THUMB,
                path=thumbnail.nhd_url,
                provider=provider.lookup_key,
                remotely_accessible=True,
            )
        )

    # large_url as secondary (if different from nhd_url)
    if thumbnail.large_url and thumbnail.large_url != thumbnail.nhd_url:
        images.append(
            MediaItemImage(
                type=ImageType.THUMB,
                path=thumbnail.large_url,
                provider=provider.lookup_key,
                remotely_accessible=True,
            )
        )

    # middle_url and listing_url are same size, skip them if nhd_url exists
    # Only add if nhd_url is not available
    if not thumbnail.nhd_url and thumbnail.middle_url:
        images.append(
            MediaItemImage(
                type=ImageType.THUMB,
                path=thumbnail.middle_url,
                provider=provider.lookup_key,
                remotely_accessible=True,
            )
        )

    return images


def parse_artist(provider: MusicProvider, owner_or_user: Owner | NicoUser) -> Artist:
    """Parse an Owner or NicoUser into an Artist."""
    item_id = str(owner_or_user.id_)
    name = str(owner_or_user.name if isinstance(owner_or_user, Owner) else owner_or_user.nickname)
    icon_url = (
        owner_or_user.icon_url if isinstance(owner_or_user, Owner) else owner_or_user.icons.large
    )
    artist = Artist(
        item_id=item_id,
        provider=provider.lookup_key,
        name=name,
        metadata=MediaItemMetadata(
            description=owner_or_user.description if isinstance(owner_or_user, NicoUser) else None,
        ),
        provider_mappings=_create_provider_mapping(
            item_id=item_id,
            provider=provider,
            available=True,
            url_path="user",
        ),
    )
    # Add icon image if available
    if icon_url:
        artist.metadata.add_image(
            MediaItemImage(
                type=ImageType.THUMB,
                path=icon_url,
                provider=provider.lookup_key,
            )
        )
    # Add links to artist metadata
    artist.metadata.links = {
        MediaItemLink(
            type=LinkType.WEBSITE,
            url=f"https://www.nicovideo.jp/user/{item_id}",
        )
    }
    if isinstance(owner_or_user, NicoUser):
        # Add SNS links if available
        for sns in owner_or_user.sns:
            artist.metadata.links.add(
                MediaItemLink(
                    type=LinkType(sns.type_),
                    url=sns.url,
                )
            )
    return artist


def parse_series_to_album_with_tracks(
    provider: MusicProvider, series_data: SeriesData
) -> AlbumWithTracks:
    """Parse SeriesData to AlbumWithTracks."""
    album = parse_album_by_series(provider, series_data)
    tracks = []
    for item in series_data.items or []:
        track = parse_track_by_essential_video(provider, item.video)
        if track:
            tracks.append(track)
    return AlbumWithTracks(album, tracks)


def _calculate_popularity(
    mylist_count: int | None = None,
    like_count: int | None = None,
) -> int:
    """Calculate popularity score using standard formula.

    Args:
        mylist_count: Number of mylists.
        like_count: Number of likes.
        view_count: Number of views (fallback if mylist/like unavailable).

    Returns:
        Popularity score (0-100).
    """
    # Primary calculation: mylist*3 + like*1 (normalized to 0-100 scale)
    if mylist_count is not None and like_count is not None:
        return min(100, max(0, int((mylist_count * 3 + like_count) / 10)))

    return 0


def _create_track_metadata(
    *,
    description: str | None = None,
    explicit: bool | None = None,
    release_date_str: str | None = None,
    popularity: int | None = None,
    thumbnail: VideoThumbnail | None = None,
    thumbnail_url: str | None = None,
    video_id: str,
    provider: MusicProvider,
) -> MediaItemMetadata:
    """Create track metadata with common fields.

    Args:
        description: Video description.
        explicit: Whether the content is explicit.
        release_date_str: Release date as string (ISO 8601 format).
        popularity: Popularity score.
        thumbnail: VideoThumbnail object for enhanced image support.
        thumbnail_url: Single thumbnail URL (fallback when VideoThumbnail not available).
        video_id: Video ID for link generation.
        provider: Music provider instance.

    Returns:
        MediaItemMetadata object with populated fields.
    """
    metadata = MediaItemMetadata()

    if description:
        metadata.description = description

    if explicit is not None:
        metadata.explicit = explicit

    if release_date_str:
        try:
            # Handle both direct ISO format and Z-suffixed format
            if release_date_str.endswith("Z"):
                clean_date_str = release_date_str.replace("Z", "+00:00")
                metadata.release_date = datetime.fromisoformat(clean_date_str)
            else:
                metadata.release_date = datetime.fromisoformat(release_date_str)
        except (ValueError, AttributeError):
            pass

    if popularity is not None:
        metadata.popularity = popularity

    # Add thumbnail images with enhanced support
    if thumbnail:
        # Use enhanced thumbnail parsing for multiple sizes
        metadata.images = _parse_video_thumbnails(provider, thumbnail)
    elif thumbnail_url:
        # Fallback to single thumbnail URL
        metadata.images = UniqueList(
            [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=thumbnail_url,
                    provider=provider.lookup_key,
                    remotely_accessible=True,
                )
            ]
        )

    # Add video link
    metadata.links = {
        MediaItemLink(
            type=LinkType.WEBSITE,
            url=f"https://www.nicovideo.jp/watch/{video_id}",
        )
    }

    return metadata


def _create_provider_mapping(
    *,
    item_id: str,
    provider: MusicProvider,
    available: bool = True,
    url_path: str | None = None,
) -> set[ProviderMapping]:
    """Create provider mapping for media items.

    Args:
        item_id: Item ID.
        provider: Music provider instance.
        available: Whether the item is available.
        url_path: Custom URL path (e.g., 'watch', 'mylist', 'series', 'user').
                 If None, defaults to 'watch' for backward compatibility.

    Returns:
        Set of ProviderMapping objects.
    """
    if url_path is None:
        url_path = "watch"

    return {
        ProviderMapping(
            item_id=item_id,
            provider_domain=provider.domain,
            provider_instance=provider.instance_id,
            url=f"https://www.nicovideo.jp/{url_path}/{item_id}",
            available=available,
        )
    }


def parse_track_by_snapshot_item(provider: MusicProvider, item: SnapshotVideoItem) -> Track | None:
    """Parse a SnapshotVideoItem into a Track.

    Args:
        provider: The music provider instance.
        item: The snapshot video item to parse.

    Returns:
        Track | None: The parsed track, or None if parsing fails.
    """
    if not item.content_id or not item.title:
        return None

    # Calculate popularity using the same formula as parse_track_by_essential_video
    popularity = _calculate_popularity(
        mylist_count=item.mylist_counter,
        like_count=item.like_counter,
    )

    # Create track with common metadata
    track = Track(
        item_id=item.content_id,
        provider=provider.lookup_key,
        name=item.title,
        duration=item.length_seconds or 0,
        # Note: SnapshotVideoItem doesn't have owner info, so no artists
        metadata=_create_track_metadata(
            description=item.description,
            release_date_str=item.start_time,
            popularity=popularity,
            thumbnail_url=item.thumbnail_url,
            video_id=item.content_id,
            provider=provider,
        ),
        provider_mappings=_create_provider_mapping(
            item_id=item.content_id,
            provider=provider,
        ),
    )

    # Trigger async tag caching for this video (fire-and-forget)
    if hasattr(provider, "tag_manager"):
        provider.tag_manager.trigger_update(item.content_id)

    return track
