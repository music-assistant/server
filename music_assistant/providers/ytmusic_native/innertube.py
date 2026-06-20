"""
Native InnerTube transport for YouTube Music.

Replays the exact youtubei/v1 request recipe the music.youtube.com web client
uses (SAPISIDHASH auth, WEB_REMIX context), reverse-engineered from a logged-in
session. No wrapper libraries. See reverseengeneer.md §2-§3 and §8.
"""

from __future__ import annotations

import re
import time
from hashlib import sha1
from typing import TYPE_CHECKING, Any

from music_assistant_models.errors import LoginFailed

from .constants import (
    ANDROID_VR_CLIENT_ID,
    ANDROID_VR_CLIENT_NAME,
    ANDROID_VR_CLIENT_VERSION,
    ANDROID_VR_OS_VERSION,
    ANDROID_VR_USER_AGENT,
    BASE_URL,
    DOMAIN,
    USER_AGENT,
    WEB_REMIX_CLIENT_ID,
    WEB_REMIX_CLIENT_NAME,
)

if TYPE_CHECKING:
    import logging

    from aiohttp import ClientSession

_SAPISID_RE = re.compile(r"(?:^|;\s*)(?:SAPISID|__Secure-3PAPISID|__Secure-1PAPISID)=([^;]+)")
_CLIENT_VERSION_RE = re.compile(r'"INNERTUBE_CLIENT_VERSION":"([^"]+)"')
_API_KEY_RE = re.compile(r'"INNERTUBE_API_KEY":"([^"]+)"')
_VISITOR_DATA_RE = re.compile(r'"(?:VISITOR_DATA|visitorData)":"([^"]+)"')
_JS_URL_RE = re.compile(r'"jsUrl":"([^"]+)"')
_STS_RE = re.compile(r"signatureTimestamp:(\d+)")
_PLAYER_ID_RE = re.compile(r"/player/([^/]+)/")


