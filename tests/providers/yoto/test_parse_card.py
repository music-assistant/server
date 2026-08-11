"""Test Yoto Provider."""

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

from music_assistant.providers.yoto import YotoProvider

if TYPE_CHECKING:
    from music_assistant_models.media_items import Track


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
    chapters = card.get("content", {}).get("chapters", [])
    album = provider._parse_album(card)

    tracks: list[Track] = []
    for idx, chapter in enumerate(chapters):
        tracks.append(provider._parse_track("test", chapter, idx, album))


def _make_multitrack_chapter_card() -> dict:
    return {
        "cardId": "Card_ID",
        "content": {
            "version": "1",
            "activity": "yoto_Player",
            "config": {
                "resumeTimeout": 2592000,
                "trackNumberOverlayTimeout": 0,
                "disableAutoOverlayLabels": True,
            },
            "availability": "",
            "cover": {"imageL": "http://image.example.com"},
            "playbackType": "linear",
            "editSettings": {"autoOverlayLabels": "chapters", "editKeys": False},
            "chapters": [
                {
                    "key": "01",
                    "title": "Chapter 1",
                    "displayOverlay": "",
                    "overlayLabel": "1",
                    "ambient": None,
                    "defaultTrackDisplay": None,
                    "defaultTrackAmbient": None,
                    "duration": 723,
                    "hasStreams": False,
                    "fileSize": 2341016,
                    "overlayLabelOverride": "1",
                    "display": {"icon16x16": "http://image.example.com"},
                    "tracks": [
                        {
                            "key": "01",
                            "title": "Track 01 01",
                            "format": "aac",
                            "type": "audio",
                            "displayOverlay": "",
                            "overlayLabel": "1",
                            "display": None,
                            "ambient": None,
                            "fileSize": 27848,
                            "channels": "mono",
                            "duration": 4,
                            "overlayLabelOverride": "1",
                            "trackUrl": "[REDACTED]",
                        },
                        {
                            "key": "02",
                            "title": "Track 01 02",
                            "format": "aac",
                            "type": "audio",
                            "displayOverlay": "",
                            "overlayLabel": "",
                            "display": None,
                            "ambient": None,
                            "fileSize": 25930,
                            "channels": "mono",
                            "duration": 4,
                            "overlayLabelOverride": "",
                            "trackUrl": "[REDACTED]",
                        },
                    ],
                },
                {
                    "title": "Chapter 2",
                    "key": "02",
                    "ambient": None,
                    "overlayLabel": "2",
                    "duration": 458,
                    "hasStreams": False,
                    "fileSize": 5745191,
                    "overlayLabelOverride": "2",
                    "defaultTrackAmbient": {"glow2x28": None},
                    "display": {"icon16x16": "http://image.example.com"},
                    "defaultTrackDisplay": {"icon16x16": None},
                    "tracks": [
                        {
                            "format": "aac",
                            "title": "Track 02 01",
                            "type": "audio",
                            "key": "01",
                            "display": None,
                            "ambient": None,
                            "overlayLabel": "2",
                            "fileSize": 65505,
                            "channels": "mono",
                            "duration": 5,
                            "overlayLabelOverride": "2",
                            "trackUrl": "[REDACTED]",
                        },
                        {
                            "format": "aac",
                            "title": "Track 02 02",
                            "type": "audio",
                            "key": "02",
                            "display": None,
                            "ambient": None,
                            "overlayLabel": "",
                            "fileSize": 47602,
                            "channels": "mono",
                            "duration": 4,
                            "overlayLabelOverride": "",
                            "trackUrl": "[REDACTED]",
                        },
                    ],
                },
            ],
        },
        "createdAt": "2020-10-23T01:17:03.729Z",
        "metadata": {
            "description": "Mock Card",
            "category": "activities",
            "author": "Yoto",
            "content": None,
            "previewAudio": "[Mock]",
            "note": "UK Version",
            "maxAge": 14,
            "minAge": 0,
            "abridged": False,
            "accent": "UK English",
            "audioPreviewUrl": "http://preview.example.com",
            "copyright": "© ℗ 2019 Yoto Ltd",
            "cover": {"imageL": "http://image.example.com"},
            "status": {"name": "live", "updatedAt": "2026-01-18T15:08:11.439Z"},
            "languages": ["en", "fr", "it", "de", "es"],
            "media": {"duration": 2814, "fileSize": 24626966, "hasStreams": False},
            "accents": ["UK English"],
            "authors": ["Yoto"],
            "copyrights": ["© ℗ 2019 Yoto Ltd"],
            "genres": ["learning-and-education"],
        },
        "slug": "mock-card",
        "title": "Mock Card",
        "updatedAt": "2024-09-12T18:38:35.187Z",
        "userId": "yoto",
        "sortkey": "mock-card",
        "clubAvailability": [{"store": "uk"}, {"store": "au"}, {"store": "eu"}, {"store": "dev"}],
    }
