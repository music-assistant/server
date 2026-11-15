"""BBC Sounds provider implementation."""

import logging
from collections.abc import AsyncGenerator, Sequence
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from auntie_sounds import LoginFailedError, SoundsClient, exceptions
from auntie_sounds.content import MAX_PODCAST_EPISODES
from auntie_sounds.models import (
    EpisodeTypes,
    Menu,
    RadioShow,
    ScheduleItem,
)
from auntie_sounds.playback import PlayStatus
from music_assistant_models.enums import (
    MediaType,
    ProviderFeature,
)
from music_assistant_models.errors import (
    LoginFailed,
    MediaNotFoundError,
    MusicAssistantError,
    UnplayableMediaError,
)
from music_assistant_models.media_items import (
    BrowseFolder,
    ItemMapping,
    MediaItemType,
    Podcast,
    PodcastEpisode,
    Radio,
    RecommendationFolder,
    SearchResults,
    Track,
)
from music_assistant_models.unique_list import UniqueList

from music_assistant.constants import (
    CONF_ENTRY_UNOFFICIAL_PROVIDER,
    CONF_PASSWORD,
    CONF_USERNAME,
)
from music_assistant.controllers.cache import use_cache
from music_assistant.helpers import datetime
from music_assistant.models.music_provider import MusicProvider
from music_assistant.models.recommendation_payload import RecommendationPayloadMixin
from music_assistant.providers.bbc_sounds.adaptor import Adaptor, ConversionError
from music_assistant.providers.bbc_sounds.browse import BrowseManager
from music_assistant.providers.bbc_sounds.constants import Constants
from music_assistant.providers.bbc_sounds.live_rewind import (
    LiveProgrammeId,
)
from music_assistant.providers.bbc_sounds.now_playing import NowPlayingManager
from music_assistant.providers.bbc_sounds.streaming import StreamManager

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry
    from music_assistant_models.streamdetails import StreamDetails

SUPPORTED_FEATURES = {
    ProviderFeature.BROWSE,
    ProviderFeature.RECOMMENDATIONS,
    ProviderFeature.SEARCH,
}

LIBRARY_LOGGER = logging.getLogger("auntie_sounds")


