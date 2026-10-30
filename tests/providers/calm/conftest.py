"""Fixtures for the Calm provider tests: a provider with a mocked MA core and Calm client."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from calm_client import models as calm

from music_assistant.helpers.throttle_retry import ThrottlerManager
from music_assistant.providers.calm import SUPPORTED_FEATURES, CalmProvider

PROGRAM_ID = "prog1"
GUIDE_ID = "guide1"

# trimmed-down shapes of real Calm API responses
PROGRAM: dict[str, Any] = {
    "id": PROGRAM_ID,
    "title": "Rain Sounds",
    "description": "Gentle rain.",
    "content_type": "Playlist",
    "narrator": {
        "id": "narr1",
        "name": "Curated by Calm",
        "headshot": {"url": "https://assets.calm.com/head.png"},
    },
    "cell_image": {"url": "https://assets.calm.com/cell.jpeg"},
    "guides": [
        {
            "id": "guide2",
            "title": "Second",
            "position": 2,
            "is_free": False,
            "assets": [
                {
                    "url": "https://assets.calm.com/b.wav",
                    "content_type": "audio/wav",
                    "duration": 60,
                }
            ],
        },
        {
            "id": GUIDE_ID,
            "title": "First",
            "position": 1,
            "is_free": False,
            "assets": [
                {
                    "url": "https://assets.calm.com/a.wav",
                    "content_type": "audio/wav",
                    "duration": 1423,
                },
                {
                    "url": "https://assets.calm.com/a.m4a",
                    "content_type": "audio/mp4",
                    "duration": 1423,
                },
                {
                    "url": "https://assets.calm.com/a.oga",
                    "content_type": "audio/ogg",
                    "duration": 1423,
                },
            ],
        },
    ],
}

FEED_GUIDE = {
    "type": "guide",
    "guide_id": GUIDE_ID,
    "program_id": PROGRAM_ID,
    "title": "First",
    "subtitle": "Rain Sounds",
    "narrator_name": "Curated by Calm",
    "is_unlocked": True,
    "image": {"url": "https://assets.calm.com/item.jpeg"},
    "assets": [
        {"url": "https://assets.calm.com/a.oga", "content_type": "audio/ogg", "duration": 2176.6}
    ],
}
FEED_PROGRAM = {
    "type": "program",
    "program_id": PROGRAM_ID,
    "title": "Rain Sounds",
    "narrator_name": "Curated by Calm",
    "is_unlocked": False,
}
# a sub-pack entry (browsable folder, no audio of its own)
PACK_FOLDER = {"type": "pack", "pack_class": "sub", "title": "Sub"}

FEED_SLEEP = {
    "id": "sleep",
    "tags": [
        {"feed_id": "sleep-music", "title": "Music"},
        {"pack_class": "tagged-x", "title": "Meditations"},
    ],
    "packs": [
        {"title": "", "pack_class": "upsell", "items": [{"type": "url", "title": "Buy"}]},
        {"title": "Featured", "pack_class": "sleep-featured", "items": [FEED_GUIDE]},
        {"title": "Recently Played", "pack_class": "recent", "items": []},
    ],
}


class FakeCalmClient:
    """Returns calm-client models from canned responses and records the calls made."""

    def __init__(self, unlocked: bool = True) -> None:
        """Create the fake; ``unlocked`` sets the lock flag of every guide summary."""
        self.calls: list[str] = []
        self.unlocked = unlocked
        # _store_credentials reads these; the provider fixture no-ops it anyway
        self.credentials = Mock()
        self.credentials.model_dump_json.return_value = "{}"

    async def get_program(self, program_id: str) -> calm.Program:
        """Return the canned program."""
        self.calls.append(f"content/programs/{program_id}")
        return calm.Program.model_validate(PROGRAM)

    async def get_guide_summary(self, guide_id: str) -> calm.GuideSummary:
        """Return a summary pointing at the canned program."""
        self.calls.append(f"programs/guides/{guide_id}")
        return calm.GuideSummary.model_validate(
            {"id": guide_id, "program_id": PROGRAM_ID, "unlocked": self.unlocked, "free": False}
        )

    async def get_category_feed(self, category: str) -> calm.FeedResponse:
        """Return the canned feed."""
        self.calls.append(f"packs/feeds/{category}")
        return calm.FeedResponse.model_validate(FEED_SLEEP)

    async def get_pack(self, pack_class: str) -> calm.Pack:
        """Return a pack with a guide, a program and a sub-pack."""
        self.calls.append(f"packs/class/{pack_class}")
        return calm.Pack.model_validate(
            {"pack_class": pack_class, "items": [FEED_GUIDE, FEED_PROGRAM, PACK_FOLDER]}
        )

    async def get_favorites(self) -> list[calm.PackItem]:
        """Return the canned favorites."""
        self.calls.append("packs/class/faves-hydrated")
        return calm.Pack.model_validate({"items": [FEED_GUIDE, FEED_PROGRAM]}).items

    async def search(
        self, query: str, *, category: str | None = None, duration: str | None = None
    ) -> calm.SearchResponse:
        """Return a program and a guide as search results."""
        self.calls.append("search/v2")
        return calm.SearchResponse.model_validate({"items": [FEED_PROGRAM, FEED_GUIDE]})


@pytest.fixture
def api() -> FakeCalmClient:
    """Return the fake Calm client."""
    return FakeCalmClient()


@pytest.fixture
def provider(api: FakeCalmClient) -> CalmProvider:
    """Create a CalmProvider with a mocked MA core (no caching) and fake Calm client."""
    mass = Mock()
    # always a cache miss, so every call reaches the (fake) client
    mass.cache.get_with_freshness = AsyncMock(return_value=(None, False, False))
    mass.cache.set = AsyncMock()
    # use_cache runs fetches through mass.create_task
    mass.create_task.side_effect = lambda coro, **_kwargs: asyncio.ensure_future(coro)
    manifest = Mock()
    manifest.domain = "calm"
    config = Mock()
    config.instance_id = "calm--test"
    config.name = "Calm"
    config.enabled = True
    config.get_value.side_effect = lambda key, default=None: {"log_level": "GLOBAL"}.get(
        key, default
    )
    prov = CalmProvider(mass, manifest, config, SUPPORTED_FEATURES)
    # the real limiter would make the tests wait on each other
    prov.throttler = ThrottlerManager(rate_limit=1000, period=1)
    prov._client = api  # type: ignore[assignment]
    # credential persistence is exercised by the login tests, not the mapping tests
    prov._store_credentials = lambda: None  # type: ignore[method-assign]
    return prov
