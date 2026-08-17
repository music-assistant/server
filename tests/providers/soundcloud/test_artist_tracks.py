"""Tests for Soundcloud artist top tracks and similar tracks."""

from __future__ import annotations

from typing import Any

from aiohttp import ClientError

from music_assistant.providers.soundcloud import SoundcloudMusicProvider


def _track_obj(track_id: int, title: str = "Some Track") -> dict[str, Any]:
    """Build a playable Soundcloud API track object."""
    return {
        "id": track_id,
        "title": title,
        "duration": 235818,
        "permalink_url": f"https://soundcloud.com/artist/{track_id}",
        "user": {"id": 1, "username": "Some Artist", "permalink": "some-artist"},
    }


def _details_for(*track_ids: int) -> Any:
    """Return a get_track_details stub answering with a track per requested id."""

    async def _get_track_details(track_id: int) -> list[dict[str, Any]]:
        assert track_id in track_ids
        return [_track_obj(track_id)]

    return _get_track_details


def test_extract_collection_prefers_collection_key(provider: SoundcloudMusicProvider) -> None:
    """The collection key is used when present."""
    assert provider._extract_collection({"collection": [{"id": 1}]}) == [{"id": 1}]


def test_extract_collection_falls_back_to_items(provider: SoundcloudMusicProvider) -> None:
    """Some endpoints return items instead of collection."""
    assert provider._extract_collection({"items": [{"id": 2}]}) == [{"id": 2}]


def test_extract_collection_without_usable_data(provider: SoundcloudMusicProvider) -> None:
    """An empty or missing response yields no collection."""
    assert provider._extract_collection(None) is None
    assert provider._extract_collection({}) is None


async def test_get_artist(provider: SoundcloudMusicProvider) -> None:
    """An artist is fetched and parsed by its provider id."""
    provider._soundcloud.get_user_details.return_value = {
        "id": 5,
        "username": "Some Artist",
        "permalink": "some-artist",
    }

    get_artist: Any = SoundcloudMusicProvider.get_artist.__wrapped__  # type: ignore[attr-defined]
    artist = await get_artist(provider, "5")

    provider._soundcloud.get_user_details.assert_awaited_once_with("5")
    assert artist.item_id == "5"
    assert artist.name == "Some Artist"


async def test_artist_toptracks_from_collection(provider: SoundcloudMusicProvider) -> None:
    """Top tracks are parsed from the user's tracks response."""
    provider._soundcloud.get_tracks_from_user.return_value = {"collection": [{"id": 1}, {"id": 2}]}
    provider._soundcloud.get_track_details = _details_for(1, 2)

    get_artist_toptracks: Any = SoundcloudMusicProvider.get_artist_toptracks.__wrapped__  # type: ignore[attr-defined]
    tracks = await get_artist_toptracks(provider, "5")

    provider._soundcloud.get_tracks_from_user.assert_awaited_once_with("5", 100)
    assert [track.item_id for track in tracks] == ["1", "2"]


async def test_artist_toptracks_falls_back_to_popular(provider: SoundcloudMusicProvider) -> None:
    """When the user has no tracks listed, popular tracks are requested instead."""
    provider._soundcloud.get_tracks_from_user.return_value = {"collection": []}
    provider._soundcloud.get_popular_tracks_user.return_value = {"items": [{"id": 3}]}
    provider._soundcloud.get_track_details = _details_for(3)

    get_artist_toptracks: Any = SoundcloudMusicProvider.get_artist_toptracks.__wrapped__  # type: ignore[attr-defined]
    tracks = await get_artist_toptracks(provider, "5")

    provider._soundcloud.get_popular_tracks_user.assert_awaited_once_with("5", 100)
    assert [track.item_id for track in tracks] == ["3"]


async def test_artist_toptracks_survives_popular_request_failure(
    provider: SoundcloudMusicProvider,
) -> None:
    """A failing popular tracks request results in an empty list, not an error."""
    provider._soundcloud.get_tracks_from_user.return_value = {"collection": []}
    provider._soundcloud.get_popular_tracks_user.side_effect = ClientError("boom")

    get_artist_toptracks: Any = SoundcloudMusicProvider.get_artist_toptracks.__wrapped__  # type: ignore[attr-defined]
    tracks = await get_artist_toptracks(provider, "5")

    assert tracks == []


async def test_artist_toptracks_skips_unparsable_track(provider: SoundcloudMusicProvider) -> None:
    """An unparsable track does not abort the whole listing."""
    provider._soundcloud.get_tracks_from_user.return_value = {"collection": [{"id": 1}, {"id": 2}]}

    async def _get_track_details(track_id: int) -> list[dict[str, Any]]:
        if track_id == 1:
            return [{"id": 1}]  # missing title/duration
        return [_track_obj(track_id)]

    provider._soundcloud.get_track_details = _get_track_details

    get_artist_toptracks: Any = SoundcloudMusicProvider.get_artist_toptracks.__wrapped__  # type: ignore[attr-defined]
    tracks = await get_artist_toptracks(provider, "5")

    assert [track.item_id for track in tracks] == ["2"]


async def test_similar_tracks(provider: SoundcloudMusicProvider) -> None:
    """Similar tracks are parsed from the recommended response."""
    provider._soundcloud.get_recommended.return_value = {"collection": [{"id": 7}]}
    provider._soundcloud.get_track_details = _details_for(7)

    get_similar_tracks: Any = SoundcloudMusicProvider.get_similar_tracks.__wrapped__  # type: ignore[attr-defined]
    tracks = await get_similar_tracks(provider, "1", 25)

    provider._soundcloud.get_recommended.assert_awaited_once_with("1", 25)
    assert [track.item_id for track in tracks] == ["7"]


async def test_similar_tracks_skips_unparsable_track(provider: SoundcloudMusicProvider) -> None:
    """An unparsable recommendation does not abort the whole listing."""
    provider._soundcloud.get_recommended.return_value = {"collection": [{"id": 7}, {"id": 8}]}

    async def _get_track_details(track_id: int) -> list[dict[str, Any]]:
        if track_id == 7:
            return [{"id": 7}]  # missing title/duration
        return [_track_obj(track_id)]

    provider._soundcloud.get_track_details = _get_track_details

    get_similar_tracks: Any = SoundcloudMusicProvider.get_similar_tracks.__wrapped__  # type: ignore[attr-defined]
    tracks = await get_similar_tracks(provider, "1", 25)

    assert [track.item_id for track in tracks] == ["8"]


async def test_similar_tracks_without_results(provider: SoundcloudMusicProvider) -> None:
    """No recommendations yields an empty list."""
    provider._soundcloud.get_recommended.return_value = {}

    get_similar_tracks: Any = SoundcloudMusicProvider.get_similar_tracks.__wrapped__  # type: ignore[attr-defined]
    tracks = await get_similar_tracks(provider, "1", 25)

    assert tracks == []
