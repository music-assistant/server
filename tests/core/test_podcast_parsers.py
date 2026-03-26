"""Tests for podcast_parsers helper module."""

from __future__ import annotations

from typing import Any

import pytest

from music_assistant.helpers.podcast_parsers import (
    get_stream_url_and_guid_from_episode,
    parse_podcast,
    parse_podcast_episode,
)

INSTANCE_ID = "test_instance"
DOMAIN = "test_domain"
FEED_URL = "https://example.com/feed.rss"


def _minimal_feed() -> dict[str, Any]:
    """Return a minimal valid parsed podcast feed dict."""
    return {
        "title": "My Podcast",
        "author": "Test Author",
        "description": "A test podcast",
        "link": "https://example.com/podcast",
        "cover_url": "https://example.com/cover.jpg",
        "episodes": [],
    }


def _minimal_episode() -> dict[str, Any]:
    """Return a minimal valid episode dict."""
    return {
        "title": "Episode 1",
        "total_time": 1800,
        "enclosures": [{"url": "https://example.com/ep1.mp3"}],
        "guid": "ep1-unique-guid",
        "published": 1700000000,
    }


# --- parse_podcast ---


def test_parse_podcast_basic_fields() -> None:
    """parse_podcast maps basic title/author/description fields."""
    feed = _minimal_feed()
    podcast = parse_podcast(
        feed_url=FEED_URL,
        parsed_feed=feed,
        instance_id=INSTANCE_ID,
        domain=DOMAIN,
    )
    assert podcast.name == "My Podcast"
    assert podcast.publisher == "Test Author"
    assert podcast.metadata.description == "A test podcast"
    assert podcast.uri == "https://example.com/podcast"


def test_parse_podcast_uses_feed_url_as_item_id_by_default() -> None:
    """parse_podcast uses feed_url as item_id when mass_item_id is not given."""
    feed = _minimal_feed()
    podcast = parse_podcast(
        feed_url=FEED_URL,
        parsed_feed=feed,
        instance_id=INSTANCE_ID,
        domain=DOMAIN,
    )
    assert podcast.item_id == FEED_URL


def test_parse_podcast_uses_mass_item_id_when_provided() -> None:
    """parse_podcast overrides item_id with mass_item_id when supplied."""
    feed = _minimal_feed()
    podcast = parse_podcast(
        feed_url=FEED_URL,
        parsed_feed=feed,
        instance_id=INSTANCE_ID,
        domain=DOMAIN,
        mass_item_id="custom-id",
    )
    assert podcast.item_id == "custom-id"


def test_parse_podcast_cover_image() -> None:
    """parse_podcast attaches cover image when cover_url present."""
    feed = _minimal_feed()
    podcast = parse_podcast(
        feed_url=FEED_URL,
        parsed_feed=feed,
        instance_id=INSTANCE_ID,
        domain=DOMAIN,
    )
    assert podcast.metadata.images is not None
    assert len(podcast.metadata.images) == 1
    assert podcast.metadata.images[0].path == "https://example.com/cover.jpg"


def test_parse_podcast_no_cover_image() -> None:
    """parse_podcast handles missing cover_url gracefully."""
    feed = _minimal_feed()
    del feed["cover_url"]
    podcast = parse_podcast(
        feed_url=FEED_URL,
        parsed_feed=feed,
        instance_id=INSTANCE_ID,
        domain=DOMAIN,
    )
    assert not podcast.metadata.images


def test_parse_podcast_genres_flat_strings() -> None:
    """parse_podcast extracts flat string genres from itunes_categories."""
    feed = _minimal_feed()
    feed["itunes_categories"] = ["Technology", "Music"]
    podcast = parse_podcast(
        feed_url=FEED_URL,
        parsed_feed=feed,
        instance_id=INSTANCE_ID,
        domain=DOMAIN,
    )
    assert podcast.metadata.genres is not None
    assert "Technology" in podcast.metadata.genres
    assert "Music" in podcast.metadata.genres


