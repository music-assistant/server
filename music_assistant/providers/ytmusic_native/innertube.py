"""
Authenticated InnerTube (youtubei/v1) transport for YouTube Music.

Replays the requests the music.youtube.com web client (WEB_REMIX) makes, signed
with the session cookie: `Authorization: SAPISIDHASH <ts>_<sha1(ts SAPISID
origin)>` plus the visitor id, client version and api key the home page hands
out. No wrapper libraries.
"""

from __future__ import annotations

import re
import time
from hashlib import sha1
from typing import TYPE_CHECKING, Any

from aiohttp import ClientError, ClientTimeout
from music_assistant_models.errors import (
    LoginFailed,
    MediaNotFoundError,
    RateLimited,
    ResourceTemporarilyUnavailable,
)

from music_assistant.helpers.throttle_retry import ThrottlerManager, throttle_with_retries

from .constants import (
    API_URL,
    BOOTSTRAP_TTL,
    DOMAIN,
    USER_AGENT,
    WEB_REMIX_CLIENT_ID,
    WEB_REMIX_CLIENT_NAME,
)

if TYPE_CHECKING:
    import logging

    from aiohttp import ClientSession

_SAPISID_RE = re.compile(r"(?:^|;\s*)(?:SAPISID|__Secure-3PAPISID|__Secure-1PAPISID)=([^;]+)")
_YTCFG_RE = {
    "client_version": re.compile(r'"INNERTUBE_CLIENT_VERSION":"([^"]+)"'),
    "api_key": re.compile(r'"INNERTUBE_API_KEY":"([^"]+)"'),
    "visitor_data": re.compile(r'"VISITOR_DATA":"([^"]+)"'),
    "gl": re.compile(r'"INNERTUBE_CONTEXT_GL":"([^"]+)"'),
    "js_url": re.compile(r'"(?:jsUrl|PLAYER_JS_URL)":"([^"]+)"'),
    "logged_in": re.compile(r'"LOGGED_IN":(true|false)'),
}
_PLAYER_ID_RE = re.compile(r"/player/([^/]+)/")
_STS_RE = re.compile(r"signatureTimestamp:(\d+)")
_PROBE_TIMEOUT = ClientTimeout(total=8)


