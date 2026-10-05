"""Test Yoto Provider."""

from unittest.mock import AsyncMock, MagicMock

from yoto_api import Card as YotoCard
from yoto_api import Chapter as YotoChapter
from yoto_api import Track as YotoTrack

from music_assistant.providers.yoto import YotoProvider


def _make_provider() -> YotoProvider:
    """Return a YotoProvider instance with mocked dependencies."""
    mass = AsyncMock()
    mass.http_session = MagicMock()
    manifest = MagicMock()
    manifest.domain = "yoto"
    config = MagicMock()
    config.name = "Yoto Provider"
    config.instance_id = "yoto_provider_instance"
    config.get_value.side_effect = lambda key, default=None: {
        "log_level": "GLOBAL",
    }.get(key, default)
    return YotoProvider(mass, manifest, config)


def test_parse_track_from_card() -> None:
    """Parse an album and tracklist from a resolved card/album API response."""
    provider = _make_provider()
    card = _make_multitrack_chapter_card()
    album = provider._parse_album(card)

    assert album.name == card.title
    assert album.item_id == card.id
    assert album.artists[0].name == card.author

    tracks = []
    for idx, chapter in enumerate(card.chapters.values()):
        tracks.append(provider._parse_track("Card_ID", chapter, idx, album))

    assert len(tracks) == 2

    # Assert parsed track/card names match the source data
    assert tracks[0].name == "Chapter 1"
    assert tracks[1].name == "Chapter 2"

    # Assert chapters with multiple tracks and no explicit duration equal sum of track durations
    ch1_tracks_sum = sum(t.duration for t in card.chapters["01"].tracks.values() if t.duration)
    assert tracks[0].duration == ch1_tracks_sum
    assert tracks[0].duration == 8

    # Assert chapter with explicit duration preserves its duration
    assert tracks[1].duration == 458


def _make_multitrack_chapter_card() -> YotoCard:
    """Generate a dummy card for testing with multiple tracks."""
    return YotoCard(
        id="Card_ID",
        title="Mock Card",
        description="Mock Card Description",
        category="activities",
        author="Yoto",
        cover_image_large="http://image.example.com",
        chapters={
            "01": YotoChapter(
                key="01",
                title="Chapter 1",
                icon="http://image.example.com",
                duration=None,
                tracks={
                    "01": YotoTrack(
                        key="01",
                        title="Track 01 01",
                        duration=4,
                        format="aac",
                        trackUrl="http://track.example.com/1",
                    ),
                    "02": YotoTrack(
                        key="02",
                        title="Track 01 02",
                        duration=4,
                        format="aac",
                        trackUrl="http://track.example.com/2",
                    ),
                },
            ),
            "02": YotoChapter(
                key="02",
                title="Chapter 2",
                icon="http://image.example.com",
                duration=458,
                tracks={
                    "01": YotoTrack(
                        key="01",
                        title="Track 02 01",
                        duration=5,
                        format="aac",
                        trackUrl="http://track.example.com/3",
                    ),
                    "02": YotoTrack(
                        key="02",
                        title="Track 02 02",
                        duration=4,
                        format="aac",
                        trackUrl="http://track.example.com/4",
                    ),
                },
            ),
        },
    )
