"""Unit tests for the Nicovideo feed activity converter."""

from __future__ import annotations

from music_assistant_models.media_items import ItemMapping
from niconico.objects.nvapi import Activity

from tests.providers.nicovideo.helpers import create_converter_manager


def _build_video_activity() -> Activity:
    """Build a minimal feed Activity referencing a playable video."""
    return Activity.model_validate(
        {
            "sensitive": False,
            "message": {"text": "posted a video"},
            "thumbnailUrl": "https://example.com/thumb.jpg",
            "label": {"text": "video"},
            "content": {
                "type": "video",
                "id": "sm123",
                "title": "Test video",
                "url": "https://www.nicovideo.jp/watch/sm123",
                "startedAt": "2026-01-01T00:00:00+09:00",
                "video": {"duration": 120},
            },
            "id": "act1",
            "kind": "video_upload",
            "createdAt": "2026-01-01T00:00:00+09:00",
            "actor": {
                "id": "user42",
                "type": "user",
                "name": "Test uploader",
                "iconUrl": "https://example.com/icon.jpg",
                "url": "https://www.nicovideo.jp/user/user42",
                "isLive": False,
            },
        }
    )


def test_convert_by_activity_artist_uses_instance_id() -> None:
    """Feed artist mappings carry the provider instance id, not the domain."""
    converter_manager = create_converter_manager()
    track = converter_manager.track.convert_by_activity(_build_video_activity())

    assert track is not None
    assert len(track.artists) == 1
    artist = track.artists[0]
    assert isinstance(artist, ItemMapping)
    assert artist.provider == converter_manager.provider.instance_id
    assert artist.provider != converter_manager.provider.domain
