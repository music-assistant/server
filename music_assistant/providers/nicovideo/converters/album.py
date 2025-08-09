"""Album converter for nicovideo objects."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import ImageType, LinkType
from music_assistant_models.media_items import (
    Album,
    Artist,
    MediaItemImage,
    MediaItemLink,
    MediaItemMetadata,
)
from music_assistant_models.unique_list import UniqueList
from niconico.objects.nvapi import SeriesData
from niconico.objects.video.search import EssentialSeries

if TYPE_CHECKING:
    from niconico.objects.user import UserSeriesItem

from music_assistant.providers.nicovideo.converters.base import NicovideoConverterBase
from music_assistant.providers.nicovideo.converters.utilities import create_provider_mapping
from music_assistant.providers.nicovideo.helpers import AlbumWithTracks


class NicovideoAlbumConverter(NicovideoConverterBase):
    """Handles album conversion for nicovideo."""

    def convert_by_series(self, series: SeriesData | UserSeriesItem | EssentialSeries) -> Album:
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
            provider=self.provider.lookup_key,
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
            provider_mappings=create_provider_mapping(
                item_id=item_id,
                provider=self.provider,
                url_path="series",
            ),
        )

        # Add artist (series owner) if available
        if owner_id:
            artist = Artist(
                item_id=str(owner_id),
                provider=self.provider.lookup_key,
                name=owner_name if owner_name else "",
                provider_mappings=create_provider_mapping(
                    item_id=str(owner_id),
                    provider=self.provider,
                    url_path="user",
                ),
            )
            album.artists = UniqueList([artist])

        # Add thumbnail image if available (exclude default no-thumbnail image)
        if thumbnail_url and not thumbnail_url.endswith("/series/no_thumbnail.png"):
            album.metadata.images = UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=thumbnail_url,
                        provider=self.provider.lookup_key,
                        remotely_accessible=True,
                    )
                ]
            )

        return album

    def convert_series_to_album_with_tracks(self, series_data: SeriesData) -> AlbumWithTracks:
        """Convert SeriesData to AlbumWithTracks."""
        album = self.convert_by_series(series_data)
        tracks = []
        for item in series_data.items or []:
            track = self.converter_hub.track.convert_by_essential_video(item.video)
            if track:
                tracks.append(track)
        return AlbumWithTracks(album, tracks)
