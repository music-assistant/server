"""Tests for gpodder external chapter enrichment on the single-episode path."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.errors import MediaNotFoundError

from music_assistant.providers.gpodder import GPodder
from music_assistant.providers.gpodder.client import EpisodeActionPlay

from .conftest import FEED, episode


class _FakeResponse:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    async def json(self, **kwargs: Any) -> Any:
        return self._payload


class _FakeGetContext:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    async def __aenter__(self) -> _FakeResponse:
        return _FakeResponse(self._session.payload)

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


class _FakeSession:
    def __init__(self, payload: Any) -> None:
        self.payload = payload
        self.calls = 0

    def get(self, url: str, **kwargs: Any) -> _FakeGetContext:
        self.calls += 1
        return _FakeGetContext(self)


def _serve(provider: GPodder) -> _FakeSession:
    """Serve a feed holding an enclosure-less episode next to one with external chapters."""
    podcast = {
        "episodes": [
            # enclosure-less: get_stream_url_and_guid_from_episode raises ValueError -> skipped
            {"enclosures": [], "guid": "guid-1"},
            episode(1, guid="guid-1", chapters_json_url="https://example.com/ch.json"),
        ]
    }
    provider._cache_get_podcast = AsyncMock(return_value=podcast)  # type: ignore[method-assign]
    action = EpisodeActionPlay(
        podcast=FEED, episode="https://example.com/ep1.mp3", position=60, total=1200
    )
    cast("Mock", provider._client).get_episode_actions = AsyncMock(return_value=([action], 999))
    session = _FakeSession(payload={"chapters": [{"startTime": 0, "title": "Intro"}]})
    cast("Mock", provider.mass).http_session = session
    return session


async def test_enriches_matching_episode_and_skips_enclosure_less(provider: GPodder) -> None:
    """An enclosure-less raw episode is skipped; the matching one's chapters are fetched."""
    session = _serve(provider)

    mass_episode = await provider.get_podcast_episode(f"{FEED} guid-1")

    assert session.calls == 1
    assert mass_episode.metadata.chapters is not None
    assert [c.name for c in mass_episode.metadata.chapters] == ["Intro"]
    assert mass_episode.resume_position_ms == 60_000


async def test_no_matching_episode_performs_no_fetch(provider: GPodder) -> None:
    """When no raw episode matches the id, nothing is fetched."""
    session = _serve(provider)

    with pytest.raises(MediaNotFoundError):
        await provider.get_podcast_episode(f"{FEED} other")

    assert session.calls == 0
