"""
mammamiradio music provider support for MusicAssistant.

Mamma Mi Radio is a self-hosted, AI-generated continuous Italian radio station
with banter, music, and ads. This provider exposes the user's Mamma Mi Radio
HA addon as a single Radio entry inside Music Assistant.

Now-playing metadata is read from the addon's versioned consumer contract
``GET /api/integrations/v1/now-playing`` (addon >= 2.13). Older addons that
predate that endpoint fall back to the legacy ``/public-status`` payload.

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
from music_assistant_models.errors import (
    MediaNotFoundError,
    ProviderUnavailableError,
    SetupFailedError,
)
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
SUPPORTED_SCHEMA_VERSIONS = {"1"}

CONF_MAMMAMIRADIO_URL = "mammamiradio_url"
DEFAULT_URL = "http://localhost:8000"
RADIO_ITEM_ID = "mammamiradio"
RADIO_NAME = "Mamma Mi Radio"
RADIO_DESCRIPTION = (
    "AI-generated Italian radio station — continuous music, banter, and ads. "
    "Self-hosted via the Mamma Mi Radio HA addon."
)
REACHABILITY_TIMEOUT = 5
# How often Music Assistant invokes the live-metadata callback (seconds). 12s is
# imperceptible on a now-playing card and keeps per-listener poll load on
# mammamiradio's single-process addon modest.
STREAM_METADATA_UPDATE_INTERVAL = 12
# Short timeout for the metadata poll so a slow addon never eats most of the
# metadata update interval.
METADATA_TIMEOUT = 3

# Addon endpoint paths.
NOWPLAYING_PATH = "/api/integrations/v1/now-playing"
PUBLIC_STATUS_PATH = "/public-status"
HEALTHZ_PATH = "/healthz"
STREAM_PATH = "/stream"

# Published stream defaults (mammamiradio core AudioConfig) used when the v1
# contract is unavailable (older addon) or a format field is missing.
DEFAULT_CODEC = "mp3"
DEFAULT_BITRATE_KBPS = 192
DEFAULT_SAMPLE_RATE_HZ = 48000
DEFAULT_CHANNELS = 2

# Legacy /public-status mapping helpers (fallback path only).
# Music titles mammamiradio emits as placeholders — treated as "no title".
_PLACEHOLDER_TITLES = {"", "unknown", "untitled", "unknown title"}
# Segment types rendered as a generic "station element" on the now-playing card.
_STATION_ELEMENT_TYPES = {"station_id", "time_check", "sweeper"}
# Segment types that represent idle/internal state — no description pushed to MA.
_IDLE_TYPES = {"skipping", "stopped"}
# v1 segment_class values that represent an actively-playing segment (i.e. a
# segment for which an "Up next" description line is meaningful).
_V1_ACTIVE_CLASSES = {"music", "voice", "interstitial"}


def _clean_str(value: Any) -> str | None:
    """
    Coerce an untrusted addon value to a stripped non-empty str, else None.

    Fields expected to be text may arrive from JSON as a number, list, or null; this
    keeps ``StreamMetadata``'s mandatory ``str | None`` typing honest rather than
    letting a raw JSON value leak onto MA media surfaces.
    """
    if isinstance(value, str):
        return value.strip() or None
    return None


def _pos_int(value: Any, default: int) -> int:
    """Return ``value`` if it is a positive (non-bool) int, else ``default``."""
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return default


def _supports_v1_schema(value: Any) -> bool:
    """Return True when ``value`` identifies a now-playing v1 payload."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        value = str(value)
    return isinstance(value, str) and value.strip() in SUPPORTED_SCHEMA_VERSIONS


