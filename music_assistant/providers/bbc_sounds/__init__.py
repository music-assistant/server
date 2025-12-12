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
from typing import TYPE_CHECKING, Literal

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
from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.datetime import LOCAL_TIMEZONE
from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers.bbc_sounds.adaptor import Adaptor

if TYPE_CHECKING:
    from collections.abc import Sequence

    from music_assistant_models.provider import ProviderManifest
    from music_assistant_models.streamdetails import StreamDetails
    from sounds.models import SoundsTypes

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
    ProviderFeature.RECOMMENDATIONS,
    ProviderFeature.SEARCH,
}

FEATURES = {"now_playing": False, "catchup_segments": False, "check_blank_image": False}

type _StreamTypes = Literal["hls", "dash"]


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
        ConfigEntry(
            key=_Constants.CONF_INTRO,
            type=ConfigEntryType.LABEL,
            label="A BBC Sounds account is optional, but some UK-only content may not work without"
            " it",
        ),
        ConfigEntry(
            key=CONF_USERNAME,
            type=ConfigEntryType.STRING,
            label="Email or username",
            required=False,
        ),
        ConfigEntry(
            key=CONF_PASSWORD,
            type=ConfigEntryType.SECURE_STRING,
            label="Password",
            required=False,
        ),
        ConfigEntry(
            key=_Constants.CONF_SHOW_LOCAL,
            category="advanced",
            type=ConfigEntryType.BOOLEAN,
            label="Show local radio stations?",
            default_value=False,
        ),
        ConfigEntry(
            key=_Constants.CONF_STREAM_FORMAT,
            category="advanced",
            label="Preferred stream format",
            type=ConfigEntryType.STRING,
            options=[
                ConfigValueOption(
                    "HLS",
                    _Constants.CONF_STREAM_FORMAT_HLS,
                ),
                ConfigValueOption(
                    "MPEG-DASH",
                    _Constants.CONF_STREAM_FORMAT_DASH,
                ),
            ],
            default_value=_Constants.CONF_STREAM_FORMAT_HLS,
        ),
    )


class _Constants:
    # This is the image id that is shown when there's no track image
    BLANK_IMAGE_NAME: str = "p0bqcdzf"
    DEFAULT_IMAGE_SIZE = 1280
    TRACK_DURATION_THRESHOLD: int = 300  # 5 minutes
    NOW_PLAYING_REFRESH_TIME: int = 5
    HLS: Literal["hls"] = "hls"
    DASH: Literal["dash"] = "dash"
    CONF_SHOW_LOCAL: str = "show_local"
    CONF_INTRO: str = "intro"
    CONF_STREAM_FORMAT: str = "stream_format"
    CONF_STREAM_FORMAT_HLS: str = HLS
    CONF_STREAM_FORMAT_DASH: str = DASH
    DEFAULT_EXPIRATION = 60 * 60 * 24 * 30  # 30 days
    SHORT_EXPIRATION = 60 * 60 * 3  # 3 hours


