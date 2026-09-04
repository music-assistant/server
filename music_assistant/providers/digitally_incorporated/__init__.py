"""
Digitally Imported Music Provider for Music Assistant.

This provider supports the Digitally Imported network of streaming radio services:
- DI.FM
- RadioTunes
- RockRadio
- JazzRadio
- ClassicalRadio
- ZenRadio

The provider requires a premium account and listen key for streaming. Library
favorites are read from each network's listen host ``favorites.pls`` (read-only;
edit favorites on the service website).
"""

from __future__ import annotations

import re
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import aiohttp
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import (
    InvalidDataError,
    MediaNotFoundError,
    ProviderUnavailableError,
)
from music_assistant_models.media_items import (
    AudioFormat,
    BrowseFolder,
    MediaItemImage,
    MediaItemMetadata,
    MediaItemType,
    ProviderMapping,
    Radio,
    SearchResults,
    UniqueList,
)
from music_assistant_models.streamdetails import MultiPartPath, StreamDetails

from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.playlists import parse_pls
from music_assistant.helpers.throttle_retry import Throttler
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import (
        ConfigEntry,
        ConfigValueOption,
        ProviderConfig,
    )
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType
else:
    from music_assistant_models.config_entries import (
        ConfigEntry,
        ConfigValueOption,
    )

SUPPORTED_FEATURES = {
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
    ProviderFeature.LIBRARY_RADIOS,
}

# API Configuration
API_BASE_URL = "api.audioaddict.com/v1"
API_TIMEOUT = 30
CACHE_CHANNELS = 86400  # 24 hours
CACHE_GENRES = 86400  # 24 hours
CACHE_STREAM_URLS = 3600  # 1 hour
CACHE_FAVORITES = 300  # 5 minutes

# Favorites playlist on listen.* (premium tier; ``public3`` is legacy and not used here).
_PLS_CHANNEL_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$", re.IGNORECASE)

# Root ``/favorites.pls`` (no tier) often returns HTTP 406 — use premium only.
FAVORITES_PLS_PATHS: tuple[str, ...] = ("premium/favorites.pls",)
FAVORITES_PLS_EXTRA_PARAMS: dict[str, str] = {"download": "1"}

# Premium PLS may use codec-specific mounts (``pianojazz_aac``) while API ``key`` is ``pianojazz``.
PLS_KEY_CODEC_SUFFIXES: tuple[str, ...] = ("_aac", "_mp3")

# Optional short listen-host basename prefixes (besides ``3rdparty_{network}_`` / ``{network}_``).
NETWORK_PLS_SHORT_PREFIXES: dict[str, tuple[str, ...]] = {
    "radiotunes": ("rt_",),
    "rockradio": ("rr_",),
    "classicalradio": ("cr_",),
    "zenradio": ("zr_",),
}

# Rate limiting
RATE_LIMIT = 2  # requests per period
RATE_PERIOD = 1  # second

# Validation constants
MIN_LISTEN_KEY_LENGTH = 10

