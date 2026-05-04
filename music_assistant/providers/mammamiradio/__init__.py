"""mammamiradio music provider support for MusicAssistant.

mammamiradio is a self-hosted, AI-generated continuous Italian radio station
with banter, music, and ads. This provider exposes the user's mammamiradio
HA addon as a single Radio entry inside Music Assistant.

See: https://github.com/florianhorner/mammamiradio
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import aiohttp
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import MediaNotFoundError, ProviderUnavailableError
from music_assistant_models.media_items import (
    AudioFormat,
    BrowseFolder,
    ItemMapping,
    MediaItemMetadata,
    MediaItemType,
    ProviderMapping,
    Radio,
    SearchResults,
    UniqueList,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from collections.abc import Sequence

    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


SUPPORTED_FEATURES = {
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
}

CONF_MAMMAMIRADIO_URL = "mammamiradio_url"
DEFAULT_URL = "http://localhost:8100"
RADIO_ITEM_ID = "mammamiradio"
RADIO_NAME = "mammamiradio"
RADIO_DESCRIPTION = (
    "AI-generated Italian radio station — continuous music, banter, and ads. "
    "Self-hosted via the mammamiradio HA addon."
)
REACHABILITY_TIMEOUT = 5


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return MammamiradioProvider(mass, manifest, config, SUPPORTED_FEATURES)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    # ruff: noqa: ARG001
    return (
        ConfigEntry(
            key=CONF_MAMMAMIRADIO_URL,
            type=ConfigEntryType.STRING,
            label="mammamiradio URL",
            required=True,
            default_value=DEFAULT_URL,
            description=(
                "URL of your mammamiradio HA addon (default matches the stock addon configuration)."
            ),
        ),
    )


class MammamiradioProvider(MusicProvider):
    """Provider implementation for mammamiradio."""

    @property
    def is_streaming_provider(self) -> bool:
        """Return True if the provider is a streaming provider."""
        # mammamiradio is an external (self-hosted) audio source whose catalog
        # is fixed (one Radio entry); treat it like other internet-radio
        # providers (SomaFM, RadioBrowser).
        return True

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider.

        Performs a non-fatal reachability check against the configured URL.
        If the addon is unreachable, the provider stays loaded so the user
        can fix the URL in the UI; the error is surfaced when the user
        actually tries to play the stream (see ``get_stream_details``).
        """
        url = self._stream_url_root()
        try:
            timeout = aiohttp.ClientTimeout(total=REACHABILITY_TIMEOUT)
            async with self.mass.http_session.get(
                f"{url}/api/capabilities", timeout=timeout
            ) as response:
                if response.status >= 400:
                    self.logger.warning(
                        "mammamiradio reachability check returned HTTP %s for %s; "
                        "the provider is loaded but playback may fail until the "
                        "addon is reachable.",
                        response.status,
                        url,
                    )
                else:
                    self.logger.info("mammamiradio addon reachable at %s", url)
        except (aiohttp.ClientError, TimeoutError) as err:
            self.logger.warning(
                "mammamiradio addon unreachable at %s: %s; "
                "the provider is loaded but playback may fail until the "
                "addon is reachable.",
                url,
                err,
            )

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """Browse this provider's items.

        mammamiradio exposes a single Radio object; ignore the path and
        always return the one entry.
        """
        return [self._build_radio()]

    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 5,
    ) -> SearchResults:
        """Perform search on the mammamiradio entry.

        Matches the substring of "mammamiradio" (case-insensitive) against
        the search query.
        """
        results = SearchResults()
        if MediaType.RADIO not in media_types:
            return results
        search_query_lower = search_query.lower().strip()
        if not search_query_lower:
            return results
        if search_query_lower in RADIO_NAME.lower():
            results.radio = [self._build_radio()]
        return results

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """Get full radio details by id."""
        if prov_radio_id != RADIO_ITEM_ID:
            msg = f"mammamiradio: radio station {prov_radio_id} not found"
            raise MediaNotFoundError(msg)
        return self._build_radio()

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get stream details for the mammamiradio radio entry."""
        if item_id != RADIO_ITEM_ID:
            msg = f"mammamiradio: radio station {item_id} not found"
            raise MediaNotFoundError(msg)

        url_root = self._stream_url_root()
        stream_path = f"{url_root}/stream"

        # Verify the stream endpoint is reachable; raise a clean
        # ProviderUnavailableError so MA reports "source unavailable"
        # rather than crashing inside ffmpeg.
        try:
            timeout = aiohttp.ClientTimeout(total=REACHABILITY_TIMEOUT)
            async with self.mass.http_session.head(
                stream_path, timeout=timeout, allow_redirects=True
            ) as response:
                if response.status >= 400:
                    msg = (
                        f"mammamiradio: stream endpoint returned HTTP "
                        f"{response.status} at {stream_path}"
                    )
                    raise ProviderUnavailableError(msg)
        except (aiohttp.ClientError, TimeoutError) as err:
            msg = f"mammamiradio: addon unreachable at {stream_path}: {err}"
            raise ProviderUnavailableError(msg) from err

        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(
                content_type=ContentType.MP3,
                bit_rate=128,
            ),
            media_type=MediaType.RADIO,
            stream_type=StreamType.HTTP,
            path=stream_path,
            allow_seek=False,
            can_seek=False,
        )

    def _stream_url_root(self) -> str:
        """Return the configured mammamiradio URL with any trailing slash trimmed."""
        url = str(self.config.get_value(CONF_MAMMAMIRADIO_URL) or DEFAULT_URL).strip()
        return url.rstrip("/")

    def _build_radio(self) -> Radio:
        """Construct the single Radio object for mammamiradio."""
        return Radio(
            provider=self.instance_id,
            item_id=RADIO_ITEM_ID,
            name=RADIO_NAME,
            metadata=MediaItemMetadata(
                description=RADIO_DESCRIPTION,
                genres={"AI Radio", "Italian"},
                languages=UniqueList(["it"]),
            ),
            provider_mappings={
                ProviderMapping(
                    item_id=RADIO_ITEM_ID,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    available=True,
                    audio_format=AudioFormat(
                        content_type=ContentType.MP3,
                        bit_rate=128,
                    ),
                )
            },
        )