def test_parse_podcast_genres_nested_lists() -> None:
    """parse_podcast flattens nested lists of genres."""
    feed = _minimal_feed()
    feed["itunes_categories"] = [["Technology", "Software"], "Music"]
    podcast = parse_podcast(
        feed_url=FEED_URL,
        parsed_feed=feed,
        instance_id=INSTANCE_ID,
        domain=DOMAIN,
    )
    assert podcast.metadata.genres is not None
    assert "Technology" in podcast.metadata.genres
    assert "Software" in podcast.metadata.genres
    assert "Music" in podcast.metadata.genres


def test_parse_podcast_language() -> None:
    """parse_podcast extracts the language field."""
    feed = _minimal_feed()
    feed["language"] = "en-US"
    podcast = parse_podcast(
        feed_url=FEED_URL,
        parsed_feed=feed,
        instance_id=INSTANCE_ID,
        domain=DOMAIN,
    )
    assert podcast.metadata.languages is not None
    assert "en-US" in podcast.metadata.languages


def test_parse_podcast_total_episodes() -> None:
    """parse_podcast sets total_episodes from the episodes list length."""
    feed = _minimal_feed()
    feed["episodes"] = [{}, {}, {}]
    podcast = parse_podcast(
        feed_url=FEED_URL,
        parsed_feed=feed,
        instance_id=INSTANCE_ID,
        domain=DOMAIN,
    )
    assert podcast.total_episodes == 3


def test_parse_podcast_fallback_author() -> None:
    """parse_podcast falls back to itunes_author if author is absent."""
    feed = _minimal_feed()
    del feed["author"]
    feed["itunes_author"] = "iTunes Author"
    podcast = parse_podcast(
        feed_url=FEED_URL,
        parsed_feed=feed,
        instance_id=INSTANCE_ID,
        domain=DOMAIN,
    )
    assert podcast.publisher == "iTunes Author"


# --- get_stream_url_and_guid_from_episode ---


def test_get_stream_url_and_guid_success() -> None:
    """Returns (url, guid) for a well-formed episode dict."""
    episode = _minimal_episode()
    url, guid = get_stream_url_and_guid_from_episode(episode=episode)
    assert url == "https://example.com/ep1.mp3"
    assert guid == "ep1-unique-guid"


def test_get_stream_url_and_guid_no_guid() -> None:
    """Returns (url, None) when no guid present."""
    episode = _minimal_episode()
    del episode["guid"]
    url, guid = get_stream_url_and_guid_from_episode(episode=episode)
    assert url == "https://example.com/ep1.mp3"
    assert guid is None


def test_get_stream_url_and_guid_guid_with_spaces_is_none() -> None:
    """Guid containing spaces is treated as invalid and returned as None."""
    episode = _minimal_episode()
    episode["guid"] = "guid with spaces"
    _url, guid = get_stream_url_and_guid_from_episode(episode=episode)
    assert guid is None


def test_get_stream_url_and_guid_missing_enclosures_raises() -> None:
    """Raises ValueError when enclosures list is empty."""
    episode = _minimal_episode()
    episode["enclosures"] = []
    with pytest.raises(ValueError, match="enclosure"):
        get_stream_url_and_guid_from_episode(episode=episode)


def test_get_stream_url_and_guid_missing_url_raises() -> None:
    """Raises ValueError when enclosure has no url key."""
    episode = _minimal_episode()
    episode["enclosures"] = [{}]
    with pytest.raises(ValueError, match="Stream URL"):
        get_stream_url_and_guid_from_episode(episode=episode)


# --- parse_podcast_episode ---


def test_parse_podcast_episode_basic() -> None:
    """parse_podcast_episode returns a PodcastEpisode for a valid episode dict."""
    episode = _minimal_episode()
    result = parse_podcast_episode(
        episode=episode,
        prov_podcast_id="feed-id",
        episode_cnt=1,
        instance_id=INSTANCE_ID,
        domain=DOMAIN,
    )
    assert result is not None
    assert result.name == "Episode 1"
    assert result.duration == 1800
    assert result.position == 1


def test_parse_podcast_episode_missing_enclosure_returns_none() -> None:
    """parse_podcast_episode returns None when the episode has no enclosures."""
    episode = _minimal_episode()
    episode["enclosures"] = []
    result = parse_podcast_episode(
        episode=episode,
        prov_podcast_id="feed-id",
        episode_cnt=1,
        instance_id=INSTANCE_ID,
        domain=DOMAIN,
    )
    assert result is None


