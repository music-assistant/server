"""Tests for Podcast Index episode metadata (persons/links), chapters and transcripts."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import LinkType
from music_assistant_models.errors import ProviderUnavailableError

from music_assistant.providers.podcast_index.helpers import (
    get_episode_transcripts_from_data,
    parse_episode_from_data,
)
from music_assistant.providers.podcast_index.provider import PodcastIndexProvider

if TYPE_CHECKING:
    from music_assistant_models.media_items import PodcastEpisode


def _episode_data(**overrides: Any) -> dict[str, Any]:
    """Return minimal Podcast Index episode API data with a valid enclosure."""
    data: dict[str, Any] = {
        "id": 123,
        "title": "Episode 1",
        "enclosureUrl": "https://example.com/ep1.mp3",
        "enclosureType": "audio/mpeg",
    }
    data.update(overrides)
    return data


def _parse(data: dict[str, Any]) -> PodcastEpisode | None:
    return parse_episode_from_data(data, "feed-1", "podcast_index--test", "podcast_index")


# --- parse_episode_from_data: persons / links ------------------------------------------------


def test_persons_map_to_performers() -> None:
    """The API persons array becomes performer names on the episode metadata."""
    episode = _parse(
        _episode_data(persons=[{"name": "Jane Host", "role": "host"}, {"name": "Joe Guest"}])
    )
    assert episode is not None
    assert episode.metadata.performers == {"Jane Host", "Joe Guest"}


def test_link_maps_to_website_link() -> None:
    """The API episode link becomes a WEBSITE link."""
    episode = _parse(_episode_data(link="https://example.com/ep1"))
    assert episode is not None
    assert episode.metadata.links is not None
    link = next(iter(episode.metadata.links))
    assert (link.type, link.url) == (LinkType.WEBSITE, "https://example.com/ep1")


def test_missing_persons_and_link_leave_metadata_unset() -> None:
    """Without persons/link, performers and links stay None and parsing still succeeds."""
    episode = _parse(_episode_data())
    assert episode is not None
    assert episode.metadata.performers is None
    assert episode.metadata.links is None


# --- chapter enrichment on the single-episode path -------------------------------------------


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


def _provider(episode_data: dict[str, Any], session: _FakeSession) -> MagicMock:
    """Build a provider stub sufficient for get_podcast_episode's single lookup."""
    provider = MagicMock()
    provider.instance_id = "podcast_index--test"
    provider.domain = "podcast_index"
    provider.logger = MagicMock()
    provider.mass.http_session = session
    provider._api_request = AsyncMock(return_value={"episode": episode_data})
    return provider


async def _call_get_episode(provider: MagicMock, prov_episode_id: str) -> PodcastEpisode:
    # bypass the @use_cache wrapper to drive the real method directly
    func: Any = PodcastIndexProvider.get_podcast_episode.__wrapped__  # type: ignore[attr-defined]
    result = await func(cast("PodcastIndexProvider", provider), prov_episode_id)
    return cast("PodcastEpisode", result)


async def test_get_podcast_episode_enriches_chapters() -> None:
    """A chaptersUrl on the single-episode path populates metadata.chapters."""
    session = _FakeSession(payload={"chapters": [{"startTime": 0, "title": "Intro"}]})
    provider = _provider(_episode_data(chaptersUrl="https://example.com/ch.json"), session)
    episode = await _call_get_episode(provider, "feed-1|123")
    assert session.calls == 1
    assert episode.metadata.chapters is not None
    assert [c.name for c in episode.metadata.chapters] == ["Intro"]


async def test_get_podcast_episode_without_chapters_url() -> None:
    """No chaptersUrl: the episode resolves normally with no chapters and no fetch."""
    session = _FakeSession(payload={"chapters": [{"startTime": 0, "title": "Intro"}]})
    provider = _provider(_episode_data(), session)
    episode = await _call_get_episode(provider, "feed-1|123")
    assert session.calls == 0
    assert episode.metadata.chapters is None


# --- transcripts ------------------------------------------------------------------------------

TRANSCRIPT = {"url": "https://example.com/ep1.vtt", "type": "text/vtt"}


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        (
            {"transcripts": [TRANSCRIPT], "transcriptUrl": "https://example.com/old.srt"},
            [TRANSCRIPT],
        ),
        (
            {"transcriptUrl": "https://example.com/old.srt"},
            [{"url": "https://example.com/old.srt"}],
        ),
        ({}, None),
    ],
)
def test_episode_transcripts(overrides: dict[str, Any], expected: list[Any] | None) -> None:
    """The transcripts list wins over the older transcriptUrl, and either marks the episode."""
    data = _episode_data(**overrides)
    episode = _parse(data)
    assert episode is not None
    assert episode.metadata.has_transcript is (expected is not None)
    assert get_episode_transcripts_from_data(data) == expected


async def test_transcript_is_looked_up_by_episode_id() -> None:
    """Opening a transcript passes the episode's transcripts on to the shared fetch."""
    provider = MagicMock()
    provider._get_episode_transcripts = AsyncMock(return_value=[TRANSCRIPT])
    with patch(
        "music_assistant.providers.podcast_index.provider.get_episode_transcript",
        AsyncMock(return_value=("Some words.", [])),
    ) as fetch:
        result = await PodcastIndexProvider.get_podcast_episode_transcript(provider, "feed-1|123")
    assert result == ("Some words.", [])
    provider._get_episode_transcripts.assert_awaited_once_with("123")
    assert fetch.await_args is not None
    assert fetch.await_args.kwargs["transcripts"] == [TRANSCRIPT]


async def test_transcript_during_an_outage_is_none() -> None:
    """An unreachable Podcast Index shows as no transcript rather than an error."""
    provider = MagicMock()
    provider._get_episode_transcripts = AsyncMock(side_effect=ProviderUnavailableError("boom"))
    result = await PodcastIndexProvider.get_podcast_episode_transcript(provider, "feed-1|123")
    assert result == (None, None)
