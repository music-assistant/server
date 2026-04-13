"""Playlist converter for nicovideo objects."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import ImageType, LinkType
from music_assistant_models.media_items import (
    MediaItemImage,
    MediaItemLink,
    MediaItemMetadata,
    Playlist,
)
from music_assistant_models.unique_list import UniqueList
from niconico.objects.video.search import EssentialMylist

from music_assistant.providers.nicovideo.converters.base import NicovideoConverterBase
from music_assistant.providers.nicovideo.helpers import PlaylistWithTracks

if TYPE_CHECKING:
    from niconico.objects.nvapi import FollowingMylistItem
    from niconico.objects.user import UserMylistItem
    from niconico.objects.video import Mylist


class NicovideoPlaylistConverter(NicovideoConverterBase):
    """Handles playlist conversion for nicovideo."""

    def convert_by_mylist(self, mylist: UserMylistItem | Mylist | EssentialMylist) -> Playlist:
        """Convert a nicovideo UserMylistItem into a Playlist."""
        playlist = Playlist(
            item_id=str(mylist.id_),
            provider=self.provider.instance_id,
            name=(mylist.title if isinstance(mylist, EssentialMylist) else mylist.name),
            owner=mylist.owner.id_ or "",
            is_editable=True,  # Own mylists are editable by default
            metadata=MediaItemMetadata(
                description=mylist.description,
                links={
                    MediaItemLink(
                        type=LinkType.WEBSITE,
                        url=f"https://www.nicovideo.jp/mylist/{mylist.id_}",
                    )
                },
            ),
            provider_mappings=self.helper.create_provider_mapping(str(mylist.id_), "mylist"),
        )

        if mylist.owner.icon_url:
            if not playlist.metadata.images:
                playlist.metadata.images = UniqueList()
            playlist.metadata.images.append(
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=mylist.owner.icon_url,
                    provider=self.provider.instance_id,
                    remotely_accessible=True,
                )
            )
        return playlist

    def convert_following_by_mylist(self, mylist: FollowingMylistItem) -> Playlist:
        """Convert a nicovideo UserMylistItem from following users into a read-only Playlist."""
        playlist = self.convert_by_mylist(mylist.detail)
        # Mark following mylists as non-editable
        playlist.is_editable = False
        return playlist

    def convert_with_tracks_by_mylist(self, mylist: Mylist) -> PlaylistWithTracks:
        """Convert a nicovideo UserMylistItem into a PlaylistWithTracks."""
        playlist = self.convert_by_mylist(mylist)
        tracks = []
        for item in mylist.items:
            track = self.converter_manager.track.convert_by_essential_video(item.video)
            if track:
                tracks.append(track)
        return PlaylistWithTracks(playlist, tracks)
