"""HTTP client for the BookOrbit API (JWT access token + refresh cookie)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from aiohttp import ClientResponseError, ClientSession

from music_assistant.providers.bookorbit.constants import API_PREFIX, AUDIO_FORMATS
from music_assistant.providers.bookorbit.helpers import normalize_base_url

if TYPE_CHECKING:
    from collections.abc import Mapping

LOGGER = logging.getLogger(__name__)

REFRESH_COOKIE = "refresh_token"
QUERY_PAGE_SIZE = 100
QUERY_MAX_PAGES = 1000  # safety bound against a server that ignores paging
_ITEM_KEYS = ("items", "content", "data", "books", "results")


class BookOrbitAuthError(Exception):
    """Raised when authentication against BookOrbit fails."""


class BookOrbitClient:
    """
    Minimal async client for BookOrbit.

    Tokens are held on the instance only (not persisted); the client re-logins
    with the configured credentials when refresh is no longer possible.
    """

    def __init__(
        self,
        session: ClientSession,
        base_url: str,
        username: str,
        password: str,
        verify_ssl: bool = True,
    ) -> None:
        """Initialize the client."""
        self.session = session
        self.base_url = normalize_base_url(base_url)
        self.username = username
        self.password = password
        self.verify_ssl = verify_ssl
        self.access_token: str | None = None
        self.refresh_token: str | None = None

    async def login(self) -> None:
        """Log in with username/password; store access token and refresh cookie."""
        async with self.session.post(
            self._url(f"{API_PREFIX}/auth/login"),
            json={"username": self.username, "password": self.password},
            ssl=self.verify_ssl,
        ) as resp:
            if resp.status >= 400:
                raise BookOrbitAuthError(f"BookOrbit login failed: HTTP {resp.status}")
            data = await resp.json()
            token = data.get("accessToken") if isinstance(data, dict) else None
            if not token:
                raise BookOrbitAuthError("BookOrbit login response has no accessToken")
            self.access_token = token
            self._capture_refresh_cookie(resp)

    async def refresh(self) -> bool:
        """Refresh the access token using the refresh cookie. Returns success."""
        if not self.refresh_token:
            return False
        try:
            async with self.session.post(
                self._url(f"{API_PREFIX}/auth/refresh"),
                headers={"Cookie": f"{REFRESH_COOKIE}={self.refresh_token}"},
                ssl=self.verify_ssl,
            ) as resp:
                if resp.status >= 400:
                    return False
                data = await resp.json()
                token = data.get("accessToken") if isinstance(data, dict) else None
                if not token:
                    return False
                self.access_token = token
                self._capture_refresh_cookie(resp)  # cookie may be rotated
                return True
        except ClientResponseError:
            return False

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Make an authenticated request; on 401 refresh (then login) once and retry."""
        if not self.access_token:
            await self.login()
        status, data = await self._send(method, path, **kwargs)
        if status != 401:
            return data
        if not await self.refresh():
            await self.login()
        status, data = await self._send(method, path, **kwargs)
        if status == 401:
            raise BookOrbitAuthError(f"{method} {path}: HTTP 401 after re-authentication")
        return data

    async def list_libraries(self) -> list[dict[str, Any]]:
        """Return all libraries."""
        return await self.request("GET", f"{API_PREFIX}/libraries")  # type: ignore[no-any-return]

    async def query_audiobooks(self, *, library_id: int | None = None) -> list[dict[str, Any]]:
        """Return all audio-format books (optionally limited to one library)."""
        books: list[dict[str, Any]] = []
        for page in range(QUERY_MAX_PAGES):
            payload = await self.request(
                "POST",
                f"{API_PREFIX}/books/query",
                json=self._build_query(library_id, page),
            )
            items = self._extract_items(payload)
            books.extend(items)
            if not items or not isinstance(payload, dict):
                break
            total_pages = payload.get("totalPages")
            if total_pages is not None:
                if page + 1 >= int(total_pages):
                    break
            elif len(items) < QUERY_PAGE_SIZE:
                break
        return books

    async def get_book(self, book_id: int) -> dict[str, Any]:
        """Return a single book."""
        return await self.request("GET", f"{API_PREFIX}/books/{book_id}")  # type: ignore[no-any-return]

    async def get_audio_progress(self, book_id: int) -> dict[str, Any] | None:
        """Return audio progress for a book, or None if there is none."""
        try:
            data = await self.request("GET", f"{API_PREFIX}/books/{book_id}/audio-progress")
        except ClientResponseError as err:
            if err.status == 404:
                return None
            raise
        return data or None

    async def upsert_audio_progress(
        self,
        book_id: int,
        *,
        percentage: float,
        current_file_id: int,
        position_seconds: float,
    ) -> None:
        """Create/update audio progress for a book."""
        await self.request(
            "PATCH",
            f"{API_PREFIX}/books/{book_id}/audio-progress",
            json={
                "percentage": percentage,
                "currentFileId": current_file_id,
                "positionSeconds": position_seconds,
            },
        )

    def stream_url(self, file_id: int) -> str:
        """Return the absolute URL serving a book file."""
        return f"{self.base_url}{API_PREFIX}/books/files/{file_id}/serve"

    def _url(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        return f"{self.base_url}{path}"

    def _capture_refresh_cookie(self, response: Any) -> None:
        morsel = response.cookies.get(REFRESH_COOKIE)
        if morsel is not None and morsel.value:
            self.refresh_token = morsel.value

    def _headers(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        headers: dict[str, str] = dict(extra or {})
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        if self.refresh_token:
            headers["Cookie"] = f"{REFRESH_COOKIE}={self.refresh_token}"
        return headers

    async def _send(self, method: str, path: str, **kwargs: Any) -> tuple[int, Any]:
        headers = self._headers(kwargs.pop("headers", None))
        async with self.session.request(
            method, self._url(path), headers=headers, ssl=self.verify_ssl, **kwargs
        ) as resp:
            status = resp.status
            if status in {401, 204}:
                return status, None
            if status >= 400:
                raise ClientResponseError(
                    resp.request_info,
                    resp.history,
                    status=status,
                    message=f"{method} {path}: HTTP {status}",
                )
            if "json" in (resp.content_type or ""):
                return status, await resp.json()
            return status, await resp.read()

    @staticmethod
    def _build_query(library_id: int | None, page: int) -> dict[str, Any]:
        # NOTE: body shape is a best guess at BookOrbit's BookQuery (not yet verified
        # against a live instance); kept in one place so it is easy to adjust.
        rules: list[dict[str, Any]] = [
            {"field": "format", "operator": "includesAny", "value": sorted(AUDIO_FORMATS)}
        ]
        if library_id is not None:
            rules.append({"field": "library", "operator": "eq", "value": library_id})
        return {
            "filter": {"conjunction": "and", "rules": rules},
            "page": page,
            "pageSize": QUERY_PAGE_SIZE,
        }

    @staticmethod
    def _extract_items(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in _ITEM_KEYS:
                if isinstance(payload.get(key), list):
                    return payload[key]  # type: ignore[no-any-return]
        return []
