"""Streaming-related code for the BBC Sounds provider."""

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

from auntie_sounds import SoundsClient, exceptions
from auntie_sounds.models import (
    LiveStation,
    PlayableItem,
    Station,
)
from auntie_sounds.playback import StreamPreference
from music_assistant_models.enums import (
    ContentType,
    MediaType,
    QueueOption,
    StreamType,
)
from music_assistant_models.errors import (
    MediaNotFoundError,
    MusicAssistantError,
)
from music_assistant_models.media_items import (
    AudioFormat,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.cache import use_cache
from music_assistant.helpers import datetime
from music_assistant.providers.bbc_sounds.constants import Constants
from music_assistant.providers.bbc_sounds.live_rewind import (
    LIVE_SEEK_TOLERANCE,
    LiveProgrammeId,
    http_fetchers,
    rewind_playlist_url,
    stream_programme,
)
from music_assistant.providers.bbc_sounds.now_playing import (
    NowPlayingManager,
)

if TYPE_CHECKING:
    from music_assistant_models.player_queue import PlayerQueue

    from music_assistant.mass import MusicAssistant
    from music_assistant.providers.bbc_sounds.adaptor import Adaptor
    from music_assistant.providers.bbc_sounds.provider import BBCSoundsProvider


class StreamManager:
    """Manages the supporting streaming functionality."""

    def __init__(self, provider: BBCSoundsProvider, now_playing: NowPlayingManager) -> None:
        """Initialise the streaming manager."""
        self.provider: BBCSoundsProvider = provider
        self.now_playing: NowPlayingManager = now_playing
        self.mass: MusicAssistant = self.provider.mass
        self.domain = self.provider.domain
        self.logger = self.provider.logger

    @property
    def client(self) -> SoundsClient:
        """Return the provider's client."""
        return self.provider.client

    @property
    def adaptor(self) -> Adaptor:
        """Return the provider's adaptor."""
        return self.provider.adaptor

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes]:
        """
        Return the audio of a programme on air now, from its start plus the seek position.

        :param streamdetails: Stream details of the programme.
        :param seek_position: Position within the programme to start from, in seconds.
        """
        programme_id = LiveProgrammeId.parse(streamdetails.item_id)
        if programme_id is None:
            raise MediaNotFoundError(f"Not a live programme: {streamdetails.item_id}")
        live_url = streamdetails.data["live_url"]
        start = programme_id.start + seek_position
        # the core replaces the buffer on a seek it can not serve, but leaves this stream
        # running until it goes idle; stop following the live edge once that happens.
        # The core attaches the buffer to the stream details before starting this stream.
        own_buffer = streamdetails.buffer
        fetch_text, fetch_bytes = http_fetchers(self.mass.http_session)
        async for chunk in stream_programme(
            fetch_text=fetch_text,
            fetch_bytes=fetch_bytes,
            rewind_url=rewind_playlist_url(live_url),
            live_url=live_url,
            start=start,
            end=programme_id.end,
            logger=self.logger,
            on_start=lambda actual_start: self._snap_to_live(
                streamdetails, programme_id, start, actual_start
            ),
            is_superseded=lambda: streamdetails.buffer is not own_buffer,
        ):
            yield chunk
        if streamdetails.buffer is own_buffer:
            # the programme is over: carry on with the station live, as the BBC players do
            self.mass.create_task(self._continue_with_station(streamdetails, programme_id))

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get streamdetails for a track/radio."""
        self.logger.debug("Getting stream details for %s (%s)", item_id, media_type)
        if programme_id := LiveProgrammeId.parse(item_id):
            return await self._live_programme_stream_details(programme_id)
        if media_type in [MediaType.PODCAST_EPISODE, MediaType.TRACK]:
            return await self._catch_up_stream_details(item_id, media_type)

        stream_details = await self._get_station_stream_details(item_id)
        # Populate the first frame of metadata up front, so the player shows the
        # current track immediately rather than the station name until the first
        # callback lands. (stolen from siriusxm provider)
        try:
            await self.now_playing.update_live_stream_metadata(stream_details, 0)
        except exceptions.SoundsException:
            self.logger.exception("Failed to update live stream metadata for %s", item_id)
        return stream_details

    @use_cache(expiration=Constants.DEFAULT_EXPIRATION)
    async def _get_vpid(self, pid: str) -> str | None:
        """Get the version pid of an episode, which play status updates need."""
        return await self.provider.client.content.get_vpid_from_pid(pid)

    async def _catch_up_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get stream details for catch-up content."""
        episode = await self.client.content.get_by_pid(
            item_id,
            include_stream=True,
        )

        stream_details = await self.adaptor.new_streamable_object(episode)

        if not stream_details or not isinstance(episode, PlayableItem):
            raise self._stream_error(item_id, media_type)

        stream_details.data = {
            "vpid": episode.id,
            "pid": episode.pid,
            "episode_metadata": stream_details.stream_metadata,
        }
        stream_details.stream_metadata_update_callback = (
            self.now_playing.update_on_demand_stream_metadata
        )
        stream_details.stream_metadata_update_interval = Constants.LISTEN_BACK_REFRESH_TIME
        return stream_details

    def _stream_error(self, item_id: str, media_type: MediaType) -> MusicAssistantError:
        return MusicAssistantError(f"Couldn't get stream details for {item_id} ({media_type})")

    async def _get_station_stream_details(self, item_id: str) -> StreamDetails:
        """Fetch stream details for a live station."""
        station: Station | LiveStation | None = await self.client.stations.get_station(
            item_id,
            include_stream=True,
            stream_preferences=StreamPreference(),
        )

        if not station:
            raise MusicAssistantError(f"Couldn't get stream details for station {item_id}")

        if not station.stream:
            raise MusicAssistantError(f"No stream found for {item_id}")

        stream_details = await self.adaptor.new_streamable_object(station)

        if not stream_details:
            raise self._stream_error(item_id, MediaType.RADIO)

        stream_details.stream_metadata_update_callback = (
            self.now_playing.update_live_stream_metadata
        )
        stream_details.stream_metadata_update_interval = Constants.NOW_PLAYING_REFRESH_TIME
        return stream_details

    async def _live_programme_stream_details(self, programme_id: LiveProgrammeId) -> StreamDetails:
        """Get stream details for a programme on air now, played from the live rewind window."""
        if datetime.utc_timestamp() >= programme_id.end:
            # once the programme is over its on-demand version is usually available
            try:
                return await self._catch_up_stream_details(
                    programme_id.pid, MediaType.PODCAST_EPISODE
                )
            except MusicAssistantError, exceptions.SoundsHttpException:
                self.logger.debug(
                    "No on-demand version of %s yet, using the live stream", programme_id.pid
                )

        station = await self.client.stations.get_station(
            programme_id.station_id,
            include_stream=True,
            stream_preferences=StreamPreference(),
        )
        if not station or not station.stream:
            raise MediaNotFoundError(f"No live stream found for {programme_id.station_id}")
        variant = await self.mass.streams.audio.get_hls_substream(str(station.stream))
        live_url = variant.path
        # fail here rather than once playback starts when the stream can not be rewound
        rewind_playlist_url(live_url)
        # no stream metadata: the queue item already describes the programme, whereas the
        # station's current programme may be another one
        return StreamDetails(
            provider=self.provider.instance_id,
            item_id=programme_id.item_id,
            # MPEG-TS segments, left for ffmpeg to detect
            audio_format=AudioFormat(content_type=ContentType.UNKNOWN),
            media_type=MediaType.PODCAST_EPISODE,
            stream_type=StreamType.CUSTOM,
            duration=programme_id.duration,
            allow_seek=True,
            can_seek=True,
            data={"live_url": live_url},
            # keep these details, and the buffer seeks are checked against, for the whole
            # programme; the live playlist URLs carry no expiring token
            expiration=max(600, programme_id.end - int(datetime.utc_timestamp()) + 600),
            stream_metadata_update_callback=self.now_playing.update_live_programme_metadata,
            stream_metadata_update_interval=Constants.NOW_PLAYING_REFRESH_TIME,
        )

    async def _continue_with_station(
        self, streamdetails: StreamDetails, programme_id: LiveProgrammeId
    ) -> None:
        """
        Queue the station's live stream after a live programme, when nothing else follows it.

        :param streamdetails: Stream details of the programme that has finished streaming.
        :param programme_id: The programme that has finished streaming.
        """
        if (
            (queue := self._queue_playing(streamdetails)) is None
            or queue.current_index is None
            or self.mass.player_queues.get_next_item(queue.queue_id, queue.current_index)
        ):
            return
        try:
            station = await self.provider.get_radio(programme_id.station_id)
            await self.mass.player_queues.play_media(
                queue.queue_id, station, option=QueueOption.ADD
            )
        except MusicAssistantError as err:
            self.logger.warning(
                "Couldn't queue %s after %s: %s", programme_id.station_id, programme_id.pid, err
            )

    def _snap_to_live(
        self,
        streamdetails: StreamDetails,
        programme_id: LiveProgrammeId,
        requested_start: float,
        actual_start: float,
    ) -> None:
        """
        Move the queue's position back to the live edge after a seek past it.

        :param streamdetails: Stream details of the programme being streamed.
        :param programme_id: The programme being streamed.
        :param requested_start: The UTC timestamp the seek asked for.
        :param actual_start: The UTC timestamp playback starts from.
        """
        if (
            requested_start - actual_start <= LIVE_SEEK_TOLERANCE
            or (queue := self._queue_playing(streamdetails)) is None
        ):
            return
        position = max(0, int(actual_start) - programme_id.start)
        self.logger.debug(
            "Seek past the live edge of %s, moving to %ss", programme_id.pid, position
        )
        with self.mass.players.without_member_power_on():
            self.mass.create_task(self.mass.player_queues.seek(queue.queue_id, position))

    def _queue_playing(self, streamdetails: StreamDetails) -> PlayerQueue | None:
        """Return the queue whose current item is being streamed with these stream details."""
        if not streamdetails.queue_id:
            return None
        queue = self.mass.player_queues.get(streamdetails.queue_id)
        if queue is None or queue.current_item is None:
            return None
        return queue if queue.current_item.streamdetails is streamdetails else None
