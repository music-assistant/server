"""Tests for the podcastfeed -> Mass parsing helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from music_assistant.helpers.podcast_parsers import parse_podcast_episode

if TYPE_CHECKING:
    from music_assistant_models.media_items import PodcastEpisode


def _episode(**overrides: Any) -> dict[str, Any]:
    """Return a minimal podcastparser episode dict with a valid enclosure."""
    episode: dict[str, Any] = {
        "title": "Episode 1",
        "enclosures": [{"url": "https://example.com/ep1.mp3"}],
    }
    episode.update(overrides)
    return episode


def _parse(episode: dict[str, Any]) -> PodcastEpisode | None:
    """Parse an episode dict with the required boilerplate args."""
    return parse_podcast_episode(
        episode=episode,
        prov_podcast_id="podcast-1",
        episode_cnt=1,
        instance_id="podcastfeed--test",
        domain="podcastfeed",
    )


def test_description_is_populated() -> None:
    """A non-empty episode description ends up on the episode metadata."""
    mass_episode = _parse(_episode(description="All about parsing podcasts."))
    assert mass_episode is not None
    assert mass_episode.metadata.description == "All about parsing podcasts."


def test_empty_description_left_unset() -> None:
    """An empty description (podcastparser's default) leaves metadata.description as None."""
    mass_episode = _parse(_episode(description=""))
    assert mass_episode is not None
    assert mass_episode.metadata.description is None


def test_missing_description_left_unset() -> None:
    """An absent description key leaves metadata.description as None."""
    mass_episode = _parse(_episode())
    assert mass_episode is not None
    assert mass_episode.metadata.description is None
