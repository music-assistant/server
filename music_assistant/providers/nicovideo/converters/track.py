"""Track converter for nicovideo objects."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from music_assistant_models.enums import ImageType, LinkType
from music_assistant_models.media_items import (
    Artist,
    AudioFormat,
    ItemMapping,
    MediaItemImage,
    MediaItemLink,
    MediaItemMetadata,
    Track,
)
from music_assistant_models.unique_list import UniqueList
from niconico.objects.video import EssentialVideo, Owner, VideoThumbnail

from music_assistant.providers.nicovideo.converters.base import NicovideoConverterBase
from music_assistant.providers.nicovideo.helpers import create_audio_format

if TYPE_CHECKING:
    from niconico.objects.nvapi import Activity
    from niconico.objects.video.watch import WatchData, WatchVideo, WatchVideoThumbnail


class NicovideoTrackConverter(NicovideoConverterBase):
    """Handles track conversion for nicovideo."""

    def convert_by_activity(self, activity: Activity) -> Track | None:
        """Convert an Activity object from feed into a Track.

        This is a lightweight conversion optimized for feed display,
        using only the information available in the activity data.
        Missing information like view counts and detailed metadata
        will be absent, but this is acceptable for feed listings.
        """
        content = activity.content

        # Only process video content
        if content.type_ != "video" or not content.video:
            return None

        # Create audio format with minimal info
        audio_format = create_audio_format()

        # Build artists from actor information using ItemMapping
        artists_list: UniqueList[Artist | ItemMapping] = UniqueList()
        if activity.actor.id_ and activity.actor.name:
            artist_mapping = ItemMapping(
                item_id=activity.actor.id_,
                provider=self.provider.domain,
                name=activity.actor.name,
            )
            artists_list.append(artist_mapping)

        # Create track with available information
        return Track(
            item_id=content.id_,
            provider=self.provider.instance_id,
            name=content.title,
            duration=content.video.duration,
            artists=artists_list,
            # Assume playable if duration > 0 (we don't have payment info here)
            is_playable=content.video.duration > 0,
            metadata=self._create_track_metadata(
                video_id=content.id_,
                release_date_str=content.started_at,
                thumbnail_url=activity.thumbnail_url,
            ),
            provider_mappings=self.helper.create_provider_mapping(
                item_id=content.id_,
                url_path="watch",
                # We don't have availability info, so default to True if playable
                available=content.video.duration > 0,
                audio_format=audio_format,
            ),
        )

    def convert_by_essential_video(self, video: EssentialVideo) -> Track | None:
        """Convert an EssentialVideo object into a Track."""
        # Skip muted videos
        if video.is_muted:
            return None

        # Calculate popularity using standard formula
        popularity = self.helper.calculate_popularity(
            mylist_count=video.count.mylist,
            like_count=video.count.like,
        )

        # Since EssentialVideo doesn't have detailed audio format info, we use defaults
        audio_format = create_audio_format()

        # Build artists using artist converter (prefer full Artist over ItemMapping)
        artists_list: UniqueList[Artist | ItemMapping] = UniqueList()
        if video.owner.id_ is not None:
            artist_obj = self.converter_manager.artist.convert_by_owner_or_user(video.owner)
            artists_list.append(artist_obj)

        # Create base track with enhanced metadata
        return Track(
            item_id=video.id_,
            provider=self.provider.instance_id,
            name=video.title,
            duration=video.duration,
            artists=artists_list,
            # Videos that cannot be played will have a duration of 0.
            is_playable=video.duration > 0 and not video.is_payment_required,
            metadata=self._create_track_metadata(
                video_id=video.id_,
                description=video.short_description,
                explicit=video.require_sensitive_masking,
                release_date_str=video.registered_at,
                popularity=popularity,
                thumbnail=video.thumbnail,
            ),
            provider_mappings=self.helper.create_provider_mapping(
                item_id=video.id_,
                url_path="watch",
                available=self.is_video_available(video),
                audio_format=audio_format,
            ),
        )

    def convert_by_watch_data(self, watch_data: WatchData) -> Track | None:
        """Convert a WatchData object into a Track."""
        video = watch_data.video

        # Skip deleted, private, or muted videos
        if video.is_deleted or video.is_private:
            return None

        # Calculate popularity using standard formula
        popularity = self.helper.calculate_popularity(
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

        # Create audio format from watch data
        audio_format = self._create_audio_format_from_watch_data(watch_data)

        # Build artists using artist converter (avoid adding if owner id is missing)
        artists_list: UniqueList[Artist | ItemMapping] = UniqueList()
        if owner.id_ is not None:
            artist_obj = self.converter_manager.artist.convert_by_owner_or_user(owner)
            artists_list.append(artist_obj)

        # Create base track with enhanced metadata
        track = Track(
            item_id=video.id_,
            provider=self.provider.instance_id,
            name=video.title,
            duration=video.duration,
            artists=artists_list,
            # Videos that cannot be played will have a duration of 0.
            is_playable=video.duration > 0 and not video.is_authentication_required,
            metadata=self._create_track_metadata_from_watch_video(
                video=video,
                watch_data=watch_data,
                popularity=popularity,
            ),
            provider_mappings=self.helper.create_provider_mapping(
                item_id=video.id_,
                url_path="watch",
                available=self.is_video_available(video),
                audio_format=audio_format,
            ),
        )

        # Add album information if series data is available (prefer full Album over ItemMapping)
        if watch_data.series is not None:
            track.album = self.converter_manager.album.convert_by_series(
                watch_data.series,
                artists_list=artists_list,
            )

        return track

    def _create_audio_format_from_watch_data(self, watch_data: WatchData) -> AudioFormat | None:
        """Create AudioFormat from WatchData audio information.

        Args:
            watch_data: WatchData object containing media information.

        Returns:
            AudioFormat object if audio information is available, None otherwise.
        """
        if (
            not watch_data.media
            or not watch_data.media.domand
            or not watch_data.media.domand.audios
        ):
            return None

        # Use the first available audio stream (typically the highest quality)
        audio = watch_data.media.domand.audios[0]

        if not audio.is_available:
            return None

        return create_audio_format(
            sample_rate=audio.sampling_rate,
            bit_rate=audio.bit_rate,
        )

    def _create_track_metadata_from_watch_video(
        self,
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
                self.logger.debug(
                    "Failed to convert release date '%s': %s", video.registered_at, err
                )

        if popularity is not None:
            metadata.popularity = popularity

        # Add tag information as genres
        if watch_data.tag and watch_data.tag.items:
            # Extract tag names from tag items and create genres set
            tag_names: list[str] = []
            for tag_item in watch_data.tag.items:
                tag_names.append(tag_item.name)

            if tag_names:
                metadata.genres = set(tag_names)

        # Add thumbnail images
        if video.thumbnail:
            metadata.images = self._convert_watch_video_thumbnails(video.thumbnail)

        # Add video link
        metadata.links = {
            MediaItemLink(
                type=LinkType.WEBSITE,
                url=f"https://www.nicovideo.jp/watch/{video.id_}",
            )
        }

        return metadata

    def _convert_watch_video_thumbnails(
        self, thumbnail: WatchVideoThumbnail
    ) -> UniqueList[MediaItemImage]:
        """Convert WatchVideo thumbnails into multiple image sizes."""
        images: UniqueList[MediaItemImage] = UniqueList()

        def _add_thumbnail_image(url: str) -> None:
            images.append(
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=url,
                    provider=self.provider.instance_id,
                    remotely_accessible=True,
                )
            )

        # Add main thumbnail URLs
        if thumbnail.url:
            _add_thumbnail_image(thumbnail.url)
        if thumbnail.middle_url:
            _add_thumbnail_image(thumbnail.middle_url)
        if thumbnail.large_url:
            _add_thumbnail_image(thumbnail.large_url)

        return images

    def _create_track_metadata(
        self,
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
                self.logger.debug("Failed to convert release date '%s': %s", release_date_str, err)

        if popularity is not None:
            metadata.popularity = popularity

        # Add thumbnail images with enhanced support
        if thumbnail:
            # Use enhanced thumbnail parsing for multiple sizes
            metadata.images = self._convert_video_thumbnails(thumbnail)
        elif thumbnail_url:
            # Fallback to single thumbnail URL
            metadata.images = UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=thumbnail_url,
                        provider=self.provider.instance_id,
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

    def _convert_video_thumbnails(self, thumbnail: VideoThumbnail) -> UniqueList[MediaItemImage]:
        """Convert video thumbnails into multiple image sizes."""
        images: UniqueList[MediaItemImage] = UniqueList()

        # nhd_url is the largest size, use it as primary
        if thumbnail.nhd_url:
            images.append(
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=thumbnail.nhd_url,
                    provider=self.provider.instance_id,
                    remotely_accessible=True,
                )
            )

        # large_url as secondary (if different from nhd_url)
        if thumbnail.large_url and thumbnail.large_url != thumbnail.nhd_url:
            images.append(
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=thumbnail.large_url,
                    provider=self.provider.instance_id,
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
                    provider=self.provider.instance_id,
                    remotely_accessible=True,
                )
            )

        return images

    def is_video_available(self, video: EssentialVideo | WatchVideo) -> bool:
        """Check if a video is available for playback.

        Args:
            video: Either EssentialVideo or WatchVideo object.

        Returns:
            True if the video is available for playback, False otherwise.
        """
        # Common check: duration must be greater than 0
        if video.duration <= 0:
            return False

        # Type-specific availability checks
        if isinstance(video, EssentialVideo):
            return not video.is_payment_required and not video.is_muted
        else:  # WatchVideo
            return not video.is_deleted
