"""SiriusXM Music Provider for Music Assistant."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Sequence
from typing import TYPE_CHECKING, Any, cast

from music_assistant.common.models.config_entries import (
    ConfigEntry,
    ConfigValueOption,
    ConfigValueType,
)
from music_assistant.common.models.enums import (
    ConfigEntryType,
    ContentType,
    LinkType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant.common.models.errors import LoginFailed, MediaNotFoundError
from music_assistant.common.models.media_items import (
    AudioFormat,
    ImageType,
    ItemMapping,
    MediaItemImage,
    MediaItemLink,
    MediaItemType,
    ProviderMapping,
    Radio,
)
from music_assistant.common.models.streamdetails import StreamDetails
from music_assistant.server.helpers.webserver import Webserver
from music_assistant.server.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from music_assistant.common.models.config_entries import ProviderConfig
    from music_assistant.common.models.provider import ProviderManifest
    from music_assistant.server import MusicAssistant
    from music_assistant.server.models import ProviderInstanceType

import sxm.http
from sxm import SXMClientAsync
from sxm.models import QualitySize, RegionChoice, XMChannel

CONF_SXM_USERNAME = "sxm_email_address"
CONF_SXM_PASSWORD = "sxm_password"
CONF_SXM_REGION = "sxm_region"
CONF_SXM_PROXY_PORT = "sxm_proxy_port"


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return SiriusXMProvider(mass, manifest, config)


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
            options=tuple(ConfigValueOption(x, x) for x in ("US", "CA")),
            label="Region",
            required=False,
        ),
        ConfigEntry(
            key=CONF_SXM_PROXY_PORT,
            type=ConfigEntryType.INTEGER,
            label="SXM local server port",
            description="The local port for hosting the SXM proxy server",
            required=True,
            default_value=9999,
            value=values.get(CONF_SXM_PROXY_PORT) if values else None,
            category="advanced",
        ),
    )


class SiriusXMProvider(MusicProvider):
    """SiriusXM Music Provider."""

    _username: str
    _password: str
    _region: str
    _client: SXMClientAsync

    _channels: list[XMChannel]

    _sxm_server: Webserver
    _base_url: str

    @property
    def supported_features(self) -> tuple[ProviderFeature, ...]:
        """Return the features supported by this Provider."""
        return (
            ProviderFeature.BROWSE,
            ProviderFeature.LIBRARY_RADIOS,
        )

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        username: str = self.config.get_value(CONF_SXM_USERNAME)
        password: str = self.config.get_value(CONF_SXM_PASSWORD)

        bind_port: int = self.config.get_value(CONF_SXM_PROXY_PORT)

        region: RegionChoice = (
            RegionChoice.US if self.config.get_value(CONF_SXM_REGION) == "US" else RegionChoice.CA
        )

        self._client = SXMClientAsync(
            username,
            password,
            region,
            quality=QualitySize.LARGE_256k,
            update_handler=self._channel_updated,
        )

        self.logger.info("Authenticating with SiriusXM")
        if not await self._client.authenticate():
            raise LoginFailed("Could not login to SiriusXM")

        self.logger.info("Successfully authenticated")

        await self._refresh_channels()

        # Set up the sxm server for streaming
        bind_ip = "127.0.0.1"
        self._base_url = f"{bind_ip}:{bind_port}"
        http_handler = sxm.http.make_http_handler(self._client)

        self._sxm_server = Webserver(self.logger)

        await self._sxm_server.setup(
            bind_ip=bind_ip,
            bind_port=bind_port,
            base_url=self._base_url,
            static_routes=[
                ("*", "/{tail:.*}", cast(Awaitable, http_handler)),
            ],
        )

        self.logger.info(f"SXM Proxy server running at {bind_ip}:{bind_port}")

    async def unload(self) -> None:
        """
        Handle unload/close of the provider.

        Called when provider is deregistered (e.g. MA exiting or config reloading).
        """
        await self._sxm_server.close()

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

    async def get_library_radios(self) -> AsyncGenerator[Radio, None]:
        """Retrieve library/subscribed radio stations from the provider."""
        for channel in self._channels_by_id.values():
            if channel.is_favorite:
                yield self._parse_radio(channel)

    async def get_radio(self, prov_radio_id: str) -> Radio:  # type: ignore[return]
        """Get full radio details by id."""
        if prov_radio_id not in self._channels_by_id:
            raise MediaNotFoundError("Station not found")

        return self._parse_radio(self._channels_by_id[prov_radio_id])

    async def get_stream_details(self, item_id: str) -> StreamDetails:
        """Get streamdetails for a track/radio."""
        hls_path = f"http://{self._base_url}/{item_id}.m3u8"

        return StreamDetails(
            item_id=item_id,
            provider=self.instance_id,
            audio_format=AudioFormat(
                content_type=ContentType.AAC,
            ),
            stream_type=StreamType.ENCRYPTED_HLS,
            media_type=MediaType.RADIO,
            path=hls_path,
            can_seek=False,
        )

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping]:
        """Browse this provider's items.

        :param path: The path to browse, (e.g. provider_id://artists).
        """
        return [self._parse_radio(channel) for channel in self._channels]

    def _channel_updated(self, live_channel_raw: dict[str, Any]) -> None:
        self.logger.debug(f"channel updated {live_channel_raw}")

    async def _refresh_channels(self) -> bool:
        self._channels = await self._client.channels

        self._channels_by_id = {}

        for channel in self._channels:
            self._channels_by_id[channel.id] = channel

        return True

    def _parse_radio(self, channel: XMChannel) -> Radio:
        radio = Radio(
            provider=self.instance_id,
            item_id=channel.id,
            name=channel.name,
            provider_mappings={
                ProviderMapping(
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    item_id=channel.id,
                )
            },
        )

        icon = next((i.url for i in channel.images if i.width == 300 and i.height == 300), None)
        banner = next(
            (i.url for i in channel.images if i.name in ("channel hero image", "background")), None
        )

        images: list[MediaItemImage] = []

        if icon is not None:
            images.append(
                MediaItemImage(
                    provider=self.instance_id,
                    type=ImageType.THUMB,
                    path=icon,
                    remotely_accessible=True,
                )
            )
            images.append(
                MediaItemImage(
                    provider=self.instance_id,
                    type=ImageType.LOGO,
                    path=icon,
                    remotely_accessible=True,
                )
            )

        if banner is not None:
            images.append(
                MediaItemImage(
                    provider=self.instance_id,
                    type=ImageType.BANNER,
                    path=banner,
                    remotely_accessible=True,
                )
            )
            images.append(
                MediaItemImage(
                    provider=self.instance_id,
                    type=ImageType.LANDSCAPE,
                    path=banner,
                    remotely_accessible=True,
                )
            )

        radio.metadata.images = images
        radio.metadata.links = [MediaItemLink(type=LinkType.WEBSITE, url=channel.url)]
        radio.metadata.description = channel.medium_description
        radio.metadata.explicit = bool(channel.is_mature)
        radio.metadata.genres = [cat.name for cat in channel.categories]

        return radio
