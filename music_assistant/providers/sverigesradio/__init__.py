from __future__ import annotations

from collections.abc import AsyncGenerator, Sequence
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import (
    BrowseFolder,
    MediaItemImage,
    MediaItemType,
    Radio,
    SearchResults,
)
from music_assistant_models.streamdetails import AudioFormat, StreamDetails

from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


SUPPORTED_FEATURES = {
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
    ProviderFeature.LIBRARY_RADIOS,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize instance"""
    return SverigesRadio(mass, manifest, config, SUPPORTED_FEATURES)




class SverigesRadio(MusicProvider):

    async def handle_async_init(self) -> None:
        """Initialise"""
        self._base_api = "https://api.sr.se/api/v2"

    # ---------------------------------------------------------------------
    # Library / browse
    # ---------------------------------------------------------------------

    async def get_library_radios(self) -> AsyncGenerator[Radio, None]:

        params = {"format": "json", "size": 500}
        async with self.mass.http_session.get(
            f"{self._base_api}/channels", params=params
        ) as resp:
            data = await resp.json()

        for ch in data.get("channels", []):
            ch_id = str(ch["id"])
            name = ch.get("name") or ch.get("channeltype") or f"SR {ch_id}"
            img_url = ch.get("image")

            radio = Radio(
                name=name,
                item_id=ch_id,
                provider=self.domain,
                provider_mappings=self._provider_mapping_for_id(ch_id),
            )

            if img_url:
                radio.metadata.add_image(
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=img_url,
                        provider=self.domain,
                        remotely_accessible=True,
                    )
                )

            yield radio

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """Get radio details by SR channel id."""
        params = {"format": "json"}
        async with self.mass.http_session.get(
            f"{self._base_api}/channels/{prov_radio_id}", params=params
        ) as resp:
            data = await resp.json()

        ch = data.get("channel")
        if not ch:
            raise MediaNotFoundError("Radio not found")

        name = ch.get("name") or ch.get("channeltype") or f"SR {prov_radio_id}"
        img_url = ch.get("image")

        radio = Radio(
            name=name,
            item_id=str(ch["id"]),
            provider=self.domain,
            provider_mappings=self._provider_mapping_for_id(str(ch["id"])),
        )

        if img_url:
            radio.metadata.add_image(
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=img_url,
                    provider=self.domain,
                    remotely_accessible=True,
                )
            )

        return radio

    async def browse(self, path: str) -> Sequence[MediaItemType | BrowseFolder]:
        """list all radio stations"""
        radios = [r async for r in self.get_library_radios()]
        return radios

    # ---------------------------------------------------------------------
    # Search (radio only, basic)
    # ---------------------------------------------------------------------

    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 5,
    ) -> SearchResults:
        """Search  channels by name"""
        radios: list[Radio] = []

        if MediaType.RADIO in media_types or not media_types:
            all_radios = [r async for r in self.get_library_radios()]
            q = search_query.lower()
            radios = [r for r in all_radios if q in (r.name or "").lower()][:limit]

        return SearchResults(radio=radios)

    # ---------------------------------------------------------------------
    # Stream resolution
    # ---------------------------------------------------------------------

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get stream details """
        if media_type != MediaType.RADIO:
            raise MediaNotFoundError("Unsupported media type for SR provider")

        params = {"format": "json"}
        async with self.mass.http_session.get(
            f"{self._base_api}/channels/{item_id}", params=params
        ) as resp:
            data = await resp.json()

        ch = data.get("channel") or {}
        liveaudio = ch.get("liveaudio") or {}
        url = liveaudio.get("url")
        if not url:
            raise MediaNotFoundError("No live audio URL for this channel")

        return StreamDetails(
            provider=self.domain,
            item_id=str(item_id),
            media_type=media_type,
            stream_type=StreamType.HTTP,
            path=url,
            audio_format=AudioFormat(content_type=ContentType.MP3),
            can_seek=False,
            allow_seek=False,
        )

    # ---------------------------------------------------------------------
    # Helper
    # ---------------------------------------------------------------------

    def _provider_mapping_for_id(self, item_id: str):
        """Build ProviderMapping set """
        from music_assistant_models.media_items import ProviderMapping

        return {
            ProviderMapping(
                item_id=str(item_id),
                provider_domain=self.domain,
                provider_instance=self.instance_id,
            )
        }

    @property
    def is_streaming_provider(self) -> bool:
        return True
