"""
Mamma Mi Radio music provider for Music Assistant.

Exposes a self-hosted Mamma Mi Radio HA addon as a single Radio entry. Live
now-playing metadata is read from the addon's versioned consumer contract
``GET /api/integrations/v1/now-playing``; the provider requires addon 2.13
or newer.

See: https://github.com/florianhorner/mammamiradio
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

import aiohttp
from music_assistant_models.enums import (
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

    from music_assistant_models.config_entries import ProviderConfig
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
    "Two Italian hosts. One very opinionated smart home. Self-hosted radio for "
    "Home Assistant: music, banter, and ads, with your home's moments worked "
    "into the show."
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
STREAM_PATH = "/stream"

# Published stream defaults (mammamiradio core AudioConfig) used when a format
# field is missing from the contract.
DEFAULT_CODEC = "mp3"
DEFAULT_BITRATE_KBPS = 192
DEFAULT_SAMPLE_RATE_HZ = 48000
DEFAULT_CHANNELS = 2

# v1 segment_class values that represent an actively-playing segment (i.e. a
# segment for which an "Up next" description line is meaningful).
_V1_ACTIVE_CLASSES = {"music", "voice", "interstitial"}


def _clean_str(value: Any) -> str | None:
    """Return ``value`` as a stripped non-empty string, or None."""
    if isinstance(value, str):
        return value.strip() or None
    return None


def _pos_int(value: Any, default: int) -> int:
    """Return ``value`` if it is a positive (non-bool) int, else ``default``."""
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return default


def _supports_v1_schema(value: Any) -> bool:
    """Return True if ``value`` identifies a supported now-playing schema version."""
    return isinstance(value, str) and value.strip() in SUPPORTED_SCHEMA_VERSIONS


def _normalize_base_url(value: Any) -> str:
    """
    Normalize a configured base URL to ``scheme://host[:port][/path]``.

    Query strings, fragments, and userinfo are discarded; a reverse-proxy path
    prefix is preserved.

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
    # urlsplit accepts characters aiohttp later rejects (e.g. a backslash);
    # rejecting them here keeps that mistake a localized setup error instead
    # of a misleading probe failure.
    if not re.fullmatch(r"[A-Za-z0-9._\-:\[\]]+", netloc):
        raise ValueError("base URL host contains invalid characters")
    return urlunsplit((parts.scheme, netloc, parts.path.rstrip("/"), "", ""))


def _stream_path_from_contract(value: Any) -> str:
    """Return a safe relative stream path from the v1 contract, else the default."""
    raw = _clean_str(value)
    if raw is None:
        return STREAM_PATH
    try:
        parts = urlsplit(raw)
    except ValueError:
        # e.g. an invalid IPv6-looking value; a malformed contract field must
        # never escape init as a non-MusicAssistantError (that would skip
        # MA's automatic load retry).
        return STREAM_PATH
    path = parts.path.rstrip("/")
    if parts.scheme or parts.netloc or not path.startswith("/") or not path:
        return STREAM_PATH
    return path


def _host_display_names(hosts: Any) -> str | None:
    """Join host display names from the contract's list of host objects."""
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
    """Build an ``AudioFormat`` from ``stream.audio_format``, with published defaults."""
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
    Map a v1 now-playing payload onto a ``StreamMetadata``.

    :param payload: The parsed now-playing response.
    :param show_upcoming: Render the "Up next" frame instead of the "Now" frame.
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
    # Album only applies to music segments.
    album: str | None = None
    seg_class: Any = None

    if now is not None:
        # Only string classes are meaningful; a non-str value must not reach the
        # set-membership test below (unhashable types would raise).
        seg_class = now.get("segment_class")
        seg_class = seg_class if isinstance(seg_class, str) else None
        np_title = _clean_str(now.get("title"))
        if seg_class == "music":
            title = np_title
            artist = _clean_str(now.get("artist"))
            image_url = _clean_str(now.get("artwork"))
            album = _clean_str(now.get("album"))
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
            # "unavailable" or any future class: show a plain station frame.
            title = station_name
    else:
        # session_state stopped / empty_queue: nothing playing.
        title = station_name

    # Ensure title is never empty.
    title = _clean_str(title) or station_name

    # Only http(s) artwork with a host may reach MA media surfaces.
    if image_url is not None:
        try:
            art = urlsplit(image_url)
        except ValueError:
            image_url = None
        else:
            if art.scheme.lower() not in ("http", "https") or not art.netloc:
                image_url = None

    description: str | None = None
    if now is not None and seg_class in _V1_ACTIVE_CLASSES and show_upcoming and up_next:
        first = up_next[0]
        # Skip idle "unavailable" up-next entries.
        if isinstance(first, dict) and first.get("segment_class") != "unavailable":
            up_label = _clean_str(first.get("title"))
            if up_label:
                description = f"Up next: {up_label}"

    return StreamMetadata(
        title=title,
        artist=_clean_str(artist),
        album=album,
        image_url=image_url,
        description=description,
    )


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return MammamiradioProvider(mass, manifest, config, SUPPORTED_FEATURES)