# Digitally Imported radio services configuration
NETWORKS = {
    "di": {
        "domain": "di.fm",
        "display_name": "DI.FM",
        "description": "Electronic music radio stations",
    },
    "radiotunes": {
        "domain": "radiotunes.com",
        "display_name": "RadioTunes",
        "description": "Variety music radio stations",
    },
    "rockradio": {
        "domain": "rockradio.com",
        "display_name": "RockRadio",
        "description": "Rock music radio stations",
    },
    "jazzradio": {
        "domain": "jazzradio.com",
        "display_name": "JazzRadio",
        "description": "Jazz music radio stations",
    },
    "classicalradio": {
        "domain": "classicalradio.com",
        "display_name": "ClassicalRadio",
        "description": "Classical music radio stations",
    },
    "zenradio": {
        "domain": "zenradio.com",
        "display_name": "ZenRadio",
        "description": "Ambient and relaxation radio stations",
    },
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return DigitallyImportedProvider(mass, manifest, config, SUPPORTED_FEATURES)


class DigitallyImportedProvider(MusicProvider):
    """Digitally Imported Music Provider."""

    _throttler: Throttler

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
        supported_features: set[ProviderFeature],
    ) -> None:
        """Initialize Digitally Imported provider."""
        super().__init__(mass, manifest, config, supported_features)
        self._throttler = Throttler(rate_limit=RATE_LIMIT, period=RATE_PERIOD)

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to setup this provider."""
        entries = []

        # Network selection - multi-select instead of individual booleans
        network_options = [
            ConfigValueOption(network_key, title=network_info["display_name"])
            for network_key, network_info in NETWORKS.items()
        ]

        entries.append(
            ConfigEntry(
                key="enabled_networks",
                type=ConfigEntryType.STRING,
                default_value=list(NETWORKS.keys()),  # Enable all by default
                required=True,
                options=network_options,
                multi_value=True,
            )
        )

        return tuple(entries)

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        # Validate configuration
        enabled_networks = self._get_active_networks()
        if not enabled_networks:
            msg = f"{self.domain}: At least one network must be enabled"
            raise ProviderUnavailableError(msg)

        listen_key = self.get_setup_value("listen_key")
        if (
            not listen_key
            or not isinstance(listen_key, str)
            or len(listen_key.strip()) < MIN_LISTEN_KEY_LENGTH
        ):
            msg = f"{self.domain}: Invalid listen key provided"
            raise ProviderUnavailableError(msg)

        # Test API connectivity by trying to get channels from first enabled network
        try:
            first_network = enabled_networks[0]
            await self._get_channels(first_network)
            self.logger.info("%s: Successfully connected to Digitally Imported API", self.domain)
        except ProviderUnavailableError, MediaNotFoundError:
            # Re-raise provider/media errors as-is (they already have domain prefix)
            raise
        except (aiohttp.ClientError, aiohttp.ServerTimeoutError) as err:
            self.logger.error(
                "%s: Failed to connect to Digitally Imported API: %s", self.domain, err
            )
            msg = f"{self.domain}: API unavailable: {err}"
            raise ProviderUnavailableError(msg) from err

    @property
    def is_streaming_provider(self) -> bool:
        """Return True if the provider is a streaming provider."""
        return True

    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 5,
    ) -> SearchResults:
        """Perform search on Digitally Imported channels."""
        results = SearchResults()

        if MediaType.RADIO not in media_types:
            return results

        search_query_lower = search_query.lower().strip()
        if not search_query_lower:
            return results

        radios = []

        # Search across all active networks
        for network_key in self._get_active_networks():
            try:
                channels = await self._get_channels(network_key)

                for channel_data in channels:
                    channel_name = str(channel_data.get("name", "")).lower()
                    if search_query_lower in channel_name:
                        radio = self._channel_to_radio(channel_data, network_key)
                        radios.append(radio)

                        if len(radios) >= limit:
                            break

            except (
                ProviderUnavailableError,
                MediaNotFoundError,
                aiohttp.ClientError,
                ValueError,
                KeyError,
            ) as err:
                self.logger.debug(
                    "%s: Search failed for network %s: %s", self.domain, network_key, err
                )
                continue

            if len(radios) >= limit:
                break

        results.radio = radios
        return results

    async def get_library_radios(self) -> AsyncGenerator[Radio]:
        """Retrieve the user's favorite radio stations from active networks."""
        for network_key in self._get_active_networks():
            try:
                channel_keys = await self._get_favorite_channel_keys(network_key)
                if not channel_keys:
                    continue

                channels = await self._get_channels(network_key)
                channels_by_key = {
                    str(ch["key"]): ch for ch in channels if isinstance(ch, dict) and ch.get("key")
                }
                channels_lower_by_key = {k.lower(): v for k, v in channels_by_key.items()}

                for channel_key in channel_keys:
                    ch_data = self._channel_data_for_pls_key(
                        channel_key, channels_by_key, channels_lower_by_key
                    )
                    if ch_data:
                        yield self._channel_to_radio(ch_data, network_key)

            except (
                ProviderUnavailableError,
                MediaNotFoundError,
                aiohttp.ClientError,
                ValueError,
                KeyError,
            ) as err:
                # the try block spans the whole per-network fetch and yield loop, so a
                # failure partway through leaves an unknown subset of this network's
                # favourites unreported; item_id=None holds back deletions for the run
                self.report_skipped_sync_item(MediaType.RADIO, None, err)
                continue

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """Get full radio details by id."""
        # Validate and parse the provider ID
        network_key, channel_key = self._validate_item_id(prov_radio_id)

        channels = await self._get_channels(network_key)

        for channel_data in channels:
            if channel_data["key"] == channel_key:
                return self._channel_to_radio(channel_data, network_key)

        msg = f"{self.domain}: Radio station not found: {prov_radio_id}"
        raise MediaNotFoundError(msg)

    def _validate_item_id(self, item_id: str) -> tuple[str, str]:
        """Validate and parse item ID into network and channel keys."""
        try:
            network_key, channel_key = item_id.split(":", 1)
        except ValueError as err:
            msg = f"{self.domain}: Invalid item ID format: {item_id} (expected 'network:channel')"
            raise MediaNotFoundError(msg) from err

        if network_key not in NETWORKS:
            msg = f"{self.domain}: Invalid network key: {network_key}"
            raise MediaNotFoundError(msg)

        if not channel_key.strip():
            msg = f"{self.domain}: Empty channel key in item ID: {item_id}"
            raise MediaNotFoundError(msg)

        return network_key, channel_key

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get streamdetails for a radio station."""
        if media_type != MediaType.RADIO:
            msg = f"{self.domain}: Unsupported media type: {media_type}"
            raise MediaNotFoundError(msg)

        # Validate and parse the provider ID
        network_key, channel_key = self._validate_item_id(item_id)

        stream_urls = await self._get_stream_urls(network_key, channel_key)

        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(
                content_type=ContentType.UNKNOWN,  # Let ffmpeg auto-detect
            ),
            media_type=MediaType.RADIO,
            stream_type=StreamType.ICY,
            path=[MultiPartPath(path=url) for url in stream_urls],
            allow_seek=False,
            can_seek=False,
            duration=0,  # Infinite duration for radio streams
        )

    @use_cache(CACHE_CHANNELS)
    async def browse(self, path: str) -> list[MediaItemType | BrowseFolder]:
        """Browse Digitally Imported radio services and channels."""
        self.logger.debug("%s: Browse called with path: %s", self.domain, path)

        # Extract meaningful path component
        subpath = ""
        if "://" in path:
            # Remove the scheme prefix and get the first meaningful path component
            path_parts = path.split("://")[1].split("/")
            meaningful_parts = [part for part in path_parts if part]
            subpath = meaningful_parts[0] if meaningful_parts else ""

        self.logger.debug("%s: Parsed subpath: %s", self.domain, subpath)

        if not subpath:
            # Return root level - show networks
            return await self._browse_networks(path)

        # Show channels for the selected network
        if subpath in NETWORKS:
            return await self._browse_network_channels(subpath)

        self.logger.debug("%s: No matching path found, returning empty list", self.domain)
        return []

    async def _browse_networks(self, base_path: str) -> list[MediaItemType | BrowseFolder]:
        """Browse available networks."""
        items: list[MediaItemType | BrowseFolder] = []
        active_networks = self._get_active_networks()
        self.logger.debug("%s: Active networks: %s", self.domain, active_networks)

        for network_key in active_networks:
            network_info = NETWORKS[network_key]
            folder = BrowseFolder(
                item_id=network_key,
                provider=self.instance_id,
                path=f"{base_path}{network_key}"
                if base_path.endswith("://")
                else f"{base_path}/{network_key}",
                name=network_info["display_name"],
            )
            items.append(folder)
            self.logger.debug(
                "%s: Added network folder: %s", self.domain, network_info["display_name"]
            )

        self.logger.debug("%s: Returning %d network folders", self.domain, len(items))
        return items

    async def _browse_network_channels(
        self, network_key: str
    ) -> list[MediaItemType | BrowseFolder]:
        """Browse channels for a specific network."""
        self.logger.debug("%s: Browsing channels for network: %s", self.domain, network_key)
        try:
            channels = await self._get_channels(network_key)
            self.logger.debug(
                "%s: Found %d channels for network %s", self.domain, len(channels), network_key
            )
            radio_items: list[MediaItemType | BrowseFolder] = []
            for ch in channels:
                radio = self._channel_to_radio(ch, network_key)
                radio_items.append(radio)
            self.logger.debug("%s: Converted to %d radio items", self.domain, len(radio_items))
            return radio_items
        except (
            ProviderUnavailableError,
            MediaNotFoundError,
            aiohttp.ClientError,
            ValueError,
            KeyError,
        ) as err:
            self.logger.warning(
                "%s: Failed to browse network %s: %s", self.domain, network_key, err
            )
            return []

    def _get_active_networks(self) -> list[str]:
        """Get list of active/enabled networks."""
        enabled_networks = self.config.get_value("enabled_networks", list(NETWORKS.keys()))
        return self._validate_and_filter_networks(enabled_networks)

    def _validate_and_filter_networks(self, networks: Any) -> list[str]:
        """Validate and filter network configuration."""
        # Handle both single value and list for backwards compatibility
        if isinstance(networks, str):
            networks = [networks]
        elif not isinstance(networks, list):
            self.logger.warning(
                "%s: Invalid networks configuration, defaulting to all networks", self.domain
            )
            return list(NETWORKS.keys())

        # Ensure all items are strings and filter out non-strings/invalid networks
        valid_networks = [str(net) for net in networks if net and str(net) in NETWORKS]

        if not valid_networks:
            self.logger.warning(
                "%s: No valid networks enabled, defaulting to all networks", self.domain
            )
            return list(NETWORKS.keys())

        return valid_networks

    async def _api_request(
        self,
        network_key: str,
        endpoint: str,
        method: str = "GET",
        use_https: bool = True,
        json_body: dict[str, Any] | None = None,
        **params: Any,
    ) -> Any:
        """Make a generic API request to Digitally Imported."""
        scheme = "https" if use_https else "http"
        base_url = f"{scheme}://{API_BASE_URL}/{network_key}"
        url = f"{base_url}/{endpoint}"

        timeout = aiohttp.ClientTimeout(total=API_TIMEOUT)

        async with (
            self._throttler,
            self.mass.http_session.request(
                method, url, params=params, json=json_body, timeout=timeout
            ) as resp,
        ):
            if resp.status == 403:
                msg = f"{self.domain}: Access denied - check your listen key and subscription"
                raise ProviderUnavailableError(msg)
            if resp.status == 404:
                msg = f"{self.domain}: API endpoint not found: {endpoint}"
                raise MediaNotFoundError(msg)
            if resp.status >= 500:
                msg = f"{self.domain}: Server error (HTTP {resp.status})"
                raise ProviderUnavailableError(msg)

            resp.raise_for_status()

            if resp.status == 204:
                return None
            return await resp.json()

    async def _fetch_favorites_pls(self, network_key: str) -> str:
        """Download favorites.pls from the listen.* host for this network."""
        domain = NETWORKS[network_key]["domain"]
        listen_key = self.get_setup_value("listen_key")
        timeout = aiohttp.ClientTimeout(total=API_TIMEOUT)
        params: dict[str, str] = {"listen_key": str(listen_key), **FAVORITES_PLS_EXTRA_PARAMS}
        try:
            for rel in FAVORITES_PLS_PATHS:
                url = f"https://listen.{domain}/{rel}"
                async with (
                    self._throttler,
                    self.mass.http_session.get(url, params=params, timeout=timeout) as resp,
                ):
                    if resp.status == 200:
                        return await resp.text()
                    if resp.status == 403:
                        self.logger.debug(
                            "%s: favorites.pls access denied for network %s (%s)",
                            self.domain,
                            network_key,
                            rel,
                        )
                        return ""
                    if resp.status in (404, 406):
                        continue
                    if resp.status >= 500:
                        self.logger.debug(
                            "%s: favorites.pls HTTP %s for network %s (%s)",
                            self.domain,
                            resp.status,
                            network_key,
                            rel,
                        )
                        return ""
                    resp.raise_for_status()
            self.logger.debug(
                "%s: favorites.pls not usable for network %s (tried %s)",
                self.domain,
                network_key,
                ", ".join(FAVORITES_PLS_PATHS),
            )
            return ""
        except (aiohttp.ClientError, aiohttp.ServerTimeoutError) as err:
            self.logger.debug(
                "%s: favorites.pls request failed for network %s: %s",
                self.domain,
                network_key,
                err,
            )
            return ""

    def _pls_url_basename(self, stream_url: str) -> str | None:
        """Last path segment of a stream URL (stream mount / slug)."""
        parsed = urlparse(stream_url.strip())
        path = parsed.path.strip("/")
        if not path:
            return None
        return path.rsplit("/", maxsplit=1)[-1]

    def _pls_basename_prefixes(self, network_key: str) -> tuple[str, ...]:
        """Prefixes to strip from PLS stream basenames; longest match wins."""
        parts: list[str] = []
        parts.extend(NETWORK_PLS_SHORT_PREFIXES.get(network_key, ()))
        parts.append(f"3rdparty_{network_key}_")
        parts.append(f"{network_key}_")
        # Dedupe, then longest first so e.g. ``3rdparty_jazzradio_`` beats ``jazzradio_``.
        unique = dict.fromkeys(parts)
        return tuple(sorted(unique, key=len, reverse=True))

    def _stream_url_to_channel_key(self, stream_url: str, network_key: str) -> str | None:
        """Map a favorites.pls stream URL to a slug we can match to API channel ``key``."""
        basename = self._pls_url_basename(stream_url)
        if not basename:
            return None
        for prefix in self._pls_basename_prefixes(network_key):
            if basename.startswith(prefix):
                key = basename[len(prefix) :]
                if key:
                    return key
        # Premium PLS often uses the API channel key alone (e.g. ``ambient``), not ``di_ambient``.
        if _PLS_CHANNEL_SLUG_RE.fullmatch(basename):
            return basename
        return None

    def _channel_data_for_pls_key(
        self,
        pls_key: str,
        channels_by_key: dict[str, dict[str, Any]],
        channels_lower_by_key: dict[str, dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Match a PLS-derived slug to channel JSON (case-insensitive; ``*_aac`` / ``*_mp3``)."""
        if pls_key in channels_by_key:
            return channels_by_key[pls_key]
        pls_lower = pls_key.lower()
        if pls_lower in channels_lower_by_key:
            return channels_lower_by_key[pls_lower]
        for suffix in PLS_KEY_CODEC_SUFFIXES:
            if pls_lower.endswith(suffix):
                trimmed_lower = pls_lower[: -len(suffix)]
                if trimmed_lower in channels_lower_by_key:
                    return channels_lower_by_key[trimmed_lower]
        return None

    def _parse_favorites_pls_channel_keys(self, pls_body: str, network_key: str) -> list[str]:
        """Return ordered, unique channel keys derived from the favorites PLS entries."""
        try:
            items = parse_pls(pls_body)
        except InvalidDataError as err:
            self.logger.debug(
                "%s: could not parse favorites PLS for network %s: %s",
                self.domain,
                network_key,
                err,
            )
            return []
        ordered: list[str] = []
        seen_keys: set[str] = set()
        unmapped_basenames: set[str] = set()
        for item in items:
            channel_key = self._stream_url_to_channel_key(item.path, network_key)
            if channel_key and channel_key not in seen_keys:
                seen_keys.add(channel_key)
                ordered.append(channel_key)
            elif not channel_key and (bn := self._pls_url_basename(item.path)):
                unmapped_basenames.add(bn)
        if unmapped_basenames:
            sample = ", ".join(sorted(unmapped_basenames)[:12])
            extra = len(unmapped_basenames) - 12
            more = f" (+{extra} more)" if extra > 0 else ""
            self.logger.debug(
                "%s: PLS for network %s: could not map %d path(s) to channel keys: %s%s",
                self.domain,
                network_key,
                len(unmapped_basenames),
                sample,
                more,
            )
        return ordered

    @use_cache(CACHE_FAVORITES)
    async def _get_favorite_channel_keys(self, network_key: str) -> list[str]:
        """Channel keys for user favorites, from listen host favorites.pls."""
        pls_body = await self._fetch_favorites_pls(network_key)
        if not pls_body.strip():
            return []
        keys = self._parse_favorites_pls_channel_keys(pls_body, network_key)
        self.logger.debug(
            "%s: Parsed %d favorite channel keys for network %s from PLS",
            self.domain,
            len(keys),
            network_key,
        )
        return keys

    @use_cache(CACHE_CHANNELS)
    async def _get_channels(self, network_key: str) -> list[dict[str, Any]]:
        """Get channels for a specific network with enriched genre data."""
        try:
            # Get all channel data (includes images, descriptions, etc.)
            channels_response = await self._api_request(network_key, "channels")

            if not channels_response or not isinstance(channels_response, list):
                self.logger.warning("No channels returned for network %s", network_key)
                return []

            self.logger.debug(
                "%s: Retrieved %d channels for network %s",
                self.domain,
                len(channels_response),
                network_key,
            )

            # Get genre filters and create a mapping
            genre_mapping = await self._get_genre_mapping(network_key)

            # Filter and enrich channels with genre data
            channels: list[dict[str, Any]] = []
            for ch in channels_response:
                if not isinstance(ch, dict) or self._is_disabled_channel(ch):
                    continue

                # Enrich channel with genre names
                channel_filter_ids = ch.get("channel_filter_ids", [])
                if channel_filter_ids and genre_mapping:
                    genres = [
                        genre_mapping[filter_id]
                        for filter_id in channel_filter_ids
                        if filter_id in genre_mapping
                    ]
                    if genres:
                        ch["genres"] = genres

                channels.append(ch)

            self.logger.debug(
                "%s: Processed %d channels for network %s",
                self.domain,
                len(channels),
                network_key,
            )

            return channels

        except (ProviderUnavailableError, MediaNotFoundError, aiohttp.ClientError) as err:
            self.logger.error("Failed to get channels for network %s: %s", network_key, err)
            raise

    @use_cache(CACHE_GENRES)
    async def _get_channel_filters(self, network_key: str) -> list[dict[str, Any]]:
        """Get channel filters (genre information) for a specific network."""
        try:
            # Get genre/filter data
            filters_response = await self._api_request(network_key, "channel_filters")

            if not filters_response or not isinstance(filters_response, list):
                self.logger.warning("No channel filters returned for network %s", network_key)
                return []

            self.logger.debug(
                "%s: Retrieved %d channel filters for network %s",
                self.domain,
                len(filters_response),
                network_key,
            )

            # Ensure all items are dictionaries and filter for actual genres
            # (genre=True indicates genre categories vs meta categories e.g. "Favorite" or "All")
            genre_filters: list[dict[str, Any]] = [
                f for f in filters_response if isinstance(f, dict) and f.get("genre", False)
            ]

            self.logger.debug(
                "%s: Found %d genre filters (out of %d total filters) for network %s",
                self.domain,
                len(genre_filters),
                len(filters_response),
                network_key,
            )

            return genre_filters

        except (ProviderUnavailableError, MediaNotFoundError, aiohttp.ClientError) as err:
            self.logger.error("Failed to get channel filters for network %s: %s", network_key, err)
            raise

    async def _get_genre_mapping(self, network_key: str) -> dict[int, str]:
        """Get a mapping of filter ID to genre name for a network."""
        try:
            genre_filters = await self._get_channel_filters(network_key)

            # Create a mapping of filter ID to genre name
            mapping = {
                f["id"]: f["name"]
                for f in genre_filters
                if isinstance(f, dict) and "id" in f and "name" in f
            }

            self.logger.debug(
                "%s: Created genre mapping with %d entries for network %s",
                self.domain,
                len(mapping),
                network_key,
            )

            return mapping

        except (ProviderUnavailableError, MediaNotFoundError, aiohttp.ClientError) as err:
            self.logger.warning(
                "%s: Failed to get genre mapping for network %s: %s",
                self.domain,
                network_key,
                err,
            )
            return {}

    @use_cache(CACHE_STREAM_URLS)
    async def _get_stream_urls(self, network_key: str, channel_key: str) -> list[str]:
        """Get the streaming URLs for a channel (ordered mirrors / failover)."""
        self.logger.debug(
            "%s: Getting stream URLs for %s:%s", self.domain, network_key, channel_key
        )

        listen_key = self.get_setup_value("listen_key")
        if not listen_key:
            msg = f"{self.domain}: Listen key not configured"
            raise ProviderUnavailableError(msg)

        try:
            playlist = await self._api_request(
                network_key,
                f"listen/premium_high/{channel_key}",
                method="GET",
                use_https=True,
                listen_key=str(listen_key),
            )

            if not playlist or not isinstance(playlist, list):
                msg = f"{self.domain}: No stream URLs returned from Digitally Imported API"
                raise MediaNotFoundError(msg)

            stream_list: list[str] = [url for url in playlist if url and isinstance(url, str)]
            self.logger.debug(
                "%s: Filtered %d valid stream URLs from playlist of %d URLs",
                self.domain,
                len(stream_list),
                len(playlist),
            )

            if not stream_list:
                msg = f"{self.domain}: No valid stream URLs found in the playlist"
                raise MediaNotFoundError(msg)

            # Log all available URLs
            for i, url in enumerate(stream_list):
                self.logger.debug("%s: Available stream URL %d: %s", self.domain, i + 1, url)

            return stream_list

        except ProviderUnavailableError, MediaNotFoundError:
            # Re-raise provider/media errors as-is (they already have domain prefix)
            raise
        except (aiohttp.ClientError, ValueError) as err:
            self.logger.error(
                "%s: Failed to get stream URLs for %s:%s: %s",
                self.domain,
                network_key,
                channel_key,
                err,
            )
            raise MediaNotFoundError(f"{self.domain}: Unable to get stream URLs: {err}") from err

    def _channel_to_radio(self, channel_data: dict[str, Any], network_key: str) -> Radio:
        """Convert channel data to Radio object."""
        # Create provider ID as network:channel_key
        channel_key = channel_data.get("key")
        if not channel_key:
            msg = f"Channel missing 'key' field: {channel_data}"
            raise ValueError(msg)

        prov_id = f"{network_key}:{channel_key}"
        channel_name = str(channel_data.get("name", "Unknown"))

        # Create metadata with optional image and genres
        metadata = MediaItemMetadata(
            description=self._get_description(channel_data),
            explicit=False,
        )

        # Add genre information from enriched channel data
        genres = channel_data.get("genres", [])
        if genres:
            metadata.genres = set(genres)
            self.logger.debug("%s: Added genres %s for channel %s", self.domain, genres, prov_id)

        # Process image URL if available
        image_url = self._extract_image_url(channel_data)
        if image_url:
            self.logger.debug(
                "%s: Found image URL for channel %s: %s", self.domain, prov_id, image_url
            )
            metadata.images = UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=image_url,
                        provider=self.instance_id,
                        remotely_accessible=True,
                    )
                ]
            )
        else:
            self.logger.debug("%s: No valid image URL found for channel %s", self.domain, prov_id)

        return Radio(
            item_id=prov_id,
            provider=self.instance_id,
            name=channel_name,
            provider_mappings={
                ProviderMapping(
                    item_id=prov_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    available=True,
                    audio_format=AudioFormat(
                        content_type=ContentType.UNKNOWN,
                    ),
                )
            },
            metadata=metadata,
        )

    def _is_disabled_channel(self, channel_data: dict[str, Any]) -> bool:
        """Check if a channel is disabled based on its name pattern."""
        name = channel_data.get("name")
        if not name or not isinstance(name, str) or len(name) < 2:
            return False

        # Disabled channels have names starting with 'X' followed by a (capitalized) channel name.
        return bool(name[0] == "X" and name[1].isupper())

    def _get_description(self, channel_data: dict[str, Any]) -> str:
        """Get combined description from channel data."""
        short_desc = channel_data.get("description_short", "")
        long_desc = channel_data.get("description_long", "")

        if not long_desc or long_desc == short_desc:
            return str(short_desc)

        return f"{short_desc}\n\n{long_desc}"

    def _extract_image_url(self, channel_data: dict[str, Any]) -> str | None:
        """Extract and normalize image URL from channel data."""
        images = channel_data.get("images")
        if not images or not isinstance(images, dict):
            return None

        image_url = images.get("square")
        if not image_url or not isinstance(image_url, str):
            return None

        # Add protocol if missing (AudioAddict returns URLs starting with //)
        if image_url.startswith("//"):
            image_url = f"https:{image_url}"

        # Remove template parts if present (URLs may contain {size} placeholders)
        return str(image_url.split("{")[0])
