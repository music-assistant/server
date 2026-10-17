"""Tests for the artwork of Plex playlists."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock
from xml.etree import ElementTree as ET

import pytest
from plexapi.playlist import Playlist as PlexPlaylist

from music_assistant.providers.plex import PlexProvider

COMPOSITE = "/playlists/42/composite/1700000000"
CUSTOM_POSTER = "/library/metadata/42/thumb/1700000001"


def _plex_playlist(**attrs: str) -> PlexPlaylist:
    """Build a plexapi playlist from the xml Plex returns for it."""
    data = ET.Element(
        "Playlist",
        {
            "key": "/playlists/42/items",
            "ratingKey": "42",
            "title": "Certified",
            "summary": "The songs that have earned their spot.",
            "smart": "1",
            "composite": COMPOSITE,
            **attrs,
        },
    )
    return PlexPlaylist(MagicMock(), data)


@pytest.mark.parametrize(
    ("attrs", "expected"),
    [({"thumb": CUSTOM_POSTER}, CUSTOM_POSTER), ({}, COMPOSITE)],
    ids=["custom poster", "composite only"],
)
async def test_playlist_artwork_prefers_custom_poster(attrs: dict[str, str], expected: str) -> None:
    """A custom Plex poster wins over the composite Plex generates from the tracks."""
    provider: Any = MagicMock()
    provider.instance_id = "plex--test"
    provider._baseurl = "http://plex.local:32400"

    playlist = await PlexProvider._parse_playlist(provider, _plex_playlist(**attrs))

    assert [image.path for image in playlist.metadata.images or []] == [expected]
