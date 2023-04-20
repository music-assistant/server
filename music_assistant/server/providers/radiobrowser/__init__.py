"""RadioBrowser musicprovider support for MusicAssistant."""
from __future__ import annotations

from collections.abc import AsyncGenerator
from time import time
from typing import TYPE_CHECKING

from radios import RadioBrowser, RadioBrowserError

from music_assistant.common.models.config_entries import ConfigEntry, ConfigValueType
from music_assistant.common.models.enums import LinkType, ProviderFeature
from music_assistant.common.models.media_items import (
    ContentType,
    ImageType,
    MediaItemImage,
    MediaItemLink,
    MediaType,
    ProviderMapping,
    Radio,
    SearchResults,
    StreamDetails,
)
from music_assistant.constants import __version__ as MASS_VERSION  # noqa: N812
from music_assistant.server.helpers.audio import get_radio_stream
from music_assistant.server.models.music_provider import MusicProvider

SUPPORTED_FEATURES = (ProviderFeature.SEARCH,)

if TYPE_CHECKING:
    from music_assistant.common.models.config_entries import ProviderConfig
    from music_assistant.common.models.provider import ProviderManifest
    from music_assistant.server import MusicAssistant
    from music_assistant.server.models import ProviderInstanceType


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    prov = RadioBrowserProvider(mass, manifest, config)

    await prov.handle_setup()
    return prov


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
    # ruff: noqa: ARG001 D205
    return tuple()  # we do not have any config entries (yet)


class RadioBrowserProvider(MusicProvider):
    """Provider implementation for RadioBrowser."""

    @property
    def supported_features(self) -> tuple[ProviderFeature, ...]:
        """Return the features supported by this Provider."""
        return SUPPORTED_FEATURES

    async def handle_setup(self) -> None:
        """Handle async initialization of the provider."""
        self.radios = RadioBrowser(
            session=self.mass.http_session, user_agent=f"MusicAssistant/{MASS_VERSION}"
        )
        try:
            # Try to get some stats to check connection to RadioBrowser API
            await self.radios.stats()
        except RadioBrowserError as err:
            self.logger.error("%s", err)

    async def search(
        self, search_query: str, media_types=list[MediaType] | None, limit: int = 10
    ) -> SearchResults:
        """Perform search on musicprovider.

        :param search_query: Search query.
        :param media_types: A list of media_types to include. All types if None.
        :param limit: Number of items to return in the search (per type).
        """
        result = SearchResults()
        searchtypes = []
        if MediaType.RADIO in media_types:
            searchtypes.append("radio")

        time_start = time()

        searchresult = await self.radios.search(name=search_query, limit=limit)

        self.logger.debug(
            "Processing RadioBrowser search took %s seconds",
            round(time() - time_start, 2),
        )
        for item in searchresult:
            result.radio.append(await self._parse_radio(item))

        return result

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """Get radio station details."""
        radio = await self.radios.station(uuid=prov_radio_id)
        return await self._parse_radio(radio)

    async def _parse_radio(self, radio_obj: dict) -> Radio:
        """Parse Radio object from json obj returned from api."""
        radio = Radio(item_id=radio_obj.uuid, provider=self.domain, name=radio_obj.name)
        radio.add_provider_mapping(
            ProviderMapping(
                item_id=radio_obj.uuid,
                provider_domain=self.domain,
                provider_instance=self.instance_id,
            )
        )
        radio.metadata.label = radio_obj.tags
        radio.metadata.popularity = radio_obj.votes
        radio.metadata.links = [MediaItemLink(LinkType.WEBSITE, radio_obj.homepage)]
        radio.metadata.images = [MediaItemImage(ImageType.THUMB, radio_obj.favicon)]

        return radio

    async def get_stream_details(self, item_id: str) -> StreamDetails:
        """Get streamdetails for a radio station."""
        stream = await self.radios.station(uuid=item_id)
        url_resolved = stream.url_resolved
        await self.radios.station_click(uuid=item_id)
        return StreamDetails(
            provider=self.domain,
            item_id=item_id,
            content_type=ContentType.try_parse(stream.codec),
            media_type=MediaType.RADIO,
            data=url_resolved,
            expires=time() + 24 * 3600,
        )

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0  # noqa: ARG002
    ) -> AsyncGenerator[bytes, None]:
        """Return the audio stream for the provider item."""
        async for chunk in get_radio_stream(self.mass, streamdetails.data, streamdetails):
            yield chunk