def _normalize_base_url(value: Any) -> str:
    """
    Normalize a configured base URL to ``scheme://host[:port][/path]``.

    Query strings, fragments, and userinfo are intentionally discarded so an
    admin token pasted into the setup field never reaches MA stream URLs,
    probe URLs, logs, or provider-unavailable errors. Trailing path slashes are
    stripped; a reverse-proxy path prefix is preserved.

    :param value: The raw configured value.
    :raises TypeError: if the value is not a string.
    :raises ValueError: if the value is not a full http(s) URL with a hostname.
    """
    if not isinstance(value, str):
        raise TypeError("base URL must be a string")
    raw = value.strip()
    if not raw:
        raise ValueError("base URL is empty")
    if any(ch.isspace() or not ch.isprintable() for ch in raw):
        raise ValueError("base URL contains whitespace or control characters")
    try:
        parts = urlsplit(raw)
        hostname = parts.hostname
        _ = parts.port  # a nonnumeric or out-of-range port raises ValueError
    except ValueError as err:
        msg = f"base URL is malformed: {err}"
        raise ValueError(msg) from err
    if parts.scheme not in ("http", "https"):
        raise ValueError("base URL must start with http:// or https://")
    if not hostname:
        raise ValueError("base URL has no hostname")
    netloc = parts.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parts.scheme, netloc, parts.path.rstrip("/"), "", ""))


def _stream_path_from_contract(value: Any) -> str:
    """Return a safe relative stream path from the v1 contract, else the default."""
    raw = _clean_str(value)
    if raw is None:
        return STREAM_PATH
    parts = urlsplit(raw)
    path = parts.path.rstrip("/")
    if parts.scheme or parts.netloc or not path.startswith("/") or not path:
        return STREAM_PATH
    return path


def _host_display_names(hosts: Any) -> str | None:
    """
    Join host display names from a list of host objects.

    The addon serialises ``brand.hosts`` / ``station.hosts`` as objects
    (``{engine_host, display_name, description}``); earlier code assumed a list of
    plain strings and silently dropped every host (so banter showed the station
    name instead of the hosts). This reads ``display_name`` (falling back to
    ``engine_host``) and tolerates the legacy bare-string shape.
    """
    if not isinstance(hosts, list):
        return None
    names: list[str] = []
    for host in hosts:
        if isinstance(host, dict):
            name = _clean_str(host.get("display_name")) or _clean_str(host.get("engine_host"))
        else:
            name = _clean_str(host)
        if name:
            names.append(name)
    return ", ".join(names) or None


def _audio_format_from_contract(fmt: Any) -> AudioFormat:
    """
    Build an ``AudioFormat`` from the v1 contract's ``stream.audio_format``.

    Falls back to the addon's published defaults (MP3 / 192 kbps / 48 kHz /
    stereo) when the contract is unavailable (older addon) or a field is
    missing/malformed, so MA always receives a coherent format.
    """
    fmt = fmt if isinstance(fmt, dict) else {}
    codec = _clean_str(fmt.get("codec")) or DEFAULT_CODEC
    content_type = ContentType.try_parse(codec)
    if content_type == ContentType.UNKNOWN:
        content_type = ContentType.MP3
    return AudioFormat(
        content_type=content_type,
        bit_rate=_pos_int(fmt.get("bitrate_kbps"), DEFAULT_BITRATE_KBPS),
        sample_rate=_pos_int(fmt.get("sample_rate_hz"), DEFAULT_SAMPLE_RATE_HZ),
        channels=_pos_int(fmt.get("channels"), DEFAULT_CHANNELS),
    )


