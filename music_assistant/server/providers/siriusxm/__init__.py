"""SiriusXM music provider support for MusicAssistant."""
from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Generator, Final, Any, Awaitable, cast

import sxm.http
from sxm import SXMClientAsync
from sxm.models import QualitySize, RegionChoice, XMCategory, XMChannel, XMLiveChannel

from music_assistant.common.helpers.util import select_free_port
from music_assistant.common.models.config_entries import ConfigEntry, ConfigValueType, ConfigValueOption
from music_assistant.common.models.enums import LinkType, ProviderFeature, ConfigEntryType, ContentType
from music_assistant.common.models.media_items import (
    AudioFormat,
    BrowseFolder,
    ImageType,
    MediaItemImage,
    MediaItemLink,
    MediaItemType,
    MediaType,
    ProviderMapping,
    Radio,
    StreamDetails
)
from music_assistant.server.helpers.audio import get_radio_stream
from music_assistant.server.helpers.playlists import parse_m3u
from music_assistant.server.helpers.webserver import Webserver
from music_assistant.server.models.music_provider import MusicProvider

SUPPORTED_FEATURES = (ProviderFeature.BROWSE, ProviderFeature.LIBRARY_RADIOS,)

if TYPE_CHECKING:
    from music_assistant.common.models.config_entries import ProviderConfig
    from music_assistant.common.models.provider import ProviderManifest
    from music_assistant.server import MusicAssistant
    from music_assistant.server.models import ProviderInstanceType

CONF_SXM_USERNAME: Final[str] = "sxm_email_address"
CONF_SXM_PASSWORD: Final[str] = "sxm_password"
CONF_SXM_REGION: Final[str] = "sxm_region"

GENERATION_POP = 0
GENERATION_DECADES = 1
GENERATION_ELSE = 2

DECADE_CATEGORY_KEY_RANGES = {
    '50s60s': (1950, 1970),
    '70s': (1970, 1980),
    '80s': (1980, 1990),
    '90s': (1990, 2000),
    '00s': (2000, 2010),
    '10s': (2010, 2020),
    '20s': (2020, 2030),
}


