"""Tests for gpodder external chapter enrichment on the single-episode path."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock

from music_assistant.helpers.podcast_parsers import parse_podcast_episode
from music_assistant.providers.gpodder import GPodder

if TYPE_CHECKING:
    from music_assistant_models.media_items import PodcastEpisode


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


def _mass_episode() -> PodcastEpisode:
    """Build a resolved episode with no chapters, as the enrichment input."""
    episode = parse_podcast_episode(
        episode={
            "title": "Episode 1",
            "enclosures": [{"url": "https://example.com/ep1.mp3"}],
            "guid": "guid-1",
        },
        prov_podcast_id="pod-1",
        position=1,
        instance_id="gpodder--test",
        domain="gpodder",
    )
    assert episode is not None
    return episode


def _provider(podcast: dict[str, Any], session: _FakeSession) -> MagicMock:
    """Build a gpodder provider stub sufficient for _enrich_episode_chapters."""
    provider = MagicMock()
    provider._cache_get_podcast = AsyncMock(return_value=podcast)
    provider.mass.http_session = session
    return provider


async def _enrich(provider: MagicMock, guid_or_stream_url: str, episode: PodcastEpisode) -> None:
    await GPodder._enrich_episode_chapters(
        cast("GPodder", provider), "pod-1", guid_or_stream_url, episode
    )


async def test_enriches_matching_episode_and_skips_enclosure_less() -> None:
    """An enclosure-less raw episode is skipped; the matching one's chapters are fetched."""
    podcast = {
        "episodes": [
            # enclosure-less: get_stream_url_and_guid_from_episode raises ValueError -> skipped
            {"enclosures": []},
            {
                "enclosures": [{"url": "https://example.com/ep1.mp3"}],
                "guid": "guid-1",
                "chapters_json_url": "https://example.com/ch.json",
            },
        ]
    }
    session = _FakeSession(payload={"chapters": [{"startTime": 0, "title": "Intro"}]})
    mass_episode = _mass_episode()
    await _enrich(_provider(podcast, session), "guid-1", mass_episode)
    assert session.calls == 1
    assert mass_episode.metadata.chapters is not None
    assert [c.name for c in mass_episode.metadata.chapters] == ["Intro"]


async def test_no_matching_episode_performs_no_fetch() -> None:
    """When no raw episode matches the id, nothing is fetched and chapters stay None."""
    podcast = {
        "episodes": [
            {"enclosures": [{"url": "https://example.com/other.mp3"}], "guid": "other"},
        ]
    }
    session = _FakeSession(payload={"chapters": [{"startTime": 0, "title": "Intro"}]})
    mass_episode = _mass_episode()
    await _enrich(_provider(podcast, session), "guid-1", mass_episode)
    assert session.calls == 0
    assert mass_episode.metadata.chapters is None
