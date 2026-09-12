"""Tests for the iHeartRadio parsers."""

from __future__ import annotations

from music_assistant_models.enums import ImageType, MediaType

from music_assistant.providers.iheartradio.parsers import (
    parse_live_station,
    parse_now_playing,
    parse_podcast,
    parse_podcast_episode,
    pick_stream_url,
)

from .conftest import DOMAIN, EPISODE, INSTANCE_ID, NOW_PLAYING, PODCAST, STATION


def test_parse_live_station() -> None:
    """A station payload becomes a Radio with artwork, genres and its website."""
    radio = parse_live_station(STATION, INSTANCE_ID, DOMAIN)
    assert radio is not None
    assert radio.item_id == "6185"
    assert radio.name == "KIIS 1065"
    assert radio.metadata.description == "Sydney's #1 Hit Music Station"
    assert radio.metadata.genres == {"Pop"}
    assert {image.type for image in radio.metadata.images or []} == {
        ImageType.THUMB,
        ImageType.LOGO,
    }
    assert next(iter(radio.provider_mappings)).url == "kiis1065.com.au"


def test_parse_live_station_from_search_hit() -> None:
    """A search hit names its artwork and genre differently from the station endpoints."""
    hit = {
        "id": 6185,
        "name": "KIIS 1065",
        "imageUrl": "https://i.iheart.com/x.png",
        "genre": "Pop",
    }
    radio = parse_live_station(hit, INSTANCE_ID, DOMAIN)
    assert radio is not None
    assert radio.image is not None
    assert radio.image.path == "https://i.iheart.com/x.png"
    assert radio.metadata.genres == {"Pop"}


def test_parse_podcast_from_search_hit() -> None:
    """A search hit calls the artwork image where the podcast endpoints call it imageUrl."""
    podcast = parse_podcast(
        {**PODCAST, "imageUrl": None, "image": "https://x/y.jpg"}, INSTANCE_ID, DOMAIN
    )
    assert podcast is not None
    assert podcast.image is not None
    assert podcast.image.path == "https://x/y.jpg"


def test_parse_podcast_episode() -> None:
    """An episode carries its podcast, publication date and the podcast's artwork."""
    episode = parse_podcast_episode(EPISODE, "21124503", 3, INSTANCE_ID, DOMAIN, PODCAST)
    assert episode is not None
    assert episode.item_id == "21124503:343681362"
    assert episode.position == 3
    assert episode.duration == 1862
    assert episode.podcast.media_type == MediaType.PODCAST
    assert episode.podcast.name == PODCAST["title"]
    assert episode.metadata.release_date is not None
    assert episode.metadata.release_date.year == 2026
    assert episode.metadata.explicit is False
    assert episode.image is not None
    assert episode.image.path == PODCAST["imageUrl"]


def test_parse_now_playing() -> None:
    """The now-playing payload gives title, artist, album, artwork and duration."""
    metadata = parse_now_playing(NOW_PLAYING, "https://station.png")
    assert metadata is not None
    assert metadata.title == "Red Rocks"
    assert metadata.artist == "Above & Beyond"
    assert metadata.album == "The Club Instrumentals"
    assert metadata.image_url == "http://image.iheart.com/red-rocks.jpg"
    assert metadata.duration == 466
    assert metadata.elapsed_time == 466
    assert parse_now_playing({}, None) is None


def test_pick_stream_url() -> None:
    """The secure HLS stream wins, then the secure Shoutcast one."""
    assert pick_stream_url(STATION["streams"]) == "https://example.com/kiis.m3u8"
    streams = {"shoutcast_stream": "http://x.aac", "secure_shoutcast_stream": "https://x.aac"}
    assert pick_stream_url(streams) == "https://x.aac"
    assert pick_stream_url({}) is None
