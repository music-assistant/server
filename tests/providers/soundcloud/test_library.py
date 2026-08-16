"""Tests for Soundcloud library listings and recommendations."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from music_assistant.providers.soundcloud import SoundcloudMusicProvider


def _artist_obj(artist_id: int | None, username: str = "Some Artist") -> dict[str, Any]:
    """Build a Soundcloud API user object."""
    return {"id": artist_id, "username": username, "permalink": "some-artist"}


def _track_obj(track_id: int, title: str = "Some Track") -> dict[str, Any]:
    """Build a playable Soundcloud API track object."""
    return {
        "id": track_id,
        "title": title,
        "duration": 235818,
        "permalink_url": f"https://soundcloud.com/artist/{track_id}",
        "user": {"id": 1, "username": "Some Artist", "permalink": "some-artist"},
    }


async def test_library_artists_skip_unparsable(provider: SoundcloudMusicProvider) -> None:
    """An artist without a valid id is skipped, the rest is returned."""
    provider._soundcloud.get_following.return_value = {
        "collection": [_artist_obj(None), _artist_obj(7, "Real Artist")]
    }

    artists = [artist async for artist in provider.get_library_artists()]

    provider._soundcloud.get_following.assert_awaited_once_with("1")
    assert [artist.item_id for artist in artists] == ["7"]
    assert artists[0].name == "Real Artist"


async def test_library_playlists(provider: SoundcloudMusicProvider) -> None:
    """Playlists are resolved to their full object before being parsed."""

    async def _account_playlists() -> AsyncGenerator[dict[str, Any]]:
        yield {"playlist": {"id": 10}}
        yield {"not_a_playlist": {}}  # unexpected shape, must be skipped

    provider._soundcloud.get_account_playlists = _account_playlists
    provider._soundcloud.get_playlist_details.return_value = {"id": 10, "title": "Summer Mix"}

    playlists = [playlist async for playlist in provider.get_library_playlists()]

    assert [playlist.item_id for playlist in playlists] == ["10"]
    assert playlists[0].name == "Summer Mix"


async def test_library_playlists_skip_failing_details(provider: SoundcloudMusicProvider) -> None:
    """A playlist whose details cannot be parsed is skipped."""

    async def _account_playlists() -> AsyncGenerator[dict[str, Any]]:
        yield {"playlist": {"id": 10}}

    provider._soundcloud.get_account_playlists = _account_playlists
    provider._soundcloud.get_playlist_details.return_value = {"id": 10}  # no title

    playlists = [playlist async for playlist in provider.get_library_playlists()]

    assert playlists == []


async def test_library_tracks_skip_unparsable(provider: SoundcloudMusicProvider) -> None:
    """A track that cannot be parsed is skipped without failing the sync."""

    async def _liked_tracks(_user_id: str) -> AsyncGenerator[dict[str, Any]]:
        yield {"id": 1}  # missing title/duration
        yield _track_obj(2)

    provider._soundcloud.get_track_details_liked = _liked_tracks

    tracks = [track async for track in provider.get_library_tracks()]

    assert [track.item_id for track in tracks] == ["2"]


async def test_recommendations(provider: SoundcloudMusicProvider) -> None:
    """Mixed selections and the subscribed feed are both offered as folders."""
    provider._soundcloud.get_mixed_selection.return_value = {
        "collection": [
            {
                "id": "mixed-1",
                "title": "Made for you",
                "items": {
                    "collection": [
                        {"id": 10, "title": "Summer Mix", "kind": "system-playlist"},
                        {"id": 11, "title": "Not a playlist", "kind": "track"},
                    ]
                },
            }
        ]
    }
    provider._soundcloud.get_subscribe_feed.return_value = {
        "collection": [
            {"type": "track", "track": _track_obj(1)},
            {"type": "track-repost", "track": _track_obj(2)},
            {"type": "playlist", "playlist": {"id": 99}},
        ]
    }

    recommendations: Any = SoundcloudMusicProvider.recommendations.__wrapped__  # type: ignore[attr-defined]
    folders = await recommendations(provider)

    assert [folder.name for folder in folders] == ["Made for you", "SoundCloud Feed"]
    # only the system playlist is kept from the mixed selection
    assert [item.item_id for item in folders[0].items] == ["10"]
    # reposts count as tracks, other feed types are ignored
    assert [item.item_id for item in folders[1].items] == ["1", "2"]


async def test_recommendations_with_empty_selection(provider: SoundcloudMusicProvider) -> None:
    """A mixed selection without items yields an empty folder instead of failing."""
    provider._soundcloud.get_mixed_selection.return_value = {
        "collection": [{"id": "mixed-1", "title": "Made for you"}]
    }
    provider._soundcloud.get_subscribe_feed.return_value = {}

    recommendations: Any = SoundcloudMusicProvider.recommendations.__wrapped__  # type: ignore[attr-defined]
    folders = await recommendations(provider)

    assert [folder.name for folder in folders] == ["Made for you"]
    assert folders[0].items == []


async def test_recommendations_skip_unparsable_feed_track(
    provider: SoundcloudMusicProvider,
) -> None:
    """An unparsable track in the feed does not discard the rest of the folder."""
    provider._soundcloud.get_mixed_selection.return_value = {}
    provider._soundcloud.get_subscribe_feed.return_value = {
        "collection": [
            {"type": "track", "track": {"id": 1}},  # missing title/duration
            {"type": "track", "track": _track_obj(2)},
        ]
    }

    recommendations: Any = SoundcloudMusicProvider.recommendations.__wrapped__  # type: ignore[attr-defined]
    folders = await recommendations(provider)

    assert [item.item_id for item in folders[0].items] == ["2"]


async def test_recommendations_without_feed(provider: SoundcloudMusicProvider) -> None:
    """An empty subscribed feed simply yields no feed folder."""
    provider._soundcloud.get_mixed_selection.return_value = {}
    provider._soundcloud.get_subscribe_feed.return_value = {}

    recommendations: Any = SoundcloudMusicProvider.recommendations.__wrapped__  # type: ignore[attr-defined]
    folders = await recommendations(provider)

    assert folders == []
