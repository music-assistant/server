"""Test YouTube Music album track parsing."""

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from music_assistant.providers.ytmusic import YoutubeMusicProvider


@pytest.fixture
def provider() -> YoutubeMusicProvider:
    """Return a YoutubeMusicProvider instance with mocked dependencies."""
    mass = AsyncMock()
    manifest = MagicMock()
    manifest.domain = "ytmusic"
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    prov = YoutubeMusicProvider(mass, manifest, config)
    prov._headers = {}
    prov._yt_user = None
    prov.language = "en"
    return prov


def _album_obj() -> dict[str, Any]:
    """Return an album response holding one track without a resolvable artist id."""
    return {
        "title": "All Stand Together",
        "artists": [{"id": "UC824jbNWWDR-NBv8wuE-OUg", "name": "Lost Frequencies"}],
        "tracks": [
            {
                "videoId": "video1",
                "title": "Collaboration Track",
                "isAvailable": True,
                "artists": [{"id": None, "name": "Lost Frequencies & Zak Abel"}],
            },
            {
                "videoId": "video2",
                "title": "Solo Track",
                "isAvailable": True,
                "artists": [{"id": "UC824jbNWWDR-NBv8wuE-OUg", "name": "Lost Frequencies"}],
            },
        ],
    }


async def test_album_tracks_fall_back_to_album_artist(provider: YoutubeMusicProvider) -> None:
    """
    An album track whose artists carry no id is kept, credited to the album artist.

    YTM returns no artist id for some album tracks, which used to drop them from the
    album listing entirely.
    """
    with patch("music_assistant.providers.ytmusic.get_album", AsyncMock(return_value=_album_obj())):
        # call the undecorated function so the @use_cache wrapper stays out of the test
        get_album_tracks = cast("Any", YoutubeMusicProvider.get_album_tracks).__wrapped__
        tracks = await get_album_tracks(provider, "album1")

    assert [track.item_id for track in tracks] == ["video1", "video2"]
    assert [artist.item_id for artist in tracks[0].artists] == ["UC824jbNWWDR-NBv8wuE-OUg"]
