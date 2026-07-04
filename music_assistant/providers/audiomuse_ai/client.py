"""
Async HTTP client for an external AudioMuse-AI server.

Only the handful of endpoints Music Assistant needs are exposed: similar-track
lookup, CLAP/lyrics free-text search, and a health/stats probe. Every track
identifier crossing this boundary is the media-server item id that AudioMuse-AI
shares with the corresponding Music Assistant provider.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aiohttp import ClientError, ClientTimeout

from music_assistant.providers.audiomuse_ai.constants import REQUEST_TIMEOUT

if TYPE_CHECKING:
    from logging import Logger

    from aiohttp import ClientSession


class AudioMuseError(Exception):
    """Raised when an AudioMuse-AI API call fails or returns a non-200 status."""


class AudioMuseClient:
    """Thin async wrapper over the AudioMuse-AI REST API."""

    def __init__(
        self,
        session: ClientSession,
        base_url: str,
        token: str | None,
        logger: Logger,
    ) -> None:
        """
        Initialize the client.

        :param session: Shared aiohttp session (``mass.http_session``).
        :param base_url: AudioMuse-AI server root, e.g. ``http://host:8000``.
        :param token: Optional API token sent as a ``Bearer`` header.
        :param logger: Provider logger.
        """
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._timeout = ClientTimeout(total=REQUEST_TIMEOUT)
        self.logger = logger

    async def health(self) -> bool:
        """Return True when the server answers its health probe with status ok."""
        try:
            data = await self._request("GET", "/api/health")
        except AudioMuseError:
            return False
        return isinstance(data, dict) and data.get("status") == "ok"

    async def similar_tracks(self, item_id: str, limit: int) -> list[dict[str, Any]]:
        """
        Return AudioMuse-AI's nearest tracks for a seed item id.

        :param item_id: Media-server item id of the seed track.
        :param limit: Max neighbours to request (maps to the API's ``n``).
        """
        data = await self._request(
            "GET", "/api/similar_tracks", params={"item_id": item_id, "n": limit}
        )
        return data if isinstance(data, list) else []

    async def clap_search(self, query: str, limit: int) -> list[dict[str, Any]]:
        """
        Return tracks matching a free-text CLAP (mood/genre/instrument) query.

        :param query: Natural-language query.
        :param limit: Max matches to request.
        """
        data = await self._request(
            "POST", "/api/clap/search", json_body={"query": query, "limit": limit}
        )
        return data.get("results", []) if isinstance(data, dict) else []

    async def lyrics_search(self, query: str, limit: int) -> list[dict[str, Any]]:
        """
        Return tracks matching a free-text semantic lyrics query.

        :param query: Natural-language query.
        :param limit: Max matches to request.
        """
        data = await self._request(
            "POST", "/api/lyrics/search/text", json_body={"query": query, "limit": limit}
        )
        return data.get("results", []) if isinstance(data, dict) else []

    async def clap_stats(self) -> dict[str, Any]:
        """Return the CLAP index stats blob; empty dict when unavailable."""
        try:
            data = await self._request("GET", "/api/clap/stats")
        except AudioMuseError:
            return {}
        return data if isinstance(data, dict) else {}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        """
        Perform one request, returning parsed JSON or raising AudioMuseError.

        :param method: HTTP method.
        :param path: Path appended to the configured base URL.
        :param params: Optional query string parameters.
        :param json_body: Optional JSON request body.
        """
        url = f"{self._base_url}{path}"
        try:
            async with self._session.request(
                method,
                url,
                params=params,
                json=json_body,
                headers=self._headers,
                timeout=self._timeout,
            ) as resp:
                if resp.status != 200:
                    body = (await resp.text())[:200]
                    msg = f"AudioMuse-AI {method} {path} returned {resp.status}: {body}"
                    raise AudioMuseError(msg)
                return await resp.json()
        except ClientError as err:
            msg = f"AudioMuse-AI {method} {path} failed: {err}"
            raise AudioMuseError(msg) from err
