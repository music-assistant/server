"""
BBC Sounds music provider support for MusicAssistant.

TODO implement seeking of live stream
"""

import asyncio
import copy
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Any, Literal, TypeVar, cast, get_args, get_origin

from music_assistant_models.config_entries import (
    ConfigEntry,
    ConfigValueOption,
    ConfigValueType,
    ProviderConfig,
)
from music_assistant_models.enums import ConfigEntryType, ImageType, MediaType, ProviderFeature
from music_assistant_models.errors import MusicAssistantError
from music_assistant_models.media_items import (
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
from sounds import (
    Container,
    LiveStation,
    Menu,
    MenuRecommendationOptions,
    PlayStatus,
    RadioShow,
    Segment,
    SoundsClient,
    exceptions,
)
from sounds import PodcastEpisode as SoundsPodcastEpisode
from sounds.models import Playlist

from music_assistant.constants import CONF_ENTRY_UNOFFICIAL_PROVIDER, CONF_PASSWORD, CONF_USERNAME
from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.datetime import LOCAL_TIMEZONE
from music_assistant.mass import MusicAssistant
from music_assistant.models import ProviderInstanceType
from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers.bbc_sounds.adaptor import Adaptor
from music_assistant.providers.bbc_sounds.constants import _Constants
from music_assistant.providers.bbc_sounds.metadata import _find_segment, _segment_to_metadata

if TYPE_CHECKING:
    from music_assistant_models.provider import ProviderManifest
    from sounds.models import SoundsTypes

SUPPORTED_FEATURES = {
    ProviderFeature.BROWSE,
    ProviderFeature.RECOMMENDATIONS,
    ProviderFeature.SEARCH,
}

type _StreamTypes = Literal["hls", "dash"]
T = TypeVar("T")


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Create new provider instance."""
    instance = BBCSoundsProvider(mass, manifest, config, SUPPORTED_FEATURES)
    await instance.handle_async_init()
    return instance


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    instance_id: id of an existing provider instance (None if new instance setup).
    action: [optional] action key called from config entries UI.
    values: the (intermediate) raw values for config entries sent with the action.
    """
    # ruff: noqa: ARG001

    return (
        CONF_ENTRY_UNOFFICIAL_PROVIDER,
        ConfigEntry(
            key=_Constants.CONF_INTRO,
            type=ConfigEntryType.LABEL,
        ),
        ConfigEntry(
            key=CONF_USERNAME,
            type=ConfigEntryType.STRING,
            required=False,
        ),
        ConfigEntry(
            key=CONF_PASSWORD,
            type=ConfigEntryType.SECURE_STRING,
            required=False,
        ),
        ConfigEntry(
            key=_Constants.CONF_SHOW_LOCAL,
            advanced=True,
            type=ConfigEntryType.BOOLEAN,
            default_value=False,
        ),
        ConfigEntry(
            key=_Constants.CONF_STREAM_FORMAT,
            advanced=True,
            type=ConfigEntryType.STRING,
            options=[
                ConfigValueOption(_Constants.CONF_STREAM_FORMAT_HLS),
                ConfigValueOption(_Constants.CONF_STREAM_FORMAT_DASH),
            ],
            default_value=_Constants.CONF_STREAM_FORMAT_HLS,
        ),
    )


class BBCSoundsProvider(MusicProvider):
    """A MusicProvider class to interact with the BBC Sounds API via auntie-sounds."""

    client: SoundsClient
    menu: Menu | None = None
    current_task: asyncio.Task[None] | None = None
    # A weak proxy variable so we can track if we should be showing the full menu or not
    logged_in: bool = False

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        # If we have an account, authenticate. Testing shows all features work without auth
        # but BBC will be disabling BBC Sounds from outside the UK at some point
        username = self.config.get_value(CONF_USERNAME)
        password = self.config.get_value(CONF_PASSWORD)
        if username and password:
            self.client = SoundsClient(
                session=self.mass.http_session,
                logger=self.logger,
                timezone=LOCAL_TIMEZONE,
                username=str(username),
                password=str(password),
            )
        else:
            self.client = SoundsClient(
                session=self.mass.http_session,
                logger=self.logger,
                timezone=LOCAL_TIMEZONE,
            )
            # This seems odd, but e.g. clearing the username and password will result in a stuck session
            await self.client.logout()
        self.show_local_stations: bool = bool(
            self.config.get_value(_Constants.CONF_SHOW_LOCAL, False)
        )
        self.stream_format: _StreamTypes = (
            _Constants.DASH
            if self.config.get_value(_Constants.CONF_STREAM_FORMAT) == _Constants.DASH
            else _Constants.HLS
        )
        self.adaptor = Adaptor(self)
        # Two simple internal caches to reduce API calls
        self._stream_details_cache: dict[str, tuple[StreamDetails, float]] = {}

    async def loaded_in_mass(self) -> None:
        """Do post-loaded actions."""
        if not self.menu or (
            isinstance(self.menu, Menu) and self.menu.sub_items and len(self.menu.sub_items) == 0
        ):
            await self._fetch_menu()

    @property
    def is_streaming_provider(self) -> bool:
        """Return True as the provider is a streaming provider."""
        return True

    def _get_provider_mapping(self, item_id: str) -> ProviderMapping:
        return ProviderMapping(
            item_id=item_id,
            provider_domain=self.domain,
            provider_instance=self.instance_id,
        )

    def _stream_error(self, item_id: str, media_type: MediaType) -> MusicAssistantError:
        return MusicAssistantError(f"Couldn't get stream details for {item_id} ({media_type})")

    async def _fetch_menu(self) -> None:
        self.logger.debug("No cached menu, fetching from API")
        self.menu = await self.client.get_menu(recommendations=MenuRecommendationOptions.EXCLUDE)

    @use_cache(expiration=_Constants.DEFAULT_EXPIRATION)
    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        episode_info = await self.client.streaming.get_by_pid(
            pid=prov_track_id, stream_format=self.stream_format
        )
        track = await self.adaptor.new_object(episode_info, force_type=Track)
        if not isinstance(track, Track):
            raise MusicAssistantError(f"Incorrect track returned for {prov_track_id}")
        return track

    @use_cache(expiration=_Constants.DEFAULT_EXPIRATION)
    async def get_podcast_episode(self, prov_episode_id: str) -> PodcastEpisode:
        # If we are requesting a previously-aired radio show, we lose access to the
        # schedule time. The best we can find out from the API is original release
        # date, so the stream title loses access to the air date
        """Get full podcast episode details by id."""
        self.logger.debug(f"Getting podcast episode for {prov_episode_id}")
        episode = await self.client.streaming.get_podcast_episode(prov_episode_id)
        ma_episode = await self.adaptor.new_object(episode, force_type=PodcastEpisode)
        if not ma_episode:
            raise MusicAssistantError(f"Podcast episode {prov_episode_id} not found")
        if not isinstance(ma_episode, PodcastEpisode):
            raise MusicAssistantError(f"Incorrect format for podcast episode {prov_episode_id}")
        ma_episode.name = (
            episode.network.short_title
            if episode.network and episode.network.short_title
            else "Unknown"
        )
        return ma_episode

    @use_cache(expiration=_Constants.DEFAULT_EXPIRATION)
    async def get_podcast(self, prov_podcast_id: str) -> Podcast:
        """Get full podcast details by id."""
        self.logger.debug(f"Getting podcast for {prov_podcast_id}")
        podcast = await self.client.streaming.get_podcast(pid=prov_podcast_id)
        ma_podcast = await self.adaptor.new_object(source_obj=podcast, force_type=Podcast)

        if isinstance(ma_podcast, Podcast):
            return ma_podcast
        raise MusicAssistantError("Incorrect format for podcast")

    async def get_podcast_episodes(
        self,
        prov_podcast_id: str,
    ) -> AsyncGenerator[PodcastEpisode]:
        """Get all PodcastEpisodes for given podcast id."""
        podcast_episodes = await self.client.streaming.get_podcast_episodes(prov_podcast_id)

        if podcast_episodes:
            for episode in podcast_episodes:
                this_episode = await self.adaptor.new_object(
                    source_obj=episode, force_type=PodcastEpisode
                )
                if this_episode and isinstance(this_episode, PodcastEpisode):
                    yield this_episode

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """Get full radio details by id."""
        self.logger.debug(f"Getting radio for {prov_radio_id}")
        station = await self.client.stations.get_station(prov_radio_id, include_stream=True)
        if station:
            ma_radio = await self.adaptor.new_object(station, force_type=Radio)
            if ma_radio and isinstance(ma_radio, Radio):
                return ma_radio
        else:
            raise MusicAssistantError(f"No station found: {prov_radio_id}")

        self.logger.debug(f"{station} {ma_radio} {type(ma_radio)}")
        raise MusicAssistantError("No valid radio stream found")

    @use_cache(expiration=_Constants.DEFAULT_EXPIRATION)
    async def _get_programme(self, pid: str) -> dict[str, Any] | None:
        """Fetch and normalise a programme from the API."""
        episode = await self.client.streaming.get_by_pid(
            pid,
            # include_stream=False,
            stream_format=self.stream_format,
        )

        if not episode:
            return None

        # Normalize object to dict to avoid cache serialization issues
        return {
            "id": episode.id,
            "titles": getattr(episode, "titles", None),
            "image_url": getattr(episode, "image_url", None),
        }

    async def _catch_up_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get stream details for catch-up content."""
        cached, expiry = self._stream_details_cache.get(item_id, (None, 0.0))

        if cached is not None and time.time() < expiry:
            self.logger.debug(f"Cache hit for {item_id}, returning")
            return copy.copy(cached)

        programme = await self._get_programme(item_id)

        if not programme:
            raise self._stream_error(item_id, media_type)

        episode = await self.client.streaming.get_by_pid(
            item_id,
            include_stream=True,
            stream_format=self.stream_format,
        )

        stream_details = await self.adaptor.new_streamable_object(episode)

        if not stream_details:
            raise self._stream_error(item_id, media_type)

        stream_details.data = {"vpid": programme["id"], "pid": episode.pid}
        stream_details.stream_metadata_update_callback = self._update_on_demand_stream_metadata
        stream_details.stream_metadata_update_interval = _Constants.NOW_PLAYING_REFRESH_TIME
        if item_id not in self._stream_details_cache:
            self.logger.debug(f"Cache miss for {item_id}, adding")
            self._stream_details_cache[item_id] = (
                copy.copy(stream_details),
                time.time() + _Constants.SHORT_EXPIRATION,
            )
        return copy.copy(stream_details)

    async def _get_station_stream_details(self, item_id: str) -> StreamDetails:
        """Fetch stream details for a live station."""
        station = await self.client.stations.get_station(
            item_id,
            include_stream=True,
            stream_format=self.stream_format,
        )

        if not station:
            raise MusicAssistantError(f"Couldn't get stream details for station {item_id}")

        if not station.stream:
            raise MusicAssistantError(f"No stream found for {item_id}")

        stream_details = await self.adaptor.new_streamable_object(station)

        if not stream_details:
            raise self._stream_error(item_id, MediaType.RADIO)

        stream_details.stream_metadata_update_callback = self._update_live_stream_metadata
        stream_details.stream_metadata_update_interval = _Constants.NOW_PLAYING_REFRESH_TIME

        return stream_details

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get streamdetails for a track/radio."""
        self.logger.debug(f"Getting stream details for {item_id} ({media_type})")
        if media_type in [MediaType.PODCAST_EPISODE, MediaType.TRACK]:
            return await self._catch_up_stream_details(item_id, media_type)
        return await self._get_station_stream_details(item_id)

    async def _get_programme_segments(self, vpid: str) -> list[Segment] | None:
        """Get on demand segments from cache or API."""
        cache_key = f"programme_segments_{vpid}"

        async def get_segments() -> list[Segment] | None:
            segments = await self.client.streaming.get_show_segments(
                vpid, fetch_missing_images=True
            )

            if isinstance(segments, list) and isinstance(segments[0], Segment):
                return segments
            self.logger.warning(f"No show segments found for vpid: {vpid}")
            return None

        return await self._get_cached_item(
            key=cache_key, fetcher=get_segments, expected_type=list[Segment]
        )

    async def _update_on_demand_stream_metadata(
        self, stream_details: StreamDetails, elapsed_time: int
    ) -> None:
        """
        Get the currently playing segment (song) for on-demand episodes.

        Called by the callback function in StreamDetails.
        """
        self.logger.debug("Updating on-demand stream metadata")

        if not stream_details or not stream_details.stream_metadata:
            return

        vpid = stream_details.data.get("vpid")
        if not vpid:
            self.logger.warning("No VPID found")
            return

        segments = await self._get_programme_segments(vpid)
        if not segments:
            return

        segment = _find_segment(segments, elapsed_time)

        if segment:
            stream_details.stream_metadata = _segment_to_metadata(segment)
            # As of June 2026, the API currently doesn't return images from this endpoint
            # We fill in missing images with the Spotify API, if any are still blank
            # then use the MA helpers here
            if not stream_details.stream_metadata.image_url:
                try:
                    async with asyncio.timeout(_Constants.ARTWORK_TIMEOUT):
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

    async def _update_live_stream_metadata(
        self, stream_details: StreamDetails, elapsed_time: int
    ) -> None:
        """Get the currently playing song for live radio streams."""
        self.logger.debug("Updating live stream metadata")
        if not stream_details or not stream_details.stream_metadata:
            return

        station_id = stream_details.item_id
        if not station_id:
            return

        now_playing = await self.client.schedules.currently_playing_song(station_id)
        if now_playing:
            self.logger.debug(f"Now playing for {station_id}: {now_playing}")
            stream_details.stream_metadata = _segment_to_metadata(now_playing)
        else:
            self.logger.debug(f"No song playing on {station_id}, fetching station info")
            station = await self.client.stations.get_station(station_id)
            if station:
                stream_details.stream_metadata = await self._station_programme_display(
                    station=station
                )

    @use_cache(expiration=_Constants.DEFAULT_EXPIRATION)
    async def _vod_programme_display(self, pid: str) -> StreamMetadata | None:
        episode = await self.client.streaming.get_by_pid(pid=pid, stream_format=self.stream_format)

        if isinstance(episode, (SoundsPodcastEpisode, RadioShow)) and episode.titles:
            return StreamMetadata(title=episode.titles.get("secondary", ""))

        return None

    @use_cache(expiration=_Constants.DEFAULT_EXPIRATION)
    async def _station_programme_display(self, station: LiveStation) -> StreamMetadata | None:
        if station and station.titles:
            title = f"{station.titles.get('secondary')} • {station.titles.get('primary')}"
            return StreamMetadata(title=title, artist=None, image_url=station.image_url)
        return None

    async def _station_list(self, include_local: bool = False) -> list[Radio]:
        """Get list of stations as Radios."""
        radio_list: list[Radio] = []
        for station in await self.client.stations.get_stations(include_local=include_local):
            if station and station.item_id:
                station_info = await self._station_programme_display(station=station)
                description = station_info.title if station_info else None
                radio_list.append(
                    Radio(
                        item_id=station.item_id,
                        name=(
                            station.network.short_title
                            if station.network and station.network.short_title
                            else "Unknown station"
                        ),
                        provider=self.domain,
                        metadata=MediaItemMetadata(
                            description=description,
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
                                if station.network and station.network.logo_url
                                else None
                            ),
                        ),
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

    async def _get_menu(
        self, path_parts: list[str] | None = None
    ) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        if not self.menu:
            await self._fetch_menu()
        if not self.menu or not self.menu.sub_items:
            raise MusicAssistantError("Menu API response is empty or invalid")
        menu_items = []
        for item in self.menu.sub_items:
            new_item = await self._render_browse_item(item, path_parts)
            if isinstance(new_item, (MediaItemType | ItemMapping | BrowseFolder)):
                menu_items.append(new_item)

        # The Sounds default menu doesn't include listings as they are linked elsewhere
        menu_items.insert(
            1,
            BrowseFolder(
                item_id="stations",
                provider=self.domain,
                name="Schedule and Programmes",
                translation_key="provider.bbc_sounds.schedule_programmes",
                path=f"{self.domain}://stations",
                image=MediaItemImage(
                    path="https://cdn.jsdelivr.net/gh/kieranhogg/auntie-sounds@main/src/sounds/icons/solid/latest.png",
                    remotely_accessible=True,
                    provider=self.domain,
                    type=ImageType.THUMB,
                ),
            ),
        )
        return menu_items

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

    async def _get_subpath_menu(
        self, path_parts: list[str]
    ) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        item_list: list[MediaItemType | ItemMapping | BrowseFolder] = []
        if not self.menu:
            return item_list
        sub_menu = self.menu.get(path_parts[0])

        if isinstance(sub_menu, Container):
            for part in path_parts[1:]:
                sub_menu = sub_menu.get(part)
                if sub_menu is None:
                    break
            else:
                if sub_menu.sub_items is not None:
                    for item in sub_menu.sub_items:
                        if new_item := await self._render_browse_item(
                            item, path_parts=[f"{self.domain}:/", *path_parts]
                        ):
                            item_list.append(new_item)
                # TODO: probably need a better way of handling this
                elif isinstance(sub_menu, Playlist):

                    async def get_playlist_items(pid: str):
                        return await self.client.streaming.get_playlist_contents(pid=pid)

                    playlist_items = await get_playlist_items(sub_menu.item_id)
                    if playlist_items:
                        item_list = [
                            await self._render_browse_item(playlist_item)
                            for playlist_item in playlist_items
                            if playlist_item is not None
                        ]

        else:
            self.logger.warning(f"Sub menu not a container: {sub_menu}")
        return item_list

    async def _get_station_schedule_menu(
        self,
        station_id: str,
        path_parts: list[str],
        date: str,
    ) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """Lookup a date schedule for a station."""
        # TODO: look at this against 2.0 API
        self.logger.debug(f"Getting schedule for {station_id} for {date}")
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

    async def _get_category(
        self, category_name: str
    ) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        category = await self.client.streaming.get_category(category=category_name)

        if category is not None and category.sub_items:
            return [
                obj
                for obj in [await self._render_browse_item(item) for item in category.sub_items]
                if obj is not None
            ]
        return []

    async def _get_collection(
        self, pid: str
    ) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        collection = await self.client.streaming.get_collection(pid=pid)
        if collection and collection.sub_items:
            return [
                obj
                for obj in [
                    await self._render_browse_item(item) for item in collection.sub_items if item
                ]
                if obj
            ]
        return []

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Browse this provider's items.

        :param path: The path to browse, (e.g. provider_id://artists).
        """
        self.logger.debug(f"Browsing path: {path}")
        if not path.startswith(f"{self.domain}://"):
            raise MusicAssistantError(f"Invalid path for {self.domain} provider: {path}")
        path_parts = path.split("://", 1)[1].split("/")
        self.logger.debug(f"Path parts: {path_parts}")

        sub_path = path_parts[0] if path_parts else ""
        sub_sub_path = path_parts[1] if len(path_parts) > 1 else ""
        sub_sub_sub_path = path_parts[2] if len(path_parts) > 2 else ""
        path_parts = [
            f"{self.domain}:/",
            *[part for part in path_parts if len(part) > 0],
        ]

        # A large part of the menu content is pre-loaded into self.menu
        # These are the exceptions, so get the extra content
        if sub_path == "":
            return await self._get_menu()
        # Categories and collections aren't in the API menus
        if sub_path == "categories" and sub_sub_path:
            return await self._get_category(sub_sub_path)
        if sub_path == "collections" and sub_sub_path:
            return await self._get_collection(sub_sub_path)
        # The main menu fetch returns up to the schedule date folders, but no contents
        # so as not to show out of date information
        if sub_path == "stations" and sub_sub_path and sub_sub_sub_path:
            station_menu = await self._get_station_schedule_menu(
                path_parts=path_parts,
                station_id=sub_sub_path,
                date=sub_sub_sub_path,
            )
            return station_menu
        # If no special cases, pass the rest of the path to iterate through
        return await self._get_subpath_menu(path_parts[1:])

    async def search(
        self, search_query: str, media_types: list[MediaType] | None, limit: int = 5
    ) -> SearchResults:
        """Perform search for BBC Sounds stations."""
        results = SearchResults()
        search_result = await self.client.streaming.search(search_query)
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

    @use_cache(expiration=_Constants.SHORT_EXPIRATION)
    async def recommendations(self) -> list[RecommendationFolder]:
        """Get available recommendations."""
        folders = []

        if self.logged_in:
            recommendations = await self.client.personal.get_uk_menu(
                recommendations=MenuRecommendationOptions.ONLY
            )
            self.logger.debug("Getting recommendations from API")
            if recommendations.sub_items:
                for recommendation in recommendations.sub_items:
                    # recommendation is a RecommendedMenuItem
                    folder = await self.adaptor.new_object(
                        recommendation, force_type=RecommendationFolder
                    )
                    if isinstance(folder, RecommendationFolder):
                        folders.append(folder)
            return folders
        return []

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
        if self.logged_in:
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
                        success = await self.client.streaming.update_play_status(
                            pid=media_item.item_id, elapsed_time=position, action=action
                        )
                        self.logger.debug(f"Updated play status: {success}")
                    except exceptions.APIResponseError as err:
                        self.logger.error(f"Error updating play status: {err}")