async def setup(
        mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    prov = SiriusXMProvider(mass, manifest, config)

    await prov.handle_setup()
    return prov


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to set up this provider.
    instance_id: id of an existing provider instance (None if new instance setup).
    action: [optional] action key called from config entries UI.
    values: the (intermediate) raw values for config entries sent with the action.
    """
    return (
        ConfigEntry(
            key=CONF_SXM_USERNAME,
            type=ConfigEntryType.STRING,
            label="Username",
            required=True,
        ),
        ConfigEntry(
            key=CONF_SXM_PASSWORD,
            type=ConfigEntryType.SECURE_STRING,
            label="Password",
            required=True,
        ),
        ConfigEntry(
            key=CONF_SXM_REGION,
            type=ConfigEntryType.STRING,
            default_value="US",
            options=tuple(ConfigValueOption(x, x) for x in {"US", "CA"}),
            label="Region",
            required=False,
        ),
    )


class SiriusXMProvider(MusicProvider):
    """Provider implementation for SiriusXM."""
    _username: str = None
    _password: str = None
    _region: RegionChoice = None
    _client: SXMClientAsync = None

    _base_url: str = None

    _server: Webserver

    _current_channel: XMChannel = None

    _pad_data: list[str] = []
    _channels_by_id: dict[str, XMChannel] = None
    _favorite_channels: list[XMChannel] = None
    _categories: dict[str, XMCategory] = None
    _channels_by_category: dict[str, list[XMChannel]] = None
    _channels_by_era: [(int, int), list[XMChannel]] = None
    _channels_by_decade: dict[(int, int), list[XMChannel]] = None
    _full_url: dict[str, str] = None

    @property
    def supported_features(self) -> tuple[ProviderFeature, ...]:
        """Return the features supported by this Provider."""
        return SUPPORTED_FEATURES

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._server = Webserver(self.logger)

    async def handle_setup(self) -> None:
        """Handle async initialization of the provider."""
        self._full_url = {}

        self._username = self.config.get_value(CONF_SXM_USERNAME)
        self._password = self.config.get_value(CONF_SXM_PASSWORD)
        self._region = RegionChoice.CA if self.config.get_value(CONF_SXM_REGION) == 'CA' else RegionChoice.US

        self._client = SXMClientAsync(
            self._username,
            self._password,
            self._region,
            quality=QualitySize.LARGE_256k,
            update_handler=self._channel_updated,
        )

        self.logger.info(f"Authenticating with SiriusXM...")
        if not await self._client.authenticate():
            raise RuntimeError(
                f"Authentication failed"
            )
        else:
            self.logger.info("Authenticated successfully")

        attempt = 1
        success = False
        while attempt < 4:
            success = await self._refresh_channels()
            if success:
                break
            else:
                delay = 2 ^ (attempt - 1)
                self.logger.warn(f"Failed to retrieve channels, retrying in {delay}s...")
                await asyncio.sleep(delay)
                attempt = attempt + 1

        if not success:
            raise RuntimeError("Unable to retrieve SiriusXM channels")

        bind_ip="127.0.0.1"
        bind_port = await select_free_port(8097, 9200)
        self._base_url = f"http://127.0.0.1:{bind_port}"
        http_handler = sxm.http.make_http_handler(self._client)

        await self._server.setup(
            bind_ip=bind_ip,
            bind_port=bind_port,
            base_url=self._base_url,
            static_routes=[
                (
                    "*",
                    "/{tail:.*}",
                    cast(Awaitable, http_handler)
                ),
            ],
        )

    async def unload(self) -> None:
        """Cleanup on exit."""
        await self._server.close()

    def _channel_updated(self, live_channel_raw: dict[str, Any]) -> None:
        live_channel = XMLiveChannel.from_dict(live_channel_raw)
        channel = self._channels_by_id[live_channel.id] if live_channel.id in self._channels_by_id else None

        if channel is None:
            self.logger.warn(f"Ignoring update for unknown channel {live_channel.id}")
            return
        elif self._current_channel is None:
            self.logger.warn(f"Ignoring update for channel {live_channel.id}. Not currently tuned to any channel")
            return
        elif channel.id != self._current_channel.id:
            self.logger.warn(f"Ignoring update for channel {live_channel.id}. Tuned to {self._current_channel.id}")
            return

        pad_data = self._parse_channel_update(channel, live_channel)
        if len(pad_data) != len(self._pad_data) or any(x != y for x, y in zip(pad_data, self._pad_data)):
            self._pad_data = pad_data
            self.logger.info(f"Channel {channel.name} updated: {pad_data[0]} - {pad_data[1]}")

    async def browse(self, path: str) -> AsyncGenerator[MediaItemType, None]:
        sub_path = path.split("://", 1)[1]
        sub_sub_path = "" if "/" not in sub_path else sub_path.split("/")[-1]

        if not sub_path:
            # return main listing
            yield BrowseFolder(
                item_id="channels",
                provider=self.domain,
                path=path + "channels",
                name="All Channels",
                label="",
            )
            yield BrowseFolder(
                item_id="favorites",
                provider=self.domain,
                path=path + "favorites",
                name="Favorites",
                label="",
            )
            yield BrowseFolder(
                item_id="categories",
                provider=self.domain,
                path=path + "categories",
                name="Categories",
                label="",
            )
            yield BrowseFolder(
                item_id="decades",
                provider=self.domain,
                path=path + "decades",
                name="Decades",
                label="",
            )

        if sub_path == "channels":
            for channel_id, channel in self._channels_by_id.items():
                yield self._parse_radio(channel)

        if sub_path == "favorites":
            for channel in self._favorite_channels:
                yield self._parse_radio(channel)

        if sub_path == "decades":
            for decade_range in self._channels_by_decade.keys():
                decade = str(decade_range[0])
                decade_name = f"{decade[-2:]}'s"
                decade_name = f"{decade_name} and 60's" if decade_name == "50's" else decade_name
                decade_name = f"20{decade_name}" if decade_name in ["00's", "10's", "20's"] else decade_name
                yield BrowseFolder(
                    item_id=decade,
                    provider=self.domain,
                    path=path + "/" + decade,
                    name=decade_name,
                    label="",
                )

        if sub_path.startswith("decades/"):
            year = int(sub_sub_path)
            channels = self._get_channels_for_year(year)
            for channel in channels:
                yield self._parse_radio(channel)

        if sub_path == "categories":
            for category_key, category in self._categories.items():
                if not category.name.endswith('Decade') and not category.name.endswith('Decades'):
                    yield BrowseFolder(
                        item_id=category_key,
                        provider=self.domain,
                        path=path + "/" + category_key,
                        name=category.name,
                        label="",
                    )

        if sub_path.startswith("categories/"):
            category_key = sub_sub_path
            channels = self._channels_by_category[category_key]
            for channel in channels:
                yield self._parse_radio(channel)

    async def get_library_radios(self) -> AsyncGenerator[Radio, None]:
        """Retrieve library/subscribed playlists from the provider."""
        for channel_guid, channel in self._channels_by_id.items():
            yield self._parse_radio(channel)

    async def get_radio(self, channel_id: str) -> Radio | None:
        channel = self._channels_by_id[channel_id] if channel_id in self._channels_by_id else None
        if channel is None:
            return None
        return self._parse_radio(channel)

    async def get_stream_details(self, item_id: str) -> StreamDetails:
        """Get StreamDetails for a radio station."""
        channel_id = item_id
        channel = self._channels_by_id[channel_id]
        playlist_url = f"{self._base_url}/{channel.id}.m3u8"

        self._current_channel = channel

        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(
                content_type=ContentType.M4A,
            ),
            data={
                'channel': channel,
                'playlist_url': playlist_url,
            },
            media_type=MediaType.RADIO,
            direct=playlist_url,
            can_seek=False,
        )

    # async def get_playlist(self, channel_id: str) -> [str | None, list[str]]:
    #     self.logger.info(f"Fetching playlist for channel: {channel_id}")
    #     playlist = await self._client.get_playlist(channel_id, use_cache=True)
    #     playlist_paths = await parse_m3u(playlist)
    #     self.logger.info(f"Found {len(playlist_paths)} segments")
    #
    #     return playlist, playlist_paths

    # async def get_audio_stream(self, stream_details: StreamDetails, seek_position: int = 0) -> AsyncGenerator[bytes, None]:
    #     channel_id = stream_details.item_id
    #
    #     if not channel_id in self._channels_by_id:
    #         self.logger.warn(f"Unknown channel {channel_id}")
    #         return
    #
    #     self.logger.info(f"Fetching playlist for channel: {channel_id}")
    #     _, playlist_paths = await self.get_playlist(channel_id)
    #
    #     for playlist_path in playlist_paths:
    #         url = f"{self._base_url}/{playlist_path}"
    #         self.logger.info(f"Requesting {url}")
    #         async for segment in get_radio_stream(self.mass, url, stream_details):
    #             yield segment

    def _parse_radio(self, channel: XMChannel) -> Radio:
        """Parse Radio object from json obj returned from api."""
        is_favorite = channel in self._favorite_channels

        radio = Radio(
            provider=self.instance_id,
            item_id=channel.id,
            name=channel.name,
            favorite=is_favorite,
            provider_mappings={
                ProviderMapping(
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    item_id=channel.id,
                )
            },
        )

        icon = next(map(lambda x: x.url,
                        filter(lambda x: x.width == 300 and x.height == 300, channel.images)), None)
        banner = next(map(lambda x: x.url, filter(lambda x: x.name == 'background', channel.images)),
                      None)

        radio.metadata.images = [MediaItemImage(type=ImageType.THUMB, path=icon),
                                 MediaItemImage(type=ImageType.BANNER, path=banner)]
        radio.metadata.links = [MediaItemLink(type=LinkType.WEBSITE, url=channel.url)]
        radio.metadata.description = channel.medium_description
        radio.metadata.explicit = bool(channel.is_mature)
        radio.metadata.genres = map(lambda x: x.name, channel.categories)

        return radio

    async def _refresh_channels(self) -> bool:
        """Get a list of channels."""
        self.logger.info(f"Refreshing channels...")
        channels: list[XMChannel] = await self._client.channels

        if len(channels) == 0:
            return False

        favorite_channels: list[XMChannel] = await self._client.favorite_channels

        categories: dict[str, XMCategory] = dict()
        sort_order_by_category = dict()
        channels_by_id: dict[str, XMChannel] = dict()
        channels_by_category: dict[str, list[XMChannel]] = defaultdict(list)
        channels_by_decade: dict[(int, int), list[XMChannel]] = defaultdict(list)
        channels_by_era: dict[(int, int), list[XMChannel]] = defaultdict(list)

        for channel in channels:
            channels_by_id[channel.id] = channel

            decades, eras = self._get_decades_and_eras(channel)

            for decade in decades:
                channels_by_decade[decade].append(channel)

            if eras is not None:
                for era in eras:
                    channels_by_era[era].append(channel)

            for category in channel.categories:
                if category.key not in categories:
                    categories[category.key] = category

                channels_by_category[category.key].append(channel)

        for category_key, channels in channels_by_category.items():
            channel_with_min = min(channels, key=lambda x: x.channel_number)
            min_channel_number = channel_with_min.channel_number
            if category_key not in sort_order_by_category:
                sort_order_by_category[category_key] = min_channel_number
            else:
                sort_order_by_category[category_key] = min(sort_order_by_category[category_key], min_channel_number)

        channels_by_id = dict(sorted(channels_by_id.items(), key=lambda x: x[1].channel_number))
        channels_by_decade = dict(sorted(channels_by_decade.items(), key=lambda x: x[0][0]))

        self._categories = categories
        self._channels_by_id = channels_by_id
        self._channels_by_category = channels_by_category
        self._channels_by_era = channels_by_era
        self._channels_by_decade = channels_by_decade

        self._favorite_channels = favorite_channels

        self.logger.info(
            f"Discovered {len(self._channels_by_id)} channels, {len(self._categories)} categories, {len(self._favorite_channels)} favorites")

        return True

    def _get_channels_for_year(self, year):
        for era, channels in self._channels_by_era.items():
            if era[0] <= year < era[1]:
                return channels
        return []

    def _get_decades_and_eras(self, channel: XMChannel) -> [list[(int, int)], list[(int, int)]]:
        decades = []

        for category in channel.categories:
            if category.key in DECADE_CATEGORY_KEY_RANGES:
                decades.append(DECADE_CATEGORY_KEY_RANGES[category.key])

        if len(decades) > 0:
            decades = list(sorted(list(set(decades)), key=lambda x: x[0]))
            eras = list(self._flatten_decades(decades))
        else:
            eras = None

        return decades, eras

    @staticmethod
    def _flatten_decades(ranges: list[(int, int)]) -> Generator[(int, int), None, None]:
        ranges = list(sorted(list(set(ranges)), key=lambda x: x[0]))
        lower = ranges[0][0]
        upper = ranges[0][1]
        for i in range(1, len(ranges)):
            if ranges[i][0] == ranges[i - 1][1]:
                upper = ranges[i][1]
            else:
                yield lower, upper
                lower = ranges[i][0]
                upper = ranges[i][1]

        yield lower, upper

    @staticmethod
    def _parse_channel_update(channel: XMChannel, live_channel: XMLiveChannel) -> list[str]:
        latest_cut = live_channel.get_latest_cut()
        latest_episode = live_channel.get_latest_episode()
        if latest_cut is not None:
            artists = ', '.join(map(lambda x: x.name, latest_cut.cut.artists))
            title = latest_cut.cut.title
            if len(artists) > 0 and len(title) > 0:
                return [f"{artists}", f"{latest_cut.cut.title}"]
            elif len(artists) > 0:
                return [f"{channel.name}", f"{artists}"]
            elif len(artists) > 0 and len(title) > 0:
                return [f"{channel.name}", f"{latest_cut.cut.title}"]
            else:
                return [f"{channel.name}", f"{channel.name}"]
        elif latest_episode is not None:
            show_title = latest_episode.episode.show.medium_title
            episode_title = latest_episode.episode.medium_title

            if len(show_title) > 0 and len(episode_title) > 0:
                return [f"{show_title}", f"{episode_title}"]
            elif len(show_title) > 0:
                return [f"{channel.name}", f"{show_title}"]
            elif len(episode_title) > 0:
                return [f"{channel.name}", f"{episode_title}"]
            else:
                return [f"{channel.name}", ""]
        else:
            return [f"{channel.name}", ""]