class InnerTube:
    """Authenticated youtubei/v1 transport for one YouTube Music session."""

    def __init__(
        self,
        http_session: ClientSession,
        cookie: str,
        logger: logging.Logger,
        translation_owner: str,
    ) -> None:
        """
        Initialize the transport.

        :param http_session: A session without a cookie jar, owned by the provider.
        :param cookie: The full Cookie header of a logged-in music.youtube.com session.
        :param logger: Provider logger.
        :param translation_owner: Owner of the provider's translated error messages.
        """
        self._http = http_session
        self.logger = logger
        self.throttler = ThrottlerManager(rate_limit=5, period=1)
        self._translation_owner = translation_owner
        self.cookie = cookie
        self.client_version = "1.20261020.01.00"
        self.api_key: str | None = None
        self.visitor_data: str | None = None
        self.gl = "US"
        self.js_url: str | None = None
        self._bootstrapped_at = 0.0
        if not _SAPISID_RE.search(cookie):
            raise LoginFailed(
                "The cookie has no SAPISID. Copy the full cookie header of a request "
                "to music.youtube.com while you are logged in.",
                translation_key="invalid_cookie",
                translation_owner=translation_owner,
            )

    @property
    def player_id(self) -> str | None:
        """Return the current player build id."""
        if self.js_url and (match := _PLAYER_ID_RE.search(self.js_url)):
            return match.group(1)
        return None

    async def bootstrap(self, force: bool = False) -> None:
        """
        Read the client config (ytcfg) from the home page, as the web client does on load.

        Refreshed every BOOTSTRAP_TTL seconds so a new client version or player
        build is picked up. Raises LoginFailed when the cookie is not logged in.

        :param force: Refresh even when the last bootstrap is recent.
        """
        if not force and time.time() - self._bootstrapped_at < BOOTSTRAP_TTL:
            return
        html = await self.get_text(f"{DOMAIN}/", with_cookie=True)
        found = {
            key: match.group(1) for key, regex in _YTCFG_RE.items() if (match := regex.search(html))
        }
        if found.get("logged_in") != "true":
            raise self._expired()
        self.client_version = found.get("client_version", self.client_version)
        self.api_key = found.get("api_key", self.api_key)
        self.visitor_data = found.get("visitor_data", self.visitor_data)
        self.gl = found.get("gl", self.gl)
        if js_url := found.get("js_url"):
            self.js_url = f"{DOMAIN}{js_url}" if js_url.startswith("/") else js_url
        self._bootstrapped_at = time.time()

    @throttle_with_retries
    async def call(self, endpoint: str, body: dict[str, Any]) -> dict[str, Any]:
        """
        Make an authenticated WEB_REMIX call.

        :param endpoint: e.g. "browse", "search", "next", "player".
        :param body: Endpoint parameters, merged with the client context.
        """
        await self.bootstrap()
        url = f"{API_URL}{endpoint}?prettyPrint=false"
        if self.api_key:
            url += f"&key={self.api_key}"
        payload = {"context": self._context(), **body}
        try:
            async with self._http.post(url, headers=self._headers(), json=payload) as resp:
                if resp.status in (400, 404):
                    raise MediaNotFoundError(f"{endpoint}: {resp.status} {await resp.text()}")
                if resp.status == 401:
                    raise self._expired()
                if resp.status == 429:
                    retry_after = resp.headers.get("Retry-After", "")
                    raise RateLimited(
                        "YouTube Music is rate limiting requests",
                        backoff_time=int(retry_after) if retry_after.isdigit() else 0,
                    )
                if resp.status >= 500:
                    raise ResourceTemporarilyUnavailable(f"YouTube Music answered {resp.status}")
                resp.raise_for_status()
                data: dict[str, Any] = await resp.json(content_type=None)
        except ClientError as err:
            raise ResourceTemporarilyUnavailable(f"{endpoint} failed: {err}") from err
        # A dead cookie is not an error: InnerTube answers as if anonymous, and only
        # the logged_in tracking param tells the two apart.
        if _logged_in(data) is False:
            raise self._expired()
        return data

    async def player(self, video_id: str, signature_timestamp: int) -> dict[str, Any]:
        """
        Call the WEB_REMIX player for one video.

        :param video_id: The video id.
        :param signature_timestamp: `signatureTimestamp` of the current base.js, so the
            ciphered formats match the player build we solve them with.
        """
        return await self.call(
            "player",
            {
                "videoId": video_id,
                "playbackContext": {
                    "contentPlaybackContext": {
                        "html5Preference": "HTML5_PREF_WANTS",
                        "signatureTimestamp": signature_timestamp,
                    }
                },
                "contentCheckOk": True,
                "racyCheckOk": True,
            },
        )

    async def base_js(self) -> tuple[str, int]:
        """Download the current player's base.js and return it with its signatureTimestamp."""
        await self.bootstrap()
        if not self.js_url:
            raise ResourceTemporarilyUnavailable("No player URL on the YouTube Music home page")
        source = await self.get_text(self.js_url)
        if not (match := _STS_RE.search(source)):
            raise ResourceTemporarilyUnavailable("No signatureTimestamp in the player")
        return source, int(match.group(1))

    async def stream_ok(self, url: str) -> bool:
        """Return True when googlevideo serves the URL (signature and n are correct)."""
        try:
            async with self._http.get(
                url,
                headers={"Range": "bytes=0-1", "User-Agent": USER_AGENT},
                timeout=_PROBE_TIMEOUT,
            ) as resp:
                return resp.status in (200, 206)
        except ClientError, TimeoutError:
            return False

    async def ping(self, url: str) -> None:
        """Send a cookie'd GET, e.g. a playback-tracking ping."""
        headers = {
            "Cookie": self.cookie,
            "Origin": DOMAIN,
            "Referer": f"{DOMAIN}/",
            "X-Goog-AuthUser": "0",
            "User-Agent": USER_AGENT,
        }
        if self.visitor_data:
            headers["X-Goog-Visitor-Id"] = self.visitor_data
        async with self._http.get(url, headers=headers) as resp:
            resp.raise_for_status()

    async def get_text(self, url: str, with_cookie: bool = False) -> str:
        """
        GET a page as text.

        :param url: The URL.
        :param with_cookie: Send the session cookie (needed for the logged-in home page).
        """
        headers = {"Accept-Language": "en", "User-Agent": USER_AGENT}
        if with_cookie:
            headers["Cookie"] = self.cookie
            if self.visitor_data:
                headers["X-Goog-Visitor-Id"] = self.visitor_data
        try:
            async with self._http.get(url, headers=headers) as resp:
                resp.raise_for_status()
                return await resp.text()
        except ClientError as err:
            raise ResourceTemporarilyUnavailable(f"GET {url} failed: {err}") from err

    def _context(self) -> dict[str, Any]:
        client: dict[str, Any] = {
            "clientName": WEB_REMIX_CLIENT_NAME,
            "clientVersion": self.client_version,
            # English labels keep the parsers locale independent
            "hl": "en",
            "gl": self.gl,
        }
        if self.visitor_data:
            client["visitorData"] = self.visitor_data
        return {"client": client, "user": {}}

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Cookie": self.cookie,
            "Authorization": self._sapisid_hash(),
            "Origin": DOMAIN,
            "X-Origin": DOMAIN,
            "Referer": f"{DOMAIN}/",
            "X-Goog-AuthUser": "0",
            "X-Youtube-Client-Name": WEB_REMIX_CLIENT_ID,
            "X-Youtube-Client-Version": self.client_version,
            "User-Agent": USER_AGENT,
        }
        if self.visitor_data:
            headers["X-Goog-Visitor-Id"] = self.visitor_data
        return headers

    def _sapisid_hash(self) -> str:
        match = _SAPISID_RE.search(self.cookie)
        sapisid = match.group(1) if match else ""
        ts = int(time.time())
        digest = sha1(f"{ts} {sapisid} {DOMAIN}".encode(), usedforsecurity=False).hexdigest()
        return f"SAPISIDHASH {ts}_{digest}"

    def _expired(self) -> LoginFailed:
        return LoginFailed(
            "YouTube signed this session out. Sign in to music.youtube.com in a private "
            "browser window, copy a new cookie, close the window without signing out, and "
            "reconfigure this provider with that cookie.",
            translation_key="cookies_expired",
            translation_owner=self._translation_owner,
        )


def _logged_in(data: dict[str, Any]) -> bool | None:
    """Read GFEEDBACK's logged_in param: True/False, or None when absent."""
    for service in data.get("responseContext", {}).get("serviceTrackingParams", []):
        if service.get("service") != "GFEEDBACK":
            continue
        for param in service.get("params", []):
            if param.get("key") == "logged_in":
                return bool(param.get("value") == "1")
    return None
