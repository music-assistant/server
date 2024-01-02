"""SiriusXM music provider support for MusicAssistant."""
from __future__ import annotations

from collections import defaultdict
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Generator, Union, Dict, Final

from sxm import SXMClientAsync
from sxm.models import QualitySize, RegionChoice, XMCategory, XMChannel, XMLiveChannel

from music_assistant.common.models.config_entries import ConfigEntry, ConfigValueType
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


# noinspection PyUnusedLocal
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
    # ruff: noqa: ARG001 D205
    return (
        ConfigEntry(
            key=CONF_SXM_USERNAME, type=ConfigEntryType.STRING, label="Username", required=True,
        ),
        ConfigEntry(
            key=CONF_SXM_PASSWORD, type=ConfigEntryType.SECURE_STRING, label="Password", required=True,
        ),
        ConfigEntry(
            key=CONF_SXM_REGION, type=ConfigEntryType.STRING, label="Region (US or CA)", required=False,
        ),
    )


class SiriusXMProvider(MusicProvider):
    """Provider implementation for SiriusXM."""
    _username: str = None
    _password: str = None
    _region: RegionChoice = None
    _client: SXMClientAsync = None

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

        await self._refresh_channels()

    def _channel_updated(self, live_channel_raw: Dict):
        live_channel = XMLiveChannel.from_dict(live_channel_raw)
        latest_cut = live_channel.get_latest_cut()
        latest_episode = live_channel.get_latest_episode()
        if latest_cut is not None:
            artists = ', '.join(map(lambda x: x.name, latest_cut.cut.artists))
            self.logger.info(f"Channel updated: {artists} - {latest_cut.cut.title}")
        elif latest_episode is not None:
            self.logger.info(f"{latest_episode.episode.show.medium_title} - {latest_episode.episode.medium_title}")
        else:
            self.logger.info("Channel updated")

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

    async def get_radio(self, channel_id: str) -> Union[Radio, None]:
        channel = self._channels_by_id[channel_id] if channel_id in self._channels_by_id else None
        if channel is None:
            return None
        return self._parse_radio(channel)

    async def get_stream_details(self, item_id: str) -> StreamDetails:
        """Get StreamDetails for a radio station."""
        # channel_id = item_id
        # channel = self._channels_by_id[channel_id]

        # playlist = await self._client.get_playlist(channel.id, use_cache=False)
        # playlist_items = list(filter(lambda l: not l.startswith("#"), playlist.split('\n')))

        # first_segment = await self._client.get_segment(playlist_items[0])

        # primary_hls_root = await self._client.get_primary_hls_root()
        # first_item = f"{primary_hls_root}/{playlist_items[0]}"

        # live_channel_raw = await self._client.get_now_playing(channel)
        # live_channel = XMLiveChannel.from_dict(live_channel_raw["moduleList"]["modules"][0])

        # playlist_url = live_channel.primary_hls.url

        # cache_key = f"{self.instance_id}.media_info.{channel_id}"
        # cached_info = await self.mass.cache.get(cache_key)
        # if cached_info:
        #     media_info = AudioTags.parse(cached_info)
        # else:
        #     # parse info with ffprobe (and store in cache)
        #     media_info = await parse_tags(self._to_generator(first_segment))
        #     await self.mass.cache.set(cache_key, media_info.raw)

        # MPEG AAC Audio (mp4a)
        # Type: Audio
        # Channels: 2
        # Sample Rate: 44100
        # Bits per sample: 32

        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(
                sample_rate=44100,
                channels=2,
                bit_depth=32,
                content_type=ContentType.M4A,
            ),
            # stream_title=channel.name,
            # duration=?,
            # data={"url": url, "format": url_details["format"]}
            # expires=time() + 24 * 3600,
            # size=?,
            media_type=MediaType.RADIO,
            # data=live_channel.primary_hls.url,
            direct=None,
            # can_seek=False,
        )

    async def get_playlist_items_generator(self, stream_details: StreamDetails):
        channel_id = stream_details.item_id

        if not channel_id in self._channels_by_id:
            self.logger.warn(f"Unknown channel {channel_id}")
            return

        self.logger.info(f"Fetching playlist for channel: {channel_id}")
        playlist = await self._client.get_playlist(channel_id, use_cache=False)

        playlist_paths = list(filter(lambda l: not l.startswith("#"), playlist.split('\n')))
        for playlist_path in playlist_paths:
            self.logger.info(f"Getting segment: {playlist_path}")
            yield await self._client.get_segment(playlist_path)

    async def get_audio_stream(self, stream_details: StreamDetails, seek_position: int = 0) -> AsyncGenerator[bytes, None]:
        async for chunk in self.get_playlist_items_generator(stream_details):
            yield bytes(chunk)

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

    async def _refresh_channels(self):
        """Get a list of channels."""
        self.logger.info(f"Refreshing channels...")
        channels: list[XMChannel] = await self._client.channels

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
                # category_is_decade = category.key in DECADE_CATEGORY_KEY_RANGES
                # category_decade = DECADE_CATEGORY_KEY_RANGES[category.key] if category_is_decade else None

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
        # sort_order_by_category = dict(sorted(sort_order_by_category.items(), key=lambda x: x[1]))
        # categories = dict(sorted(categories.items(), key=lambda x: sort_order_by_category[x[0]]))
        # channels_by_category = dict(sorted(channels_by_category.items(), key=lambda x: int(sort_order_by_category[x[0]])))

        channels_by_decade = dict(sorted(channels_by_decade.items(), key=lambda x: x[0][0]))

        self._categories = categories
        self._channels_by_id = channels_by_id
        self._channels_by_category = channels_by_category
        self._channels_by_era = channels_by_era
        self._channels_by_decade = channels_by_decade

        self.logger.info(f"Refreshing favorites...")
        favorite_channels: list[XMChannel] = await self._client.favorite_channels
        self._favorite_channels = favorite_channels

        self.logger.info(
            f"Discovered {len(self._channels_by_id)} channels, {len(self._categories)} categories, {len(self._favorite_channels)} favorites")

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
