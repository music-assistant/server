"""Read-only client based on the inspected official web application's protocol."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
import time
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Self, cast
from urllib.parse import quote, urlencode, urlsplit
from weakref import WeakValueDictionary

import aiohttp
from music_assistant_models.errors import (
    InvalidDataError,
    LoginFailed,
    MediaNotFoundError,
    ProviderPermissionDenied,
    RateLimited,
    ResourceTemporarilyUnavailable,
    UnplayableMediaError,
)

_LOGIN_LOCKS: WeakValueDictionary[tuple[asyncio.AbstractEventLoop, str], asyncio.Lock] = (
    WeakValueDictionary()
)


class AuthenticationError(LoginFailed):
    """Authentication is required or rejected."""


class NotFoundError(MediaNotFoundError):
    """A resource was not found."""


class PermissionDeniedError(ProviderPermissionDenied):
    """The server explicitly rejected an operation (HTTP 403)."""


class StreamRejectedError(UnplayableMediaError):
    """The stream endpoint refused audio; the precise reason may be version-specific."""


class ProtocolError(InvalidDataError):
    """The server returned an unexpected response."""


class NetworkError(ResourceTemporarilyUnavailable):
    """Connection or timeout failure."""


class RateLimitError(RateLimited):
    """Server rate limit; callers must back off."""


@dataclass(frozen=True)
class ProtocolProfile:
    """Public protocol constants extracted from a locally inspected web bundle."""

    prefix: str
    signing_salt: str = field(repr=False)
    api_key: str = field(repr=False)


def make_signature(
    profile: ProtocolProfile,
    method: str,
    path: str,
    params: Mapping[str, str | int] | None,
    body: str,
    *,
    nonce: str | None = None,
    timestamp: str | None = None,
) -> str:
    """Calculate the web client's authx signature; this is not user authentication."""
    nonce = nonce or str(secrets.randbelow(900000) + 100000)
    timestamp = timestamp or str(time.time_ns() // 1_000_000)
    if method == "GET":
        # Official code sorts keys, URL-encodes then decodes for hashing.
        payload = "&".join(
            f"{key}={value}" for key, value in sorted((params or {}).items()) if value is not None
        )
    else:
        payload = body
    digest = hashlib.md5(payload.encode(), usedforsecurity=False).hexdigest()
    material = f"{profile.signing_salt}_{path}_{nonce}_{timestamp}_{digest}_{profile.api_key}"
    signature = hashlib.md5(material.encode(), usedforsecurity=False).hexdigest()
    return f"nonce={nonce}&timestamp={timestamp}&sign={signature}"


def classify_media(data: bytes) -> str:
    """Recognize a small media prefix; this does not prove successful decoding."""
    stripped = data.lstrip().lower()
    if stripped.startswith((b"<!doctype", b"<html", b"{", b"[")):
        return "html-or-json"
    if data.startswith(b"fLaC"):
        return "flac"
    if data.startswith(b"ID3"):
        return "id3-tagged-audio"
    if data.startswith(b"OggS"):
        return "ogg-container"
    if data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        return "wav"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return "mp4-container"
    if len(data) > 2 and data[0] == 255 and data[1] & 0xE0 == 0xE0:
        return "mpeg-or-aac-frame"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp"
    return "unknown"


def check_media_response(status: int, data: bytes, *, stream: bool) -> None:
    """Reject transport and bounded business errors before bytes reach a decoder."""
    if status == 401:
        raise AuthenticationError("Media authentication required")
    if status == 403:
        raise PermissionDeniedError("Media operation denied")
    if status == 404:
        raise NotFoundError("Media not found")
    if status == 429:
        raise RateLimitError("Media rate limit", backoff_time=60)
    if status >= 500:
        raise NetworkError("Media service temporarily unavailable", backoff_time=30)
    if status not in {200, 206}:
        raise ProtocolError("Unexpected media HTTP response")
    if data.lstrip().startswith(b"{"):
        try:
            result = json.loads(data[:4096])
        except ValueError as err:
            raise ProtocolError("Invalid bounded media error response") from err
        if not isinstance(result, dict) or type(result.get("code")) is not int:
            raise ProtocolError("Invalid media error envelope")
        code = result["code"]
        if code in {120001, 120002}:
            raise AuthenticationError("Media authentication expired")
        if code == 100005:
            raise NotFoundError("Media not found")
        if stream and code == 100004:
            # Observed on /track/stream in Music 1.0.1 (0.8.41). Do not assign
            # a global permission meaning to this code on unrelated endpoints.
            raise StreamRejectedError("FeiNiu rejected this stream (100004)")
        raise ProtocolError("Unexpected media business response")


class FeiNiuClient:
    """Per-instance authentication with bounded requests and no implicit retry."""

    def __init__(
        self,
        web_url: str,
        profile: ProtocolProfile,
        *,
        timeout: float = 15,
        session_factory: Callable[[], aiohttp.ClientSession] | None = None,
        acquire: Callable[[], Awaitable[float]] | None = None,
    ) -> None:
        """Initialize an instance without opening a connection."""
        parsed = urlsplit(web_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Expected credential-free HTTP(S) web URL")
        if profile.prefix != "/music/api/v1":
            raise ValueError("Only the inspected v1 profile is supported")
        self._origin = f"{parsed.scheme}://{parsed.netloc}"
        self._profile = profile
        self._timeout = timeout
        self._session: aiohttp.ClientSession | None = None
        self._token: str | None = None
        self._lock = asyncio.Lock()
        self._last_request = 0.0
        self._session_factory = session_factory
        self._acquire = acquire

    async def __aenter__(self) -> Self:
        """Open an isolated HTTP session."""
        if self._session_factory:
            self._session = self._session_factory()
            return self
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self._timeout, connect=min(5, self._timeout)),
            cookie_jar=aiohttp.DummyCookieJar(),
            trust_env=False,
            connector=aiohttp.TCPConnector(limit=2),
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the session and forget authentication."""
        if self._session:
            await self._session.close()
        self._session = None
        self._token = None

    async def login(self, username: str, password: str, device_id: str) -> dict[str, Any]:
        """Authenticate the music account, without reusing a browser cookie."""
        if not username or not password or not re.fullmatch(r"[a-fA-F0-9]{32}", device_id):
            raise ValueError("Missing credentials or invalid device ID")
        # Music 1.0.1 can reject overlapping password logins with code 120001.
        # Serialize only login on the same origin; no sleep, retry or shared token.
        key = (asyncio.get_running_loop(), self._origin)
        lock = _LOGIN_LOCKS.setdefault(key, asyncio.Lock())
        async with lock:
            self._token = None
            data = await self._json(
                "POST",
                "/user/password-login",
                body={
                    "username": username.strip(),
                    "password": hashlib.sha256(password.encode()).hexdigest(),
                    "deviceId": device_id,
                },
            )
            if (
                not isinstance(data, dict)
                or not isinstance(data.get("userToken"), str)
                or not data["userToken"]
            ):
                raise ProtocolError("Login response lacks a token")
            if not isinstance(data.get("user"), dict):
                raise ProtocolError("Login response lacks a user")
            self._token = data["userToken"]
            return cast("dict[str, Any]", data["user"])

    async def current_user(self) -> dict[str, Any]:
        """Check the current music session."""
        return await self._json("GET", "/user/me")

    async def system_config(self) -> dict[str, Any]:
        """Read the public music bootstrap configuration."""
        return await self._json("GET", "/sys/config")

    async def page(self, kind: str, page: int, size: int = 100) -> dict[str, Any]:
        """Read a one-based collection page."""
        if kind not in {"track", "album", "artist"} or page < 1 or not 1 <= size <= 100:
            raise ValueError("Invalid collection or page")
        result = await self._json("GET", f"/{kind}/list", params={"page": page, "size": size})
        if (
            not isinstance(result, dict)
            or not isinstance(result.get("list"), list)
            or type(result.get("total")) is not int
            or result["total"] < 0
        ):
            raise ProtocolError("Invalid page envelope")
        if len(result["list"]) > size or not all(isinstance(x, dict) for x in result["list"]):
            raise ProtocolError("Invalid page items")
        return result

    async def items(self, kind: str, size: int = 100) -> AsyncIterator[dict[str, Any]]:
        """Iterate complete pages, failing visibly if pagination stops progressing."""
        if kind == "playlist":
            for item in await self.playlists():
                yield item
            return
        seen: set[str] = set()
        expected_total = None
        for number in range(1, 10001):
            data = await self.page(kind, number, size)
            if expected_total is None:
                expected_total = data["total"]
            elif data["total"] != expected_total:
                raise ProtocolError("Library changed during pagination; restart the sync")
            for item in data["list"]:
                guid = item.get("guid")
                if not isinstance(guid, str) or not guid or guid in seen:
                    raise ProtocolError("Missing or repeated stable item ID")
                seen.add(guid)
                yield item
            if len(seen) == expected_total:
                return
            if len(seen) > expected_total or not data["list"]:
                raise ProtocolError("Pagination ended before the reported total")
        raise ProtocolError("Pagination safety limit reached")

    async def playlists(self) -> list[dict[str, Any]]:
        """Read the complete non-paginated native playlist collection."""
        data = await self._json("GET", "/playlist/list")
        items = data.get("list")
        total = data.get("total")
        if not isinstance(items, list) or type(total) is not int or len(items) != total:
            raise ProtocolError("Incomplete playlist collection")
        seen: set[str] = set()
        for item in items:
            guid = item.get("guid") if isinstance(item, dict) else None
            if not isinstance(guid, str) or not guid or guid in seen:
                raise ProtocolError("Missing or repeated playlist ID")
            seen.add(guid)
        return cast("list[dict[str, Any]]", items)

    async def detail(self, kind: str, guid: str) -> dict[str, Any]:
        """Read an object by its stable GUID."""
        if kind not in {"track", "album", "artist", "playlist"} or not guid:
            raise ValueError("Invalid detail request")
        endpoint = "metadata" if kind == "track" else "detail"
        return await self._json("GET", f"/{kind}/{endpoint}", params={"guid": guid})

    async def search(self, kind: str, query: str, page: int = 1, size: int = 25) -> dict[str, Any]:
        """Search one media type with native pagination."""
        if kind not in {"track", "album", "artist", "playlist"} or page < 1 or not 1 <= size <= 100:
            raise ValueError("Invalid search request")
        return await self._json(
            "GET", f"/search/{kind}", params={"q": query, "page": page, "size": size}
        )

    async def related(
        self, kind: str, guid: str, page: int = 1, size: int = 100, *, albums: bool = False
    ) -> dict[str, Any]:
        """Read a page of tracks or artist albums."""
        if (
            kind not in {"album", "artist", "playlist"}
            or not guid
            or page < 1
            or not 1 <= size <= 100
        ):
            raise ValueError("Invalid related-items request")
        if albums and kind != "artist":
            raise ValueError("Only artists have an album relationship")
        collection = "album" if albums else "track"
        return await self._json(
            "GET",
            f"/{collection}/{kind}-detail/list",
            params={f"{kind}GUID": guid, "page": page, "size": size},
        )

    async def lyrics(self, guid: str) -> dict[str, Any]:
        """Read the native lyric list without changing lyric preferences."""
        if not guid:
            raise ValueError("Missing track ID")
        return await self._json("GET", "/lyric/list", params={"trackGUID": guid})

    async def cover(self, identifier: str) -> bytes:
        """Read an authenticated image with a fixed size limit."""
        if not identifier:
            raise ValueError("Missing cover ID")
        status, _, data = await self._request(
            "GET",
            "/static/cover",
            params={"coverId": identifier},
            limit=8 * 1024 * 1024,
            signed=False,
        )
        check_media_response(status, data, stream=False)
        if status != 200 or classify_media(data) not in {"jpeg", "png", "webp"}:
            raise ProtocolError("Cover is not a supported image")
        return data

    async def audio_stream(self, identifier: str) -> AsyncGenerator[bytes]:
        """Stream original audio to a consumer; credentials never enter ffmpeg arguments."""
        if not self._session or not identifier:
            raise ValueError("Client not open or missing track ID")
        if not self._token:
            raise AuthenticationError("No active session")
        async with self._lock:
            await self._throttle()
        url = (
            self._origin + self._profile.prefix + "/track/stream?" + urlencode({"guid": identifier})
        )
        try:
            async with self._session.get(
                url,
                headers={"Cookie": "music-token=" + quote(self._token, safe="")},
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=None, connect=5, sock_read=30),
            ) as response:
                prefix = bytearray()
                while len(prefix) < 4096:
                    chunk = await response.content.read(4096 - len(prefix))
                    if not chunk:
                        break
                    prefix.extend(chunk)
                check_media_response(response.status, bytes(prefix), stream=True)
                if classify_media(bytes(prefix)) not in {
                    "id3-tagged-audio",
                    "mpeg-or-aac-frame",
                    "flac",
                    "ogg-container",
                    "wav",
                    "mp4-container",
                }:
                    raise ProtocolError("Stream did not start with recognized audio")
                yield bytes(prefix)
                async for chunk in response.content.iter_chunked(65536):
                    yield chunk
        except (TimeoutError, aiohttp.ClientError, OSError) as err:
            if isinstance(err, aiohttp.ClientResponseError | aiohttp.ClientPayloadError) or (
                isinstance(err, aiohttp.ServerDisconnectedError)
                and not isinstance(err.message, str)
            ):
                # aiohttp parser errors can contain raw response/header fragments.
                raise NetworkError(
                    f"Audio response failed ({type(err).__name__}, status {getattr(err, 'status', 0)})",
                    backoff_time=30,
                ) from None
            raise NetworkError("Audio connection failed or timed out", backoff_time=30) from err

    async def media_prefix(
        self,
        kind: str,
        identifier: str,
        *,
        start: int = 0,
        limit: int = 65536,
        authenticated: bool = True,
        validate: bool = False,
    ) -> tuple[dict[str, Any], bytes]:
        """Read a bounded prefix in memory; never follow unverified redirects."""
        if kind not in {"audio", "cover"} or not identifier or start < 0 or not 1 <= limit <= 65536:
            raise ValueError("Invalid media probe")
        path, params = (
            ("/track/stream", {"guid": identifier})
            if kind == "audio"
            else ("/static/cover", {"coverId": identifier})
        )
        status, headers, data = await self._request(
            "GET",
            path,
            params=params,
            limit=limit,
            extra_headers={"Range": f"bytes={start}-{start + limit - 1}"},
            authenticated=authenticated,
            truncate=True,
            signed=False,
        )
        mime = headers.get("Content-Type", "").split(";")[0].lower()
        if validate:
            check_media_response(status, data, stream=kind == "audio")
        if not re.fullmatch(r"[a-z0-9.+-]+/[a-z0-9.+-]+", mime):
            mime = "unknown"
        content_range = headers.get("Content-Range", "")
        if not re.fullmatch(r"bytes (?:\d+-\d+|\*)/(?:\d+|\*)", content_range):
            content_range = "absent-or-invalid"
        return {
            "status": status,
            "mime": mime,
            "sample_bytes": len(data),
            "signature": classify_media(data) if start == 0 else "nonzero-range",
            "content_range": content_range,
            "accepts_bytes": headers.get("Accept-Ranges") == "bytes",
            "redirect": 300 <= status < 400,
        }, data

    async def _json(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str | int] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        status, _, data = await self._request(method, path, params=params, body=body)
        if status == 401:
            self._token = None
            raise AuthenticationError(f"HTTP {status}")
        if status == 403:
            raise PermissionDeniedError("Operation denied")
        if status == 404:
            raise NotFoundError("HTTP 404")
        if status == 429:
            raise RateLimitError("HTTP 429", backoff_time=60)
        if status >= 500:
            raise NetworkError("Service temporarily unavailable", backoff_time=30)
        if status != 200:
            raise ProtocolError(f"Unexpected HTTP status {status}")
        try:
            result = json.loads(data)
        except (ValueError, UnicodeDecodeError) as err:
            raise ProtocolError("Response is not JSON") from err
        if not isinstance(result, dict) or type(result.get("code")) is not int:
            raise ProtocolError("Invalid response envelope")
        code = result["code"]
        if code in {120001, 120002}:
            self._token = None
            raise AuthenticationError(f"API code {code}")
        if code == 100005:
            raise NotFoundError("API code 100005")
        if code not in {0, 200}:
            raise ProtocolError(f"API code {code}")
        if "data" not in result:
            raise ProtocolError("Response lacks data")
        if not isinstance(result["data"], dict):
            raise ProtocolError("Expected a response data object")
        return cast("dict[str, Any]", result["data"])

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str | int] | None = None,
        body: dict[str, Any] | None = None,
        limit: int = 2 * 1024 * 1024,
        extra_headers: dict[str, str] | None = None,
        authenticated: bool = True,
        truncate: bool = False,
        signed: bool = True,
    ) -> tuple[int, Mapping[str, str], bytes]:
        if self._session is None:
            raise RuntimeError("Use the client as an async context manager")
        full_path = self._profile.prefix + path
        body_text = (
            json.dumps(body, separators=(",", ":"), ensure_ascii=False) if body is not None else ""
        )
        headers = {"Accept": "application/json"}
        if signed:
            headers["authx"] = make_signature(self._profile, method, full_path, params, body_text)
        if body is not None:
            headers["Content-Type"] = "application/json"
        headers.update(extra_headers or {})
        url = self._origin + full_path
        if params:
            url += "?" + urlencode(params, quote_via=quote)
        async with self._lock:
            await self._throttle()
            if self._token and authenticated:
                headers["Cookie"] = "music-token=" + quote(self._token, safe="")
            try:
                async with self._session.request(
                    method,
                    url,
                    headers=headers,
                    data=body_text.encode() if body is not None else None,
                    timeout=aiohttp.ClientTimeout(
                        total=self._timeout, connect=min(5, self._timeout)
                    ),
                    allow_redirects=False,
                ) as response:
                    chunks = bytearray()
                    while len(chunks) < limit + (not truncate):
                        chunk = await response.content.read(
                            min(16384, limit + (not truncate) - len(chunks))
                        )
                        if not chunk:
                            break
                        chunks.extend(chunk)
                    if len(chunks) > limit:
                        raise ProtocolError("Response exceeds size limit")
                    return response.status, response.headers, bytes(chunks)
            except (TimeoutError, aiohttp.ClientError, OSError) as err:
                if isinstance(err, aiohttp.ClientResponseError | aiohttp.ClientPayloadError) or (
                    isinstance(err, aiohttp.ServerDisconnectedError)
                    and not isinstance(err.message, str)
                ):
                    # aiohttp parser errors can contain raw response/header fragments.
                    raise NetworkError(
                        f"HTTP response failed ({type(err).__name__}, status {getattr(err, 'status', 0)})",
                        backoff_time=30,
                    ) from None
                raise NetworkError("HTTP connection failed or timed out", backoff_time=30) from err

    async def _throttle(self) -> None:
        if self._acquire:
            await self._acquire()
        else:
            await asyncio.sleep(max(0, 0.25 - (time.monotonic() - self._last_request)))
        self._last_request = time.monotonic()
