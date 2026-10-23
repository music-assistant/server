"""Provider-link enrichment through their supported public APIs."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast
from urllib.parse import quote

from aiohttp import ClientError, ClientTimeout
from music_assistant_models.enums import ExternalID, MediaType
from music_assistant_models.errors import (
    RateLimited,
    ResourceTemporarilyUnavailable,
    RetriesExhausted,
)
from music_assistant_models.media_items import ItemMapping

from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.compare import compare_strings
from music_assistant.helpers.external_ids import is_valid_isrc, normalize_external_id
from music_assistant.helpers.throttle_retry import (
    ThrottlerManager,
    parse_retry_after,
    throttle_with_retries,
)
from music_assistant.models.music_provider import PROVIDER_FETCH_ERRORS
from music_assistant.providers.share_provider.links import compact_provider_ref

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

LOGGER = logging.getLogger(__name__)
HTTP_RATE_LIMIT = 1
HTTP_RATE_PERIOD = 1.0


class _ShareHTTPClient:
    """Throttled JSON requests to one external metadata service."""

    def __init__(self, mass: MusicAssistant, service: str) -> None:
        self.mass = mass
        self.service = service
        self.logger = LOGGER
        self.throttler = ThrottlerManager(rate_limit=HTTP_RATE_LIMIT, period=HTTP_RATE_PERIOD)

    @throttle_with_retries
    async def get_json(self, url: str, params: dict[str, str | int] | None = None) -> object | None:
        """Request JSON while honoring the service's rate limit and Retry-After."""
        try:
            async with self.mass.http_session.get(
                url, params=params, timeout=ClientTimeout(total=4)
            ) as response:
                if response.status == 404:
                    return None
                if response.status == 429:
                    retry_after = parse_retry_after(response.headers.get("Retry-After")) or 1
                    raise RateLimited(
                        f"{self.service} rate limited the share lookup",
                        backoff_time=retry_after,
                    )
                if response.status >= 500 or response.status in (408, 425):
                    raise ResourceTemporarilyUnavailable(
                        f"{self.service} temporarily unavailable", backoff_time=1
                    )
                if response.status != 200:
                    return None
                return cast("object", await response.json())
        except (ClientError, TimeoutError) as err:
            raise ResourceTemporarilyUnavailable(
                f"{self.service} request failed", backoff_time=1
            ) from err


