"""Streaming support for the iHeartRadio provider."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.errors import (
    MediaNotFoundError,
    MusicAssistantError,
    UnplayableMediaError,
)
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails, StreamMetadata

from music_assistant.controllers.streams.constants import (
    STREAMDETAILS_INBAND_TITLE_HANDOFF_KEY,
    STREAMDETAILS_INBAND_TITLE_KEY,
)

from .constants import REPORT_STATUS_START, STREAM_METADATA_UPDATE_INTERVAL
from .parsers import parse_now_playing, pick_stream_url, split_episode_item_id

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .provider import IHeartRadioProvider

# StreamDetails.data key holding the station's own artwork, shown while a station reports
# no track of its own.
DATA_STATION_IMAGE = "station_image"


class IHeartRadioStreamingManager:
    """Handle stream details and live metadata for iHeartRadio."""

    def __init__(self, provider: IHeartRadioProvider) -> None:
        """Initialize the streaming manager."""
        self.provider = provider
        self.instance_id = provider.instance_id
        self.logger = provider.logger

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """
        Return the stream details for a live station, a podcast episode or a radio track.

        :param item_id: The live station id, the MA episode id or the track id.
        :param media_type: The requested media type.
        """
        if media_type == MediaType.RADIO:
            return await self._station_stream(item_id)
        if media_type == MediaType.PODCAST_EPISODE:
            return await self._episode_stream(item_id)
        if media_type == MediaType.TRACK:
            return self._track_stream(item_id)
        raise UnplayableMediaError(f"Unsupported media type: {media_type}")

    async def update_stream_metadata(self, streamdetails: StreamDetails, elapsed_time: int) -> None:
        """
        Refresh the now-playing metadata of a live station.

        :param streamdetails: The stream to update.
        :param elapsed_time: Elapsed playback time in seconds (unused: the station
            reports the position of the track within its own broadcast).
        """
        try:
            now_playing = await self.provider.get_now_playing(streamdetails.item_id)
        except MusicAssistantError as err:
            # a failed refresh says nothing about what is playing, so the last known
            # metadata stays put
            self.logger.debug("Could not update now-playing metadata: %s", err)
            return
        streamdetails.stream_metadata = self._station_metadata(streamdetails, now_playing)

    async def _station_stream(self, item_id: str) -> StreamDetails:
        """
        Return the stream details for a live station.

        :param item_id: The iHeartRadio live station id.
        """
        station = await self.provider.get_station(item_id)
        if station is None:
            raise MediaNotFoundError(f"Station {item_id} not found")
        # resolved on every playback: the station url redirects to a per-listener stream
        # whose ad token is short-lived, so the redirect target is never kept
        if not (url := pick_stream_url(station.get("streams") or {})):
            raise UnplayableMediaError(f"Station {item_id} offers no playable stream")
        streamdetails = StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            # the stream is AAC or MP3 depending on the variant; let ffmpeg detect it
            audio_format=AudioFormat(content_type=ContentType.UNKNOWN),
            media_type=MediaType.RADIO,
            stream_type=StreamType.HTTP,
            path=url,
            allow_seek=False,
            can_seek=False,
            duration=0,
        )
        try:
            now_playing = await self.provider.get_now_playing(item_id)
        except MediaNotFoundError:
            # the station publishes no track metadata at all, so the titles the stream
            # itself carries (handled by the streams controller) are all there is
            return streamdetails
        except MusicAssistantError as err:
            self.logger.debug("Could not fetch now-playing metadata: %s", err)
            now_playing = None
        # Owning the metadata turns off the streams controller's own HLS poller and keeps
        # its ICY parsing from overwriting us: the station's now-playing endpoint reports
        # artwork, album and the track's position, where an in-band title is only a
        # string. The handoff still records that title so it can serve as the fallback.
        streamdetails.data = {
            STREAMDETAILS_INBAND_TITLE_HANDOFF_KEY: True,
            DATA_STATION_IMAGE: station.get("logo") or None,
        }
        streamdetails.stream_metadata_update_callback = self.update_stream_metadata
        streamdetails.stream_metadata_update_interval = STREAM_METADATA_UPDATE_INTERVAL
        streamdetails.stream_metadata = self._station_metadata(streamdetails, now_playing)
        return streamdetails

    async def _episode_stream(self, item_id: str) -> StreamDetails:
        """
        Return the stream details for a podcast episode.

        :param item_id: The MA episode id, holding the podcast and episode id.
        """
        if (parsed := split_episode_item_id(item_id)) is None:
            raise MediaNotFoundError(f"Not an iHeartRadio episode: {item_id}")
        _, episode_id = parsed
        # only the episode endpoint carries the media url, so it is resolved here rather
        # than for every episode of a listing
        episode = await self.provider.get_episode(episode_id)
        if episode is None:
            raise MediaNotFoundError(f"Episode {episode_id} not found")
        if not (url := episode.get("mediaUrl")):
            raise UnplayableMediaError(f"Episode {episode_id} offers no playable stream")
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(content_type=_episode_content_type(episode, str(url))),
            media_type=MediaType.PODCAST_EPISODE,
            stream_type=StreamType.HTTP,
            path=str(url),
            duration=int(episode.get("duration") or 0) or None,
            allow_seek=True,
            can_seek=True,
        )

    def _track_stream(self, item_id: str) -> StreamDetails:
        """
        Return the stream details for a track served by an artist radio batch.

        :param item_id: The iHeartRadio track id.
        """
        now = time.time()
        if (found := self.provider.stations.find(item_id)) is None:
            raise MediaNotFoundError(f"Track {item_id} is no longer available from iHeartRadio")
        batch, item = found
        if batch.urls_expired(now):
            # a long pause outlives the audio url; refusing keeps the failure named rather
            # than an opaque ffmpeg error
            raise MediaNotFoundError(f"Track {item_id} expired while playback was stopped")
        content = item.get("content") or {}
        duration = int(content.get("duration") or 0)
        streamdetails = StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(content_type=ContentType.AAC),
            media_type=MediaType.TRACK,
            stream_type=StreamType.HLS,
            path=str(item["streamUrl"]),
            duration=duration or None,
            allow_seek=duration > 0,
            can_seek=duration > 0,
            expiration=batch.seconds_left(now),
        )
        self.provider.mass.create_task(self.provider.report_play(item_id, REPORT_STATUS_START, 0))
        return streamdetails

    def _station_metadata(
        self, streamdetails: StreamDetails, now_playing: Mapping[str, Any] | None
    ) -> StreamMetadata | None:
        """
        Return the metadata to show for a station, or None when it plays no track.

        :param streamdetails: The station's stream, holding the recorded in-band title.
        :param now_playing: The station's currentTrackMeta payload, None when it is
            between tracks (an ad break, or talk programming).
        """
        data = streamdetails.data if isinstance(streamdetails.data, dict) else {}
        station_image = data.get(DATA_STATION_IMAGE)
        if now_playing and (metadata := parse_now_playing(now_playing, station_image)):
            return metadata
        if icy_title := data.get(STREAMDETAILS_INBAND_TITLE_KEY):
            return StreamMetadata(title=str(icy_title), image_url=station_image)
        return None


def _episode_content_type(episode: Mapping[str, Any], url: str) -> ContentType:
    """
    Return the content type of an episode's audio.

    :param episode: The episode payload.
    :param url: The episode's media url, used when the payload names no mime type.
    """
    mime_types = [mime for mime in episode.get("mimeTypes") or [] if isinstance(mime, str)]
    for candidate in (*mime_types, url):
        if (content_type := ContentType.try_parse(candidate)) != ContentType.UNKNOWN:
            return content_type
    return ContentType.UNKNOWN
