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
    Artist,
    MediaItemImage,
    MediaItemLink,
    MediaItemMetadata,
    Playlist,
    ProviderMapping,
    Track,
)
from music_assistant_models.unique_list import UniqueList
from niconico.objects.user import NicoUser, UserMylistItem
from niconico.objects.video import EssentialVideo, Mylist, Owner
from niconico.objects.video.search import EssentialMylist

from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers.niconico.helpers import PlaylistWithTracks

if TYPE_CHECKING:
    from niconico.objects.user import (
        UserMylistItem,
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
