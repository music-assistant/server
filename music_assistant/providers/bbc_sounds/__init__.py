"""
BBC Sounds music provider support for MusicAssistant.

TODO implement seeking of live stream
TODO watch for settings change
TODO add podcast menu to non-UK menu
FIXME skipping in non-live radio shows restarts the stream but keeps the seek time
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from datetime import timedelta
from typing import TYPE_CHECKING, cast

from music_assistant_models.config_entries import (
    ConfigEntry,
    ConfigValueOption,
    ConfigValueType,
    ProviderConfig,
)
from music_assistant_models.enums import ConfigEntryType, ImageType, MediaType, ProviderFeature
from music_assistant_models.errors import LoginFailed, MusicAssistantError
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
from music_assistant_models.unique_list import UniqueList

import music_assistant.helpers.datetime as dt
from music_assistant.constants import CONF_PASSWORD, CONF_USERNAME
from music_assistant.helpers.datetime import LOCAL_TIMEZONE
from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers.bbc_sounds.adaptor import Adaptor

if TYPE_CHECKING:
    from collections.abc import Sequence

    from music_assistant_models.provider import ProviderManifest
    from music_assistant_models.streamdetails import StreamDetails
    from sounds.models import ScheduleItem, SoundsTypes

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

from sounds import (
    Container,
    LiveStation,
    Menu,
    MenuRecommendationOptions,
    PlayStatus,
    SoundsClient,
    exceptions,
)

SUPPORTED_FEATURES = {
    ProviderFeature.BROWSE,
    ProviderFeature.LIBRARY_PODCASTS,
    ProviderFeature.LIBRARY_RADIOS,
    ProviderFeature.LIBRARY_TRACKS,
    ProviderFeature.RECOMMENDATIONS,
    ProviderFeature.SEARCH,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Create new provider instance."""
    instance = BBCSoundsProvider(mass, manifest, config)
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
        ConfigEntry(
            key=_Constants.CONF_ENABLE_UK_CONTENT,
            type=ConfigEntryType.BOOLEAN,
            label="Enable U.K. Sounds content (beta)",
            default_value=False,
            description="Enabling this setting unlocks the full content catalog if you are a U.K."
            "listener. As the API returns a wide range of media items under the same type, this "
            "is turned off by default until it more widely tested for stability.",
        ),
        ConfigEntry(
            key=_Constants.CONF_INTRO,
            type=ConfigEntryType.LABEL,
            label="A BBC Sounds account is optional, but some streams may not work or be served "
            "in reduced quality if outside the U.K.",
            depends_on=_Constants.CONF_ENABLE_UK_CONTENT,
            depends_on_value=True,
            # hidden=True,
        ),
        ConfigEntry(
            key=CONF_USERNAME,
            type=ConfigEntryType.STRING,
            label="Email or username",
            required=False,
            depends_on=_Constants.CONF_ENABLE_UK_CONTENT,
            depends_on_value=True,
            # hidden=True,
        ),
        ConfigEntry(
            key=CONF_PASSWORD,
            type=ConfigEntryType.SECURE_STRING,
            label="Password",
            required=False,
            depends_on=_Constants.CONF_ENABLE_UK_CONTENT,
            depends_on_value=True,
            # hidden=True,
        ),
        ConfigEntry(
            key=_Constants.CONF_SHOW_LOCAL,
            category="advanced",
            type=ConfigEntryType.BOOLEAN,
            label="Show local radio stations?",
            default_value=False,
            depends_on=_Constants.CONF_ENABLE_UK_CONTENT,
            depends_on_value=True,
            # hidden=True,
        ),
        ConfigEntry(
            key=_Constants.CONF_NOW_PLAYING,
            category="advanced",
            type=ConfigEntryType.BOOLEAN,
            label="Show 'now playing' details?",
            description=(
                "Show details of the currently playing track instead of the station details"
            ),
            default_value=True,
        ),
        ConfigEntry(
            key=_Constants.CONF_UPDATE_INTERVAL,
            category="advanced",
            type=ConfigEntryType.INTEGER,
            label="Player update interval (seconds)",
            description="How often to check for now playing updates",
            default_value=5,
            range=(1, 60),
            depends_on="now_playing",
            depends_on_value=True,
        ),
        ConfigEntry(
            key=_Constants.CONF_RECOMMENDATIONS,
            category="advanced",
            label="Show recommendations?",
            description="BBC Sounds has several recommendation categories, configure if "
            "and where these are shown",
            type=ConfigEntryType.STRING,
            options=[
                ConfigValueOption("Show recommendations on the home page", "homepage"),
                ConfigValueOption("Show recommendations in folders in the browse page", "browse"),
                ConfigValueOption("Disable recommendations", "disable"),
            ],
            default_value="homepage",
            required=True,
            depends_on=_Constants.CONF_ENABLE_UK_CONTENT,
            depends_on_value=True,
            # hidden=True,
        ),
    )


