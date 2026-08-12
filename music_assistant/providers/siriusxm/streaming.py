"""Streaming support for the SiriusXM provider."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

from .constants import (
    BITRATE_256,
    STREAM_EXPIRATION,
    STREAM_METADATA_UPDATE_INTERVAL,
)
from .parsers import THUMB_SIZE, parse_stream_metadata, split_track_item_id

if TYPE_CHECKING:
    from .provider import SiriusXMProvider


class SiriusXMStreamingManager:
    """Handle stream details and live metadata for SiriusXM."""

    def __init__(self, provider: SiriusXMProvider) -> None:
        """Initialize the streaming manager."""
        self.provider = provider
        self.mass = provider.mass
        self.instance_id = provider.instance_id
        self.logger = provider.logger

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """
        Return the stream details for a channel or a station track.

        :param item_id: A channel id, or a composite station track id.
        :param media_type: The requested media type.
        """
        if media_type == MediaType.TRACK:
            return await self._track_stream(item_id)
        if media_type != MediaType.RADIO:
            raise MediaNotFoundError(f"Unsupported media type: {media_type}")

        channel = await self.provider.get_channel(item_id)
        # Playback goes through the local proxy: SiriusXM requires an
        # Authorization header on every playlist, key and segment request,
        # which ffmpeg will not send.
        bitrate = self.provider.preferred_bitrate
        path = (
            f"{self.provider.proxy_base_url}"
            f"/stream/{channel.type}/{channel.id}/playlist.m3u8?bitrate={bitrate}"
        )

        streamdetails = StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(
                content_type=ContentType.AAC,
                bit_rate=_bitrate_kbps(bitrate),
            ),
            media_type=MediaType.RADIO,
            stream_type=StreamType.HLS,
            path=path,
            can_seek=False,
            allow_seek=False,
            expiration=STREAM_EXPIRATION,
            stream_metadata_update_callback=self.update_stream_metadata,
            stream_metadata_update_interval=STREAM_METADATA_UPDATE_INTERVAL,
        )

        # Populate the first frame of metadata up front, so the player shows the
        # current track immediately rather than the station name until the first
        # callback lands.
        await self.update_stream_metadata(streamdetails, 0)
        return streamdetails

    async def update_stream_metadata(self, streamdetails: StreamDetails, elapsed_time: int) -> None:
        """
        Refresh the now-playing metadata for a live channel.

        :param streamdetails: The stream to update.
        :param elapsed_time: Elapsed playback time in seconds (unused: SiriusXM
            publishes what is playing now, not a timeline).
        """
        try:
            now_playing = await self.provider.client.get_now_playing(streamdetails.item_id)
        except Exception as err:
            # Never let a metadata refresh interrupt playback.
            self.logger.debug("Could not update now-playing metadata: %s", err)
            return
        if now_playing is None:
            return

        fallback_image: str | None = None
        if channel := self.provider.channels_by_id.get(streamdetails.item_id):
            fallback_image = channel.image_url("tile", "1x1", THUMB_SIZE)

        if metadata := parse_stream_metadata(now_playing, fallback_image):
            streamdetails.stream_metadata = metadata

    async def _track_stream(self, item_id: str) -> StreamDetails:
        """
        Return the stream details for one track of a station's queue.

        :param item_id: A composite ``entity_type|entity_id|track_id`` id.
        """
        parsed = split_track_item_id(item_id)
        if parsed is None:
            raise MediaNotFoundError(f"Not a SiriusXM track: {item_id}")
        entity_type, entity_id, track_id = parsed

        track = await self.provider.get_queue_track(item_id)
        if track is None or not track.url:
            raise MediaNotFoundError(f"Track {track_id} is no longer available")
        # This track is being played, so the station has moved past it.
        self.provider.drop_played(item_id)

        # An Xtra track is encrypted HLS whose key request needs the session's
        # auth header, so it goes through the local proxy. Artist-station tracks
        # are plain media files the player can fetch itself.
        bitrate = self.provider.preferred_bitrate
        if _is_hls(track.url):
            path = (
                f"{self.provider.proxy_base_url}/stream/{entity_type}/{entity_id}"
                f"/track/{track_id}/playlist.m3u8?bitrate={bitrate}"
            )
            content_type = ContentType.AAC
        else:
            path = track.url
            content_type = ContentType.UNKNOWN

        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(content_type=content_type),
            media_type=MediaType.TRACK,
            stream_type=StreamType.HTTP,
            path=path,
            duration=int(track.duration) if track.duration else None,
            can_seek=True,
            allow_seek=True,
            expiration=STREAM_EXPIRATION,
        )


def _is_hls(url: str) -> bool:
    """Whether a track url is an HLS playlist rather than a plain media file."""
    return url.split("?", maxsplit=1)[0].endswith(".m3u8")


def _bitrate_kbps(bitrate: str) -> int | None:
    """Convert a SiriusXM bitrate label (e.g. '256k') to kbps."""
    try:
        return int(bitrate.removesuffix("k"))
    except ValueError:
        return int(BITRATE_256.removesuffix("k"))
