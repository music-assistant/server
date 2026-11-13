"""BBC Sounds music provider support for MusicAssistant."""

import asyncio
import logging
from collections.abc import AsyncGenerator, Sequence
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from auntie_sounds import LoginFailedError, SoundsClient, exceptions
from auntie_sounds.models import (
    CategoryItemContainer,
    Container,
    EpisodeTypes,
    LiveStation,
    Menu,
    MenuItem,
    PlayableItem,
    Playlist,
    RadioClip,
    RadioShow,
    ScheduleItem,
    Segment,
    Station,
)
from auntie_sounds.models import PodcastEpisode as SoundsPodcastEpisode
from auntie_sounds.playback import PlayStatus, StreamPreference
from auntie_sounds.schedule import catch_up_dates, schedule_dates
from music_assistant_models.enums import (
    ContentType,
    ImageType,
    MediaType,
    ProviderFeature,
    QueueOption,
    StreamType,
)
from music_assistant_models.errors import (
    LoginFailed,
    MediaNotFoundError,
    MusicAssistantError,
    UnplayableMediaError,
)
from music_assistant_models.media_items import (
    AudioFormat,
    BrowseFolder,
    ItemMapping,
    MediaItemImage,
    MediaItemMetadata,
    MediaItemType,
    Podcast,
    PodcastEpisode,
    ProviderMapping,
    Radio,
    RecommendationFolder,
    SearchResults,
    Track,
)
from music_assistant_models.streamdetails import StreamDetails, StreamMetadata
from music_assistant_models.unique_list import UniqueList

from music_assistant.constants import (
    CONF_ENTRY_UNOFFICIAL_PROVIDER,
    CONF_PASSWORD,
    CONF_USERNAME,
    VERBOSE_LOG_LEVEL,
)
from music_assistant.controllers.cache import use_cache
from music_assistant.helpers import datetime
from music_assistant.mass import MusicAssistant
from music_assistant.models import ProviderInstanceType
from music_assistant.models.music_provider import MusicProvider
from music_assistant.models.recommendation_payload import RecommendationPayloadMixin
from music_assistant.providers.bbc_sounds.adaptor import Adaptor, ConversionError
from music_assistant.providers.bbc_sounds.constants import Constants, ValidMenuIDs
from music_assistant.providers.bbc_sounds.live_rewind import (
    LIVE_SEEK_TOLERANCE,
    LiveProgrammeId,
    http_fetchers,
    rewind_playlist_url,
    stream_programme,
)
from music_assistant.providers.bbc_sounds.metadata import (
    _find_segment,
    _segment_to_metadata,
    _station_programme_display,
)

if TYPE_CHECKING:
    from auntie_sounds.models import SoundsTypes
    from music_assistant_models.config_entries import ConfigEntry, ProviderConfig
    from music_assistant_models.player_queue import PlayerQueue
    from music_assistant_models.provider import ProviderManifest


SUPPORTED_FEATURES = {
    ProviderFeature.BROWSE,
    ProviderFeature.RECOMMENDATIONS,
    ProviderFeature.SEARCH,
}

LIBRARY_LOGGER = logging.getLogger("auntie_sounds")

# Keep a backwards-compatible list around to keep the previous URL working
_STATION_DATE_MENUS = (ValidMenuIDs.SCHEDULES, ValidMenuIDs.STATIONS)


def _ordinal(day: int) -> str:
    """Return the English ordinal suffix for a day of the month."""
    if 4 <= day <= 20 or 24 <= day <= 30:
        return "th"
    return ("st", "nd", "rd")[day % 10 - 1]  # codespell:ignore


