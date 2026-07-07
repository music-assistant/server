"""Last.fm API client for recommendations."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from aiohttp import ClientError
from music_assistant_models.errors import (
    AuthenticationFailed,
    InvalidDataError,
    InvalidToken,
    MusicAssistantError,
    ResourceTemporarilyUnavailable,
)

from music_assistant.helpers.app_vars import app_var
from music_assistant.helpers.throttle_retry import ThrottlerManager
from music_assistant.providers.lastfm_recommendations.constants import CONF_API_KEY

# Built-in Last.fm API key (read-only access, no secret needed)
_DEFAULT_API_KEY: str = app_var("lastfm_api_key")

if TYPE_CHECKING:
    import logging

    from aiohttp import ClientSession

    from music_assistant.providers.lastfm_recommendations import LastFMRecommendationsProvider


class LastFMAPIClient:
    """Last.fm API client for fetching recommendations."""

    BASE_URL = "https://ws.audioscrobbler.com/2.0/"
    throttler = ThrottlerManager(rate_limit=5, period=1)  # 5 requests per second

    # Map known Last.fm API error codes to MA exceptions. Unmapped codes raise
    # InvalidDataError which callers treat as a recoverable empty response.
    _ERROR_MAP: ClassVar[dict[int, type[MusicAssistantError]]] = {
        4: AuthenticationFailed,
        10: InvalidToken,
        26: InvalidToken,
        29: ResourceTemporarilyUnavailable,
    }

    def __init__(self, provider: LastFMRecommendationsProvider) -> None:
        """
        Initialize Last.fm API client.

        :param provider: The Last.fm recommendations provider instance.
        """
        self.provider = provider
        self.http_session: ClientSession = provider.mass.http_session

    @property
    def logger(self) -> logging.Logger:
        """Return the provider's active logger."""
        return self.provider.logger

    async def _get_data(self, method: str, **params: Any) -> dict[str, Any]:
        """
        Make a request to the Last.fm API.

        :param method: The Last.fm API method to call.
        :param params: Additional query parameters.
        """
        async with self.throttler.acquire():
            api_key = self.provider.config.get_value(CONF_API_KEY) or _DEFAULT_API_KEY
            params.update(
                {
                    "method": method,
                    "api_key": api_key,
                    "format": "json",
                }
            )

            async with self.http_session.get(self.BASE_URL, params=params) as response:
                response.raise_for_status()
                data: dict[str, Any] = await response.json()

        # Last.fm returns errors in the response body rather than as an HTTP status.
        if "error" in data:
            error_code = int(data.get("error", 0))
            error_msg = data.get("message", "Unknown error")
            msg = f"Last.fm API error {error_code}: {error_msg} (method: {method})"
            exc_cls = self._ERROR_MAP.get(error_code, InvalidDataError)
            raise exc_cls(msg)

        return data

    async def _get_list(
        self,
        method: str,
        container: str,
        item_key: str,
        log_label: str,
        primary_params: dict[str, Any],
        fallback_params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Fetch a list-type Last.fm response, retrying with the fallback params when empty.

        :param method: Last.fm API method to call.
        :param container: Top-level response key wrapping the result list.
        :param item_key: Key of the result list within the container.
        :param log_label: Human-readable label used in debug logging.
        :param primary_params: Preferred request params (MBID when available).
        :param fallback_params: Name-based params to retry with, or None for no retry.
        """
        # Last.fm's MBID index is sparse (recording MBIDs especially), so a missing MBID
        # is retried by name rather than treated as "no similar items exist".
        attempts = [primary_params]
        if fallback_params is not None:
            attempts.append(fallback_params)

        for index, params in enumerate(attempts):
            if index:
                self.logger.debug("%s: MBID lookup empty, retrying by name", log_label)
            try:
                data = await self._get_data(method, **params)
            except (TimeoutError, ClientError, InvalidDataError) as err:
                self.logger.debug("%s request failed: %s", log_label, err)
                continue

            items: list[dict[str, Any]] | dict[str, Any] = data.get(container, {}).get(item_key, [])
            # Last.fm returns a single dict when only one result is present.
            if isinstance(items, dict):
                return [items]
            if items:
                return items

        return []

    async def get_similar_artists(
        self, artist_name: str, artist_mbid: str | None = None, limit: int = 10
    ) -> list[dict[str, Any]]:
        """
        Get similar artists from Last.fm.

        :param artist_name: Name of the artist.
        :param artist_mbid: Optional MusicBrainz ID for more accurate matching.
        :param limit: Maximum number of similar artists to return.
        """
        self.logger.debug(
            "Fetching similar artists for: %s (MBID: %s)",
            artist_name,
            artist_mbid or "none",
        )
        name_params: dict[str, Any] | None = (
            {"limit": limit, "artist": artist_name, "autocorrect": 1} if artist_name else None
        )
        primary_params: dict[str, Any] | None
        fallback_params: dict[str, Any] | None
        # Lead with the MBID (exact when Last.fm has it), fall back to the name lookup.
        if artist_mbid:
            primary_params = {"limit": limit, "mbid": artist_mbid}
            fallback_params = name_params
        else:
            primary_params = name_params
            fallback_params = None

        if primary_params is None:
            return []

        return await self._get_list(
            "artist.getSimilar",
            "similarartists",
            "artist",
            "Similar artists",
            primary_params,
            fallback_params,
        )

    async def get_similar_tracks(
        self,
        artist_name: str,
        track_name: str,
        track_mbid: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """
        Get similar tracks from Last.fm.

        :param artist_name: Name of the track's artist.
        :param track_name: Name of the track.
        :param track_mbid: Optional MusicBrainz ID for more accurate matching.
        :param limit: Maximum number of similar tracks to return.
        """
        self.logger.debug(
            "Fetching similar tracks for: %s - %s (MBID: %s)",
            artist_name,
            track_name,
            track_mbid or "none",
        )
        name_params: dict[str, Any] | None = (
            {"limit": limit, "artist": artist_name, "track": track_name, "autocorrect": 1}
            if artist_name and track_name
            else None
        )
        primary_params: dict[str, Any] | None
        fallback_params: dict[str, Any] | None
        # Lead with the MBID (exact when Last.fm has it), fall back to the name lookup.
        if track_mbid:
            primary_params = {"limit": limit, "mbid": track_mbid}
            fallback_params = name_params
        else:
            primary_params = name_params
            fallback_params = None

        if primary_params is None:
            return []

        return await self._get_list(
            "track.getSimilar",
            "similartracks",
            "track",
            "Similar tracks",
            primary_params,
            fallback_params,
        )

    async def get_artist_top_tracks(
        self, artist_name: str, artist_mbid: str | None = None, limit: int = 10
    ) -> list[dict[str, Any]]:
        """
        Get an artist's top tracks from Last.fm, ordered by popularity.

        :param artist_name: Name of the artist.
        :param artist_mbid: Optional MusicBrainz ID for more accurate matching.
        :param limit: Maximum number of top tracks to return.
        """
        self.logger.debug(
            "Fetching top tracks for artist: %s (MBID: %s)",
            artist_name,
            artist_mbid or "none",
        )
        name_params: dict[str, Any] | None = (
            {"limit": limit, "artist": artist_name, "autocorrect": 1} if artist_name else None
        )
        primary_params: dict[str, Any] | None
        fallback_params: dict[str, Any] | None
        # Lead with the MBID (exact when Last.fm has it), fall back to the name lookup.
        if artist_mbid:
            primary_params = {"limit": limit, "mbid": artist_mbid}
            fallback_params = name_params
        else:
            primary_params = name_params
            fallback_params = None

        if primary_params is None:
            return []

        return await self._get_list(
            "artist.getTopTracks",
            "toptracks",
            "track",
            "Artist top tracks",
            primary_params,
            fallback_params,
        )

    async def get_chart_top_artists(self, limit: int = 10) -> list[dict[str, Any]]:
        """
        Get global top artists chart from Last.fm.

        :param limit: Maximum number of artists to return.
        """
        try:
            data = await self._get_data("chart.getTopArtists", limit=limit)
        except (TimeoutError, ClientError, InvalidDataError) as err:
            self.logger.debug("Chart top artists request failed: %s", err)
            return []

        artists: list[dict[str, Any]] | dict[str, Any] = data.get("artists", {}).get("artist", [])

        # Last.fm returns a single dict when only one result is present.
        if isinstance(artists, dict):
            return [artists]

        return artists

    async def get_chart_top_tracks(self, limit: int = 10) -> list[dict[str, Any]]:
        """
        Get global top tracks chart from Last.fm.

        :param limit: Maximum number of tracks to return.
        """
        try:
            data = await self._get_data("chart.getTopTracks", limit=limit)
        except (TimeoutError, ClientError, InvalidDataError) as err:
            self.logger.debug("Chart top tracks request failed: %s", err)
            return []

        tracks: list[dict[str, Any]] | dict[str, Any] = data.get("tracks", {}).get("track", [])

        # Last.fm returns a single dict when only one result is present.
        if isinstance(tracks, dict):
            return [tracks]

        return tracks

    async def get_user_top_artists(
        self, username: str, period: str = "overall", limit: int = 50
    ) -> list[dict[str, Any]]:
        """
        Get a user's most played artists from Last.fm, most played first.

        :param username: Last.fm username.
        :param period: Span to rank over (overall, 7day, 1month, 3month, 6month, 12month).
        :param limit: Maximum number of artists to return.
        """
        self.logger.debug("Fetching top artists for user: %s (period: %s)", username, period)
        try:
            data = await self._get_data(
                "user.getTopArtists", user=username, period=period, limit=limit
            )
        except (TimeoutError, ClientError, InvalidDataError) as err:
            self.logger.debug("User top artists request failed: %s", err)
            return []

        artists: list[dict[str, Any]] | dict[str, Any] = data.get("topartists", {}).get(
            "artist", []
        )

        # Last.fm returns a single dict when only one result is present.
        if isinstance(artists, dict):
            return [artists]

        return artists

    async def get_artist_top_tags(
        self, artist_name: str, artist_mbid: str | None = None, limit: int = 5
    ) -> list[dict[str, Any]]:
        """
        Get an artist's top community tags from Last.fm, most agreed-upon first.

        :param artist_name: Name of the artist.
        :param artist_mbid: Optional MusicBrainz ID for more accurate matching.
        :param limit: Maximum number of tags to return.
        """
        self.logger.debug(
            "Fetching top tags for artist: %s (MBID: %s)", artist_name, artist_mbid or "none"
        )
        name_params: dict[str, Any] | None = (
            {"artist": artist_name, "autocorrect": 1} if artist_name else None
        )
        primary_params: dict[str, Any] | None
        fallback_params: dict[str, Any] | None
        # Lead with the MBID (exact when Last.fm has it), fall back to the name lookup.
        if artist_mbid:
            primary_params = {"mbid": artist_mbid}
            fallback_params = name_params
        else:
            primary_params = name_params
            fallback_params = None

        if primary_params is None:
            return []

        # artist.getTopTags has no limit parameter, so cap the (count-ordered) result ourselves.
        tags = await self._get_list(
            "artist.getTopTags",
            "toptags",
            "tag",
            "Artist top tags",
            primary_params,
            fallback_params,
        )
        return tags[:limit]

    async def get_tag_top_artists(self, tag: str, limit: int = 10) -> list[dict[str, Any]]:
        """
        Get top artists for a tag from Last.fm.

        :param tag: Tag name (genre).
        :param limit: Maximum number of artists to return.
        """
        self.logger.debug("Fetching top artists for tag: %s (limit: %d)", tag, limit)
        try:
            data = await self._get_data("tag.getTopArtists", tag=tag, limit=limit)
        except (TimeoutError, ClientError, InvalidDataError) as err:
            self.logger.debug("Tag top artists request failed: %s", err)
            return []

        artists: list[dict[str, Any]] | dict[str, Any] = data.get("topartists", {}).get(
            "artist", []
        )

        # Last.fm returns a single dict when only one result is present.
        if isinstance(artists, dict):
            return [artists]

        return artists

    async def get_tag_top_albums(self, tag: str, limit: int = 10) -> list[dict[str, Any]]:
        """
        Get top albums for a tag from Last.fm.

        :param tag: Tag name (genre).
        :param limit: Maximum number of albums to return.
        """
        self.logger.debug("Fetching top albums for tag: %s (limit: %d)", tag, limit)
        try:
            data = await self._get_data("tag.getTopAlbums", tag=tag, limit=limit)
        except (TimeoutError, ClientError, InvalidDataError) as err:
            self.logger.debug("Tag top albums request failed: %s", err)
            return []

        albums: list[dict[str, Any]] | dict[str, Any] = data.get("albums", {}).get("album", [])

        # Last.fm returns a single dict when only one result is present.
        if isinstance(albums, dict):
            return [albums]

        return albums

    async def get_tag_top_tracks(self, tag: str, limit: int = 10) -> list[dict[str, Any]]:
        """
        Get top tracks for a tag from Last.fm.

        :param tag: Tag name (genre).
        :param limit: Maximum number of tracks to return.
        """
        self.logger.debug("Fetching top tracks for tag: %s (limit: %d)", tag, limit)
        try:
            data = await self._get_data("tag.getTopTracks", tag=tag, limit=limit)
        except (TimeoutError, ClientError, InvalidDataError) as err:
            self.logger.debug("Tag top tracks request failed: %s", err)
            return []

        tracks: list[dict[str, Any]] | dict[str, Any] = data.get("tracks", {}).get("track", [])

        # Last.fm returns a single dict when only one result is present.
        if isinstance(tracks, dict):
            return [tracks]

        return tracks

    async def get_geo_top_artists(self, country: str, limit: int = 10) -> list[dict[str, Any]]:
        """
        Get top artists for a country from Last.fm.

        :param country: Country name (e.g., "United States", "Spain").
        :param limit: Maximum number of artists to return.
        """
        self.logger.debug("Fetching geo top artists for country: %s (limit: %d)", country, limit)
        try:
            data = await self._get_data("geo.getTopArtists", country=country, limit=limit)
        except (TimeoutError, ClientError, InvalidDataError) as err:
            self.logger.debug("Geo top artists request failed: %s", err)
            return []

        artists: list[dict[str, Any]] | dict[str, Any] = data.get("topartists", {}).get(
            "artist", []
        )

        # Last.fm returns a single dict when only one result is present.
        if isinstance(artists, dict):
            return [artists]

        return artists

    async def get_geo_top_tracks(self, country: str, limit: int = 10) -> list[dict[str, Any]]:
        """
        Get top tracks for a country from Last.fm.

        :param country: Country name (e.g., "United States", "Spain").
        :param limit: Maximum number of tracks to return.
        """
        self.logger.debug("Fetching geo top tracks for country: %s (limit: %d)", country, limit)
        try:
            data = await self._get_data("geo.getTopTracks", country=country, limit=limit)
        except (TimeoutError, ClientError, InvalidDataError) as err:
            self.logger.debug("Geo top tracks request failed: %s", err)
            return []

        tracks: list[dict[str, Any]] | dict[str, Any] = data.get("tracks", {}).get("track", [])

        # Last.fm returns a single dict when only one result is present.
        if isinstance(tracks, dict):
            return [tracks]

        return tracks
