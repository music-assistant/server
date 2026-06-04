"""mammamiradio music provider support for MusicAssistant.

mammamiradio is a self-hosted, AI-generated continuous Italian radio station
with banter, music, and ads. This provider exposes the user's mammamiradio
HA addon as a single Radio entry inside Music Assistant.

See: https://github.com/florianhorner/mammamiradio
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
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
from music_assistant_models.streamdetails import StreamDetails, StreamMetadata

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
# How often Music Assistant invokes the live-metadata callback (seconds). 12s is
# imperceptible on a now-playing card and keeps per-listener poll load on
# mammamiradio's single-process addon modest.
STREAM_METADATA_UPDATE_INTERVAL = 12
# Short timeout for the /public-status poll so a slow addon never eats most of
# the metadata update interval.
METADATA_TIMEOUT = 3

# Music titles mammamiradio emits as placeholders — treated as "no title".
_PLACEHOLDER_TITLES = {"", "unknown", "untitled", "unknown title"}
# Segment types rendered as a generic "station element" on the now-playing card.
_STATION_ELEMENT_TYPES = {"station_id", "time_check", "sweeper"}
# Segment types that represent idle/internal state — no description pushed to MA.
_IDLE_TYPES = {"skipping", "stopped"}


def _clean_str(value: Any) -> str | None:
    """Coerce an untrusted ``/public-status`` value to a stripped non-empty str, else None.

    Fields expected to be text may arrive from JSON as a number, list, or null; this
    keeps ``StreamMetadata``'s mandatory ``str | None`` typing honest rather than
    letting a raw JSON value leak onto MA media surfaces.
    """
    if isinstance(value, str):
        return value.strip() or None
    return None


def _segment_to_stream_metadata(
    now: dict[str, Any],
    upcoming: list[Any],
    ha: dict[str, Any],
    brand: dict[str, Any],
    *,
    show_upcoming: bool,
) -> StreamMetadata:
    """Map a ``/public-status`` segment snapshot onto a ``StreamMetadata``.

    Total by construction: every input — an unknown segment ``type``, an empty
    ``now``, missing or wrong-typed metadata fields — yields a ``StreamMetadata``
    with a non-empty ``title`` (``StreamMetadata.title`` is a mandatory ``str``).
    Untrusted ``/public-status`` values are coerced via ``_clean_str`` / isinstance
    guards before use, and the terminal title clamp below is the load-bearing
    guarantee.

    ``description`` combines the typed "Up next" line and the Home Assistant
    "A casa" mood line rather than alternating them, so a single glance at the
    now-playing card surfaces both. It is suppressed for idle/internal segments
    so mammamiradio's stopped/skipping state never reaches MA lock screens or
    speaker displays.
    """
    station_name = _clean_str(brand.get("station_name")) or RADIO_NAME
    seg_type = now.get("type")
    label = _clean_str(now.get("label")) or ""
    meta = now.get("metadata")
    meta = meta if isinstance(meta, dict) else {}

    title: str | None = None
    artist: str | None = None
    image_url: str | None = None

    if seg_type == "music":
        raw_title = _clean_str(meta.get("title_only")) or _clean_str(meta.get("title")) or ""
        if raw_title.lower() in _PLACEHOLDER_TITLES:
            raw_title = ""
        title = raw_title
        artist = meta.get("artist")
        image_url = meta.get("album_art")
    elif seg_type == "banter":
        title = label or "Host banter"
        hosts = brand.get("hosts")
        host_names = [h for h in hosts if isinstance(h, str)] if isinstance(hosts, list) else []
        artist = ", ".join(host_names) or station_name
    elif seg_type == "ad":
        title = label or "Ad break"
        artist = "Pubblicità"
    elif seg_type == "news_flash":
        title = label or "News flash"
        artist = _clean_str(meta.get("host")) or station_name
    elif seg_type in _STATION_ELEMENT_TYPES:
        title = label or station_name
        artist = station_name
    else:
        # skipping / stopped / empty now_streaming / any unrecognized type.
        title = station_name
        artist = ""

    # Terminal title clamp — the invariant: title is always a non-empty str.
    title = str(title or "").strip() or label.strip() or station_name

    description: str | None = None
    if seg_type is not None and seg_type not in _IDLE_TYPES:
        parts: list[str] = []
        if show_upcoming and upcoming:
            first = upcoming[0]
            up_label = _clean_str(first.get("label")) if isinstance(first, dict) else None
            if up_label:
                parts.append(f"Up next: {up_label}")
        casa = _clean_str(ha.get("mood")) or _clean_str(ha.get("weather"))
        if casa:
            parts.append(f"A casa: {casa}")
        description = " · ".join(parts) or None

    return StreamMetadata(
        title=title,
        artist=_clean_str(artist),
        album=station_name,
        image_url=_clean_str(image_url),
        description=description,
    )


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

        Live now-playing metadata is delivered separately, after the stream is
        already resolved: ``stream_metadata_update_callback`` is invoked by MA's
        player-queue controller every ``STREAM_METADATA_UPDATE_INTERVAL`` seconds
        (see ``_update_stream_metadata``). No HTTP call is made here — the
        no-probe contract above holds for metadata too. The first metadata frame
        arrives within one update interval; until then MA shows the static Radio
        item info.
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
            stream_metadata_update_callback=self._update_stream_metadata,
            stream_metadata_update_interval=STREAM_METADATA_UPDATE_INTERVAL,
        )

    async def _update_stream_metadata(
        self, stream_details: StreamDetails, elapsed_time: int
    ) -> None:
        """Refresh now-playing metadata mid-stream from mammamiradio's /public-status.

        Invoked by Music Assistant's player-queue controller every
        ``STREAM_METADATA_UPDATE_INTERVAL`` seconds. Polls the unauthenticated
        ``/public-status`` endpoint, maps the current typed segment onto a
        ``StreamMetadata``, and alternates the now/up-next view. Any failure is
        swallowed (logged at debug) so a transient addon hiccup never raises
        inside the callback or disturbs playback; the prior metadata stays in
        place.

        :param stream_details: StreamDetails object to update with metadata.
        :param elapsed_time: Elapsed playback time in seconds (unused).
        """
        payload = await self._fetch_public_status()
        if payload is None:
            return

        if stream_details.data is None:
            stream_details.data = {}
        data = stream_details.data

        # Type-guard each field, not just absence: ``/public-status`` is untrusted
        # JSON, and a truthy non-dict ``now_streaming`` would reach the ``seg_key``
        # computation below (outside the try/except) and raise into MA's task.
        now = payload.get("now_streaming")
        now = now if isinstance(now, dict) else {}
        upcoming = payload.get("upcoming")
        upcoming = upcoming if isinstance(upcoming, list) else []
        ha = payload.get("ha_moments")
        ha = ha if isinstance(ha, dict) else {}
        brand = payload.get("brand")
        brand = brand if isinstance(brand, dict) else {}

        # Stable per-segment identity. ``epoch``/``started`` alone may be absent
        # or unstable for non-music segments, so key change-detection on a
        # composite tuple.
        seg_key = (now.get("type"), now.get("label"), now.get("started"))
        if seg_key != data.get("last_segment"):
            data["last_segment"] = seg_key
            data["show_upcoming"] = False

        # Read the display mode BEFORE mutating, so the first frame of every
        # segment renders the "Now" view (mutate-then-read would flip this).
        show_upcoming = data.get("show_upcoming", False)
        try:
            stream_details.stream_metadata = _segment_to_stream_metadata(
                now, upcoming, ha, brand, show_upcoming=show_upcoming
            )
        except Exception as err:
            # No-raise contract: a malformed /public-status payload must never escape
            # into MA's metadata-callback task. Keep the prior frame; retry next tick.
            self.logger.debug("mammamiradio metadata mapping failed: %s", err)
            return
        data["show_upcoming"] = not show_upcoming

    async def _fetch_public_status(self) -> dict[str, Any] | None:
        """GET ``/public-status``, returning the parsed payload or None on any failure."""
        url = f"{self._stream_url_root()}/public-status"
        try:
            timeout = aiohttp.ClientTimeout(total=METADATA_TIMEOUT)
            async with self.mass.http_session.get(url, timeout=timeout) as response:
                if response.status >= 400:
                    self.logger.debug(
                        "mammamiradio /public-status returned HTTP %s", response.status
                    )
                    return None
                payload = await response.json()
                if not isinstance(payload, dict):
                    self.logger.debug(
                        "mammamiradio /public-status returned non-object JSON (%s)",
                        type(payload).__name__,
                    )
                    return None
                return payload or None
        except (aiohttp.ClientError, TimeoutError) as err:
            self.logger.debug("mammamiradio /public-status request failed: %s", err)
            return None
        except (ValueError, TypeError) as err:
            self.logger.debug("mammamiradio /public-status returned bad JSON: %s", err)
            return None

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
