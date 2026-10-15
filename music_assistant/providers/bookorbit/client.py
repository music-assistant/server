"""HTTP client for the BookOrbit API (JWT access token + refresh cookie)."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from aiohttp import (
    ClientResponse,
    ClientResponseError,
    ClientSession,
    ClientTimeout,
    DummyCookieJar,
)

from music_assistant.providers.bookorbit.constants import (
    AIOHTTP_TIMEOUT,
    API_PREFIX,
    AUDIO_FORMATS,
)
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
    """Async client for the BookOrbit ``/api/v1`` endpoints."""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        verify_ssl: bool = True,
        session: ClientSession | None = None,
    ) -> None:
        """
        Initialize the client.

        :param base_url: BookOrbit server URL.
        :param username: Login username.
        :param password: Login password.
        :param verify_ssl: Whether to verify TLS certificates.
        :param session: Optional shared session; when omitted the client owns one.
        """
        self._session = session
        self._owns_session = session is None
        self.base_url = normalize_base_url(base_url)
        self.username = username
        self.password = password
        self.verify_ssl = verify_ssl
        self.access_token: str | None = None
        self.refresh_token: str | None = None
        self._auth_lock = asyncio.Lock()

    @property
    def session(self) -> ClientSession:
        """Return the HTTP session (created on first use when owned)."""
        if self._session is None or (self._owns_session and self._session.closed):
            # Own a dummy jar so the refresh cookie never leaks into a shared session.
            self._session = ClientSession(cookie_jar=DummyCookieJar(), timeout=AIOHTTP_TIMEOUT)
        return self._session

    async def close(self) -> None:
        """Close the session if it is owned by this client."""
        if self._owns_session and self._session is not None and not self._session.closed:
            await self._session.close()

    async def login(self) -> None:
        """Log in with username/password and store access token and refresh cookie."""
        async with self._auth_lock:
            await self._login()

    async def refresh(self) -> bool:
        """Refresh the access token using the refresh cookie."""
        async with self._auth_lock:
            return await self._refresh()

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        """
        Make an authenticated request, refreshing once on HTTP 401.

        :param method: HTTP method.
        :param path: Absolute path or URL under the BookOrbit API.
        :returns: Parsed JSON, response bytes, or ``None``.
        :raises BookOrbitAuthError: If authentication still fails after retry.
        """
        await self._ensure_token()
        token = self.access_token
        status, data = await self._send(method, path, **kwargs)
        if status != 401:
            return data
        await self._reauthenticate(token)
        status, data = await self._send(method, path, **kwargs)
        if status == 401:
            raise BookOrbitAuthError(f"{method} {path}: HTTP 401 after re-authentication")
        return data

    async def list_libraries(self) -> list[dict[str, Any]]:
        """Return all libraries."""
        return await self.request("GET", f"{API_PREFIX}/libraries")  # type: ignore[no-any-return]

    async def query_audiobooks(self, *, library_id: int | None = None) -> list[dict[str, Any]]:
        """
        Return all audio-format books.

        :param library_id: When set, limit the query to that library.
        """
        if library_id is not None:
            query_path = f"{API_PREFIX}/libraries/{library_id}/books"
        else:
            query_path = f"{API_PREFIX}/books/query"
        books: list[dict[str, Any]] = []
        for page in range(QUERY_MAX_PAGES):
            payload = await self.request(
                "POST",
                query_path,
                json=self._build_query(page),
            )
            items = self._extract_items(payload)
            books.extend(items)
            if not items or not isinstance(payload, dict):
                break
            total = payload.get("total")
            page_size = int(payload.get("size") or QUERY_PAGE_SIZE)
            if total is not None:
                if len(books) >= int(total):
                    break
            elif len(items) < page_size:
                break
        return books

    async def get_book(self, book_id: int) -> dict[str, Any]:
        """Return a single book detail payload."""
        return await self.request("GET", f"{API_PREFIX}/books/{book_id}")  # type: ignore[no-any-return]

    async def search_books(self, query: str) -> list[dict[str, Any]]:
        """Search books by query string."""
        return await self._search("books/search", query)

    async def list_authors(self) -> list[dict[str, Any]]:
        """Return all authors, including photo fields when present."""
        authors: list[dict[str, Any]] = []
        for page in range(QUERY_MAX_PAGES):
            payload = await self.request(
                "GET",
                f"{API_PREFIX}/authors",
                params={"page": page, "size": QUERY_PAGE_SIZE},
            )
            items = self._extract_items(payload)
            authors.extend(items)
            if not items or not isinstance(payload, dict):
                break
            total = payload.get("total")
            page_size = int(payload.get("size") or QUERY_PAGE_SIZE)
            if total is not None:
                if len(authors) >= int(total):
                    break
            elif len(items) < page_size:
                break
        return authors

    async def search_authors(self, query: str) -> list[dict[str, Any]]:
        """Search authors in the catalog."""
        return await self._search("metadata/authors", query)

    async def search_narrators(self, query: str) -> list[dict[str, Any]]:
        """Search narrators in the catalog."""
        return await self._search("metadata/narrators", query)

    async def get_audio_progress(self, book_id: int) -> dict[str, Any] | None:
        """Return legacy audio progress for a book, or ``None`` if absent."""
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
        """Create or update legacy audio progress for a book."""
        await self.request(
            "PATCH",
            f"{API_PREFIX}/books/{book_id}/audio-progress",
            json={
                "percentage": percentage,
                "currentFileId": current_file_id,
                "positionSeconds": position_seconds,
            },
        )

    async def get_playback_state(self, book_id: int) -> dict[str, Any] | None:
        """Return audiobook playback state, or ``None`` if absent."""
        try:
            data = await self.request("GET", f"{API_PREFIX}/audiobooks/{book_id}/playback-state")
        except ClientResponseError as err:
            if err.status == 404:
                return None
            raise
        return data or None

    async def put_playback_state(
        self,
        book_id: int,
        *,
        asset_id: str,
        position_ms: int,
        captured_at: str,
        operation_id: str,
        base_revision: int,
        manifest_revision: str,
    ) -> dict[str, Any]:
        """Create or update audiobook playback state."""
        return await self.request(  # type: ignore[no-any-return]
            "PUT",
            f"{API_PREFIX}/audiobooks/{book_id}/playback-state",
            json={
                "assetId": asset_id,
                "positionMs": position_ms,
                "capturedAt": captured_at,
                "operationId": operation_id,
                "baseRevision": base_revision,
                "manifestRevision": manifest_revision,
            },
        )

    async def get_cover(self, url: str) -> bytes | None:
        """Return cover image bytes for the given URL, or ``None`` if missing."""
        try:
            data = await self.request("GET", url)
        except ClientResponseError as err:
            if err.status == 404:
                return None
            raise
        return data if isinstance(data, bytes) and data else None

    async def get_audiobook_manifest(self, book_id: int) -> dict[str, Any]:
        """Return the audiobook playback manifest for a book."""
        return await self.request(  # type: ignore[no-any-return]
            "GET", f"{API_PREFIX}/audiobooks/{book_id}/manifest"
        )

    def stream_url(self, book_id: int, asset_id: str) -> str:
        """Return the absolute URL that serves one audiobook asset."""
        # Audio must use the audiobook content route; /books/files/{id}/serve rejects audio.
        return f"{self.base_url}{API_PREFIX}/audiobooks/{book_id}/assets/{asset_id}/content"

    async def open_file(
        self,
        book_id: int,
        asset_id: str,
        *,
        range_header: str | None = None,
        timeout: ClientTimeout | None = None,
    ) -> ClientResponse:
        """
        Open an audiobook asset for streaming.

        The caller must release the response. On HTTP 401 the token is refreshed once
        and the request is retried.

        :param book_id: Book identifier.
        :param asset_id: Manifest asset identifier.
        :param range_header: Optional HTTP Range header value.
        :param timeout: Optional request timeout override.
        :returns: Open aiohttp response.
        :raises BookOrbitAuthError: If authentication still fails after retry.
        """
        await self._ensure_token()
        extra = {"Range": range_header} if range_header else None
        for attempt in range(2):
            token = self.access_token
            kwargs: dict[str, Any] = {"headers": self._headers(extra), "ssl": self.verify_ssl}
            if timeout is not None:
                kwargs["timeout"] = timeout
            resp = await self.session.get(self.stream_url(book_id, asset_id), **kwargs)
            if resp.status != 401:
                return resp
            resp.release()
            if attempt == 0:
                await self._reauthenticate(token)
        raise BookOrbitAuthError(
            f"audiobook {book_id} asset {asset_id}: HTTP 401 after re-authentication"
        )

    async def _ensure_token(self) -> None:
        """Ensure an access token is available before a request."""
        if self.access_token:
            return
        async with self._auth_lock:
            if not self.access_token:
                await self._login()

    async def _reauthenticate(self, rejected_token: str | None) -> None:
        """Replace a rejected access token via refresh or login."""
        async with self._auth_lock:
            if self.access_token and self.access_token != rejected_token:
                return
            if not await self._refresh():
                await self._login()

    async def _login(self) -> None:
        """Perform username/password login and capture tokens."""
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

    async def _refresh(self) -> bool:
        """Attempt token refresh; return whether a new access token was stored."""
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

    async def _search(self, endpoint: str, query: str) -> list[dict[str, Any]]:
        """Run a GET search endpoint and return item dicts."""
        payload = await self.request("GET", f"{API_PREFIX}/{endpoint}", params={"q": query})
        return self._extract_items(payload)

    def _url(self, path: str) -> str:
        """Build an absolute URL from a path or return an absolute URL unchanged."""
        if path.startswith(("http://", "https://")):
            return path
        return f"{self.base_url}{path}"

    def _capture_refresh_cookie(self, response: Any) -> None:
        """Store the refresh cookie from an auth response when present."""
        morsel = response.cookies.get(REFRESH_COOKIE)
        if morsel is not None and morsel.value:
            self.refresh_token = morsel.value

    def _headers(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        """Build request headers including the bearer token when available."""
        headers: dict[str, str] = dict(extra or {})
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        return headers

    async def _send(self, method: str, path: str, **kwargs: Any) -> tuple[int, Any]:
        """Send one HTTP request and return status plus body."""
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
    def _build_query(page: int) -> dict[str, Any]:
        """Build a BookOrbit books query payload for one page."""
        return {
            "filter": {
                "type": "group",
                "join": "AND",
                "rules": [
                    {
                        "type": "rule",
                        "field": "format",
                        "operator": "includesAny",
                        "value": sorted(AUDIO_FORMATS),
                    }
                ],
            },
            "sort": [{"field": "title", "dir": "asc"}],
            "pagination": {"page": page, "size": QUERY_PAGE_SIZE},
        }

    @staticmethod
    def _extract_items(payload: Any) -> list[dict[str, Any]]:
        """Extract a list of item dicts from a list or paged payload."""
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in _ITEM_KEYS:
                if isinstance(payload.get(key), list):
                    return payload[key]  # type: ignore[no-any-return]
        return []