class MammamiradioProvider(MusicProvider):
    """Provider implementation for mammamiradio."""

    # All values are set in handle_async_init.
    _base_url: str
    _audio_format_dict: dict[str, Any] | None
    _stream_path: str

    @property
    def max_concurrent_streams(self) -> None:
        """Allow unlimited concurrent upstream source streams."""
        return None

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        raw = self.get_setup_value(CONF_MAMMAMIRADIO_URL)
        try:
            self._base_url = _normalize_base_url(DEFAULT_URL if raw is None else raw)
        except (TypeError, ValueError) as err:
            msg = "invalid base URL configured; enter a full http(s):// URL"
            raise SetupFailedError(
                msg,
                translation_key="invalid_base_url",
                translation_owner=self.translation_owner,
            ) from err
        payload = await self._probe_now_playing()
        stream = payload.get("stream")
        stream = stream if isinstance(stream, dict) else {}
        audio_format = stream.get("audio_format")
        self._audio_format_dict = audio_format if isinstance(audio_format, dict) else None
        self._stream_path = _stream_path_from_contract(stream.get("relative_url"))
        self.logger.info("now-playing contract reachable at %s", self._base_url)

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        await super().loaded_in_mass()
        await self.mass.music.add_item_to_library(self._build_radio())

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """Browse this provider's items."""
        # mammamiradio exposes exactly one Radio entry; the path is irrelevant.
        return [self._build_radio()]

    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 5,
    ) -> SearchResults:
        """Perform search on the single Mamma Mi Radio entry."""
        results = SearchResults()
        if MediaType.RADIO not in media_types:
            return results
        search_query_lower = search_query.lower().strip()
        if not search_query_lower:
            return results
        # Match both the display name and the provider slug.
        if search_query_lower in RADIO_NAME.lower() or search_query_lower in RADIO_ITEM_ID:
            results.radio = [self._build_radio()]
        return results

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """Get full radio details by id."""
        if prov_radio_id != RADIO_ITEM_ID:
            msg = f"radio station {prov_radio_id} not found"
            raise MediaNotFoundError(msg)
        return self._build_radio()

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Return the streamdetails for the mammamiradio radio stream."""
        if item_id != RADIO_ITEM_ID:
            msg = f"radio station {item_id} not found"
            raise MediaNotFoundError(msg)
        # Liveness was checked at init; no probe at stream time.
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=self._audio_format(),
            media_type=MediaType.RADIO,
            stream_type=StreamType.HTTP,
            path=f"{self._base_url}{self._stream_path}",
            allow_seek=False,
            can_seek=False,
            stream_metadata_update_callback=self._update_stream_metadata,
            stream_metadata_update_interval=STREAM_METADATA_UPDATE_INTERVAL,
        )

    async def _update_stream_metadata(
        self, stream_details: StreamDetails, elapsed_time: int
    ) -> None:
        """
        Refresh now-playing metadata for the active stream.

        :param stream_details: StreamDetails object to update with metadata.
        :param elapsed_time: Elapsed playback time in seconds (unused).
        """
        if stream_details.data is None:
            stream_details.data = {}
        # Namespace our per-stream state so it can never collide with keys MA core
        # stashes in StreamDetails.data (e.g. hls_media_playlist_url for HLS).
        data = stream_details.data.setdefault("mammamiradio", {})
        payload = await self._fetch_now_playing(data)
        if payload is None:
            return

        # Detect segment changes via a stable per-segment identity, not the
        # contract's changed_at clock: the addon advances changed_at on any state
        # change (e.g. a queue append mid-segment), which would snap the
        # alternation back to "Now" mid-segment.
        now = payload.get("now_playing")
        now = now if isinstance(now, dict) else {}
        # started_at is a stable per-segment start timestamp when the addon
        # knows it (None otherwise); including it gives true per-segment identity.
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

        # Read the display mode before flipping it, so the first frame of every
        # segment renders the "Now" view.
        show_upcoming = data.get("show_upcoming", False)
        stream_details.stream_metadata = _v1_to_stream_metadata(
            payload, show_upcoming=show_upcoming
        )
        data["show_upcoming"] = not show_upcoming

    async def _probe_now_playing(self) -> dict[str, Any]:
        """
        Probe the v1 now-playing endpoint and return its payload.

        :raises ProviderUnavailableError: if the addon is unreachable, unhealthy,
            or does not expose a supported v1 now-playing contract (addon 2.13+).
        """
        endpoint = f"{self._base_url}{NOWPLAYING_PATH}"
        requires_msg = (
            f"Mamma Mi Radio addon at {self._base_url} does not expose the now-playing "
            "contract; this provider requires addon 2.13 or newer"
        )
        try:
            timeout = aiohttp.ClientTimeout(total=REACHABILITY_TIMEOUT)
            async with self.mass.http_session.get(endpoint, timeout=timeout) as response:
                if response.status in (404, 405, 501):
                    raise ProviderUnavailableError(requires_msg)
                if response.status >= 400:
                    msg = (
                        f"Mamma Mi Radio addon at {self._base_url} returned HTTP {response.status}"
                    )
                    raise ProviderUnavailableError(msg)
                payload = await response.json()
                if not isinstance(payload, dict):
                    raise ProviderUnavailableError(requires_msg)
                schema = payload.get("schema_version")
                if not _supports_v1_schema(schema):
                    if not isinstance(schema, str):
                        # No usable version field: treat as a pre-2.13 addon (or
                        # some other service answering on this port).
                        raise ProviderUnavailableError(requires_msg)
                    msg = (
                        f"Mamma Mi Radio addon at {self._base_url} publishes unsupported "
                        f"now-playing schema_version {str(schema)[:32]!r}; this provider "
                        "supports v1 (addon 2.13+)"
                    )
                    raise ProviderUnavailableError(msg)
                return payload
        # ContentTypeError subclasses ClientError but means the endpoint answered
        # with a non-JSON body, so it must be caught first: an HTML splash page
        # reports "requires addon 2.13+" instead of "unreachable".
        except (aiohttp.ContentTypeError, ValueError) as err:
            raise ProviderUnavailableError(requires_msg) from err
        except (aiohttp.ClientError, TimeoutError) as err:
            msg = f"Mamma Mi Radio addon unreachable at {self._base_url}: {err}"
            raise ProviderUnavailableError(msg) from err

    async def _fetch_now_playing(self, data: dict[str, Any]) -> dict[str, Any] | None:
        """
        Poll the now-playing endpoint, returning the payload or None on failure.

        Sends a conditional request with the stored ETag; a 304 reuses the cached
        payload. A 200 without an ETag header drops the stored validator so
        polling becomes unconditional.

        :param data: Per-stream state (ETag validator and cached payload).
        """
        url = f"{self._base_url}{NOWPLAYING_PATH}"
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
                    self.logger.debug("v1 now-playing returned HTTP %s", response.status)
                    return None
                payload = await response.json()
                if not isinstance(payload, dict):
                    return None
                if not _supports_v1_schema(payload.get("schema_version")):
                    self.logger.debug(
                        "v1 now-playing returned unsupported schema_version %r",
                        payload.get("schema_version"),
                    )
                    return None
                new_etag = response.headers.get("ETag")
                if isinstance(new_etag, str):
                    data["v1_etag"] = new_etag
                else:
                    # The server stopped emitting ETags: drop the stored validator
                    # so polling actually becomes unconditional.
                    data.pop("v1_etag", None)
                data["v1_last"] = payload
                return payload
        except (aiohttp.ClientError, TimeoutError) as err:
            # A poisoned stored ETag (e.g. control characters from a broken proxy)
            # fails at request time on every tick; drop it so the next tick recovers.
            data.pop("v1_etag", None)
            self.logger.debug("v1 now-playing request failed: %s", err)
            return None
        except ValueError as err:
            data.pop("v1_etag", None)
            self.logger.debug("v1 now-playing returned bad JSON: %s", err)
            return None

    def _audio_format(self) -> AudioFormat:
        """Return the shared stream AudioFormat (v1 contract if known, else defaults)."""
        return _audio_format_from_contract(self._audio_format_dict)

    def _build_radio(self) -> Radio:
        """Construct the single Radio object for mammamiradio."""
        return Radio(
            provider=self.instance_id,
            item_id=RADIO_ITEM_ID,
            name=RADIO_NAME,
            metadata=MediaItemMetadata(
                description=RADIO_DESCRIPTION,
                genres={"Italian", "Talk Radio"},
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