class BBCSoundsProvider(MusicProvider):
    """A MusicProvider class to interact with the BBC Sounds API via auntie-sounds."""

    client: SoundsClient
    menu: Menu | None = None
    current_task: asyncio.Task[None] | None = None

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self.client = SoundsClient(
            session=self.mass.http_session,
            logger=self.logger,
            timezone=LOCAL_TIMEZONE,
        )

        self.show_local_stations: bool = bool(
            self.config.get_value(_Constants.CONF_SHOW_LOCAL, False)
        )
        self.stream_format: _StreamTypes = (
            _Constants.DASH
            if self.config.get_value(_Constants.CONF_STREAM_FORMAT) == _Constants.DASH
            else _Constants.HLS
        )
        self.adaptor = Adaptor(self)

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

    async def loaded_in_mass(self) -> None:
        """Do post-loaded actions."""
        if not self.menu or (
            isinstance(self.menu, Menu) and self.menu.sub_items and len(self.menu.sub_items) == 0
        ):
            is_uk_listener = await self.client.auth.is_uk_listener
            if self.client.auth.is_logged_in and is_uk_listener:
                await self._fetch_menu()

    def _get_provider_mapping(self, item_id: str) -> ProviderMapping:
        return ProviderMapping(
            item_id=item_id,
            provider_domain=self.domain,
            provider_instance=self.instance_id,
        )

    async def _fetch_menu(self) -> None:
        self.logger.debug("No cached menu, fetching from API")
        self.menu = await self.client.personal.get_experience_menu(
            recommendations=MenuRecommendationOptions.EXCLUDE
        )

    def _stream_error(self, item_id: str, media_type: MediaType) -> MusicAssistantError:
        return MusicAssistantError(f"Couldn't get stream details for {item_id} ({media_type})")

    @property
    def is_streaming_provider(self) -> bool:
        """Return True as the provider is a streaming provider."""
        return True

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
        if not isinstance(ma_episode, PodcastEpisode):
            raise MusicAssistantError(f"Incorrect format for podcast episode {prov_episode_id}")
        return ma_episode

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get streamdetails for a track/radio."""
        self.logger.debug(f"Getting stream details for {item_id} ({media_type})")
        if media_type == MediaType.PODCAST_EPISODE:
            episode_info = await self.client.streaming.get_by_pid(
                item_id, include_stream=True, stream_format=self.stream_format
            )
            stream_details = await self.adaptor.new_streamable_object(episode_info)
            if not stream_details:
                raise self._stream_error(item_id, media_type)

            # Hide behind a feature flag until it can be better tested
            if episode_info and FEATURES["catchup_segments"]:
                # .item_id is the VPID
                self.current_task = self.mass.create_task(
                    self._check_for_segments(item_id, stream_details)
                )
            return stream_details
        elif media_type is MediaType.TRACK:
            track = await self.client.streaming.get_by_pid(
                item_id, include_stream=True, stream_format=self.stream_format
            )
            stream_details = await self.adaptor.new_streamable_object(track)
            if not stream_details:
                raise self._stream_error(item_id, media_type)
            self.current_task = self.mass.create_task(
                self._check_for_segments(item_id, stream_details)
            )
            return stream_details
        else:
            self.logger.debug(f"Getting stream details for station {item_id}")
            station = await self.client.stations.get_station(
                item_id, include_stream=True, stream_format=self.stream_format
            )
            if not station:
                raise MusicAssistantError(f"Couldn't get stream details for station {item_id}")

            self.logger.debug(f"Found station: {station}")
            if not station.stream:
                raise MusicAssistantError(f"No stream found for {item_id}")

            stream_details = await self.adaptor.new_streamable_object(station)

            if not stream_details:
                raise self._stream_error(item_id, media_type)

            # Start a background task to keep these details updated
            if FEATURES["now_playing"]:
                self.current_task = self.mass.create_task(
                    self._watch_stream_details(stream_details)
                )
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

        while True:
            if not stream_details.stream_metadata:
                await asyncio.sleep(_Constants.NOW_PLAYING_REFRESH_TIME)
                continue

            now_playing = await self.client.schedules.currently_playing_song(
                station_id, image_size=_Constants.DEFAULT_IMAGE_SIZE
            )

            if now_playing and stream_details.stream_metadata:
                self.logger.debug(f"Now playing for {station_id}: {now_playing}")

                # removed check temporarily as images not working
                if not FEATURES["check_blank_image"] or (
                    now_playing.image_url
                    and _Constants.BLANK_IMAGE_NAME not in now_playing.image_url
                ):
                    stream_details.stream_metadata.image_url = now_playing.image_url
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
            await asyncio.sleep(_Constants.NOW_PLAYING_REFRESH_TIME)

    def _station_programme_display(self, station: LiveStation) -> str | None:
        if station and station.titles:
            return f"{station.titles.get('secondary')} • {station.titles.get('primary')}"
        return None

    async def _station_list(self, include_local: bool = False) -> list[Radio]:
        return [
            Radio(
                item_id=station.item_id,
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
                        item_id=station.item_id,
                        provider_domain=self.domain,
                        provider_instance=self.instance_id,
                    )
                },
            )
            for station in await self.client.stations.get_stations(include_local=include_local)
            if station and station.item_id
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
        if self.client.auth.is_logged_in and await self.client.auth.is_uk_listener:
            return await self._get_full_menu(path_parts=path_parts)
        else:
            return await self._get_slim_menu(path_parts=path_parts)

    async def _get_full_menu(
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

    async def _get_slim_menu(
        self, path_parts: list[str] | None
    ) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        return [
            BrowseFolder(
                item_id="listen_live",
                provider=self.domain,
                name="Listen Live",
                path=f"{self.domain}://listen_live",
                image=MediaItemImage(
                    path="https://cdn.jsdelivr.net/gh/kieranhogg/auntie-sounds@main/src/sounds/icons/solid/listen_live.png",
                    remotely_accessible=True,
                    provider=self.domain,
                    type=ImageType.THUMB,
                ),
            ),
            BrowseFolder(
                item_id="stations",
                provider=self.domain,
                name="Schedules and Programmes",
                path=f"{self.domain}://stations",
                image=MediaItemImage(
                    path="https://cdn.jsdelivr.net/gh/kieranhogg/auntie-sounds@main/src/sounds/icons/solid/latest.png",
                    remotely_accessible=True,
                    provider=self.domain,
                    type=ImageType.THUMB,
                ),
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

    async def _get_subpath_menu(
        self, sub_path: str
    ) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        item_list: list[MediaItemType | ItemMapping | BrowseFolder] = []
        if self.client.auth.is_logged_in:
            if not self.menu:
                return item_list
            sub_menu = self.menu.get(sub_path)

            if sub_menu and sub_path != "listen_live" and isinstance(sub_menu, Container):
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
                self.show_local_stations, path_parts, sub_sub_path, sub_sub_sub_path
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
    ) -> AsyncGenerator[PodcastEpisode, None]:
        """Get all PodcastEpisodes for given podcast id."""
        podcast_episodes = await self.client.streaming.get_podcast_episodes(prov_podcast_id)

        if podcast_episodes:
            for episode in podcast_episodes:
                this_episode = await self.adaptor.new_object(
                    source_obj=episode, force_type=PodcastEpisode
                )
                if this_episode and isinstance(this_episode, PodcastEpisode):
                    yield this_episode

    @use_cache(expiration=_Constants.SHORT_EXPIRATION)
    async def recommendations(self) -> list[RecommendationFolder]:
        """Get available recommendations."""
        folders = []

        if self.client.auth.is_logged_in:
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
            if ma_radio and isinstance(ma_radio, Radio):
                return ma_radio
        else:
            raise MusicAssistantError(f"No station found: {prov_radio_id}")

        self.logger.debug(f"{station} {ma_radio} {type(ma_radio)}")
        raise MusicAssistantError("No valid radio stream found")

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
        # Cancel now playing task
        if FEATURES["now_playing"] and not is_playing and self.current_task:
            self.current_task.cancel()