def _v1_to_stream_metadata(payload: dict[str, Any], *, show_upcoming: bool) -> StreamMetadata:
    """
    Map a v1 ``/api/integrations/v1/now-playing`` response onto a ``StreamMetadata``.

    Total by construction (same invariant as the legacy mapper): every input
    yields a non-empty ``title``. The display bucket branches on the contract's
    stable ``segment_class`` (music / voice / interstitial / unavailable) rather
    than the raw ``segment_type``, so a future additive segment type renders as a
    clean station-name frame instead of leaking an unmapped label.

    The Home Assistant "A casa" mood line of the legacy ``/public-status`` payload
    has no v1 equivalent (it is intentionally excluded from the contract's
    metadata allowlist), so the v1 description carries only the typed "Up next"
    line.
    """
    station = payload.get("station")
    station = station if isinstance(station, dict) else {}
    station_name = _clean_str(station.get("name")) or RADIO_NAME

    now = payload.get("now_playing")
    now = now if isinstance(now, dict) else None
    up_next = payload.get("up_next")
    up_next = up_next if isinstance(up_next, list) else []

    title: str | None = None
    artist: str | None = None
    image_url: str | None = None
    # Album only carries a value for music segments; the addon serializer emits
    # album=None for voice/interstitial/unavailable, so mirror that (an ad should
    # not render the station name as its album).
    album: str | None = None
    seg_class: Any = None

    if now is not None:
        seg_class = now.get("segment_class")
        np_title = _clean_str(now.get("title"))
        if seg_class == "music":
            title = np_title
            artist = _clean_str(now.get("artist"))
            image_url = _clean_str(now.get("artwork"))
            album = _clean_str(now.get("album")) or station_name
        elif seg_class == "voice":
            title = np_title or "Host banter"
            artist = (
                _clean_str(now.get("host"))
                or _host_display_names(station.get("hosts"))
                or station_name
            )
        elif seg_class == "interstitial":
            title = np_title or station_name
            artist = station_name
        else:
            # "unavailable" or any future/unknown class -> idle station frame.
            title = station_name
    else:
        # session_state stopped / empty_queue -> nothing playing.
        title = station_name

    # Terminal title clamp — the invariant: title is always a non-empty str.
    title = _clean_str(title) or station_name

    description: str | None = None
    if now is not None and seg_class in _V1_ACTIVE_CLASSES and show_upcoming and up_next:
        first = up_next[0]
        # Skip an up-next entry that maps to the idle "unavailable" bucket so a
        # future additive segment type never surfaces a stale "Up next:" line.
        if isinstance(first, dict) and first.get("segment_class") != "unavailable":
            up_label = _clean_str(first.get("title"))
            if up_label:
                description = f"Up next: {up_label}"

    return StreamMetadata(
        title=title,
        artist=_clean_str(artist),
        album=_clean_str(album),
        image_url=image_url,
        description=description,
    )


