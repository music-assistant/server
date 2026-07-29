"""Tests for the podcastfeed -> Mass parsing helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from music_assistant.helpers.podcast_parsers import (
    get_stream_url_from_episode,
    parse_podcast_episode,
)

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


# --- enclosure / stream url selection ---------------------------------------------------------


def test_stream_url_prefers_audio_over_leading_image() -> None:
    """A leading image media:content enclosure is skipped in favor of the audio one (#5920)."""
    audio_url = "https://rss.wbur.org/the-midnight-rebellion/ep1.mp3"
    cover_url = "https://rss.wbur.org/the-midnight-rebellion/cover.jpg"
    episode = _episode(
        enclosures=[
            {"url": cover_url, "mime_type": "image/jpeg"},
            {"url": audio_url, "mime_type": "audio/mpeg"},
            {"url": audio_url, "mime_type": "audio/mpeg"},
        ]
    )
    assert get_stream_url_from_episode(episode=episode) == audio_url


def test_stream_url_accepts_bogus_mime_type() -> None:
    """A real audio enclosure with a bogus declared mime type is still used (#5692)."""
    episode = _episode(
        enclosures=[{"url": "https://example.com/ep1.mp3", "mime_type": "application/octet-stream"}]
    )
    assert get_stream_url_from_episode(episode=episode) == "https://example.com/ep1.mp3"


def test_stream_url_accepts_video_enclosure() -> None:
    """A video/mp4 enclosure is accepted as a playable stream."""
    video_url = "https://example.com/ep1.mp4"
    episode = _episode(enclosures=[{"url": video_url, "mime_type": "video/mp4"}])
    assert get_stream_url_from_episode(episode=episode) == video_url


def test_stream_url_skips_enclosures_without_url() -> None:
    """An enclosure missing a url is skipped in favor of a later usable one."""
    episode = _episode(
        enclosures=[
            {"mime_type": "audio/mpeg"},
            {"url": "https://example.com/ep1.mp3", "mime_type": "audio/mpeg"},
        ]
    )
    assert get_stream_url_from_episode(episode=episode) == "https://example.com/ep1.mp3"


def test_stream_url_image_only_returns_none() -> None:
    """An episode whose only enclosure is an image has no playable stream."""
    episode = _episode(
        enclosures=[{"url": "https://example.com/cover.jpg", "mime_type": "image/jpeg"}]
    )
    assert get_stream_url_from_episode(episode=episode) is None
    assert _parse(episode) is None


def test_stream_url_first_audio_enclosure_wins() -> None:
    """When several audio enclosures are declared, the first one wins."""
    episode = _episode(
        enclosures=[
            {"url": "https://example.com/ep1-first.mp3", "mime_type": "audio/mpeg"},
            {"url": "https://example.com/ep1-second.mp3", "mime_type": "audio/mpeg"},
        ]
    )
    assert get_stream_url_from_episode(episode=episode) == "https://example.com/ep1-first.mp3"


def test_stream_url_missing_mime_type_falls_back_to_url() -> None:
    """The default `_episode()` enclosure has no mime_type key and still resolves (fallback)."""
    assert get_stream_url_from_episode(episode=_episode()) == "https://example.com/ep1.mp3"
