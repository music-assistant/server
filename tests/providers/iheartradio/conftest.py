"""Shared fixtures for the iHeartRadio provider tests."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from music_assistant.providers.iheartradio.auth import IHeartRadioAuthManager, IHeartRadioSession
from music_assistant.providers.iheartradio.browse import IHeartRadioBrowseManager
from music_assistant.providers.iheartradio.library import IHeartRadioLibraryManager
from music_assistant.providers.iheartradio.provider import IHeartRadioProvider
from music_assistant.providers.iheartradio.stations import ArtistRadioStore
from music_assistant.providers.iheartradio.streaming import IHeartRadioStreamingManager
from tests.common import use_real_create_task

INSTANCE_ID = "iheartradio--test123"
DOMAIN = "iheartradio"
PROFILE_ID = "13071248118"
SESSION_ID = "Lgpyk317G7rmHFDG1kXnU"

STATION: dict[str, Any] = {
    "id": 6185,
    "name": "KIIS 1065",
    "description": "Sydney's #1 Hit Music Station",
    "callLetters": "2WFM-FM",
    "band": "FM",
    "freq": "106.5",
    "website": "kiis1065.com.au",
    "logo": "https://i.iheart.com/v3/re/assets/images/kiis.png",
    "isActive": True,
    "genres": [{"id": 16, "name": "Pop", "primary": True}],
    "streams": {
        "hls_stream": "http://example.com/kiis.m3u8",
        "shoutcast_stream": "http://example.com/kiis.aac",
        "secure_hls_stream": "https://example.com/kiis.m3u8",
        "secure_shoutcast_stream": "https://example.com/kiis.aac",
    },
}

NOW_PLAYING = {
    "title": "Red Rocks",
    "artist": "Above & Beyond",
    "album": "The Club Instrumentals",
    "trackDuration": 466,
    "imagePath": "http://image.iheart.com/red-rocks.jpg",
    "startTime": 1789223779063,
    "endTime": 1789224245063,
}

PODCAST = {
    "id": 21124503,
    "title": "Stuff You Missed in History Class",
    "description": "History is a lot more than names and dates.",
    "imageUrl": "https://i.iheart.com/v3/url/history.jpg",
}

EPISODE = {
    "id": 343681362,
    "title": "Paddles and The British Museum",
    "description": "Behind the scenes.",
    "startDate": 1789117200000,
    "duration": 1862,
    "isExplicit": False,
    "mimeTypes": ["audio/mpeg"],
    "podcastId": 21124503,
}

ARTIST_STATION: dict[str, Any] = {
    "id": "6aa557d2c868d200017276ff",
    "seedArtistId": 1805,
    "artistName": "Tom Petty",
}

RADIO_ITEM: dict[str, Any] = {
    "contentType": "TRACK",
    "streamUrl": "https://custom-hls.iheart.com/track/607288_96.m4a.m3u8",
    "reportPayload": "opaque-report-token",
    "content": {
        "id": 607288,
        "title": "I Won't Back Down",
        "duration": 176,
        "artistId": 1805,
        "artistName": "Tom Petty",
        "albumId": 607279,
        "albumName": "Full Moon Fever",
        "version": None,
        "imagePath": "https://i.iheart.com/v3/url/full-moon-fever.jpg",
        "playbackRights": {"onDemand": True},
    },
}


class FakeApi:
    """Serve canned API responses per path and record what was requested."""

    def __init__(self) -> None:
        """Initialize the fake."""
        # keyed by path (any GET) or by (method, path)
        self.responses: dict[str | tuple[str, str], Any] = {}
        # paths served page by page, in order
        self.pages: dict[str, list[Any]] = {}
        # every GET as (path, params), for the listing tests
        self.calls: list[tuple[str, dict[str, Any]]] = []
        # every request as (method, path, body), body being the json or form sent
        self.requests: list[tuple[str, str, dict[str, Any] | None]] = []

    async def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """Return the canned response for a GET, mirroring the provider's own helper."""
        return await self.request("GET", path, params=params)

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        form: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        authenticated: bool = True,
        retry_auth: bool = True,
    ) -> Any:
        """Return the canned response for a request, mirroring the provider's own method."""
        self.requests.append((method, path, json or form))
        if method == "GET":
            self.calls.append((path, dict(params or {})))
        if (method, path) in self.responses:
            response = self.responses[(method, path)]
        elif path in self.pages:
            response = self.pages[path].pop(0)
        elif path in self.responses:
            response = self.responses[path]
        elif method in ("PUT", "DELETE", "HEAD"):
            # the follow endpoints answer without a body
            response = None
        else:
            raise KeyError(f"No canned response for {method} {path}")
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture
def api() -> FakeApi:
    """Return the fake iHeartRadio API."""
    return FakeApi()


@pytest.fixture
def provider(api: FakeApi) -> IHeartRadioProvider:
    """Return a provider with a guest session whose API calls are served by the fake."""
    provider = IHeartRadioProvider.__new__(IHeartRadioProvider)
    provider.mass = Mock()
    provider.manifest = Mock(domain=DOMAIN)
    provider.config = Mock(instance_id=INSTANCE_ID, name="iHeartRadio Test")
    provider.logger = Mock()
    provider._country = "au"
    provider._base_url = "https://au.api.iheart.com"
    provider.host_name = "webapp.AU"
    provider._headers = {}
    provider.stations = ArtistRadioStore()
    provider.browse_manager = IHeartRadioBrowseManager(provider)
    provider.library_manager = IHeartRadioLibraryManager(provider)
    provider.streaming_manager = IHeartRadioStreamingManager(provider)
    provider.auth = IHeartRadioAuthManager(provider)
    provider.auth.session = IHeartRadioSession(PROFILE_ID, SESSION_ID, "")
    provider.request = api.request  # type: ignore[method-assign]
    provider._get_json = api.get_json  # type: ignore[method-assign]
    # treat every lookup as a cache miss so the tests see the real behaviour
    provider.mass.cache.get_with_freshness = AsyncMock(return_value=(None, False, False))
    provider.mass.cache.set = AsyncMock()
    # nothing persisted yet; secrets pass through untouched
    provider.mass.config.get_raw_provider_config_value = Mock(return_value=None)
    provider.mass.config.decrypt_string = Mock(side_effect=lambda value: value)
    use_real_create_task(provider.mass)
    return provider