class InnerTube:
    """
    Authenticated youtubei/v1 transport for one captured session.

    Builds every request from the session cookie (SAPISIDHASH) plus the blessed
    visitorData, and exposes the WEB_REMIX (metadata + premium player) and
    ANDROID_VR (bot-wall-free audio fallback) calls.
    """

    def __init__(
        self,
        http_session: ClientSession,
        cookie: str,
        logger: logging.Logger,
        visitor_data: str | None = None,
    ) -> None:
        """
        Initialize the transport.

        :param http_session: Shared aiohttp session from the MA instance.
        :param cookie: Full Cookie header from a logged-in music.youtube.com session.
        :param logger: Provider logger.
        :param visitor_data: Optional X-Goog-Visitor-Id override; auto-detected when omitted.
        """
        self._http = http_session
        self._cookie = cookie
        self.logger = logger
        self.visitor_data = visitor_data
        self.api_key: str | None = None
        self.client_version: str = "1.20240101.01.00"
        self.player_id: str | None = None
        self.js_url: str | None = None
        self.sts: int | None = None
        sapisid_match = _SAPISID_RE.search(cookie)
        if not sapisid_match:
            raise LoginFailed(
                "Invalid cookie: missing SAPISID/__Secure-3PAPISID. "
                "Copy the full cookie header from a logged-in music.youtube.com request."
            )
        self._sapisid = sapisid_match.group(1)

    async def setup(self) -> None:
        """
        Bootstrap session details (client version, api key, visitor data, player) from the home page.

        Must be called once before any other call.
        """
        html = await self._get_text(DOMAIN + "/")
        if client_version := _CLIENT_VERSION_RE.search(html):
            self.client_version = client_version.group(1)
        if api_key := _API_KEY_RE.search(html):
            self.api_key = api_key.group(1)
        if not self.visitor_data and (visitor_data := _VISITOR_DATA_RE.search(html)):
            self.visitor_data = visitor_data.group(1)
        if not self.visitor_data:
            raise LoginFailed("Could not determine visitorData from the home page.")
        if js_url := _JS_URL_RE.search(html):
            url = js_url.group(1)
            if url.startswith("/"):
                url = DOMAIN + url
            self.js_url = url
            if player_id := _PLAYER_ID_RE.search(url):
                self.player_id = player_id.group(1)

    async def call_music(self, endpoint: str, body: dict[str, Any]) -> dict[str, Any]:
        """
        Make an authenticated WEB_REMIX youtubei/v1 call (metadata path).

        :param endpoint: e.g. "browse", "search", "next".
        :param body: Endpoint-specific params merged with the WEB_REMIX context.
        """
        payload = {"context": self._web_context(), **body}
        url = f"{BASE_URL}{endpoint}?prettyPrint=false"
        if self.api_key:
            url += f"&key={self.api_key}"
        async with self._http.post(
            url, headers=self._auth_headers(), json=payload, ssl=False
        ) as resp:
            return await resp.json()

    async def call_player_web(self, video_id: str) -> dict[str, Any]:
        """
        Make an authenticated WEB_REMIX `player` call (premium signatureCipher formats).

        Requires a fresh signatureTimestamp from base.js, fetched lazily.
        """
        if self.sts is None:
            await self.fetch_base_js()
        body = {
            "context": self._web_context(),
            "videoId": video_id,
            "playbackContext": {
                "contentPlaybackContext": {
                    "html5Preference": "HTML5_PREF_WANTS",
                    "signatureTimestamp": self.sts,
                }
            },
            "contentCheckOk": True,
            "racyCheckOk": True,
        }
        url = f"{BASE_URL}player?prettyPrint=false"
        if self.api_key:
            url += f"&key={self.api_key}"
        async with self._http.post(url, headers=self._auth_headers(), json=body, ssl=False) as resp:
            return await resp.json()

    async def call_player_android_vr(self, video_id: str) -> dict[str, Any]:
        """
        ANDROID_VR `player` call: ready-to-stream URLs (~150k), no signature cipher.

        The bot-wall is bypassed by sending the blessed visitorData and NO cookies.
        """
        body = {
            "context": {
                "client": {
                    "clientName": ANDROID_VR_CLIENT_NAME,
                    "clientVersion": ANDROID_VR_CLIENT_VERSION,
                    "androidSdkVersion": 32,
                    "osName": "Android",
                    "osVersion": ANDROID_VR_OS_VERSION,
                    "hl": "en",
                    "gl": "US",
                    "visitorData": self.visitor_data,
                },
                "user": {},
            },
            "videoId": video_id,
            "contentCheckOk": True,
            "racyCheckOk": True,
        }
        headers = {
            "Content-Type": "application/json",
            "User-Agent": ANDROID_VR_USER_AGENT,
            "X-Goog-Visitor-Id": self.visitor_data or "",
            "X-Youtube-Client-Name": ANDROID_VR_CLIENT_ID,
            "X-Youtube-Client-Version": ANDROID_VR_CLIENT_VERSION,
        }
        url = f"{BASE_URL}player?prettyPrint=false"
        if self.api_key:
            url += f"&key={self.api_key}"
        # deliberately no cookies on this call (cookies get it bot-walled)
        async with self._http.post(url, headers=headers, json=body, ssl=False) as resp:
            return await resp.json()

    async def fetch_base_js(self) -> str:
        """Fetch the player base.js (reading its signatureTimestamp) and return the JS source."""
        if not self.js_url:
            await self.setup()
        if not self.js_url:
            raise LoginFailed("Could not locate the player jsUrl on the home page.")
        base_js = await self._get_text(self.js_url)
        if sts := _STS_RE.search(base_js):
            self.sts = int(sts.group(1))
        return base_js

    # ----------------- private -----------------

    def _web_context(self) -> dict[str, Any]:
        return {
            "client": {
                "clientName": WEB_REMIX_CLIENT_NAME,
                "clientVersion": self.client_version,
                "hl": "en",
                "gl": "US",
                "visitorData": self.visitor_data,
            },
            "user": {},
        }

    def _sapisid_hash(self) -> str:
        ts = int(time.time())
        digest = sha1(f"{ts} {self._sapisid} {DOMAIN}".encode()).hexdigest()
        return f"SAPISIDHASH {ts}_{digest}"

    def _auth_headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Cookie": self._cookie,
            "Authorization": self._sapisid_hash(),
            "Origin": DOMAIN,
            "X-Origin": DOMAIN,
            "Referer": DOMAIN + "/",
            "X-Goog-AuthUser": "0",
            "X-Goog-Visitor-Id": self.visitor_data or "",
            "X-Youtube-Client-Name": WEB_REMIX_CLIENT_ID,
            "X-Youtube-Client-Version": self.client_version,
            "User-Agent": USER_AGENT,
        }

    async def _get_text(self, url: str) -> str:
        headers = {
            "Cookie": self._cookie,
            "Accept-Language": "en",
            "User-Agent": USER_AGENT,
        }
        if self.visitor_data:
            headers["X-Goog-Visitor-Id"] = self.visitor_data
        async with self._http.get(url, headers=headers, ssl=False) as resp:
            return await resp.text()
