"""Shared MagicMock builders for FastMCP server tests."""
# mypy: disable-error-code="no-untyped-def, assignment"

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from music_assistant_models.enums import MediaType


def fake_media_item(media_type: MediaType, **kwargs: Any) -> MagicMock:
    """Build a stub item exposing ``media_type`` and the usual brief fields."""
    item = MagicMock(
        spec_set=[
            "media_type",
            "uri",
            "name",
            "artists",
            "artist",
            "album",
            "duration",
            "year",
            "track_count",
            "owner",
            "description",
            "metadata",
            "item_id",
            "provider",
            "available",
            "disc_number",
            "track_number",
        ]
    )
    item.media_type = media_type
    item.uri = kwargs.get("uri", f"library://{media_type.value}/1")
    item.name = kwargs.get("name", f"{media_type.value.title()} Name")
    item.artists = kwargs.get("artists", [])
    item.artist = kwargs.get("artist")
    item.album = kwargs.get("album")
    item.duration = kwargs.get("duration", 180)
    item.year = kwargs.get("year")
    item.track_count = kwargs.get("track_count")
    item.owner = kwargs.get("owner")
    item.description = kwargs.get("description")
    item.metadata = kwargs.get("metadata")
    item.item_id = kwargs.get("item_id", "1")
    item.provider = kwargs.get("provider", "library")
    item.available = kwargs.get("available", True)
    item.disc_number = kwargs.get("disc_number")
    item.track_number = kwargs.get("track_number")
    return item


def fake_album(**kwargs: Any) -> MagicMock:
    """Album-shaped stub with drill-down fields populated."""
    defaults = {
        "uri": "library://album/7",
        "name": "Black Sands",
        "artist": "Bonobo",
        "year": 2010,
        "item_id": "7",
    }
    defaults.update(kwargs)
    return fake_media_item(MediaType.ALBUM, **defaults)


def fake_artist(**kwargs: Any) -> MagicMock:
    """Artist-shaped stub with drill-down fields populated."""
    defaults = {
        "uri": "library://artist/27",
        "name": "deadmau5",
        "item_id": "27",
    }
    defaults.update(kwargs)
    return fake_media_item(MediaType.ARTIST, **defaults)


def fake_track(**kwargs: Any) -> MagicMock:
    """Track-shaped stub for album track listings (no ``media_type``)."""
    track = MagicMock(
        spec_set=[
            "uri",
            "name",
            "artists",
            "album",
            "duration",
            "disc_number",
            "track_number",
            "available",
        ]
    )
    track.uri = kwargs.get("uri", "library://track/1")
    track.name = kwargs.get("name", "Kiara")
    track.artists = kwargs.get("artists", [])
    track.album = kwargs.get("album", "Black Sands")
    track.duration = kwargs.get("duration", 230)
    track.disc_number = kwargs.get("disc_number", 1)
    track.track_number = kwargs.get("track_number", 1)
    track.available = kwargs.get("available", True)
    return track