def test_parse_podcast_episode_with_published_date() -> None:
    """parse_podcast_episode sets release_date when published timestamp present."""
    episode = _minimal_episode()
    result = parse_podcast_episode(
        episode=episode,
        prov_podcast_id="feed-id",
        episode_cnt=1,
        instance_id=INSTANCE_ID,
        domain=DOMAIN,
    )
    assert result is not None
    assert result.metadata.release_date is not None


def test_parse_podcast_episode_published_zero_is_none() -> None:
    """parse_podcast_episode treats published=0 as unknown (no release_date)."""
    episode = _minimal_episode()
    episode["published"] = 0
    result = parse_podcast_episode(
        episode=episode,
        prov_podcast_id="feed-id",
        episode_cnt=1,
        instance_id=INSTANCE_ID,
        domain=DOMAIN,
    )
    assert result is not None
    assert result.metadata.release_date is None


def test_parse_podcast_episode_uses_podcast_cover_as_fallback() -> None:
    """parse_podcast_episode uses podcast cover when episode has no episode_art_url."""
    episode = _minimal_episode()
    result = parse_podcast_episode(
        episode=episode,
        prov_podcast_id="feed-id",
        episode_cnt=1,
        podcast_cover="https://example.com/podcast-cover.jpg",
        instance_id=INSTANCE_ID,
        domain=DOMAIN,
    )
    assert result is not None
    assert result.metadata.images is not None
    assert result.metadata.images[0].path == "https://example.com/podcast-cover.jpg"


def test_parse_podcast_episode_episode_cover_preferred() -> None:
    """parse_podcast_episode prefers episode_art_url over podcast cover."""
    episode = _minimal_episode()
    episode["episode_art_url"] = "https://example.com/ep-cover.jpg"
    result = parse_podcast_episode(
        episode=episode,
        prov_podcast_id="feed-id",
        episode_cnt=1,
        podcast_cover="https://example.com/podcast-cover.jpg",
        instance_id=INSTANCE_ID,
        domain=DOMAIN,
    )
    assert result is not None
    assert result.metadata.images is not None
    assert result.metadata.images[0].path == "https://example.com/ep-cover.jpg"


def test_parse_podcast_episode_uses_mass_item_id_when_provided() -> None:
    """parse_podcast_episode overrides item_id with mass_item_id when supplied."""
    episode = _minimal_episode()
    result = parse_podcast_episode(
        episode=episode,
        prov_podcast_id="feed-id",
        episode_cnt=1,
        instance_id=INSTANCE_ID,
        domain=DOMAIN,
        mass_item_id="my-custom-ep-id",
    )
    assert result is not None
    assert result.item_id == "my-custom-ep-id"


def test_parse_podcast_episode_with_chapters() -> None:
    """parse_podcast_episode parses chapter data from the episode dict."""
    episode = _minimal_episode()
    episode["chapters"] = [
        {"title": "Intro", "start": 0},
        {"title": "Main", "start": 60},
    ]
    result = parse_podcast_episode(
        episode=episode,
        prov_podcast_id="feed-id",
        episode_cnt=1,
        instance_id=INSTANCE_ID,
        domain=DOMAIN,
    )
    assert result is not None
    # PodcastEpisode does not expose chapters as a field, but confirm the episode
    # parsed correctly with chapter data present (name and duration must survive).
    assert result.name == "Episode 1"
    assert result.duration == 1800


def test_parse_podcast_episode_skips_non_dict_chapters() -> None:
    """parse_podcast_episode skips chapters that are not dicts."""
    episode = _minimal_episode()
    # mix of non-dict and dict chapters; non-dict items must be skipped
    episode["chapters"] = ["not a dict", {"title": "Main", "start": 60}]
    result = parse_podcast_episode(
        episode=episode,
        prov_podcast_id="feed-id",
        episode_cnt=1,
        instance_id=INSTANCE_ID,
        domain=DOMAIN,
    )
    # Should complete without error even with bad chapter entries
    assert result is not None
