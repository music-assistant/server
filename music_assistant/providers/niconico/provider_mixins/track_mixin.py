"""MixIn for NiconicoMusicProvider: track-related methods."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

from music_assistant_models.enums import ContentType, MediaType, ProviderFeature, StreamType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.providers.niconico.helpers import get_library_items, handle_niconico_errors
from music_assistant.providers.niconico.provider_mixins.mixin_base import (
    NiconicoMusicProviderMixinBase,
)

if TYPE_CHECKING:
    from music_assistant_models.media_items import MediaItemType, Track


class NiconicoMusicProviderTrackMixin(NiconicoMusicProviderMixinBase):
    """Track-related methods for NiconicoMusicProvider."""

    _supported_features = {
        ProviderFeature.LIBRARY_TRACKS,
        ProviderFeature.LIBRARY_TRACKS_EDIT,
    }

    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        track = await self.niconico_adapter.video.get_video(prov_track_id)
        if not track:
            raise MediaNotFoundError(f"Track with id {prov_track_id} not found on Niconico.")
        return track

    async def get_library_tracks(
        self,
    ) -> AsyncGenerator[Track, None]:
        """Retrieve library tracks from the provider."""
        if not self.niconico_adapter.auth.is_logged_in():
            return

        # Check config settings for including tracks
        include_following_tracks = self.niconico_config.get_include_followed_mylists_tracks()
        include_own_tracks = self.niconico_config.get_include_own_mylists_tracks()

        # Get all library playlists
        playlists = await get_library_items(
            self.provider,
            cache_key="playlist",
            query_table="playlists",
            query_method=self.provider.mass.music.playlists.library_items,
        )

        for playlist in playlists:
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
                playlist_tracks = await self.provider.get_playlist_tracks(prov_map.item_id, page)
                if not playlist_tracks:
                    break
                for track in playlist_tracks:
                    yield track
                page += 1

    async def get_stream_details_for_mixin(
        self, item_id: str, media_type: MediaType
    ) -> StreamDetails | None:
        """Get stream details (streaming URL and format) for given item."""
        if media_type != MediaType.TRACK:
            return None

        stream_format = await self.niconico_adapter.video.get_stream_format(item_id=item_id)
        self.provider.logger.debug(
            "Found stream_format: %s for song %s", stream_format["audio_ext"], item_id
        )

        extra_args = [
            "-user_agent",
            str(stream_format["user_agent"]),
            "-referer",
            "https://www.nicovideo.jp/",
            "-headers",
            "Cookie: " + str(stream_format["cookies"]) + "\r\n",
        ]

        stream_details = StreamDetails(
            provider=self.provider.instance_id,
            duration=stream_format.get("duration"),
            item_id=item_id,
            audio_format=AudioFormat(
                content_type=ContentType.try_parse(str(stream_format["audio_ext"])),
            ),
            stream_type=StreamType.HTTP,
            path=str(stream_format["url"]),
            extra_input_args=[str(arg) for arg in extra_args],
            allow_seek=True,
            can_seek=True,
            # If an expiring URL is used, it may not play when pausing and resuming.
            enable_cache=True,
        )

        if (
            stream_format.get("audio_channels")
            and str(stream_format.get("audio_channels")).isdigit()
        ):
            stream_details.audio_format.channels = int(
                str(stream_format.get("audio_channels") or "0")
            )
        if stream_format.get("asr"):
            stream_details.audio_format.sample_rate = int(str(stream_format.get("asr") or "0"))
        return stream_details

    async def library_add_for_mixin(self, item: MediaItemType) -> bool | None:
        """Add item to provider's library. Return true on success."""
        if item.media_type == MediaType.TRACK:
            # Check if auto-like is enabled
            auto_like_enabled = self.niconico_config.get_auto_like_on_library_add()
            if not auto_like_enabled:
                return True  # Successfully "added" but no action needed

            async with handle_niconico_errors(self.provider.logger, "liking video", item.item_id):
                # Extract video ID from provider item ID
                video_id = item.item_id

                # Like the video using niconico.py
                like_result = await self.niconico_adapter.call_with_throttler(
                    self.niconico_adapter.niconico_py_client.video.like_video, video_id
                )

                if like_result:
                    self.provider.logger.info("Successfully liked video %s", video_id)
                else:
                    self.provider.logger.warning("Failed to like video %s", video_id)

            # Always return True for library add, regardless of like success/failure
            return True

        return None  # Not handled by this mixin

    async def library_remove_for_mixin(
        self, prov_item_id: str, media_type: MediaType
    ) -> bool | None:
        """Remove item from provider's library. Return true on success."""
        if media_type == MediaType.TRACK:
            # For now, we don't implement unlike functionality for tracks
            # because Niconico's "like" feature is more of an optional engagement feature
            # rather than a core library management feature.
            return True

        return None  # Not handled by this mixin
