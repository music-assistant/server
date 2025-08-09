"""MixIn for NicovideoMusicProvider: track-related methods."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, override

from music_assistant_models.enums import ContentType, MediaType, ProviderFeature, StreamType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.providers.nicovideo.provider_mixins.base import (
    NicovideoMusicProviderMixinBase,
)

if TYPE_CHECKING:
    from music_assistant_models.media_items import MediaItemType, Track


class NicovideoMusicProviderTrackMixin(NicovideoMusicProviderMixinBase):
    """Track-related methods for NicovideoMusicProvider."""

    _supported_features = {
        ProviderFeature.LIBRARY_TRACKS,
        ProviderFeature.LIBRARY_TRACKS_EDIT,
    }

    @override
    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        track = await self.service_hub.video.get_video(prov_track_id)
        if not track:
            raise MediaNotFoundError(f"Track with id {prov_track_id} not found on nicovideo.")
        return track

    @override
    async def get_library_tracks(
        self,
    ) -> AsyncGenerator[Track, None]:
        """Retrieve library tracks from the provider."""
        if not self.service_hub.auth.is_logged_in():
            return

        # Check config settings for including tracks
        include_following_tracks = self.nicovideo_config.get_include_followed_mylists_tracks()
        include_own_tracks = self.nicovideo_config.get_include_own_mylists_tracks()
        include_own_videos_tracks = self.nicovideo_config.get_include_own_videos_tracks()

        # Process all library playlists for this provider
        async for playlist in self.mass.music.playlists.iter_library_items(
            provider=self.instance_id,
        ):
            # Filter based on playlist type and config settingげ
            # Own mylists are editable (is_editable=True)
            # Following mylists are not editable (is_editable=False)
            if playlist.is_editable and not include_own_tracks:
                continue
            if not playlist.is_editable and not include_following_tracks:
                continue

            prov_map = next(iter(playlist.provider_mappings), None)
            if not prov_map:
                continue
            page = 0
            while True:
                playlist_tracks = await self.get_playlist_tracks(prov_map.item_id, page)
                if not playlist_tracks:
                    break
                for track in playlist_tracks:
                    yield track
                page += 1

        # Include own uploaded videos if enabled
        if include_own_videos_tracks:
            own_videos = await self.service_hub.user.get_own_videos()
            for track in own_videos:
                yield track

    async def get_stream_details_for_mixin(
        self, item_id: str, media_type: MediaType
    ) -> StreamDetails | None:
        """Get stream details (streaming URL and format) for given item."""
        if media_type != MediaType.TRACK:
            return None

        stream_format = await self.service_hub.video.get_stream_format(item_id=item_id)

        # Get http_headers safely - it may be a dict or None
        http_headers = stream_format.get("http_headers")
        user_agent = "Mozilla/5.0"
        if isinstance(http_headers, dict):
            user_agent = http_headers.get("User-Agent", "Mozilla/5.0")

        extra_args = [
            "-user_agent",
            user_agent,
            "-referer",
            "https://www.nicovideo.jp/",
            "-headers",
            f"Cookie: {stream_format['cookies']}\r\n",
        ]

        # Set both content_type and codec_type for accurate format detection
        content_type = ContentType.try_parse(stream_format.get("audio_ext", "unknown"))
        codec_type = ContentType.try_parse(stream_format.get("acodec", "unknown"))

        # Calculate estimated file size if available
        duration = int(stream_format.get("duration", 0))
        bit_rate = int(stream_format.get("abr", 0)) if stream_format.get("abr") else None

        # Get track information for stream title
        track = await self.get_track(item_id)
        stream_title = track.name if track else None

        self.logger.debug(
            "Found stream format for %s (audio_ext: %s, acodec: %s)",
            item_id,
            str(content_type),
            str(codec_type),
        )

        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(
                content_type=content_type,
                codec_type=codec_type,
                sample_rate=int(stream_format.get("asr", 44100)),
                channels=int(stream_format.get("channels", 2)),
                bit_rate=bit_rate,
            ),
            media_type=MediaType.TRACK,
            stream_type=StreamType.HTTP,
            duration=duration,
            stream_title=stream_title,
            path=stream_format["url"],
            extra_input_args=extra_args,
            allow_seek=True,
            can_seek=True,
            # If an expiring URL is used, it may not play when pausing and resuming.
            enable_cache=True,
        )

    async def library_add_for_mixin(self, item: MediaItemType) -> bool | None:
        """Add item to provider's library. Return true on success."""
        if item.media_type == MediaType.TRACK:
            # Check if auto-like is enabled
            auto_like_enabled = self.nicovideo_config.get_auto_like_on_library_add()
            if not auto_like_enabled:
                return True  # Successfully "added" but no action needed

            # Extract video ID from provider item ID
            video_id = item.item_id

            # Like the video using niconico.py
            like_result = await self.service_hub.video.like_video(video_id)

            if like_result:
                self.logger.debug("Successfully liked video %s", video_id)
            else:
                self.logger.warning("Failed to like video %s", video_id)

            # Always return True for library add, regardless of like success/failure
            return True

        return None  # Not handled by this mixin

    async def library_remove_for_mixin(
        self, prov_item_id: str, media_type: MediaType
    ) -> bool | None:
        """Remove item from provider's library. Return true on success."""
        if media_type == MediaType.TRACK:
            # For now, we don't implement unlike functionality for tracks
            # because niconico's "like" feature is more of an optional engagement feature
            # rather than a core library management feature.
            return True

        return None  # Not handled by this mixin
