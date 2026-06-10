"""Last.fm API client for recommendations."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aiohttp import ClientError
from music_assistant_models.errors import (
    AuthenticationFailed,
    InvalidDataError,
    InvalidToken,
    MusicAssistantError,
    ResourceTemporarilyUnavailable,
)

from music_assistant.helpers.app_vars import app_var  # type: ignore[attr-defined]
from music_assistant.helpers.throttle_retry import ThrottlerManager
from music_assistant.providers.lastfm_recommendations.constants import CONF_API_KEY

# Built-in Last.fm API key (read-only access, no secret needed)
_DEFAULT_API_KEY: str = app_var(12)

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
    _ERROR_MAP: dict[int, type[MusicAssistantError]] = {
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

    async def get_similar_artists(
        self, artist_name: str, artist_mbid: str | None = None, limit: int = 10
    ) -> list[dict[str, Any]]:
        """
        Get similar artists from Last.fm.

        :param artist_name: Name of the artist.
        :param artist_mbid: Optional MusicBrainz ID for more accurate matching.
        :param limit: Maximum number of similar artists to return.
        """
        params: dict[str, Any] = {"limit": limit}

        # Prefer MBID for more accurate matching; fall back to name with autocorrect.
        if artist_mbid:
            params["mbid"] = artist_mbid
        else:
            params["artist"] = artist_name
            params["autocorrect"] = 1

        self.logger.debug(
            "Fetching similar artists for: %s (MBID: %s)",
            artist_name,
            artist_mbid or "none",
        )
        try:
            data = await self._get_data("artist.getSimilar", **params)
        except (TimeoutError, ClientError, InvalidDataError) as err:
            self.logger.debug("Similar artists request failed: %s", err)
            return []

        similar_artists: list[dict[str, Any]] | dict[str, Any] = data.get("similarartists", {}).get(
            "artist", []
        )

        # Last.fm returns a single dict when only one result is present.
        if isinstance(similar_artists, dict):
            return [similar_artists]

        return similar_artists

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
        params: dict[str, Any] = {"limit": limit}

        # Prefer MBID for more accurate matching; fall back to name with autocorrect.
        if track_mbid:
            params["mbid"] = track_mbid
        else:
            params["artist"] = artist_name
            params["track"] = track_name
            params["autocorrect"] = 1

        self.logger.debug(
            "Fetching similar tracks for: %s - %s (MBID: %s)",
            artist_name,
            track_name,
            track_mbid or "none",
        )
        try:
            data = await self._get_data("track.getSimilar", **params)
        except (TimeoutError, ClientError, InvalidDataError) as err:
            self.logger.debug("Similar tracks request failed: %s", err)
            return []

        similar_tracks: list[dict[str, Any]] | dict[str, Any] = data.get("similartracks", {}).get(
            "track", []
        )

        # Last.fm returns a single dict when only one result is present.
        if isinstance(similar_tracks, dict):
            return [similar_tracks]

        return similar_tracks

    async def get_artist_top_tracks(
        self, artist_name: str, artist_mbid: str | None = None, limit: int = 10
    ) -> list[dict[str, Any]]:
        """
        Get an artist's top tracks from Last.fm, ordered by popularity.

        :param artist_name: Name of the artist.
        :param artist_mbid: Optional MusicBrainz ID for more accurate matching.
        :param limit: Maximum number of top tracks to return.
        """
        params: dict[str, Any] = {"limit": limit}

        # Prefer MBID for more accurate matching; fall back to name with autocorrect.
        if artist_mbid:
            params["mbid"] = artist_mbid
        else:
            params["artist"] = artist_name
            params["autocorrect"] = 1

        self.logger.debug(
            "Fetching top tracks for artist: %s (MBID: %s)",
            artist_name,
            artist_mbid or "none",
        )
        try:
            data = await self._get_data("artist.getTopTracks", **params)
        except (TimeoutError, ClientError, InvalidDataError) as err:
            self.logger.debug("Artist top tracks request failed: %s", err)
            return []

        tracks: list[dict[str, Any]] | dict[str, Any] = data.get("toptracks", {}).get("track", [])

        # Last.fm returns a single dict when only one result is present.
        if isinstance(tracks, dict):
            return [tracks]

        return tracks

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

    async def get_user_top_tags(self, username: str, limit: int = 1) -> list[dict[str, Any]]:
        """
        Get a user's top tags from Last.fm.

        :param username: Last.fm username.
        :param limit: Maximum number of tags to return (default 1 for top genre).
        """
        self.logger.debug("Fetching top tags for user: %s", username)
        try:
            data = await self._get_data("user.getTopTags", user=username, limit=limit)
        except (TimeoutError, ClientError, InvalidDataError) as err:
            self.logger.debug("User top tags request failed: %s", err)
            return []

        tags: list[dict[str, Any]] | dict[str, Any] = data.get("toptags", {}).get("tag", [])

        # Last.fm returns a single dict when only one result is present.
        if isinstance(tags, dict):
            return [tags]

        return tags

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