class ShareLinkEnricher:
    """Add validated public links for services mapped from one share item."""

    def __init__(self, mass: MusicAssistant, instance_id: str) -> None:
        """Initialize per-provider service clients and their caches."""
        self.mass = mass
        self.instance_id = instance_id
        self.domain = "share_provider"
        self._deezer_client = _ShareHTTPClient(mass, "Deezer")
        self._apple_music_client = _ShareHTTPClient(mass, "Apple Music")

    async def enrich(
        self,
        provider_refs: dict[str, str],
        isrc: str | None,
        title: str,
        artist: str | None,
        apple_music_track_id: str | None = None,
    ) -> None:
        """Add missing Deezer, Apple Music and configured YouTube Music links."""
        if not isrc and not title:
            return

        query = f"{artist} {title}" if artist else title
        if "deezer" not in provider_refs:
            deezer_link: str | None = None
            lookup_failed = False
            if isrc and is_valid_isrc(isrc):
                try:
                    deezer_link = await self._get_deezer_track_url(
                        normalize_external_id(ExternalID.ISRC, isrc)
                    )
                except (ClientError, TimeoutError, ValueError, RetriesExhausted) as err:
                    lookup_failed = isinstance(err, RetriesExhausted)
                    LOGGER.debug("Could not look up Deezer track by ISRC: %s", err)
            if not deezer_link and not lookup_failed:
                search_query = f'track:"{title.replace(chr(34), "")}"'
                if artist:
                    safe_artist = artist.replace(chr(34), "")
                    search_query = f'artist:"{safe_artist}" {search_query}'
                try:
                    deezer_link = await self._search_deezer_track_url(search_query, title, artist)
                except (ClientError, TimeoutError, ValueError, RetriesExhausted) as err:
                    LOGGER.debug("Could not enrich share link with Deezer search: %s", err)
            if deezer_link:
                provider_refs["deezer"] = compact_provider_ref("deezer", deezer_link)

        if "apple_music" not in provider_refs:
            try:
                apple_link = None
                if apple_music_track_id:
                    apple_link = await self._get_apple_music_track_url_by_id(apple_music_track_id)
                if not apple_link:
                    apple_link = await self._get_apple_music_track_url(query, title, artist)
                if apple_link:
                    provider_refs["apple_music"] = compact_provider_ref("apple_music", apple_link)
            except (ClientError, TimeoutError, ValueError, RetriesExhausted) as err:
                LOGGER.debug("Could not enrich share link with Apple Music: %s", err)

        if "ytmusic" in provider_refs:
            return
        try:
            results = await self.mass.music.search(
                query, [MediaType.TRACK], limit=3, providers=["ytmusic"]
            )
            for track in results.tracks or []:
                if isinstance(track, ItemMapping):
                    continue
                if not compare_strings(title, track.name, strict=False) or (
                    artist and not compare_strings(artist, track.artist_str, strict=False)
                ):
                    continue
                for mapping in track.provider_mappings:
                    if mapping.provider_domain == "ytmusic":
                        url = f"https://music.youtube.com/watch?v={quote(mapping.item_id, safe='')}"
                        provider_refs["ytmusic"] = compact_provider_ref("ytmusic", url)
                        break
                if "ytmusic" in provider_refs:
                    break
        except PROVIDER_FETCH_ERRORS as err:
            LOGGER.debug("Could not enrich share link with YouTube Music: %s", err)

    @use_cache(3600 * 24 * 30)
    async def _get_deezer_track_url(self, isrc: str) -> str | None:
        """Return the public Deezer track URL for an ISRC, if available."""
        data = await self._deezer_client.get_json(f"https://api.deezer.com/track/isrc:{isrc}")
        if not isinstance(data, dict):
            return None
        link = data.get("link")
        return link if isinstance(link, str) else None

    @use_cache(3600 * 24 * 30)
    async def _search_deezer_track_url(
        self, query: str, title: str, artist: str | None
    ) -> str | None:
        """Find a Deezer track by title and artist when ISRC lookup misses."""
        data = await self._deezer_client.get_json(
            "https://api.deezer.com/search", params={"q": query, "limit": 5}
        )
        if not isinstance(data, dict):
            return None
        return self._find_matching_track_url(data.get("data", []), title, artist)

    @use_cache(3600 * 24 * 30)
    async def _get_apple_music_track_url(
        self, query: str, title: str, artist: str | None
    ) -> str | None:
        """Return the public Apple Music track URL for a text query, if available."""
        data = await self._apple_music_client.get_json(
            "https://itunes.apple.com/search",
            params={"term": query, "entity": "song", "limit": 5},
        )
        if not isinstance(data, dict):
            return None
        return self._find_matching_track_url(data.get("results", []), title, artist)

    @use_cache(3600 * 24 * 30)
    async def _get_apple_music_track_url_by_id(self, track_id: str) -> str | None:
        """Return the canonical Apple Music URL for a catalog track ID."""
        data = await self._apple_music_client.get_json(
            "https://itunes.apple.com/lookup",
            params={"id": track_id, "entity": "song", "limit": 1},
        )
        results = data.get("results", []) if isinstance(data, dict) else []
        for result in results:
            if (
                isinstance(result, dict)
                and result.get("kind") == "song"
                and isinstance(result.get("trackViewUrl"), str)
            ):
                return cast("str", result["trackViewUrl"])
        return None

    @staticmethod
    def _find_matching_track_url(results: object, title: str, artist: str | None) -> str | None:
        if not isinstance(results, list):
            return None
        best_score = (-1, -1)
        best_url: str | None = None
        for result in results:
            if not isinstance(result, dict) or result.get("kind") not in (None, "song"):
                continue
            result_title = result.get("trackName") or result.get("title")
            result_artist = result.get("artistName")
            if result_artist is None and isinstance(result.get("artist"), dict):
                result_artist = result["artist"].get("name")
            result_url = result.get("trackViewUrl") or result.get("link")
            if (
                not isinstance(result_title, str)
                or not isinstance(result_url, str)
                or not compare_strings(title, result_title, strict=False)
                or (artist and not isinstance(result_artist, str))
                or (
                    artist
                    and isinstance(result_artist, str)
                    and not compare_strings(artist, result_artist, strict=False)
                )
            ):
                continue
            score = (
                int(compare_strings(title, result_title, strict=True)),
                int(
                    not artist
                    or (
                        isinstance(result_artist, str)
                        and compare_strings(artist, result_artist, strict=True)
                    )
                ),
            )
            if score > best_score:
                best_score = score
                best_url = result_url
        return best_url
