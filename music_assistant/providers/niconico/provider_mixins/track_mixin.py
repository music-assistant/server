"""MixIn for NiconicoMusicProvider: track-related methods."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.providers.niconico.provider_mixins.mixin_base import (
    NiconicoMusicProviderMixinBase,
)

if TYPE_CHECKING:
    from music_assistant_models.media_items import Track


class NiconicoMusicProviderTrackMixin(NiconicoMusicProviderMixinBase):
    """Track-related methods for NiconicoMusicProvider."""

    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        track = await self.niconico_adapter.video.get_video(prov_track_id)
        if not track:
            raise MediaNotFoundError(f"Track with id {prov_track_id} not found on Niconico.")
        return track

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