class BBCSoundsProvider(RecommendationPayloadMixin, MusicProvider):
    """A MusicProvider class to interact with the BBC Sounds API via auntie-sounds."""

    recommendation_payload_ttl = 3600 * 3

    @property
    def max_concurrent_streams(self) -> None:
        """Allow unlimited concurrent upstream source streams."""
        return None

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to set up this provider."""
        return (CONF_ENTRY_UNOFFICIAL_PROVIDER,)

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        username = self.get_setup_value(CONF_USERNAME)
        password = self.get_setup_value(CONF_PASSWORD)
        log_level = self.logger.getEffectiveLevel()
        if username and password:
            self.client = SoundsClient(
                session=self.mass.http_session,
                username=username or None,
                password=password or None,
                log_level=log_level,
                cookie_file_location=Path(self.mass.storage_path)
                / f"bbc_sounds_{self.instance_id}.jar",
            )
            await self.client.start()
            try:
                await self.client.login()
            except BaseException as e:
                await self.client.close()
                if isinstance(e, LoginFailedError):
                    raise LoginFailed(e) from e
                raise

        else:
            self.client = SoundsClient(session=self.mass.http_session, log_level=log_level)
            await self.client.start()

        self._create_managers()

    async def unload(self, is_removed: bool = False) -> None:
        """Close the provider's session and, on removal, delete its stored cookies."""
        self.logger.debug("Closing BBC Sounds provider session...")
        try:
            client = getattr(self, "client", None)
            if client:
                await client.close()
            # If the provider has been removed, clean up the cookie jar
            if client and is_removed:
                jar = client.cookie_path
                jar.unlink(missing_ok=True)
                jar.with_name(f"{jar.name}.owner").unlink(missing_ok=True)
        finally:
            await super().unload(is_removed)

    async def loaded_in_mass(self) -> None:
        """Do post-loaded actions."""
        if not self.browse_manager.menu or (
            isinstance(self.browse_manager.menu, Menu) and not self.browse_manager.menu.sub_items
        ):
            await self.browse_manager._refresh_menu_from_api()

    @use_cache(expiration=Constants.DEFAULT_EXPIRATION)
    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        episode_info = await self.client.content.get_by_pid(pid=prov_track_id)
        track = await self.adaptor.new_object(episode_info, force_type=Track)
        if not isinstance(track, Track):
            raise MusicAssistantError(f"Incorrect track returned for {prov_track_id}")
        return track

    async def get_podcast_episode(self, prov_episode_id: str) -> PodcastEpisode:
        """Get full podcast episode details by id."""
        if programme_id := LiveProgrammeId.parse(prov_episode_id):
            return await self._get_live_programme_episode(programme_id)
        return await self._get_on_demand_episode(prov_episode_id)

    @use_cache(expiration=Constants.DEFAULT_EXPIRATION)
    async def get_podcast(self, prov_podcast_id: str) -> Podcast:
        """Get full podcast details by id."""
        self.logger.debug("Getting podcast for %s", prov_podcast_id)
        podcast = await self.client.content.get_podcast(pid=prov_podcast_id)
        ma_podcast = await self.adaptor.new_object(source_obj=podcast, force_type=Podcast)

        if isinstance(ma_podcast, Podcast):
            return ma_podcast
        raise MusicAssistantError("Incorrect format for podcast")

    async def get_podcast_episodes(
        self,
        prov_podcast_id: str,
    ) -> AsyncGenerator[PodcastEpisode]:
        """Get all PodcastEpisodes for given podcast id."""
        self.logger.debug("Getting podcast episodes for %s", prov_podcast_id)
        podcast_episodes = await self.client.content.get_podcast_episodes(
            prov_podcast_id, max_items=MAX_PODCAST_EPISODES
        )
        if podcast_episodes:
            for episode in podcast_episodes:
                try:
                    this_episode = await self.adaptor.new_object(
                        source_obj=episode, force_type=PodcastEpisode
                    )
                    if this_episode and isinstance(this_episode, PodcastEpisode):
                        yield this_episode
                except ConversionError:
                    self.logger.error("Couldn't convert podcast episode %s", episode)
                    continue

    @use_cache(expiration=Constants.SHORT_EXPIRATION)
    async def get_radio(self, prov_radio_id: str) -> Radio:
        """Get full radio details by id."""
        self.logger.debug("Getting radio for %s", prov_radio_id)
        station = await self.client.stations.get_station(prov_radio_id, include_stream=True)
        if station:
            ma_radio = await self.adaptor.new_object(station, force_type=Radio)
            if ma_radio and isinstance(ma_radio, Radio):
                return ma_radio
        else:
            raise MediaNotFoundError(f"No station found: {prov_radio_id}")

        self.logger.error("Failed to retrieve station with details: %s %s", station, ma_radio)
        raise MediaNotFoundError("No valid radio stream found")

    @property
    def is_streaming_provider(self) -> bool:
        """Return True as the provider is a streaming provider."""
        return True

    async def get_recommendations(self) -> list[RecommendationFolder]:
        """Get this provider's available recommendation rows, without items."""
        if not self.client.is_signed_in:
            return []
        return await self._recommendation_rows_from_payload()

    async def get_recommendation_items(
        self, item_id: str
    ) -> UniqueList[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Get the items for a single recommendation row.

        :param item_id: The item_id of the row, as returned by get_recommendations.
        """
        if not self.client.is_signed_in:
            return UniqueList()
        return await self._recommendation_items_from_payload(item_id)

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Browse this provider's items.

        :param path: The path to browse, (e.g. provider_id://artists).
        """
        return await self.browse_manager.browse(path)

    async def search(
        self, search_query: str, media_types: list[MediaType] | None, limit: int = 5
    ) -> SearchResults:
        """Perform search for BBC Sounds stations."""
        results = SearchResults()
        search_result = await self.client.content.search(search_query)
        self.logger.debug(search_result)
        if media_types is None or MediaType.RADIO in media_types:
            radios = [await self.adaptor.new_object(radio) for radio in search_result.stations]
            results.radio = [radio for radio in radios if isinstance(radio, Radio)]
        if (
            media_types is None
            or MediaType.TRACK in media_types
            or MediaType.PODCAST_EPISODE in media_types
        ):
            episodes = [await self.adaptor.new_object(track) for track in search_result.episodes]
            results.tracks = [track for track in episodes if type(track) is Track]

        if media_types is None or MediaType.PODCAST in media_types:
            podcasts = [await self.adaptor.new_object(show) for show in search_result.shows]
            results.podcasts = [podcast for podcast in podcasts if isinstance(podcast, Podcast)]

        return results

    async def on_played(
        self,
        media_type: MediaType,
        prov_item_id: str,
        fully_played: bool,
        position: int,
        media_item: MediaItemType,
        is_playing: bool = False,
    ) -> None:
        """Handle callback when a (playable) media item has been played."""
        # live programme ids are not known to the BBC, even when the programme has ended and
        # was played from its on-demand version
        if self.client.is_signed_in and not LiveProgrammeId.parse(prov_item_id):
            if media_type != MediaType.RADIO:
                # Handle Sounds API play status updates
                action = None

                if is_playing:
                    action = PlayStatus.STARTED if position < 30 else PlayStatus.HEARTBEAT
                elif fully_played:
                    action = PlayStatus.ENDED
                else:
                    action = PlayStatus.PAUSED

                if action:
                    try:
                        vpid = await self.stream_manager._get_vpid(prov_item_id)
                        if not vpid:
                            self.logger.debug("No version pid for %s", prov_item_id)
                            return
                        success = await self.client.playback.update_episode_play_status(
                            pid=prov_item_id, vpid=vpid, action=action, elapsed_time=position
                        )
                        self.logger.debug("Updated play status: %s", success)
                    except exceptions.SoundsException:
                        self.logger.exception("Error updating play status")

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes]:
        """
        Return the audio of a programme on air now, from its start plus the seek position.

        :param streamdetails: Stream details of the programme.
        :param seek_position: Position within the programme to start from, in seconds.
        """
        async for chunk in self.stream_manager.get_audio_stream(streamdetails, seek_position):
            yield chunk

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get streamdetails for a track/radio."""
        return await self.stream_manager.get_stream_details(item_id, media_type)

    def _create_managers(self) -> None:
        self.adaptor = Adaptor(self)
        self.now_playing = NowPlayingManager(self)
        self.stream_manager = StreamManager(self, self.now_playing)
        self.browse_manager = BrowseManager(self)

    async def _fetch_recommendation_payload(self) -> list[RecommendationFolder]:
        """Fetch the recommendation menu folders, with items."""
        self.logger.debug("Getting recommendations from API")
        folders: list[RecommendationFolder] = []

        for item in await self.client.get_recommendation_folders():
            folder = await self.adaptor.new_object(item, force_type=RecommendationFolder)
            if isinstance(folder, RecommendationFolder):
                folders.append(folder)
        return folders

    @use_cache(expiration=Constants.SHORT_EXPIRATION)
    async def _get_live_programme_episode(self, programme_id: LiveProgrammeId) -> PodcastEpisode:
        """Get the episode of a programme that is (or was) played from the live stream."""
        episode_date = (
            datetime.from_utc_timestamp(programme_id.start)
            .astimezone(ZoneInfo(Constants.SCHEDULE_TIMEZONE))
            .date()
            .isoformat()
        )
        schedule = await self.client.schedules.get_schedule(
            station_id=programme_id.station_id, date=episode_date
        )
        # a programme that has aired is listed as a RadioShow rather than a ScheduleItem
        for item in schedule.sub_items if schedule and schedule.sub_items else []:
            if isinstance(item, (ScheduleItem, RadioShow)) and item.item_id == programme_id.pid:
                return await self.adaptor.new_live_programme(item)
        raise MediaNotFoundError(
            f"Programme {programme_id.pid} not found in the {episode_date} schedule"
        )

    @use_cache(expiration=Constants.DEFAULT_EXPIRATION)
    async def _get_on_demand_episode(self, prov_episode_id: str) -> PodcastEpisode:
        """Get the details of an on-demand podcast episode or radio show by id."""
        # If we are requesting a previously-aired radio show, we lose access to the
        # schedule time. The best we can find out from the API is original release
        # date, so the stream title loses access to the air date
        self.logger.debug("Getting podcast episode for %s", prov_episode_id)
        episode = await self.client.content.get_by_pid(prov_episode_id)
        # This can return a mixture of episode/clip types
        if not isinstance(episode, EpisodeTypes):
            raise UnplayableMediaError(
                f"Unexpected type {type(episode).__name__} for episode {prov_episode_id}"
            )
        ma_episode = await self.adaptor.new_object(episode, force_type=PodcastEpisode)
        if not ma_episode:
            raise MediaNotFoundError(f"Podcast episode {prov_episode_id} not found")
        if type(ma_episode) is not PodcastEpisode:
            raise UnplayableMediaError(f"Incorrect format for episode {prov_episode_id}")
        return ma_episode
