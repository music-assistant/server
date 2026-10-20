"""Tests for the release date of BBC Sounds podcast episodes."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from music_assistant_models.media_items import PodcastEpisode as MAPodcastEpisode
from sounds.models import Podcast, PodcastEpisode

from music_assistant.providers.bbc_sounds.adaptor import Context, PodcastConverter

if TYPE_CHECKING:
    from music_assistant.providers.bbc_sounds import BBCSoundsProvider


@pytest.mark.parametrize(
    ("release", "availability", "expected"),
    [
        (
            {"date": "2026-09-18T00:00:00Z", "label": "18 Sep 2026"},
            {"from": "2026-09-18T18:00:00Z"},
            datetime(2026, 9, 18, tzinfo=UTC),
        ),
        (
            {"date": None, "label": None},
            {"from": "2026-09-25T18:00:00Z"},
            datetime(2026, 9, 25, 18, tzinfo=UTC),
        ),
        (None, None, None),
    ],
)
async def test_podcast_episode_gets_the_release_date(
    provider: BBCSoundsProvider,
    release: dict[str, Any] | None,
    availability: dict[str, Any] | None,
    expected: datetime | None,
) -> None:
    """The release date is used, or when the episode became available if it has none."""
    converter = PodcastConverter(Context(provider=provider, provider_domain="bbc_sounds"))
    episode = PodcastEpisode(
        id="e01",
        pid="e01",
        titles={"primary": "A podcast", "secondary": "An episode"},
        container=Podcast(id="p01", titles={"primary": "A podcast"}),
        release=release,
        availability=availability,
    )

    result = await converter.convert(episode)

    assert isinstance(result, MAPodcastEpisode)
    assert result.metadata.release_date == expected
