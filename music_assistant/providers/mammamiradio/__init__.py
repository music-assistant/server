"""mammamiradio music provider support for MusicAssistant.

mammamiradio is a self-hosted, AI-generated continuous Italian radio station
with banter, music, and ads. This provider exposes the user's mammamiradio
HA addon as a single Radio entry inside Music Assistant.

See: https://github.com/florianhorner/mammamiradio
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urlsplit, urlunsplit

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
DEFAULT_URL = "http://localhost:8000"
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

        Performs a reachability check against ``${url}/healthz`` and raises
        ``ProviderUnavailableError`` on failure. This is the canonical place
        for liveness detection in Music Assistant providers — matches the
        pattern in RadioBrowser (``stats()`` call) and surfaces a clean
        unavailable error to MA at provider load time rather than letting the
        stream URL fail silently inside ffmpeg later.

        Stream-time probing is intentionally absent (see ``get_stream_details``).
        """
        url = self._stream_url_root()
        try:
            timeout = aiohttp.ClientTimeout(total=REACHABILITY_TIMEOUT)
            async with self.mass.http_session.get(f"{url}/healthz", timeout=timeout) as response:
                if response.status >= 400:
                    msg = (
                        f"mammamiradio addon at {url} returned HTTP {response.status} "
                        f"on /healthz; the addon is reachable but unhealthy."
                    )
                    raise ProviderUnavailableError(msg)
                self.logger.info("mammamiradio addon reachable at %s", url)
        except (aiohttp.ClientError, TimeoutError) as err:
            msg = f"mammamiradio addon unreachable at {url}: {err}"
            raise ProviderUnavailableError(msg) from err

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
        """Get stream details for the mammamiradio radio entry.

        No stream-time HTTP probe. Liveness is checked at provider init via
        ``handle_async_init`` (probes ``/healthz``); stream failures from a
        running-but-broken addon surface naturally via MA's ffmpeg pipeline.
        This matches the dominant pattern across MA's live-radio providers
        (NTS, RadioBrowser, ORF Radiothek): probe at init, pass through at
        stream-details time.

        Probing the stream URL itself is counterproductive here: GET on an
        Icecast stream immediately starts pushing audio bytes which we never
        consume; HEAD returns 200 even when the source is offline (a known
        Icecast quirk). A dedicated ``/healthz`` endpoint is the only reliable
        liveness signal, and that check belongs at init.
        """
        if item_id != RADIO_ITEM_ID:
            msg = f"mammamiradio: radio station {item_id} not found"
            raise MediaNotFoundError(msg)

        url_root = self._stream_url_root()
        stream_path = f"{url_root}/stream"

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
        """Return the configured mammamiradio URL stripped of query, fragment, and trailing slash."""
        raw = str(self.config.get_value(CONF_MAMMAMIRADIO_URL) or DEFAULT_URL).strip()
        parts = urlsplit(raw)
        return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))

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
