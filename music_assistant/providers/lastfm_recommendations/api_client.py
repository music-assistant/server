"""Last.fm API client for recommendations."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aiohttp import ClientError
from music_assistant_models.errors import InvalidDataError

from music_assistant.helpers.throttle_retry import ThrottlerManager

if TYPE_CHECKING:
    from aiohttp import ClientSession

    from music_assistant.providers.lastfm_recommendations import LastFMRecommendationsProvider


class LastFMAPIClient:
    """Last.fm API client for fetching recommendations."""

    BASE_URL = "https://ws.audioscrobbler.com/2.0/"
    throttler = ThrottlerManager(rate_limit=5, period=1)  # 5 requests per second

    # Last.fm error codes that should be logged as warnings
    CRITICAL_ERROR_CODES = {
        4,  # Authentication Failed
        10,  # Invalid API key
        26,  # Suspended API key
        29,  # Rate limit exceeded
    }

    def __init__(self, provider: LastFMRecommendationsProvider) -> None:
        """Initialize Last.fm API client.

        :param provider: The Last.fm recommendations provider instance.
        """
        self.provider = provider
        self.logger = provider.logger
        self.http_session: ClientSession = provider.mass.http_session

    async def _get_data(self, method: str, **params: Any) -> dict[str, Any]:
        """Make a request to the Last.fm API.

        :param method: The Last.fm API method to call.
        :param params: Additional query parameters.
        """
        async with self.throttler.acquire():
            params.update(
                {
                    "method": method,
                    "api_key": self.provider.config.get_value("api_key"),
                    "format": "json",
                }
            )

            async with self.http_session.get(self.BASE_URL, params=params) as response:
                response.raise_for_status()
                data: dict[str, Any] = await response.json()

                # Last.fm returns errors in the response body rather than as an HTTP status.
                if "error" in data:
                    error_code = data.get("error", 0)
                    error_msg = data.get("message", "Unknown error")

                    if error_code in self.CRITICAL_ERROR_CODES:
                        self.logger.warning(
                            "Last.fm API error %s: %s (method: %s)",
                            error_code,
                            error_msg,
                            method,
                        )
                    else:
                        self.logger.debug(
                            "Last.fm API error %s: %s (method: %s)",
                            error_code,
                            error_msg,
                            method,
                        )

                    msg = f"Last.fm API error {error_code}: {error_msg}"
                    raise InvalidDataError(msg)

                return data

    async def get_similar_artists(
        self, artist_name: str, artist_mbid: str | None = None, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Get similar artists from Last.fm.

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

        try:
            self.logger.debug(
                "Fetching similar artists for: %s (MBID: %s)",
                artist_name,
                artist_mbid or "none",
            )
            data = await self._get_data("artist.getSimilar", **params)
            similar_artists: list[dict[str, Any]] | dict[str, Any] = data.get(
                "similarartists", {}
            ).get("artist", [])

            # Last.fm returns a single dict when only one result is present.
            if isinstance(similar_artists, dict):
                return [similar_artists]

            return similar_artists

        except (TimeoutError, ClientError, InvalidDataError, KeyError):
            return []

    async def get_similar_tracks(
        self,
        artist_name: str,
        track_name: str,
        track_mbid: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Get similar tracks from Last.fm.

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

        try:
            self.logger.debug(
                "Fetching similar tracks for: %s - %s (MBID: %s)",
                artist_name,
                track_name,
                track_mbid or "none",
            )
            data = await self._get_data("track.getSimilar", **params)
            similar_tracks: list[dict[str, Any]] | dict[str, Any] = data.get(
                "similartracks", {}
            ).get("track", [])

            # Last.fm returns a single dict when only one result is present.
            if isinstance(similar_tracks, dict):
                return [similar_tracks]

            return similar_tracks

        except (TimeoutError, ClientError, InvalidDataError, KeyError):
            return []

    async def get_chart_top_artists(self, limit: int = 10) -> list[dict[str, Any]]:
        """Get global top artists chart from Last.fm.

        :param limit: Maximum number of artists to return.
        """
        try:
            data = await self._get_data("chart.getTopArtists", limit=limit)
            artists: list[dict[str, Any]] | dict[str, Any] = data.get("artists", {}).get(
                "artist", []
            )

            # Last.fm returns a single dict when only one result is present.
            if isinstance(artists, dict):
                return [artists]

            return artists

        except (TimeoutError, ClientError, InvalidDataError, KeyError):
            return []

    async def get_chart_top_tracks(self, limit: int = 10) -> list[dict[str, Any]]:
        """Get global top tracks chart from Last.fm.

        :param limit: Maximum number of tracks to return.
        """
        try:
            data = await self._get_data("chart.getTopTracks", limit=limit)
            tracks: list[dict[str, Any]] | dict[str, Any] = data.get("tracks", {}).get("track", [])

            # Last.fm returns a single dict when only one result is present.
            if isinstance(tracks, dict):
                return [tracks]

            return tracks

        except (TimeoutError, ClientError, InvalidDataError, KeyError):
            return []

    async def get_user_top_tags(self, username: str, limit: int = 1) -> list[dict[str, Any]]:
        """Get a user's top tags from Last.fm.

        :param username: Last.fm username.
        :param limit: Maximum number of tags to return (default 1 for top genre).
        """
        try:
            self.logger.debug("Fetching top tags for user: %s", username)
            data = await self._get_data("user.getTopTags", user=username, limit=limit)
            tags: list[dict[str, Any]] | dict[str, Any] = data.get("toptags", {}).get("tag", [])

            # Last.fm returns a single dict when only one result is present.
            if isinstance(tags, dict):
                return [tags]

            return tags

        except (TimeoutError, ClientError, InvalidDataError, KeyError):
            return []

    async def get_tag_top_artists(self, tag: str, limit: int = 10) -> list[dict[str, Any]]:
        """Get top artists for a tag from Last.fm.

        :param tag: Tag name (genre).
        :param limit: Maximum number of artists to return.
        """
        try:
            self.logger.debug("Fetching top artists for tag: %s (limit: %d)", tag, limit)
            data = await self._get_data("tag.getTopArtists", tag=tag, limit=limit)
            artists: list[dict[str, Any]] | dict[str, Any] = data.get("topartists", {}).get(
                "artist", []
            )

            # Last.fm returns a single dict when only one result is present.
            if isinstance(artists, dict):
                return [artists]

            return artists

        except (TimeoutError, ClientError, InvalidDataError, KeyError):
            return []

    async def get_tag_top_albums(self, tag: str, limit: int = 10) -> list[dict[str, Any]]:
        """Get top albums for a tag from Last.fm.

        :param tag: Tag name (genre).
        :param limit: Maximum number of albums to return.
        """
        try:
            self.logger.debug("Fetching top albums for tag: %s (limit: %d)", tag, limit)
            data = await self._get_data("tag.getTopAlbums", tag=tag, limit=limit)
            albums: list[dict[str, Any]] | dict[str, Any] = data.get("albums", {}).get("album", [])

            # Last.fm returns a single dict when only one result is present.
            if isinstance(albums, dict):
                return [albums]

            return albums

        except (TimeoutError, ClientError, InvalidDataError, KeyError):
            return []

    async def get_tag_top_tracks(self, tag: str, limit: int = 10) -> list[dict[str, Any]]:
        """Get top tracks for a tag from Last.fm.

        :param tag: Tag name (genre).
        :param limit: Maximum number of tracks to return.
        """
        try:
            self.logger.debug("Fetching top tracks for tag: %s (limit: %d)", tag, limit)
            data = await self._get_data("tag.getTopTracks", tag=tag, limit=limit)
            tracks: list[dict[str, Any]] | dict[str, Any] = data.get("tracks", {}).get("track", [])

            # Last.fm returns a single dict when only one result is present.
            if isinstance(tracks, dict):
                return [tracks]

            return tracks

        except (TimeoutError, ClientError, InvalidDataError, KeyError):
            return []

    async def get_geo_top_artists(self, country: str, limit: int = 10) -> list[dict[str, Any]]:
        """Get top artists for a country from Last.fm.

        :param country: Country name (e.g., "United States", "Spain").
        :param limit: Maximum number of artists to return.
        """
        try:
            self.logger.debug(
                "Fetching geo top artists for country: %s (limit: %d)", country, limit
            )
            data = await self._get_data("geo.getTopArtists", country=country, limit=limit)
            artists: list[dict[str, Any]] | dict[str, Any] = data.get("topartists", {}).get(
                "artist", []
            )

            # Last.fm returns a single dict when only one result is present.
            if isinstance(artists, dict):
                return [artists]

            return artists

        except (TimeoutError, ClientError, InvalidDataError, KeyError):
            return []

    async def get_geo_top_tracks(self, country: str, limit: int = 10) -> list[dict[str, Any]]:
        """Get top tracks for a country from Last.fm.

        :param country: Country name (e.g., "United States", "Spain").
        :param limit: Maximum number of tracks to return.
        """
        try:
            self.logger.debug("Fetching geo top tracks for country: %s (limit: %d)", country, limit)
            data = await self._get_data("geo.getTopTracks", country=country, limit=limit)
            tracks: list[dict[str, Any]] | dict[str, Any] = data.get("tracks", {}).get("track", [])

            # Last.fm returns a single dict when only one result is present.
            if isinstance(tracks, dict):
                return [tracks]

            return tracks

        except (TimeoutError, ClientError, InvalidDataError, KeyError):
            return []
