"""Shared fixtures for the iHeartRadio provider tests."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from music_assistant.providers.iheartradio.browse import IHeartRadioBrowseManager
from music_assistant.providers.iheartradio.provider import IHeartRadioProvider
from music_assistant.providers.iheartradio.streaming import IHeartRadioStreamingManager
from tests.common import use_real_create_task

INSTANCE_ID = "iheartradio--test123"
DOMAIN = "iheartradio"

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


class FakeApi:
    """Serve canned API responses per path and record what was requested."""

    def __init__(self) -> None:
        """Initialize the fake."""
        self.responses: dict[str, Any] = {}
        # paths served page by page, in order
        self.pages: dict[str, list[Any]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """Return the canned response for a path, mirroring the provider's own helper."""
        self.calls.append((path, dict(params or {})))
        response = self.pages[path].pop(0) if path in self.pages else self.responses[path]
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture
def api() -> FakeApi:
    """Return the fake iHeartRadio API."""
    return FakeApi()


@pytest.fixture
def provider(api: FakeApi) -> IHeartRadioProvider:
    """Return a provider whose API calls are served by the fake."""
    provider = IHeartRadioProvider.__new__(IHeartRadioProvider)
    provider.mass = Mock()
    provider.manifest = Mock(domain=DOMAIN)
    provider.config = Mock(instance_id=INSTANCE_ID, name="iHeartRadio Test")
    provider.logger = Mock()
    provider._country = "au"
    provider._base_url = "https://au.api.iheart.com"
    provider._headers = {}
    provider.browse_manager = IHeartRadioBrowseManager(provider)
    provider.streaming_manager = IHeartRadioStreamingManager(provider)
    provider._get_json = api.get_json  # type: ignore[method-assign]
    # treat every lookup as a cache miss so the tests see the real behaviour
    provider.mass.cache.get_with_freshness = AsyncMock(return_value=(None, False, False))
    provider.mass.cache.set = AsyncMock()
    use_real_create_task(provider.mass)
    return provider
