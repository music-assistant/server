"""
BBC Sounds music provider support for MusicAssistant.

TODO change programme display when a programme finishes
TODO cache data such as schedules and stream URLs
TODO implement seeking of live stream
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast

from music_assistant_models.config_entries import (
    ConfigEntry,
    ConfigValueType,
    ProviderConfig,
)
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    ImageType,
    MediaType,
    PlayerState,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import LoginFailed, MusicAssistantError
from music_assistant_models.media_items import (
    AudioFormat,
    BrowseFolder,
    ItemMapping,
    MediaItemImage,
    MediaItemMetadata,
    MediaItemType,
    ProviderMapping,
    Radio,
    SearchResults,
)
from music_assistant_models.streamdetails import StreamDetails
from music_assistant_models.unique_list import UniqueList

from music_assistant.constants import CONF_PASSWORD, CONF_USERNAME
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from collections.abc import Sequence

    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

from sounds import exceptions
from sounds.client import SoundsClient

SUPPORTED_FEATURES = {
    ProviderFeature.BROWSE,
    ProviderFeature.LIBRARY_RADIOS,
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
            key="intro",
            type=ConfigEntryType.LABEL,
            label="A BBC Sounds account is optional, but some streams may not work or be served "
            "in reduced quality if outside the U.K.",
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
            key="show_local",
            category="advanced",
            type=ConfigEntryType.BOOLEAN,
            label="Show local radio stations?",
            default_value=False,
        ),
        ConfigEntry(
            key="now_playing",
            category="advanced",
            type=ConfigEntryType.BOOLEAN,
            label="Show 'now playing' details?",
            description=(
                "Show details of the currently playing track instead of the stationdetails"
            ),
            default_value=True,
        ),
        ConfigEntry(
            key="update_interval",
            category="advanced",
            type=ConfigEntryType.INTEGER,
            label="Player update interval (seconds)",
            description="How often to check for now playing updates",
            default_value=5,
            range=(1, 60),
            depends_on="now_playing",
            depends_on_value=True,
        ),
    )


class BBCSoundsProvider(MusicProvider):
    """A MusicProvider class to interact with the BBC Sounds API via auntie-sounds."""

    # This is the image id that is shown when there's no track image
    BLANK_IMAGE_NAME = "p0bqcdzf"
    client: SoundsClient

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self.client = SoundsClient(
            session=self.mass.http_session,
            logger=self.logger,
        )

        # If we have an account, authenticate. Testing shows all features work without auth
        # but BBC will be disabling BBC Sounds from outside the UK at some point
        if self.config.get_value(CONF_USERNAME) and self.config.get_value(CONF_PASSWORD):
            try:
                await self.client.auth.authenticate(
                    username=str(self.config.get_value(CONF_USERNAME)),
                    password=str(self.config.get_value(CONF_PASSWORD)),
                )
            except exceptions.LoginFailedError as e:
                raise LoginFailed(e)

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        self.mass.create_task(self._schedule_now_playing_updates())
        return await super().loaded_in_mass()

    async def _update_player_now_playing(self, queue_id: str, station_id: str) -> None:
        """Manually update a player's now playing information."""
        # TODO: detect blank song images
        queue = self.mass.player_queues.get(queue_id=queue_id)
        if not queue:
            return
        station = await self.client.stations.get_station(station_id=station_id)
        if not station:
            return
        now_playing = await self.client.schedules.currently_playing_song(station, image_size=1280)

        # TODO: check if there is a neater way to do this
        if (
            now_playing
            and queue.current_item
            and queue.current_item.media_item
            and queue.current_item.streamdetails
        ):
            image = now_playing.image_url
            if self.BLANK_IMAGE_NAME not in image:
                image_types = [ImageType.THUMB, ImageType.BANNER, ImageType.LOGO]
                images = UniqueList(
                    [
                        MediaItemImage(
                            type=img_type,
                            path=image,
                            remotely_accessible=True,
                            provider=self.domain,
                        )
                        for img_type in image_types
                    ]
                )
                queue.current_item.media_item.metadata.images = images

            queue.current_item.media_item.name = now_playing.secondary_title
            queue.current_item.streamdetails.stream_title = now_playing.primary_title
            self.mass.player_queues.signal_update(queue_id, items_changed=True)
        else:
            await self._display_station(queue_id=queue_id, station_id=station_id)

    async def _display_station(self, queue_id: str, station_id: str) -> None:
        """Push the current playing station to a player metadata."""
        queue = self.mass.player_queues.get(queue_id=queue_id)
        station = await self.client.stations.get_station(station_id=station_id)
        if (
            station
            and queue
            and queue.current_item
            and queue.current_item.media_item
            and queue.current_item.streamdetails
        ):
            image_types = [ImageType.THUMB, ImageType.BANNER, ImageType.LOGO]
            images = UniqueList(
                [
                    MediaItemImage(
                        type=img_type,
                        path=station.logo_url,
                        remotely_accessible=True,
                        provider=self.domain,
                    )
                    for img_type in image_types
                ]
            )
            queue.current_item.media_item.name = f"BBC {station.name}"
            queue.current_item.streamdetails.stream_title = station.description
            queue.current_item.media_item.metadata.images = images
            self.mass.player_queues.signal_update(queue_id, items_changed=True)

    async def _schedule_now_playing_updates(self) -> None:
        """Set up now playing watchdog."""
        self.logger.debug("Loaded Now Playing scheduled task")
        while True:
            if bool(self.config.get_value("now_playing")):
                try:
                    await self._update_our_queues()
                except exceptions.SoundsException as e:
                    self.logger.error(f"BBC Sounds API error during player update: {e}")
                except Exception as e:
                    self.logger.error(f"Unexpected error during now playing update: {e}")
            await asyncio.sleep(cast("int", self.config.get_value("update_interval")))

    async def _update_our_queues(self) -> None:
        """Update all queues managed by us.

        Poll the now playing endpoint and update the player metadata with the currently
        playing song.
        """
        # TODO: cache queues on queue change rather than polling all
        for queue in self.mass.player_queues.all():
            if not queue.current_item:
                self.logger.debug("Nothing in queue, skipping")
                continue
            if not queue.current_item.streamdetails:
                self.logger.debug("Not a stream, skipping")
                continue
            if queue.current_item.streamdetails.provider == self.domain:
                station_id = queue.current_item.streamdetails.item_id
                self.logger.debug("Found a bbc_sounds queue")
                if queue.state == PlayerState.PLAYING and queue.current_item is not None:
                    self.logger.debug("Queue is playing, updating its now playing metadata")
                    await self._update_player_now_playing(queue.queue_id, station_id)

                elif queue.state in [PlayerState.PAUSED, PlayerState.IDLE]:
                    self.logger.debug("Queue is paused, reverting to station information")
                    await self._display_station(queue.queue_id, station_id)

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

        Setting this to True will only query one instance of the provider for search and lookups.
        Setting this to False will query all instances of this provider for search and lookups.
        """
        return True

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """Get full radio details by id."""
        station = await self.client.stations.get_station(prov_radio_id, include_stream=True)
        if not station or not station.stream:
            raise MusicAssistantError("No valid radio stream found")
        return Radio(
            item_id=prov_radio_id,
            provider=self.domain,
            name=f"BBC {station.name}",  # main title on details page
            uri=station.stream.uri,
            metadata=MediaItemMetadata(
                description="",  # subtitle on details page
                label=station.stream.show_title,
                images=UniqueList(
                    [
                        MediaItemImage(
                            type=ImageType.THUMB,
                            path=station.logo_url,
                            provider=self.lookup_key,
                            remotely_accessible=True,
                        )
                    ]
                ),
            ),
            provider_mappings={
                ProviderMapping(
                    item_id=prov_radio_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get streamdetails for a track/radio."""
        station = await self.client.stations.get_station(item_id, include_stream=True)
        if not station or not station.stream:
            raise MusicAssistantError(f"No stream found for {item_id}")

        return StreamDetails(
            stream_title=station.description,
            media_type=MediaType.RADIO,
            stream_type=StreamType.HLS,
            path=station.stream.uri,
            item_id=item_id,
            provider=self.domain,
            audio_format=AudioFormat(content_type=ContentType.try_parse(station.stream.uri)),
            can_seek=station.stream.can_seek,
            data={"provider": self.domain, "station": station.id},
        )

    async def _station_list(self, include_local: bool = False) -> list[Radio]:
        return [
            Radio(
                item_id=station.id,
                name=station.name,
                provider=self.domain,
                metadata=MediaItemMetadata(
                    description=station.description,
                    images=UniqueList(
                        [
                            MediaItemImage(
                                type=ImageType.THUMB,
                                provider=self.domain,
                                path=station.logo_url,
                                remotely_accessible=True,
                            ),
                        ]
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
        ]

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """Browse this provider's items.

        :param path: The path to browse, (e.g. provider_id://artists).
        """
        item_path = path.split("://", 1)[1]
        if not item_path:
            item_path = ""

        if item_path == "":
            return [BrowseFolder(item_id="live", provider=self.domain, name="Listen Live")]
        elif item_path == "live":
            show_local = cast("bool", self.config.get_value("show_local"))
            return await self._station_list(include_local=show_local)
        else:
            return []

    async def search(
        self, search_query: str, media_types: list[MediaType] | None, limit: int = 5
    ) -> SearchResults:
        """Perform search for BBC Sounds stations."""
        results = SearchResults()
        if media_types is None or MediaType.RADIO in media_types:
            show_local = cast("bool", self.config.get_value("show_local"))
            stations = await self._station_list(include_local=show_local)

            # TODO: better way of ordering
            results.radio = [station for station in stations if search_query in station.name][
                :limit
            ]
        return results