class _Constants:
    # This is the image id that is shown when there's no track image
    BLANK_IMAGE_NAME = "p0bqcdzf"

    CONF_UPDATE_INTERVAL = "update_interval"
    CONF_NOW_PLAYING = "now_playing"
    CONF_SHOW_LOCAL = "show_local"
    CONF_INTRO = "intro"
    CONF_RECOMMENDATIONS = "recommendations"
    CONF_ENABLE_UK_CONTENT = "uk_content"


class BBCSoundsProvider(MusicProvider):
    """A MusicProvider class to interact with the BBC Sounds API via auntie-sounds."""

    client: SoundsClient
    menu: Menu | None = None

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self.client = SoundsClient(
            session=self.mass.http_session,
            logger=self.logger,
            timezone=LOCAL_TIMEZONE,
            debug_login=True,
        )

        self.adaptor = Adaptor(self)
        self.use_now_playing = self.config.get_value(_Constants.CONF_NOW_PLAYING)
        self.now_playing_poll_time = self.config.get_value(_Constants.CONF_UPDATE_INTERVAL)
        self.show_local_stations = self.config.get_value(_Constants.CONF_SHOW_LOCAL)
        self.recommendation_location = self.config.get_value(_Constants.CONF_RECOMMENDATIONS)

        # If we have an account, authenticate. Testing shows all features work without auth
        # but BBC will be disabling BBC Sounds from outside the UK at some point
        if self.config.get_value(CONF_USERNAME) and self.config.get_value(CONF_PASSWORD):
            if self.client.auth.is_logged_in:
                # Check if we need to reauth
                try:
                    await self.client.personal.get_experience_menu()
                    return
                except (exceptions.UnauthorisedError, exceptions.APIResponseError):
                    await self.client.auth.renew_session()

            try:
                await self.client.auth.authenticate(
                    username=str(self.config.get_value(CONF_USERNAME)),
                    password=str(self.config.get_value(CONF_PASSWORD)),
                )
            except exceptions.LoginFailedError as e:
                raise LoginFailed(e)

    def _get_provider_mapping(self, item_id: str) -> ProviderMapping:
        return ProviderMapping(
            item_id=item_id,
            provider_domain=self.domain,
            provider_instance=self.instance_id,
        )

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return the features supported by this Provider."""
        return SUPPORTED_FEATURES

    @property
    def is_streaming_provider(self) -> bool:
        """
        Return True if the provider is a streaming provider.

        This literally means that the catalog is not the same as the library contents.
        For local based providers (files, plex), the catalog is the same as the library content.
        It also means that data is if this provider is NOT a streaming provider,
        data cross instances is unique, the catalog and library differs per instance.

        Setting this to True will only query one instance of the provider for 75 and lookups.
        Setting this to False will query all instances of this provider for search and lookups.
        """
        return False

    async def _get_episode_info(
        self, station_id: str, episode_id: str, date: str
    ) -> ScheduleItem | None:
        station = await self.client.stations.get_station(
            station_id=station_id, include_schedule=True, date=date
        )
        if station and station.schedule and station.schedule.sub_items:
            for item in station.schedule.sub_items:
                if item.episode_id == episode_id:
                    return item
        return None

    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id.

        Only called if provider supports ProviderFeature.LIBRARY_TRACKS.
        """
        episode_info = await self.client.streaming.get_by_pid(prov_track_id)
        track = await self.adaptor.new_object(episode_info, force_type=Track)
        if not isinstance(track, Track):
            raise MusicAssistantError(f"Incorrect track returned for {prov_track_id}")
        return track

    async def get_podcast_episode(self, prov_episode_id: str) -> PodcastEpisode:
        # If we are requesting a previously-aired radio show, we lose access to the
        # schedule time. The best we can find out from the API is original release
        # date, so the stream title loses access to the air date
        """Get full podcast epsisode details by id."""
        self.logger.debug(f"Getting podcast episode for {prov_episode_id}")
        episode = await self.client.streaming.get_podcast_episode(prov_episode_id)
        ma_episode = await self.adaptor.new_object(episode, force_type=PodcastEpisode)
        if not isinstance(ma_episode, PodcastEpisode):
            raise MusicAssistantError(f"Incorrect format for podcast episode {prov_episode_id}")
        return ma_episode

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get streamdetails for a track/radio."""
        self.logger.debug(f"Getting stream details for {item_id} ({media_type})")
        if media_type == MediaType.PODCAST_EPISODE:
            episode_info = await self.client.streaming.get_by_pid(item_id, include_stream=True)
            stream_details = await self.adaptor.new_streamable_object(episode_info)
            if not stream_details:
                raise MusicAssistantError(
                    f"Couldn't get stream details for {item_id} ({media_type})"
                )

            if self.use_now_playing and episode_info:
                # .id is the VPID
                self.mass.create_task(self._check_for_segments(item_id, stream_details))
            return stream_details
        elif media_type is MediaType.TRACK:
            track = await self.client.streaming.get_by_pid(item_id, include_stream=True)
            stream_details = await self.adaptor.new_streamable_object(track)
            if not stream_details:
                raise MusicAssistantError(
                    "Couldn't get stream details for {item_id} ({media_type})"
                )
            if self.use_now_playing:
                self.mass.create_task(self._check_for_segments(item_id, stream_details))
            return stream_details
        else:
            self.logger.debug(f"Getting stream details for station {item_id}")
            station = await self.client.stations.get_station(item_id, include_stream=True)
            if not station:
                raise MusicAssistantError("Couldn't get stream details for station {item_id}")

            self.logger.debug(f"Found station: {station}")
            if not station or not station.stream:
                raise MusicAssistantError(f"No stream found for {item_id}")

            stream_details = await self.adaptor.new_streamable_object(station)

            if not stream_details:
                raise MusicAssistantError(
                    "Couldn't get stream details for {item_id} ({media_type})"
                )
            if stream_details.path and "norewind" in stream_details.path:
                # Replace with skippable stream for future use
                if isinstance(stream_details.path, str):
                    stream_details.path = stream_details.path.replace(".norewind", "")
            # Start a background task to keep these details updated
            if self.use_now_playing:
                self.mass.create_task(self._watch_stream_details(stream_details))
            return stream_details

    async def _check_for_segments(self, vpid: str, stream_details: StreamDetails) -> None:
        # seeking past the current segment needs fixing
        segments = await self.client.streaming.get_show_segments(vpid)
        offset = stream_details.seek_position + (stream_details.seconds_streamed or 0)
        if segments:
            seconds = 0 + offset
            segments_iter = iter(segments)
            segment = next(segments_iter)
            if seconds > 0:
                # Skip to the correct segment
                prev = None
                while seconds > segment.offset["start"]:
                    self.logger.info("Advancing to next segment")
                    prev = segment
                    segment = next(segments_iter)
                self.logger.warning("Starting with first segment")
                if prev and seconds > prev.offset["start"] and seconds < prev.offset["end"]:
                    if stream_details.stream_metadata:
                        stream_details.stream_metadata.artist = prev.titles["primary"]
                        stream_details.stream_metadata.title = prev.titles["secondary"]
                        if prev.image_url:
                            stream_details.stream_metadata.image_url = prev.image_url
            while True:
                if seconds == segment.offset["start"] and stream_details.stream_metadata:
                    self.logger.warning("Updating segment")
                    stream_details.stream_metadata.artist = segment.titles["primary"]
                    stream_details.stream_metadata.title = segment.titles["secondary"]
                    if segment.image_url:
                        stream_details.stream_metadata.image_url = segment.image_url
                    segment = next(segments_iter)
                await asyncio.sleep(1)
                seconds += 1
        else:
            self.logger.warning("No segments found")

    async def _watch_stream_details(self, stream_details: StreamDetails) -> None:
        station_id = stream_details.data["station"]

        # this didn't work
        # while not stream_details.seconds_streamed:
        while True:
            now_playing = await self.client.schedules.currently_playing_song(
                station_id, image_size=1280
            )
            if now_playing and stream_details.stream_metadata:
                self.logger.debug(f"Now playing for {station_id}: {now_playing}")

                # removed check temporarily as images not working
                # if self.BLANK_IMAGE_NAME not in now_playing.image_url:
                image = now_playing.image_url
                stream_details.stream_metadata.image_url = image
                song = now_playing.titles["secondary"]
                artist = now_playing.titles["primary"]
                stream_details.stream_metadata.title = song
                stream_details.stream_metadata.artist = artist
            elif stream_details.stream_metadata:
                station = await self.client.stations.get_station(station_id=station_id)
                if station:
                    self.logger.debug(f"Station details: {station}")
                    display = self._station_programme_display(station)
                    if display:
                        stream_details.stream_metadata.title = display
                        stream_details.stream_metadata.artist = None
                        stream_details.stream_metadata.image_url = station.image_url
            await asyncio.sleep(cast("int", self.now_playing_poll_time))

    def _station_programme_display(self, station: LiveStation) -> str | None:
        if station and station.titles:
            return f"{station.titles.get('secondary')} • {station.titles.get('primary')}"
        return None

    async def _station_list(self, include_local: bool = False) -> list[Radio]:
        return [
            Radio(
                item_id=station.id,
                name=(
                    station.network.short_title
                    if station.network and station.network.short_title
                    else "Unknown station"
                ),
                provider=self.domain,
                metadata=MediaItemMetadata(
                    description=self._station_programme_display(station=station),
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
                        item_id=station.id,
                        provider_domain=self.domain,
                        provider_instance=self.instance_id,
                    )
                },
            )
            for station in await self.client.stations.get_stations(include_local=include_local)
            if station
        ]

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
        else:
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
        else:
            return []

    async def _get_menu(
        self, path_parts: list[str] | None = None
    ) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        if (
            self.config.get_value(_Constants.CONF_ENABLE_UK_CONTENT)
            and self.client.auth.is_logged_in
            and await self.client.auth.is_uk_listener
        ):
            return await self._get_full_menu(path_parts=path_parts)
        else:
            return await self._get_slim_menu(path_parts=path_parts)

    async def _get_full_menu(
        self, path_parts: list[str] | None = None
    ) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
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
                path=f"{self.domain}://stations",
                image=MediaItemImage(
                    path="https://cdn.jsdelivr.net/gh/kieranhogg/auntie-sounds@main/src/sounds/icons/dark/listen_live.png",
                    remotely_accessible=True,
                    provider=self.domain,
                    type=ImageType.THUMB,
                ),
            ),
        )
        return menu_items

    async def _get_slim_menu(
        self, path_parts: list[str] | None
    ) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        return [
            BrowseFolder(
                item_id="listen_live",
                provider=self.domain,
                name="Listen Live",
                path=f"{self.domain}://listen_live",
            ),
            BrowseFolder(
                item_id="stations",
                provider=self.domain,
                name="Schedules and Programmes",
                path=f"{self.domain}://stations",
            ),
        ]

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
        else:
            return None

    async def _fetch_menu(self) -> None:
        self.logger.debug("No cached menu, fetching from API")

        # Include recommendation folders in the menu if set in settings
        recommendations = MenuRecommendationOptions.EXCLUDE
        if self.recommendation_location == "browse":
            recommendations = MenuRecommendationOptions.INCLUDE

        self.menu = await self.client.personal.get_experience_menu(recommendations=recommendations)

    async def _get_subpath_menu(
        self, sub_path: str
    ) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        if not self.menu:
            return []
        sub_menu = self.menu.get(sub_path)
        item_list = []

        if sub_menu and isinstance(sub_menu, Container):
            if sub_menu.sub_items:
                # We have some sub-items, so let's show those
                for item in sub_menu.sub_items:
                    new_item = await self._render_browse_item(item)
                    if new_item:
                        item_list.append(new_item)
            else:
                new_item = await self._render_browse_item(sub_menu)
                if new_item:
                    item_list.append(new_item)

        if sub_path == "listen_live":
            for item in await self.client.stations.get_stations():
                new_item = await self._render_browse_item(item)
                if new_item:
                    item_list.append(new_item)
            # Check if we need to append local stations
            if self.show_local_stations:
                for item in await self.client.stations.get_local_stations():
                    new_item = await self._render_browse_item(item)
                    if new_item is not None:
                        item_list.append(new_item)
        return item_list

    async def _get_station_schedule_menu(
        self,
        show_local: bool,
        path_parts: list[str],
        sub_sub_path: str,
        sub_sub_sub_path: str,
    ) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        if sub_sub_sub_path:
            # Lookup a date schedule
            self.logger.debug(
                await self.client.schedules.get_schedule(
                    station_id=sub_sub_path,
                    date=sub_sub_sub_path,
                )
            )
            schedule = await self.client.schedules.get_schedule(
                station_id=sub_sub_path,
                date=sub_sub_sub_path,
            )
            self.logger.debug(schedule)
            items = []
            if schedule and schedule.sub_items:
                for folder in schedule.sub_items:
                    new_folder = await self._render_browse_item(folder, path_parts=path_parts)
                    if new_folder:
                        items.append(new_folder)
            return items
        elif sub_sub_path:
            # Date listings for a station
            date_folders = [
                BrowseFolder(
                    item_id="today",
                    name="Today",
                    provider=self.domain,
                    path="/".join([*path_parts, dt.now().strftime("%Y-%m-%d")]),
                ),
                BrowseFolder(
                    item_id="yesterday",
                    name="Yesterday",
                    provider=self.domain,
                    path="/".join(
                        [
                            *path_parts,
                            (dt.now() - timedelta(days=1)).strftime("%Y-%m-%d"),
                        ]
                    ),
                ),
            ]
            # Maximum is 30 days prior
            for diff in range(28):
                this_date = dt.now() - timedelta(days=2 + diff)
                date_string = this_date.strftime("%Y-%m-%d")
                date_folders.extend(
                    [
                        BrowseFolder(
                            item_id=date_string,
                            name=date_string,
                            provider=self.domain,
                            path="/".join([*path_parts, date_string]),
                        )
                    ]
                )
            return date_folders
        else:
            return [
                BrowseFolder(
                    item_id=station.item_id,
                    provider=self.domain,
                    name=station.name,
                    path="/".join([*path_parts, station.item_id]),
                    image=(
                        MediaItemImage(
                            type=ImageType.THUMB,
                            path=station.metadata.images[0].path,
                            provider=self.domain,
                        )
                        if station.metadata.images
                        else None
                    ),
                )
                for station in await self._station_list(include_local=show_local)
            ]

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """Browse this provider's items.

        :param path: The path to browse, (e.g. provider_id://artists).
        """
        self.logger.debug(f"Browsing path: {path}")
        if not self.menu:
            await self._fetch_menu()
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

        show_local = cast("bool", self.config.get_value("show_local"))

        if sub_path == "":
            return await self._get_menu()
        elif sub_path == "categories" and sub_sub_path:
            return await self._get_category(sub_sub_path)
        elif sub_path == "collections" and sub_sub_path:
            return await self._get_collection(sub_sub_path)
        elif sub_path != "stations":
            return await self._get_subpath_menu(sub_path)
        elif sub_path == "stations":
            return await self._get_station_schedule_menu(
                show_local, path_parts, sub_sub_path, sub_sub_sub_path
            )
        else:
            return []

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
        if self.config.get_value(_Constants.CONF_ENABLE_UK_CONTENT) and (
            media_types is None
            or MediaType.TRACK in media_types
            or MediaType.PODCAST_EPISODE in media_types
        ):
            episodes = [await self.adaptor.new_object(track) for track in search_result.episodes]
            results.tracks = [track for track in episodes if type(track) is Track]
            # results.podcasts = [podcast for podcast in episodes if type(podcast) is Track]

        if self.config.get_value(_Constants.CONF_ENABLE_UK_CONTENT) and (
            media_types is None or MediaType.PODCAST in media_types
        ):
            podcasts = [await self.adaptor.new_object(show) for show in search_result.shows]
            results.podcasts = [podcast for podcast in podcasts if isinstance(podcast, Podcast)]

        return results

    async def get_podcast(self, prov_podcast_id: str, **kwargs: str) -> Podcast:
        """Get full podcast details by id.

        Only called if provider supports ProviderFeature.LIBRARY_PODCASTS.
        """
        self.logger.debug(f"Getting podcast for {prov_podcast_id}")
        podcast = await self.client.streaming.get_podcast(pid=prov_podcast_id)
        ma_podcast = await self.adaptor.new_object(source_obj=podcast, force_type=Podcast)

        if isinstance(ma_podcast, Podcast):
            return ma_podcast
        raise MusicAssistantError("Incorrect format for podcast")

    async def get_podcast_episodes(
        self,
        prov_podcast_id: str,
    ) -> AsyncGenerator[PodcastEpisode, None]:
        """Get all PodcastEpisodes for given podcast id.

        Only called if provider supports ProviderFeature.LIBRARY_PODCASTS.
        """
        podcast_episodes = await self.client.streaming.get_podcast_episodes(prov_podcast_id)

        if podcast_episodes:
            for episode in podcast_episodes:
                this_episode = await self.adaptor.new_object(
                    source_obj=episode, force_type=PodcastEpisode
                )
                if this_episode and isinstance(this_episode, PodcastEpisode):
                    yield this_episode

    # @use_cache(3600)
    async def recommendations(self) -> list[RecommendationFolder]:
        """Get available recommendations."""
        folders = []

        if self.config.get_value(_Constants.CONF_ENABLE_UK_CONTENT):
            if self.recommendation_location == "homepage":
                recommendations = await self.client.personal.get_experience_menu(
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

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """Get full radio details by id."""
        self.logger.debug(f"Getting radio for {prov_radio_id}")
        station = await self.client.stations.get_station(prov_radio_id, include_stream=True)
        if station:
            ma_radio = await self.adaptor.new_object(station, force_type=Radio)
            if ma_radio and station.stream and isinstance(ma_radio, Radio):
                return ma_radio
        raise MusicAssistantError("No valid radio stream found")

    async def on_streamed(
        self,
        streamdetails: StreamDetails,
    ) -> None:
        """
        Handle callback when given streamdetails completed streaming.

        To get the number of seconds streamed, see streamdetails.seconds_streamed.
        To get the number of seconds seeked/skipped, see streamdetails.seek_position.
        Note that seconds_streamed is the total streamed seconds, so without seeked time.

        NOTE: Due to internal and player buffering,
        this may be called in advance of the actual completion.
        """
        # This is an OPTIONAL callback that is called when an item has been streamed.
        # You can use this e.g. for playback reporting or statistics.

    async def on_played(
        self,
        media_type: MediaType,
        prov_item_id: str,
        fully_played: bool,
        position: int,
        media_item: MediaItemType,
        is_playing: bool = False,
    ) -> None:
        """
        Handle callback when a (playable) media item has been played.

        This is called by the Queue controller when;
            - a track has been fully played
            - a track has been stopped (or skipped) after being played
            - every 30s when a track is playing

        Fully played is True when the track has been played to the end.

        Position is the last known position of the track in seconds, to sync resume state.
        When fully_played is set to false and position is 0,
        the user marked the item as unplayed in the UI.

        is_playing is True when the track is currently playing.

        media_item is the full media item details of the played/playing track.
        """
        # This is an OPTIONAL callback that is called when an item has been streamed.
        # You can use this e.g. for playback reporting or statistics.
        if media_type != MediaType.RADIO:
            action = None

            if is_playing:
                action = PlayStatus.STARTED if position < 30 else PlayStatus.HEARTBEAT
            elif fully_played:
                action = PlayStatus.ENDED
            else:
                action = PlayStatus.PAUSED

            if action:
                success = await self.client.streaming.update_play_status(
                    pid=media_item.item_id, elapsed_time=position, action=action
                )
                self.logger.info(f"Updated play status: {success}")
