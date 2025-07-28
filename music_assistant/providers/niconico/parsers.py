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
from niconico.objects.video import EssentialVideo, Mylist, Owner
from niconico.objects.video.search import EssentialMylist, EssentialSeries

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
        metadata=MediaItemMetadata(
            description=getattr(mylist, "description", ""),
            links={
                MediaItemLink(
                    type=LinkType.WEBSITE,
                    url=f"https://www.nicovideo.jp/mylist/{mylist.id_}",
                )
            },
        ),
        provider_mappings={
            ProviderMapping(
                item_id=str(mylist.id_),
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
                url=f"https://www.nicovideo.jp/mylist/{mylist.id_}",
                available=True,
            )
        },
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
        provider_mappings={
            ProviderMapping(
                item_id=item_id,
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
                url=f"https://www.nicovideo.jp/series/{item_id}",
            )
        },
    )

    # Add artist (series owner) if available
    if owner_id:
        artist = Artist(
            item_id=str(owner_id),
            provider=provider.lookup_key,
            name=owner_name if owner_name else "",
            provider_mappings={
                ProviderMapping(
                    item_id=str(owner_id),
                    provider_domain=provider.domain,
                    provider_instance=provider.instance_id,
                    url=f"https://www.nicovideo.jp/user/{owner_id}",
                )
            },
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
    tracks = [parse_track_by_essential_video(provider, item.video) for item in mylist.items]
    return PlaylistWithTracks(playlist, tracks)


def parse_track_by_essential_video(provider: MusicProvider, video: EssentialVideo) -> Track:
    """Parse an EssentialVideo object into a Track."""
    return Track(
        item_id=video.id_,
        provider=provider.lookup_key,
        name=video.title,
        duration=video.duration,
        artists=UniqueList([parse_artist(provider, video.owner)]),
        is_playable=video.duration > 0,
        metadata=MediaItemMetadata(
            description=video.short_description,
            explicit=video.require_sensitive_masking,
            release_date=datetime.fromisoformat(video.registered_at),
            images=UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=video.thumbnail.nhd_url,
                        provider=provider.lookup_key,
                        remotely_accessible=True,
                    )
                ]
            ),
            links={
                MediaItemLink(
                    type=LinkType.WEBSITE,
                    url=f"https://www.nicovideo.jp/watch/{video.id_}",
                )
            },
        ),
        provider_mappings={
            ProviderMapping(
                item_id=video.id_,
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
                url=f"https://www.nicovideo.jp/watch/{video.id_}",
                available=True,
            )
        },
    )


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
        provider_mappings={
            ProviderMapping(
                item_id=item_id,
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
                url=f"https://www.nicovideo.jp/user/{item_id}",
                available=True,
            )
        },
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
    tracks = [
        parse_track_by_essential_video(provider, item.video) for item in series_data.items or []
    ]
    return AlbumWithTracks(album, tracks)
