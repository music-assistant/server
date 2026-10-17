"""Tests for YouTube Music get_similar_artists."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.media_items import Artist, MediaItemImage

from music_assistant.providers.ytmusic import YoutubeMusicProvider


@pytest.fixture
def provider() -> YoutubeMusicProvider:
    """Return a YoutubeMusicProvider instance with mocked dependencies."""
    mass = AsyncMock()
    manifest = MagicMock()
    manifest.domain = "ytmusic"
    config = MagicMock()
    config.instance_id = "ytmusic--test"
    config.get_value.return_value = "GLOBAL"
    prov = YoutubeMusicProvider(mass, manifest, config)
    prov._headers = {}
    prov._yt_user = None
    prov.language = "en"
    return prov


def _artist_with_related() -> dict[str, Any]:
    """Return an artist payload containing related artists."""
    return {
        "channelId": "UCartist1",
        "name": "Artist 1",
        "related": {
            "results": [
                {
                    "title": "Similar Artist A",
                    "browseId": "UCsimilarA",
                    "thumbnails": [
                        {
                            "url": "https://lh3.googleusercontent.com/a1=w544-h544",
                            "width": 544,
                            "height": 544,
                        }
                    ],
                },
                {
                    "title": "Similar Artist B",
                    "browseId": "UCsimilarB",
                    "thumbnails": [
                        {
                            "url": "https://lh3.googleusercontent.com/b1=w544-h544",
                            "width": 544,
                            "height": 544,
                        }
                    ],
                },
            ]
        },
    }


async def test_get_similar_artists_success(provider: YoutubeMusicProvider) -> None:
    """get_similar_artists parses related artists and their metadata."""
    with patch(
        "music_assistant.providers.ytmusic.get_artist",
        AsyncMock(return_value=_artist_with_related()),
    ) as mock_get_artist:
        get_similar = cast("Any", YoutubeMusicProvider.get_similar_artists).__wrapped__
        artists = await get_similar(provider, "UCartist1")

    mock_get_artist.assert_awaited_once_with(prov_artist_id="UCartist1", headers=provider._headers)
    assert len(artists) == 2
    assert all(isinstance(a, Artist) for a in artists)

    artist_a = artists[0]
    assert artist_a.item_id == "UCsimilarA"
    assert artist_a.name == "Similar Artist A"
    assert artist_a.provider == provider.instance_id
    assert len(artist_a.provider_mappings) == 1
    mapping_a = next(iter(artist_a.provider_mappings))
    assert mapping_a.item_id == "UCsimilarA"
    assert mapping_a.provider_domain == "ytmusic"
    assert mapping_a.provider_instance == provider.instance_id
    assert mapping_a.url == "https://music.youtube.com/channel/UCsimilarA"
    assert len(artist_a.metadata.images) == 1
    assert isinstance(artist_a.metadata.images[0], MediaItemImage)
    assert artist_a.metadata.images[0].path == "https://lh3.googleusercontent.com/a1=w600-h600-p"

    artist_b = artists[1]
    assert artist_b.item_id == "UCsimilarB"
    assert artist_b.name == "Similar Artist B"


async def test_get_similar_artists_missing_related(provider: YoutubeMusicProvider) -> None:
    """get_similar_artists returns empty list when related section is missing."""
    data = {"channelId": "UCartist1", "name": "Artist 1"}
    with patch("music_assistant.providers.ytmusic.get_artist", AsyncMock(return_value=data)):
        get_similar = cast("Any", YoutubeMusicProvider.get_similar_artists).__wrapped__
        artists = await get_similar(provider, "UCartist1")

    assert artists == []
