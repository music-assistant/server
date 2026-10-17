"""Tests for Subsonic ids of Music Assistant media items."""

from __future__ import annotations

import pytest
from music_assistant_models.enums import MediaType

from music_assistant.providers.subsonic_server.ids import (
    ItemRef,
    parse_subsonic_id,
    to_subsonic_id,
)
from music_assistant.providers.subsonic_server.response import SubsonicError


def test_library_ids() -> None:
    """Library items get a short prefixed id that parses back."""
    assert to_subsonic_id(MediaType.ALBUM, 12) == "al-12"
    ref = parse_subsonic_id("al-12")
    assert ref == ItemRef(MediaType.ALBUM, "12")
    assert ref.is_library
    assert ref.uri == "library://album/12"


@pytest.mark.parametrize(
    ("provider", "item_id"),
    [
        ("opensubsonic--Bnc7DaHk", "V1RccGAZDO2SsPgKvAaYBv"),
        ("filesystem_local--abc", "Artist/Album (2020)/01 - Track.flac"),
        ("spotify--x", "spotify:track:6rqhFgbbKwnb9MLmUQDhG6"),
    ],
)
def test_provider_ids_round_trip(provider: str, item_id: str) -> None:
    """Provider items round trip through an url-safe id."""
    subsonic_id = to_subsonic_id(MediaType.TRACK, item_id, provider)
    assert subsonic_id.startswith("tr-p")
    assert all(char.isalnum() or char in "-_" for char in subsonic_id)
    ref = parse_subsonic_id(subsonic_id)
    assert ref == ItemRef(MediaType.TRACK, item_id, provider)
    assert not ref.is_library
    assert ref.uri == f"{provider}://track/{item_id}"


@pytest.mark.parametrize("value", ["", "xx-1", "al-", "al-abc", "al-p!!!", "al-pYWJj", "12"])
def test_invalid_ids(value: str) -> None:
    """Malformed ids give the not-found error."""
    with pytest.raises(SubsonicError) as err:
        parse_subsonic_id(value)
    assert err.value.code == 70


def test_media_type_restriction() -> None:
    """An id of another media type than allowed is rejected."""
    assert parse_subsonic_id("ar-3", MediaType.ARTIST).media_type == MediaType.ARTIST
    with pytest.raises(SubsonicError):
        parse_subsonic_id("ar-3", MediaType.ALBUM)