def _schedule_date_title(day: date, today: date) -> str:
    """Return a folder name for a schedule date, e.g. "Today" or "Sunday 8th November"."""
    match (day - today).days:
        case 0:
            return "Today"
        case -1:
            return "Yesterday"
        case 1:
            return "Tomorrow"
    return f"{day:%A} {day.day}{_ordinal(day.day)} {day:%B}"


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Create new provider instance."""
    return BBCSoundsProvider(mass, manifest, config, SUPPORTED_FEATURES)


class BBCSoundsProvider(RecommendationPayloadMixin, MusicProvider):
    """A MusicProvider class to interact with the BBC Sounds API via auntie-sounds."""

    recommendation_payload_ttl = 3600 * 3

    client: SoundsClient
    menu: Menu | None = None
    menu_last_fetched: float | None = None
    adaptor: Adaptor

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

        self.adaptor = Adaptor(self)

    async def unload(self, is_removed: bool = False) -> None:
        """Close the client's own HTTP session, which shares MA's connection pool."""
        self.logger.debug("Closing BBC Sounds provider session...")
        if client := getattr(self, "client", None):
            await client.close()
        await super().unload(is_removed)

    async def loaded_in_mass(self) -> None:
        """Do post-loaded actions."""
        if not self.menu or (isinstance(self.menu, Menu) and not self.menu.sub_items):
            await self._refresh_menu_from_api()

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
            prov_podcast_id, fetch_all_items=True
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
        self.logger.debug("Browsing path: %s", path)
        if not path.startswith(f"{self.domain}://"):
            raise MusicAssistantError(f"Invalid path for {self.domain} provider: {path}")
        path_parts = path.split("://", 1)[1].split("/")
        self.logger.debug("Path parts: %s", path_parts)

        path_parts = [
            f"{self.domain}:/",
            *[part for part in path_parts if len(part) > 0],
        ]
        dispatch_menu = path_parts[1] if len(path_parts) > 1 else ""

        if dispatch_menu == "live":
            return await self._browse_live(path_parts)
        if dispatch_menu == "catch_up":
            return await self._browse_catch_up(path_parts)
        if dispatch_menu in _STATION_DATE_MENUS:
            return await self._browse_schedules(path_parts)
        if dispatch_menu == "categories":
            return await self._browse_categories(path_parts)
        if dispatch_menu == "collections":
            return await self._browse_collections(path_parts)
        if dispatch_menu == "playlists":
            if len(path_parts) < 3:
                raise KeyError("Invalid subpath")
            return await self._get_playlist(path_parts[2])
        if (
            dispatch_menu != ""
            and dispatch_menu not in ValidMenuIDs
            and Constants.LATEST_NEWS_PLAYLIST_SUFFIX not in dispatch_menu
        ):
            raise KeyError("Invalid subpath")
        return await self._browse_menu(path_parts)

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
                        vpid = await self._get_vpid(prov_item_id)
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

    @use_cache(expiration=Constants.DEFAULT_EXPIRATION)
    async def _get_vpid(self, pid: str) -> str | None:
        """Get the version pid of an episode, which play status updates need."""
        return await self.client.content.get_vpid_from_pid(pid)

    @property
    def _menu_is_stale(self) -> bool:
        if not self.menu:
            self.logger.debug("No menu set")
            return True
        if not self.menu.sub_items:
            self.logger.debug("Menu has no items")
            return True
        if self.menu_last_fetched is not None and (
            (datetime.utc_timestamp() - self.menu_last_fetched) >= Constants.SHORT_EXPIRATION
        ):
            self.logger.debug("Menu has expired")
            return True
        return False

    async def _browse_menu(
        self, path_parts: list[str]
    ) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        if len(path_parts) == 1:
            # bbc_sounds://
            return await self._get_menu()
        return await self._get_subpath_menu(path_parts[1:])

    async def _browse_live(
        self, path_parts: list[str]
    ) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        if len(path_parts) >= 3 and path_parts[2] == "local":
            self.logger.debug("Getting local stations")
            return await self._station_list(
                path_parts=path_parts,
                include_national=False,
                include_local=True,
                show_current_programme=True,
            )
        if len(path_parts) >= 3 and path_parts[2] == "international":
            self.logger.debug("Getting international stations")
            return await self._station_list(
                path_parts=path_parts,
                include_national=False,
                include_local=False,
                include_international=True,
                show_current_programme=False,
            )
        self.logger.debug("Getting national stations")
        return await self._station_list(
            path_parts=path_parts, include_local=False, show_current_programme=True
        )

    async def _browse_categories(
        self, path_parts: list[str]
    ) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        if len(path_parts) == 3:
            # bbc_sounds://categories/<category>
            return await self._get_category(path_parts[2])
        # bbc_sounds://categories/
        return await self._get_subpath_menu(path_parts[1:])

    async def _browse_collections(
        self, path_parts: list[str]
    ) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        if len(path_parts) == 3:
            # bbc_sounds://collections/<collection>
            return await self._get_collection(path_parts[2])
        # bbc_sounds://collections/
        return await self._get_subpath_menu(path_parts[1:])

    async def _browse_catch_up(
        self, path_parts: list[str]
    ) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        if len(path_parts) == 4:
            # bbc_sounds://catch_up/<station_id>/<date>
            return await self._get_station_catch_up_menu(
                path_parts=path_parts,
                station_id=path_parts[2],
                date=path_parts[3],
            )
        if len(path_parts) == 3:
            # bbc_sounds://catch_up/<station_id> OR
            # bbc_sounds://catch_up/local
            if path_parts[2] == "local":
                return await self._station_list_as_folders(
                    path_parts=path_parts,
                    include_national=False,
                    include_local=True,
                    include_international=False,
                )
            return await self._get_station_menu(path_parts)

        # bbc_sounds://catch_up
        return await self._station_list_as_folders(path_parts=path_parts)

    async def _browse_schedules(
        self, path_parts: list[str]
    ) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        if len(path_parts) == 4:
            # bbc_sounds://schedules/<station_id>/<date>
            return await self._get_station_catch_up_menu(
                path_parts=path_parts,
                station_id=path_parts[2],
                date=path_parts[3],
            )
        if len(path_parts) == 3:
            # bbc_sounds://schedules/<station_id> OR bbc_sounds://schedules/local
            if path_parts[2] == "local":
                return await self._station_list_as_folders(
                    path_parts=path_parts, include_national=False, include_local=True
                )
            return await self._get_station_menu(path_parts)

        # bbc_sounds://stations
        return await self._station_list_as_folders(path_parts=path_parts)

    async def _get_station_menu(
        self,
        path_parts: list[str],
    ) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """
        List the dates a station's programmes can be browsed for.

        :param path_parts: The browse path, e.g. bbc_sounds://catch_up/<station_id>.
        """
        # the schedule looks ahead, catch-up (and the old stations menu) looks back
        dates = schedule_dates() if path_parts[1] == ValidMenuIDs.SCHEDULES else catch_up_dates()
        today = dates[0]
        item_list: list[MediaItemType | ItemMapping | BrowseFolder] = []
        for day in dates:
            folder = MenuItem(id=day.isoformat(), title=_schedule_date_title(day, today))
            if new_folder := await self._render_browse_item(folder, path_parts=path_parts):
                item_list.append(new_folder)
        return item_list

    async def _refresh_menu_from_api(self) -> None:
        """Get a fresh copy of the main Sounds menu from the API."""
        self.logger.debug("No cached menu, fetching from API")
        # TODO: Keep recommendations=True until full recommendation rail browsing lands
        self.menu = await self.client.get_menu(
            include_recommendations=True,
        )
        self.menu_last_fetched = datetime.utc_timestamp()

    async def _convert_menu(self) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        menu_items = []
        if self.menu:
            for item in self.menu.sub_items:
                new_item = await self._render_browse_item(item)
                if isinstance(new_item, (MediaItemType | ItemMapping | BrowseFolder)):
                    menu_items.append(new_item)
        return menu_items

    async def _get_menu(self) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """Access self.menu data, refreshing when necessary."""
        if self._menu_is_stale:
            await self._refresh_menu_from_api()

        if not self.menu or not self.menu.sub_items:
            raise MusicAssistantError("Menu API response is empty or invalid")
        utc_timestamp = datetime.utc_timestamp()
        if self.menu_last_fetched:
            menu_valid_for_mins = int(
                (Constants.SHORT_EXPIRATION - (utc_timestamp - self.menu_last_fetched)) / 60
            )
            self.logger.debug("Menu valid for %s more mins", menu_valid_for_mins)
        return await self._convert_menu()

    def _get_provider_mapping(self, item_id: str) -> ProviderMapping:
        return ProviderMapping(
            item_id=item_id,
            provider_domain=self.domain,
            provider_instance=self.instance_id,
        )

    @use_cache(expiration=Constants.DEFAULT_EXPIRATION)
    async def _get_playlist(self, pid: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Get a playlist from the API.

        When browsing the main menu, or through the "Top Picks for You" section, we might
        be presented with a Playlist. These don't usually contain their own contents,
        so we need a helper function to populate them.
        """
        playlist_contents = await self.client.content.get_playlist_contents(pid=pid)
        if playlist_contents and type(playlist_contents) is list:
            return [
                obj
                for obj in [
                    await self._render_browse_item(item) for item in playlist_contents if item
                ]
                if obj
            ]
        return []

    def _stream_error(self, item_id: str, media_type: MediaType) -> MusicAssistantError:
        return MusicAssistantError(f"Couldn't get stream details for {item_id} ({media_type})")

    async def _catch_up_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get stream details for catch-up content."""
        episode = await self.client.content.get_by_pid(
            item_id,
            include_stream=True,
        )

        stream_details = await self.adaptor.new_streamable_object(episode)

        if not stream_details or not isinstance(episode, PlayableItem):
            raise self._stream_error(item_id, media_type)

        stream_details.data = {"vpid": episode.id, "pid": episode.pid}
        stream_details.stream_metadata_update_callback = self._update_on_demand_stream_metadata
        stream_details.stream_metadata_update_interval = Constants.NOW_PLAYING_REFRESH_TIME
        return stream_details

    async def _get_station_stream_details(self, item_id: str) -> StreamDetails:
        """Fetch stream details for a live station."""
        station: Station | LiveStation | None = await self.client.stations.get_station(
            item_id,
            include_stream=True,
        )

        if not station:
            raise MusicAssistantError(f"Couldn't get stream details for station {item_id}")

        if not station.stream:
            raise MusicAssistantError(f"No stream found for {item_id}")

        stream_details = await self.adaptor.new_streamable_object(station)

        if not stream_details:
            raise self._stream_error(item_id, MediaType.RADIO)

        stream_details.stream_metadata_update_callback = self._update_live_stream_metadata
        stream_details.stream_metadata_update_interval = Constants.NOW_PLAYING_REFRESH_TIME
        return stream_details

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
            await self._update_live_stream_metadata(stream_details, 0)
        except exceptions.SoundsException:
            self.logger.exception("Failed to update live stream metadata for %s", item_id)
        return stream_details

    async def _get_programme_segments(self, vpid: str) -> list[Segment]:
        """Get on demand segments from cache or API."""
        cache_key = f"programme_segments_v3_{vpid}"
        cached = await self.mass.cache.get(provider=self.domain, key=cache_key)
        if isinstance(cached, list):
            self.logger.debug("Cache hit for programme segments for %s", vpid)
            return [Segment.from_dict(item) for item in cached]
        self.logger.debug("No cache for programme segments for %s", vpid)
        segments = await self.client.content.get_show_segments(vpid, fetch_missing_images=True)
        # The track list can be published after the programme, so an empty one isn't cached
        if segments:
            await self.mass.cache.set(
                provider=self.domain,
                key=cache_key,
                data=[segment.to_dict() for segment in segments],
            )
        return segments

    async def _update_on_demand_stream_metadata(
        self, stream_details: StreamDetails, elapsed_time: int
    ) -> None:
        """
        Get the currently playing segment (song) for on-demand episodes.

        Called by the callback function in StreamDetails.
        """
        self.logger.log(VERBOSE_LOG_LEVEL, "Updating on-demand stream metadata")

        if not stream_details or not stream_details.stream_metadata:
            return

        vpid = stream_details.data.get("vpid")
        if not vpid:
            self.logger.warning("No VPID found")
            return

        # Store the segments here rather than the cache so we don't have to worry about
        # caching a show's segments before they are available
        segments = stream_details.data.get("segments")
        # A show with no segments is valid, so only check for None
        if segments is None:
            segments = await self._get_programme_segments(vpid)
            stream_details.data["segments"] = segments
        if not segments:
            return

        segment = _find_segment(segments, elapsed_time)

        if segment:
            metadata = _segment_to_metadata(segment)
            if metadata:
                stream_details.stream_metadata = metadata
            # As of June 2026, the API currently doesn't return images from this endpoint
            # We fill in missing images with the Spotify API, if any are still blank
            # then use the MA helpers here
            if not stream_details.stream_metadata.image_url:
                try:
                    async with asyncio.timeout(Constants.ARTWORK_TIMEOUT):
                        await self.mass.metadata.update_radio_stream_artwork(stream_details)
                except TimeoutError:
                    self.logger.debug("Timeout while waiting for artwork")
            return

        # Nothing playing; show episode metadata
        episode_info = await self._catch_up_stream_details(
            item_id=stream_details.data.get("pid"),
            media_type=stream_details.media_type,
        )

        if episode_info.stream_title:
            stream_details.stream_title = episode_info.stream_title

        if episode_info.stream_metadata:
            stream_details.stream_metadata = episode_info.stream_metadata

    @use_cache(expiration=Constants.DYNAMIC_EXPIRATION, base_class=StreamMetadata)
    async def _station_current_programme_metadata(self, station_id: str) -> StreamMetadata | None:
        """Reduce downstream API calls via simple wrapper for caching."""
        # TODO: we can actually cache this for the remainder of the programme if we
        # calculate that value
        self.logger.debug("Fetching fresh programme result from API")
        station = await self.client.schedules.current_programme(station_id)
        if not station:
            return None
        self.logger.debug("%s(%s)", type(station), station.id)
        self.logger.log(VERBOSE_LOG_LEVEL, station)
        if station_metadata := _station_programme_display(station):
            return station_metadata
        return None

    async def _update_live_stream_metadata(
        self, stream_details: StreamDetails, elapsed_time: int
    ) -> None:
        """Get the currently playing song for live radio streams."""
        self.logger.log(VERBOSE_LOG_LEVEL, "Updating live stream metadata")
        if not stream_details or not stream_details.stream_metadata:
            return

        station_id = stream_details.item_id
        if not station_id:
            return

        now_playing = await self.client.schedules.currently_playing_song(station_id)
        if now_playing:
            self.logger.log(VERBOSE_LOG_LEVEL, "Now playing for %s: %s", station_id, now_playing)
            stream_details.stream_metadata = _segment_to_metadata(now_playing)
        else:
            self.logger.debug(
                "No song playing on %s, displaying current programme info", station_id
            )
            if metadata := await self._station_current_programme_metadata(station_id):
                stream_details.stream_metadata = metadata

    @use_cache(expiration=Constants.DEFAULT_EXPIRATION)
    async def _vod_programme_display(self, pid: str) -> StreamMetadata | None:
        episode = await self.client.content.get_by_pid(pid=pid)

        if (
            isinstance(episode, (SoundsPodcastEpisode, RadioShow))
            and episode.titles
            and episode.titles.secondary
        ):
            return StreamMetadata(title=episode.titles.secondary)
        return None

    async def _station_list_as_folders(
        self,
        path_parts: list[str],
        include_local: bool = False,
        include_national: bool = True,
        include_international: bool = False,
    ) -> list[BrowseFolder]:
        """Get list of stations as BrowseFolders."""
        radio_list: list[BrowseFolder] = []
        if include_national:
            radio_list.extend(
                [
                    BrowseFolder(
                        item_id="local",
                        translation_key="local_radio",
                        name="Local Radio",
                        provider=self.domain,
                        path="/".join([*path_parts, "local"]),
                    ),
                ]
            )
            path = "/".join([*path_parts])
        else:
            path = "/".join([*path_parts[:-1]])
        stations = await self.client.stations.get_stations(
            include_national_stations=include_national,
            include_local_stations=include_local,
            include_international_stations=include_international,
        )
        self.logger.debug("Output: %s", stations)
        for station in stations:
            if station:
                radio_list.append(
                    BrowseFolder(
                        item_id=station.id,
                        path=f"{path}/{station.id}",
                        name=(
                            station.network.short_title
                            if station.network and station.network.short_title
                            else "Unknown station"
                        ),
                        provider=self.domain,
                        image=MediaItemImage(
                            type=ImageType.THUMB,
                            provider=self.domain,
                            path=station.network.logo_url,
                            remotely_accessible=True,
                        )
                        if station.network and station.network.logo_url
                        else None,
                    )
                )
        return radio_list

    async def _render_browse_item(
        self,
        item: SoundsTypes,
        path_parts: list[str] | None = None,
    ) -> BrowseFolder | Track | Podcast | PodcastEpisode | RecommendationFolder | Radio | None:
        new_item = await self.adaptor.new_object(item, path_parts=path_parts)
        if isinstance(
            new_item,
            (BrowseFolder | Track | Podcast | PodcastEpisode | RecommendationFolder | Radio),
        ):
            return new_item
        return None

    @use_cache(expiration=Constants.DYNAMIC_EXPIRATION)
    async def _station_list(
        self,
        path_parts: list[str],
        include_local: bool = False,
        include_national: bool = True,
        include_international: bool = False,
        show_current_programme: bool = False,
    ) -> list[Radio | BrowseFolder]:
        """
        Get list of stations as Radios.

        We do this manually so we can append the current programme. We don't want this by
        default as it gets cached in other places.
        """
        # National is the default, so only show the extra folders there
        radio_list: list[Radio | BrowseFolder] = []
        if include_national:
            radio_list.extend(
                [
                    BrowseFolder(
                        item_id="local",
                        translation_key="local_radio",
                        name="Local Radio",
                        provider=self.domain,
                        path="/".join([*path_parts, "local"]),
                    ),
                    BrowseFolder(
                        item_id="international",
                        translation_key="international_radio",
                        name="International Radio",
                        provider=self.domain,
                        path="/".join([*path_parts, "international"]),
                    ),
                ]
            )

        def _get_station_title(station: LiveStation | Station) -> str:
            if isinstance(station, LiveStation):
                return str(station.network.short_title) if station.network else "Unknown Station"
            return station.title or station.id

        for station in await self.client.stations.get_stations(
            include_local_stations=include_local,
            include_national_stations=include_national,
            include_international_stations=include_international,
        ):
            if (
                station
                and station.id
                and (isinstance(station, LiveStation) or not station.international)
            ):
                # A plain Station has no programme, so its titles and synopses are empty
                synopsis = station.synopses.short if station.synopses else None
                details = (station.titles.secondary, synopsis)
                programme_details = " • ".join(filter(None, details)).replace(
                    " - ",
                    "–",  # ruff: ignore[RUF001]
                )
                station_title = _get_station_title(station)
                programme_title = station.titles.primary
                if not programme_title:
                    display_name = station_title
                elif station_title in programme_title:
                    display_name = programme_title
                else:
                    display_name = f"{station_title}: {programme_title}"
                metadata = MediaItemMetadata(
                    description=programme_details or None,
                    images=(
                        UniqueList(
                            [
                                MediaItemImage(
                                    type=ImageType.THUMB,
                                    provider=self.domain,
                                    path=station.network.logo_url,
                                    remotely_accessible=True,
                                ),
                            ]
                        )
                        if station and station.network and station.network.logo_url
                        else None
                    ),
                )
            elif isinstance(station, Station) and station.international:
                display_name = station.title if station.title is not None else station.id
                metadata = MediaItemMetadata(description=station.description)
            else:
                continue
            radio_list.append(
                Radio(
                    item_id=station.item_id,
                    name=display_name,
                    provider=self.domain,
                    metadata=metadata,
                    provider_mappings={
                        ProviderMapping(
                            item_id=station.item_id,
                            provider_domain=self.domain,
                            provider_instance=self.instance_id,
                        )
                    },
                )
            )
        return radio_list

    async def _get_subpath_menu(
        self, path_parts: list[str]
    ) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        item_list: list[MediaItemType | ItemMapping | BrowseFolder] = []
        if not self.menu:
            return item_list
        sub_menu: SoundsTypes | None = self.menu.get(path_parts[0])

        if isinstance(sub_menu, Container):
            for part in path_parts[1:]:
                # only a MenuItem can be descended into, but the last part can be anything
                if not isinstance(sub_menu, MenuItem):
                    break
                sub_menu = sub_menu.get(part)
                if sub_menu is None:
                    break
            else:
                if isinstance(sub_menu, MenuItem) and sub_menu.sub_items is not None:
                    for item in sub_menu.sub_items:
                        if new_item := await self._render_browse_item(
                            item, path_parts=[f"{self.domain}:/", *path_parts]
                        ):
                            item_list.append(new_item)
                # Playlists are returned empty in the main menu API
                elif isinstance(sub_menu, Playlist) and sub_menu.item_id is not None:
                    if sub_menu.item_id is not None:
                        item_list += await self._get_playlist(sub_menu.item_id)
        else:
            self.logger.warning("Sub menu not a container: %s", sub_menu)
        return item_list

    async def _get_station_catch_up_menu(
        self,
        station_id: str,
        path_parts: list[str],
        date: str,
    ) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """Lookup a catch-up menu for a station."""
        self.logger.debug("Getting catch-up for %s for %s", station_id, date)
        schedule = await self.client.schedules.get_schedule(
            station_id=station_id,
            date=date,
        )
        items = []
        if schedule and schedule.sub_items:
            for folder in schedule.sub_items:
                new_folder = await self._render_browse_item(folder, path_parts=path_parts)
                if new_folder:
                    items.append(new_folder)
        return items

    @use_cache(expiration=Constants.DEFAULT_EXPIRATION)
    async def _get_category(
        self, category_name: str
    ) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        category: CategoryItemContainer | None = await self.client.content.get_category(
            category=category_name
        )

        if category is not None and category.sub_items:
            return [
                obj
                for obj in [await self._render_browse_item(item) for item in category.sub_items]
                if obj is not None
            ]
        return []

    @use_cache(expiration=Constants.DEFAULT_EXPIRATION)
    async def _get_collection(
        self, pid: str
    ) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        collection: list[
            SoundsPodcastEpisode | RadioShow | RadioClip | Container
        ] = await self.client.content.get_collection(pid=pid)
        if collection:
            rendered = [await self._render_browse_item(item) for item in collection]
            return [obj for obj in rendered if obj is not None]
        return []

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

    async def _live_programme_stream_details(self, programme_id: LiveProgrammeId) -> StreamDetails:
        """Get stream details for a programme on air now, played from the live rewind window."""
        if datetime.utc_timestamp() >= programme_id.end:
            # once the programme is over its on-demand version is usually available
            try:
                return await self._catch_up_stream_details(
                    programme_id.pid, MediaType.PODCAST_EPISODE
                )
            except MusicAssistantError, exceptions.NotFoundError:
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
            provider=self.instance_id,
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
            station = await self.get_radio(programme_id.station_id)
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
        self.mass.create_task(self.mass.player_queues.seek(queue.queue_id, position))

    def _queue_playing(self, streamdetails: StreamDetails) -> PlayerQueue | None:
        """Return the queue whose current item is being streamed with these stream details."""
        if not streamdetails.queue_id:
            return None
        queue = self.mass.player_queues.get(streamdetails.queue_id)
        if queue is None or queue.current_item is None:
            return None
        return queue if queue.current_item.streamdetails is streamdetails else None