def _segment_to_stream_metadata(
    now: dict[str, Any],
    upcoming: list[Any],
    ha: dict[str, Any],
    brand: dict[str, Any],
    *,
    show_upcoming: bool,
) -> StreamMetadata:
    """
    Map a legacy ``/public-status`` segment snapshot onto a ``StreamMetadata``.

    Total by construction: every input — an unknown segment ``type``, an empty
    ``now``, missing or wrong-typed metadata fields — yields a ``StreamMetadata``
    with a non-empty ``title`` (``StreamMetadata.title`` is a mandatory ``str``).
    Untrusted values are coerced via ``_clean_str`` / isinstance guards before
    use, and the terminal title clamp below is the load-bearing guarantee.

    ``description`` combines the typed "Up next" line and the Home Assistant
    "A casa" mood line. It is suppressed for idle/internal segments so
    mammamiradio's stopped/skipping state never reaches MA lock screens or
    speaker displays. Used only on the fallback path for addons that predate the
    v1 now-playing contract.
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
        artist = _host_display_names(brand.get("hosts")) or station_name
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
        # Album only carries a value for music segments, mirroring the v1 mapper
        # (an ad or banter break should not render the station name as its album).
        album=station_name if seg_type == "music" else None,
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
            required=True,
            default_value=DEFAULT_URL,
        ),
    )


class MammamiradioProvider(MusicProvider):
    """Provider implementation for mammamiradio."""

    # Selected in handle_async_init. Class-level defaults keep the accessors safe
    # if a method is somehow reached before init runs.
    _use_v1: bool = False
    _audio_format_dict: dict[str, Any] | None = None
    _stream_path: str = STREAM_PATH
    _base_url: str | None = None

    @property
    def is_streaming_provider(self) -> bool:
        """Return True if the provider is a streaming provider."""
        # mammamiradio is an external (self-hosted) audio source whose catalog
        # is fixed (one Radio entry); treat it like other internet-radio
        # providers (SomaFM, RadioBrowser).
        return True

    async def handle_async_init(self) -> None:
        """
        Probe the addon and select the now-playing contract.

        Prefers the addon's versioned consumer contract
        ``GET /api/integrations/v1/now-playing`` (addon >= 2.13): a 200 response
        confirms reachability AND publishes the stream audio format, which is
        cached for ``get_stream_details``. A 404 means an older addon without the
        v1 endpoint, so the provider falls back to the legacy ``/healthz``
        liveness probe and the ``/public-status`` metadata path. An unreachable
        or unhealthy addon raises ``ProviderUnavailableError`` so MA surfaces a
        clean error at load time instead of failing later inside ffmpeg.
        """
        url = self._stream_url_root()
        payload = await self._probe_now_playing(url)
        if payload is not None:
            self._use_v1 = True
            stream = payload.get("stream")
            stream = stream if isinstance(stream, dict) else {}
            audio_format = stream.get("audio_format")
            self._audio_format_dict = audio_format if isinstance(audio_format, dict) else None
            self._stream_path = _stream_path_from_contract(stream.get("relative_url"))
            self.logger.info("mammamiradio v1 now-playing contract reachable at %s", url)
            return
        self._use_v1 = False
        self._stream_path = STREAM_PATH
        await self._probe_healthz(url)
        self.logger.info("mammamiradio addon reachable at %s (legacy /public-status mode)", url)

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Browse this provider's items.

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
        """
        Perform search on the Mamma Mi Radio entry.

        Matches the query (case-insensitive) against either the display name or
        the provider slug, so both "mamma mi radio" and "mammamiradio" find it.
        """
        results = SearchResults()
        if MediaType.RADIO not in media_types:
            return results
        search_query_lower = search_query.lower().strip()
        if not search_query_lower:
            return results
        if search_query_lower in RADIO_NAME.lower() or search_query_lower in RADIO_ITEM_ID:
            results.radio = [self._build_radio()]
        return results

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """Get full radio details by id."""
        if prov_radio_id != RADIO_ITEM_ID:
            msg = f"mammamiradio: radio station {prov_radio_id} not found"
            raise MediaNotFoundError(msg)
        return self._build_radio()

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """
        Get stream details for the mammamiradio radio entry.

        No stream-time HTTP probe. Liveness is checked at provider init (see
        ``handle_async_init``), matching MA's live-radio providers (NTS,
        RadioBrowser, ORF Radiothek): probe at init, pass through at
        stream-details time. The audio format is taken from the v1 contract's
        ``stream.audio_format`` cached at init (or the published defaults for an
        older addon) rather than hard-coded, so the bitrate/sample-rate MA sees
        match what the addon actually serves.

        Live now-playing metadata is delivered separately, after the stream is
        already resolved: ``stream_metadata_update_callback`` is invoked by MA's
        player-queue controller every ``STREAM_METADATA_UPDATE_INTERVAL`` seconds
        (see ``_update_stream_metadata``). No HTTP call is made here. The first
        metadata frame arrives within one update interval; until then MA shows
        the static Radio item info.
        """
        if item_id != RADIO_ITEM_ID:
            msg = f"mammamiradio: radio station {item_id} not found"
            raise MediaNotFoundError(msg)

        url_root = self._stream_url_root()

        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=self._audio_format(),
            media_type=MediaType.RADIO,
            stream_type=StreamType.HTTP,
            path=f"{url_root}{self._stream_path}",
            allow_seek=False,
            can_seek=False,
            stream_metadata_update_callback=self._update_stream_metadata,
            stream_metadata_update_interval=STREAM_METADATA_UPDATE_INTERVAL,
        )

    async def _update_stream_metadata(
        self, stream_details: StreamDetails, elapsed_time: int
    ) -> None:
        """
        Refresh now-playing metadata mid-stream (invoked every interval by MA).

        Dispatches to the v1 ``/api/integrations/v1/now-playing`` contract when
        the addon exposes it (detected at init), else the legacy
        ``/public-status`` path. Any failure is swallowed so a transient addon
        hiccup never raises into MA's metadata-callback task; the prior frame
        stays in place.

        :param stream_details: StreamDetails object to update with metadata.
        :param elapsed_time: Elapsed playback time in seconds (unused).
        """
        if stream_details.data is None:
            stream_details.data = {}
        # Namespace our per-stream state so it can never collide with keys MA core
        # stashes in StreamDetails.data (e.g. hls_media_playlist_url for HLS).
        state = stream_details.data.setdefault("mammamiradio", {})
        if self._use_v1:
            await self._update_from_v1(stream_details, state)
        else:
            await self._update_from_public_status(stream_details, state)

    async def _update_from_v1(self, stream_details: StreamDetails, data: dict[str, Any]) -> None:
        """
        v1 metadata path: poll the now-playing contract and map the response.

        Sends a conditional request (``If-None-Match`` with the stored weak
        ETag); a 304 reuses the cached payload so the now/up-next alternation
        keeps advancing without re-downloading or re-parsing the body.
        """
        payload = await self._fetch_now_playing(data)
        if payload is None:
            return

        # Detect segment changes via a stable per-segment identity, NOT the
        # contract's ``changed_at`` clock: the addon advances ``changed_at`` on any
        # state change (e.g. a queue-append that fills the lookahead buffer
        # mid-segment), which would otherwise snap the alternation back to "Now"
        # mid-segment. ``changed_at`` is also structurally optional in the contract.
        now = payload.get("now_playing")
        now = now if isinstance(now, dict) else {}
        # Key on fields the v1 contract actually emits (segment_type / title /
        # artist / host); started_at is absent from current payloads but kept so
        # an addon that later adds it (additive within v1.*) gains true
        # per-segment identity without a provider change.
        seg_key = (
            now.get("segment_type"),
            now.get("title"),
            now.get("artist"),
            now.get("host"),
            now.get("started_at"),
        )
        if seg_key != data.get("v1_segment"):
            data["v1_segment"] = seg_key
            data["show_upcoming"] = False

        show_upcoming = data.get("show_upcoming", False)
        try:
            stream_details.stream_metadata = _v1_to_stream_metadata(
                payload, show_upcoming=show_upcoming
            )
        except Exception as err:
            # No-raise contract: a malformed payload must never escape into MA's
            # metadata-callback task. Keep the prior frame; retry next tick.
            self.logger.debug("mammamiradio v1 metadata mapping failed: %s", err)
            return
        data["show_upcoming"] = not show_upcoming

    async def _update_from_public_status(
        self, stream_details: StreamDetails, data: dict[str, Any]
    ) -> None:
        """Legacy metadata path: poll ``/public-status`` (addons < 2.13)."""
        payload = await self._fetch_public_status()
        if payload is None:
            return

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

    async def _probe_now_playing(self, url: str) -> dict[str, Any] | None:
        """
        Probe the v1 now-playing endpoint.

        Returns the parsed response on a 2xx, or ``None`` when the v1 contract is
        not usable — an absent route (404/405/501 on an older addon), a transient
        ingress/addon 5xx (the contract always answers 200 in healthy operation),
        or a non-JSON body. In every ``None`` case the caller falls back to the
        ``/healthz`` liveness gate, so a momentary v1 hiccup never permanently
        fails provider load. Only an unreachable addon (connection error/timeout)
        raises ``ProviderUnavailableError``.
        """
        endpoint = f"{url}{NOWPLAYING_PATH}"
        try:
            timeout = aiohttp.ClientTimeout(total=REACHABILITY_TIMEOUT)
            async with self.mass.http_session.get(endpoint, timeout=timeout) as response:
                if response.status >= 400:
                    self.logger.debug(
                        "mammamiradio v1 now-playing probe returned HTTP %s; "
                        "falling back to /healthz",
                        response.status,
                    )
                    return None
                payload = await response.json()
                if not isinstance(payload, dict):
                    return None
                if not _supports_v1_schema(payload.get("schema_version")):
                    self.logger.debug(
                        "mammamiradio v1 now-playing probe returned unsupported "
                        "schema_version %r; falling back to /healthz",
                        payload.get("schema_version"),
                    )
                    return None
                return payload
        # ContentTypeError subclasses ClientError but means the endpoint answered
        # with a non-JSON content-type, so it must be caught first: it demotes to
        # the /healthz fallback rather than raising ProviderUnavailableError.
        except (aiohttp.ContentTypeError, ValueError, TypeError) as err:
            self.logger.debug("mammamiradio v1 now-playing probe returned non-JSON: %s", err)
            return None
        except (aiohttp.ClientError, TimeoutError) as err:
            msg = f"mammamiradio addon unreachable at {url}: {err}"
            raise ProviderUnavailableError(msg) from err

    async def _probe_healthz(self, url: str) -> None:
        """
        Liveness probe for older addons without the v1 contract.

        Raises ``ProviderUnavailableError`` if ``/healthz`` is unreachable or
        reports unhealthy (HTTP >= 400).
        """
        endpoint = f"{url}{HEALTHZ_PATH}"
        try:
            timeout = aiohttp.ClientTimeout(total=REACHABILITY_TIMEOUT)
            async with self.mass.http_session.get(endpoint, timeout=timeout) as response:
                if response.status >= 400:
                    msg = (
                        f"mammamiradio addon at {url} returned HTTP {response.status} "
                        f"on /healthz; reachable but unhealthy."
                    )
                    raise ProviderUnavailableError(msg)
        except (aiohttp.ClientError, TimeoutError) as err:
            msg = f"mammamiradio addon unreachable at {url}: {err}"
            raise ProviderUnavailableError(msg) from err

    async def _fetch_now_playing(self, data: dict[str, Any]) -> dict[str, Any] | None:
        """
        GET the v1 now-playing contract, returning the payload or None on failure.

        Uses the stored weak ETag for a conditional request: on 304 the cached
        payload is returned unchanged; on 200 the fresh payload and its ETag are
        cached for the next tick.
        """
        url = f"{self._stream_url_root()}{NOWPLAYING_PATH}"
        headers: dict[str, str] = {}
        etag = data.get("v1_etag")
        if isinstance(etag, str):
            headers["If-None-Match"] = etag
        try:
            timeout = aiohttp.ClientTimeout(total=METADATA_TIMEOUT)
            async with self.mass.http_session.get(
                url, headers=headers, timeout=timeout
            ) as response:
                if response.status == 304:
                    cached = data.get("v1_last")
                    return cached if isinstance(cached, dict) else None
                if response.status >= 400:
                    self.logger.debug(
                        "mammamiradio v1 now-playing returned HTTP %s", response.status
                    )
                    return None
                payload = await response.json()
                if not isinstance(payload, dict):
                    return None
                if not _supports_v1_schema(payload.get("schema_version")):
                    self.logger.debug(
                        "mammamiradio v1 now-playing returned unsupported schema_version %r",
                        payload.get("schema_version"),
                    )
                    return None
                new_etag = response.headers.get("ETag")
                if isinstance(new_etag, str):
                    data["v1_etag"] = new_etag
                else:
                    # The server stopped emitting ETags: drop the stored validator
                    # so polling actually becomes unconditional, as documented.
                    data.pop("v1_etag", None)
                data["v1_last"] = payload
                return payload
        except (aiohttp.ClientError, TimeoutError) as err:
            self.logger.debug("mammamiradio v1 now-playing request failed: %s", err)
            return None
        except (ValueError, TypeError) as err:
            self.logger.debug("mammamiradio v1 now-playing returned bad JSON: %s", err)
            return None

    async def _fetch_public_status(self) -> dict[str, Any] | None:
        """GET ``/public-status``, returning the parsed payload or None on any failure."""
        url = f"{self._stream_url_root()}{PUBLIC_STATUS_PATH}"
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

    def _audio_format(self) -> AudioFormat:
        """Return the shared stream AudioFormat (v1 contract if known, else defaults)."""
        return _audio_format_from_contract(self._audio_format_dict)

    def _stream_url_root(self) -> str:
        """
        Return the validated base URL of the configured addon (cached per instance).

        Normalized once on first use and cached for the instance's lifetime, so an
        already-resolved stream and its bound metadata callback stay on one
        endpoint even if ``self.config`` is replaced while a config-change reload
        is pending. Raises a provider-localized ``SetupFailedError`` before any
        HTTP request when the configured value is not a full http(s) URL with a
        hostname; the error intentionally never echoes the raw input.
        """
        if self._base_url is None:
            raw = self.config.get_value(CONF_MAMMAMIRADIO_URL)
            try:
                self._base_url = _normalize_base_url(DEFAULT_URL if raw is None else raw)
            except (TypeError, ValueError) as err:
                msg = "mammamiradio: invalid base URL configured; enter a full http(s):// URL"
                raise SetupFailedError(
                    msg,
                    translation_key="invalid_base_url",
                    translation_owner=self.translation_owner,
                ) from err
        return self._base_url

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
                    audio_format=self._audio_format(),
                )
            },
        )
