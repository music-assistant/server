"""
Converter for the nicovideo provider in Music Assistant.

This module contains functions to convert various nicovideo objects such as playlists,
tracks, and artists into Music Assistant media items.
"""

from __future__ import annotations

import logging
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
from niconico.objects.video.search import EssentialMylist, EssentialSeries

from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers.nicovideo.helpers import (
    AlbumWithTracks,
    PlaylistWithTracks,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from niconico.objects.user import (
        UserMylistItem,
        UserSeriesItem,
    )
    from niconico.objects.video import Mylist
    from niconico.objects.video.watch import (
        WatchData,
        WatchVideo,
        WatchVideoThumbnail,
    )


def convert_playlist_by_mylist(
    provider: MusicProvider, mylist: UserMylistItem | Mylist | EssentialMylist
) -> Playlist:
    """Convert a nicovideo UserMylistItem into a Playlist."""
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


def convert_following_playlist_by_mylist(
    provider: MusicProvider, mylist: UserMylistItem | Mylist | EssentialMylist
) -> Playlist:
    """Convert a nicovideo UserMylistItem from following users into a read-only Playlist."""
    playlist = convert_playlist_by_mylist(provider, mylist)
    # Mark following mylists as non-editable
    playlist.is_editable = False
    return playlist


def convert_album_by_series(
    provider: MusicProvider, series: SeriesData | UserSeriesItem | EssentialSeries
) -> Album:
    """Convert a nicovideo SeriesData, UserSeriesItem, or EssentialSeries into an Album."""
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


def convert_playlist_with_tracks_by_mylist(
    provider: MusicProvider, mylist: Mylist
) -> PlaylistWithTracks:
    """Convert a nicovideo UserMylistItem into a PlaylistWithTracks."""
    playlist = convert_playlist_by_mylist(provider, mylist)
    tracks = []
    for item in mylist.items:
        track = convert_track_by_essential_video(provider, item.video)
        if track:
            tracks.append(track)
    return PlaylistWithTracks(playlist, tracks)


def convert_track_by_watch_data(provider: MusicProvider, watch_data: WatchData) -> Track | None:
    """Convert a WatchData object into a Track."""
    video = watch_data.video

    # Skip deleted, private, or muted videos
    if video.is_deleted or video.is_private:
        return None

    # Calculate popularity using standard formula
    popularity = _calculate_popularity(
        mylist_count=video.count.mylist,
        like_count=video.count.like,
    )

    # Create owner object for artist conversion based on channel vs user video
    if watch_data.channel:
        # Channel video case
        owner = Owner(
            ownerType="channel",
            type="channel",
            visibility="visible",
            id=watch_data.channel.id_,
            name=watch_data.channel.name,
            iconUrl=watch_data.channel.thumbnail.url if watch_data.channel.thumbnail else None,
        )
    else:
        # User video case
        owner = Owner(
            ownerType="user",
            type="user",
            visibility="visible",
            id=str(watch_data.owner.id_) if watch_data.owner else None,
            name=watch_data.owner.nickname if watch_data.owner else None,
            iconUrl=watch_data.owner.icon_url if watch_data.owner else None,
        )

    # Create base track with enhanced metadata
    track = Track(
        item_id=video.id_,
        provider=provider.lookup_key,
        name=video.title,
        duration=video.duration,
        artists=UniqueList([convert_artist(provider, owner)]),
        # Videos that cannot be played will have a duration of 0.
        is_playable=video.duration > 0 and not video.is_authentication_required,
        metadata=_create_track_metadata_from_watch_video(
            provider=provider,
            video=video,
            watch_data=watch_data,
            popularity=popularity,
        ),
        provider_mappings=_create_provider_mapping(
            item_id=video.id_,
            provider=provider,
            available=not video.is_authentication_required and not video.is_deleted,
        ),
    )

    # Add album information if series data is available
    if watch_data.series:
        track.album = Album(
            item_id=str(watch_data.series.id_),
            provider=provider.lookup_key,
            name=watch_data.series.title,
            artists=UniqueList([convert_artist(provider, owner)]),
            metadata=MediaItemMetadata(
                description=watch_data.series.description,
                images=UniqueList(
                    [
                        MediaItemImage(
                            type=ImageType.THUMB,
                            path=watch_data.series.thumbnail_url,
                            provider=provider.lookup_key,
                            remotely_accessible=True,
                        )
                    ]
                )
                if watch_data.series.thumbnail_url
                else None,
            ),
            provider_mappings=_create_provider_mapping(
                item_id=str(watch_data.series.id_),
                provider=provider,
                url_path="series",
            ),
        )

    return track


def _create_track_metadata_from_watch_video(
    provider: MusicProvider,
    video: WatchVideo,
    watch_data: WatchData,
    *,
    popularity: int | None = None,
) -> MediaItemMetadata:
    """Create track metadata from WatchVideo object."""
    metadata = MediaItemMetadata()

    if video.description:
        metadata.description = video.description

    if video.registered_at:
        try:
            # Handle both direct ISO format and Z-suffixed format
            if video.registered_at.endswith("Z"):
                clean_date_str = video.registered_at.replace("Z", "+00:00")
                metadata.release_date = datetime.fromisoformat(clean_date_str)
            else:
                metadata.release_date = datetime.fromisoformat(video.registered_at)
        except (ValueError, AttributeError) as err:
            # Log debug message for date parsing failures to help with troubleshooting
            logger.debug("Failed to convert release date '%s': %s", video.registered_at, err)

    if popularity is not None:
        metadata.popularity = popularity

    # Add tag information as genres
    if watch_data.tag and watch_data.tag.items:
        # Extract tag names from tag items and create genres set
        tag_names = []
        for tag_item in watch_data.tag.items:
            # Tag items might be Tag objects or dictionaries
            if hasattr(tag_item, "name"):
                tag_names.append(tag_item.name)
            elif isinstance(tag_item, dict) and "name" in tag_item:
                tag_names.append(tag_item["name"])
            elif isinstance(tag_item, str):
                tag_names.append(tag_item)

        if tag_names:
            metadata.genres = set(tag_names)

    # Add thumbnail images
    if video.thumbnail:
        metadata.images = _convert_watch_video_thumbnails(provider, video.thumbnail)

    # Add video link
    metadata.links = {
        MediaItemLink(
            type=LinkType.WEBSITE,
            url=f"https://www.nicovideo.jp/watch/{video.id_}",
        )
    }

    return metadata


def _convert_watch_video_thumbnails(
    provider: MusicProvider, thumbnail: WatchVideoThumbnail
) -> UniqueList[MediaItemImage]:
    """Convert WatchVideo thumbnails into multiple image sizes."""
    images: UniqueList[MediaItemImage] = UniqueList()

    # WatchVideoThumbnail has: url, middle_url, large_url, player, ogp
    # Use the largest available size first
    if thumbnail.large_url:
        images.append(
            MediaItemImage(
                type=ImageType.THUMB,
                path=thumbnail.large_url,
                provider=provider.lookup_key,
                remotely_accessible=True,
            )
        )
    elif thumbnail.url:
        images.append(
            MediaItemImage(
                type=ImageType.THUMB,
                path=thumbnail.url,
                provider=provider.lookup_key,
                remotely_accessible=True,
            )
        )

    # Add middle_url as secondary option if different from large_url
    if thumbnail.middle_url and thumbnail.middle_url != thumbnail.large_url:
        images.append(
            MediaItemImage(
                type=ImageType.THUMB,
                path=thumbnail.middle_url,
                provider=provider.lookup_key,
                remotely_accessible=True,
            )
        )

    return images


def convert_track_by_essential_video(
    provider: MusicProvider, video: EssentialVideo
) -> Track | None:
    """Convert an EssentialVideo object into a Track."""
    # Skip muted videos
    if video.is_muted:
        return None

    # Calculate popularity using standard formula
    popularity = _calculate_popularity(
        mylist_count=video.count.mylist,
        like_count=video.count.like,
    )

    # Create base track with enhanced metadata
    return Track(
        item_id=video.id_,
        provider=provider.lookup_key,
        name=video.title,
        duration=video.duration,
        artists=UniqueList([convert_artist(provider, video.owner)]),
        # Videos that cannot be played will have a duration of 0.
        is_playable=video.duration > 0 and not video.is_payment_required,
        metadata=_create_track_metadata(
            provider=provider,
            video_id=video.id_,
            description=video.short_description,
            explicit=video.require_sensitive_masking,
            release_date_str=video.registered_at,
            popularity=popularity,
            thumbnail=video.thumbnail,
        ),
        provider_mappings=_create_provider_mapping(
            item_id=video.id_,
            provider=provider,
            available=not video.is_payment_required and not video.is_muted,
        ),
    )


def _convert_video_thumbnails(
    provider: MusicProvider, thumbnail: VideoThumbnail
) -> UniqueList[MediaItemImage]:
    """Convert video thumbnails into multiple image sizes."""
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


def convert_artist(provider: MusicProvider, owner_or_user: Owner | NicoUser) -> Artist:
    """Convert an Owner or NicoUser into an Artist."""
    item_id = str(owner_or_user.id_)
    name = str(owner_or_user.name if isinstance(owner_or_user, Owner) else owner_or_user.nickname)
    icon_url = (
        owner_or_user.icon_url if isinstance(owner_or_user, Owner) else owner_or_user.icons.large
    )

    # Determine URL path based on owner type
    url_path = "user"  # Default for users and NicoUser
    if isinstance(owner_or_user, Owner) and owner_or_user.owner_type == "channel":
        url_path = "channel"

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
            url_path=url_path,
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
            url=f"https://www.nicovideo.jp/{url_path}/{item_id}",
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


def convert_series_to_album_with_tracks(
    provider: MusicProvider, series_data: SeriesData
) -> AlbumWithTracks:
    """Convert SeriesData to AlbumWithTracks."""
    album = convert_album_by_series(provider, series_data)
    tracks = []
    for item in series_data.items or []:
        track = convert_track_by_essential_video(provider, item.video)
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
    provider: MusicProvider,
    video_id: str,
    *,
    description: str | None = None,
    explicit: bool | None = None,
    release_date_str: str | None = None,
    popularity: int | None = None,
    thumbnail: VideoThumbnail | None = None,
    thumbnail_url: str | None = None,
) -> MediaItemMetadata:
    """Create track metadata with common fields."""
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
        except (ValueError, AttributeError) as err:
            # Log debug message for date parsing failures to help with troubleshooting
            logger.debug("Failed to convert release date '%s': %s", release_date_str, err)

    if popularity is not None:
        metadata.popularity = popularity

    # Add thumbnail images with enhanced support
    if thumbnail:
        # Use enhanced thumbnail parsing for multiple sizes
        metadata.images = _convert_video_thumbnails(provider, thumbnail)
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
