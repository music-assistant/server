"""Tests for Soundcloud artist and track metadata parsing."""

from __future__ import annotations

from typing import Any

import pytest
from music_assistant_models.errors import InvalidDataError

from music_assistant.providers.soundcloud import SoundcloudMusicProvider


def _artist_obj(**extra: Any) -> dict[str, Any]:
    """Build a Soundcloud API user object."""
    return {"id": 5, "username": "Some Artist", "permalink": "some-artist", **extra}


def _track_obj(**extra: Any) -> dict[str, Any]:
    """Build a Soundcloud API track object."""
    return {
        "id": 1,
        "title": "Some Track (Radio Edit)",
        "duration": 235818,
        "permalink_url": "https://soundcloud.com/artist/1",
        "user": {"id": 5, "username": "Some Artist", "permalink": "some-artist"},
        **extra,
    }


async def test_parse_artist_requires_id(provider: SoundcloudMusicProvider) -> None:
    """A user without an id cannot be turned into an artist."""
    with pytest.raises(InvalidDataError):
        await provider._parse_artist(_artist_obj(id=None))


async def test_parse_artist_metadata(provider: SoundcloudMusicProvider) -> None:
    """Description and avatar are carried over, with the artwork upgraded."""
    artist = await provider._parse_artist(
        _artist_obj(
            description="Bio text",
            avatar_url="https://i1.sndcdn.com/avatars-abc-large.jpg",
        )
    )

    assert artist.item_id == "5"
    assert artist.metadata.description == "Bio text"
    assert artist.metadata.images is not None
    assert artist.metadata.images[0].path.endswith("avatars-abc-t500x500.jpg")
    mapping = next(iter(artist.provider_mappings))
    assert mapping.url == "https://soundcloud.com/some-artist"


async def test_parse_artist_skips_default_avatar(provider: SoundcloudMusicProvider) -> None:
    """The placeholder avatar is not used, since it has no high resolution variant."""
    artist = await provider._parse_artist(
        _artist_obj(avatar_url="https://a1.sndcdn.com/images/default_avatar_large.png")
    )

    assert not artist.metadata.images


async def test_parse_track_metadata(provider: SoundcloudMusicProvider) -> None:
    """Version, artwork, description, genre and tags end up on the track."""
    provider._soundcloud.get_user_details.return_value = _artist_obj()

    track = await provider._parse_track(
        _track_obj(
            artwork_url="https://i1.sndcdn.com/artworks-abc-large.jpg",
            description="Track notes",
            genre="House",
            tag_list="house dance",
        )
    )

    assert track.name == "Some Track"
    assert track.version == "Radio Edit"
    assert track.duration == 235
    assert track.metadata.images is not None
    assert track.metadata.images[0].path.endswith("artworks-abc-t500x500.jpg")
    assert track.metadata.description == "Track notes"
    assert track.metadata.genres == {"House"}
    assert track.metadata.style == "house dance"
    assert [artist.item_id for artist in track.artists] == ["5"]


async def test_parse_track_position(provider: SoundcloudMusicProvider) -> None:
    """The playlist position is stored on the track."""
    provider._soundcloud.get_user_details.return_value = _artist_obj()

    track = await provider._parse_track(_track_obj(), 4)

    assert track.position == 4
