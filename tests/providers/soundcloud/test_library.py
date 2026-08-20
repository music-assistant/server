"""Tests for Soundcloud library listings and recommendations."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from music_assistant_models.enums import MediaType

from music_assistant.models.music_provider import SyncRunState
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


async def test_library_artists_skip_unparsable(
    provider: SoundcloudMusicProvider, sync_run: SyncRunState
) -> None:
    """An artist without a valid id is skipped, the rest is returned."""
    provider._soundcloud.get_following.return_value = {
        "collection": [_artist_obj(None), _artist_obj(7, "Real Artist")]
    }

    artists = [artist async for artist in provider.get_library_artists()]

    provider._soundcloud.get_following.assert_awaited_once_with("1")
    assert [artist.item_id for artist in artists] == ["7"]
    assert artists[0].name == "Real Artist"
    # the skipped artist has no id to report, so the run can not be a basis for deletions
    assert sync_run.skipped_item_ids == {}
    assert sync_run.incomplete is True


async def test_skipped_artist_is_reported_with_its_item_id(
    provider: SoundcloudMusicProvider, sync_run: SyncRunState
) -> None:
    """
    A skipped artist is reported under the id its provider mapping carries.

    That id is what the deletion pass resolves the item by, so an artist Soundcloud still
    follows is left alone instead of being removed from the library.
    """
    # an artist that carries an id, but no username to build the item from
    provider._soundcloud.get_following.return_value = {
        "collection": [{"id": 7, "permalink": "some-artist"}]
    }

    assert [artist async for artist in provider.get_library_artists()] == []

    assert sync_run.skipped_item_ids == {MediaType.ARTIST: {"7"}}
    assert sync_run.incomplete is False


async def test_library_playlists(provider: SoundcloudMusicProvider, sync_run: SyncRunState) -> None:
    """Playlists are resolved to their full object before being parsed."""

    async def _account_playlists() -> AsyncGenerator[dict[str, Any]]:
        yield {"playlist": {"id": 10}}
        yield {"not_a_playlist": {}}  # unexpected shape, must be skipped

    provider._soundcloud.get_account_playlists = _account_playlists
    provider._soundcloud.get_playlist_details.return_value = {"id": 10, "title": "Summer Mix"}

    playlists = [playlist async for playlist in provider.get_library_playlists()]

    assert [playlist.item_id for playlist in playlists] == ["10"]
    assert playlists[0].name == "Summer Mix"
    # the unexpected entry holds no id at all, so the deletion pass is held back
    assert sync_run.incomplete is True


async def test_library_playlists_skip_failing_details(
    provider: SoundcloudMusicProvider, sync_run: SyncRunState
) -> None:
    """A playlist whose details cannot be parsed is skipped and reported under its id."""

    async def _account_playlists() -> AsyncGenerator[dict[str, Any]]:
        yield {"playlist": {"id": 10}}

    provider._soundcloud.get_account_playlists = _account_playlists
    provider._soundcloud.get_playlist_details.return_value = {"id": 10}  # no title

    playlists = [playlist async for playlist in provider.get_library_playlists()]

    assert playlists == []
    assert sync_run.skipped_item_ids == {MediaType.PLAYLIST: {"10"}}
    assert sync_run.incomplete is False


async def test_library_tracks_skip_unparsable(
    provider: SoundcloudMusicProvider, sync_run: SyncRunState
) -> None:
    """A track that cannot be parsed is skipped without failing the sync."""

    async def _liked_tracks(_user_id: str) -> AsyncGenerator[dict[str, Any]]:
        yield {"id": 1}  # missing title/duration
        yield _track_obj(2)

    provider._soundcloud.get_track_details_liked = _liked_tracks

    tracks = [track async for track in provider.get_library_tracks()]

    assert [track.item_id for track in tracks] == ["2"]
    assert sync_run.skipped_item_ids == {MediaType.TRACK: {"1"}}
    assert sync_run.incomplete is False


async def test_recommendation_payload(provider: SoundcloudMusicProvider) -> None:
    """The payload holds the mixed selections, keeping only system playlists."""
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

    folders = await provider._fetch_recommendation_payload()

    assert [folder.name for folder in folders] == ["Made for you"]
    assert [item.item_id for item in folders[0].items] == ["10"]
    # the feed row is filled on demand, so building the payload must not fetch it
    provider._soundcloud.get_subscribe_feed.assert_not_awaited()


async def test_recommendation_payload_with_empty_selection(
    provider: SoundcloudMusicProvider,
) -> None:
    """A mixed selection without items yields an empty folder instead of failing."""
    provider._soundcloud.get_mixed_selection.return_value = {
        "collection": [{"id": "mixed-1", "title": "Made for you"}]
    }

    folders = await provider._fetch_recommendation_payload()

    assert [folder.name for folder in folders] == ["Made for you"]
    assert folders[0].items == []


async def test_subscribed_feed_tracks(provider: SoundcloudMusicProvider) -> None:
    """Reposts count as tracks in the feed, other item types are ignored."""
    provider._soundcloud.get_subscribe_feed.return_value = {
        "collection": [
            {"type": "track", "track": _track_obj(1)},
            {"type": "track-repost", "track": _track_obj(2)},
            {"type": "playlist", "playlist": {"id": 99}},
        ]
    }

    feed_tracks: Any = SoundcloudMusicProvider._get_subscribed_feed_tracks.__wrapped__  # type: ignore[attr-defined]
    tracks = await feed_tracks(provider)

    assert [track.item_id for track in tracks] == ["1", "2"]


async def test_subscribed_feed_skips_unparsable_track(provider: SoundcloudMusicProvider) -> None:
    """An unplayable track in the feed does not discard the rest of the row."""
    provider._soundcloud.get_subscribe_feed.return_value = {
        "collection": [
            {"type": "track", "track": {"id": 1}},  # missing title/duration
            {"type": "track", "track": _track_obj(2)},
        ]
    }

    feed_tracks: Any = SoundcloudMusicProvider._get_subscribed_feed_tracks.__wrapped__  # type: ignore[attr-defined]
    tracks = await feed_tracks(provider)

    assert [track.item_id for track in tracks] == ["2"]


async def test_subscribed_feed_without_results(provider: SoundcloudMusicProvider) -> None:
    """An empty subscribed feed yields no tracks."""
    provider._soundcloud.get_subscribe_feed.return_value = {}

    feed_tracks: Any = SoundcloudMusicProvider._get_subscribed_feed_tracks.__wrapped__  # type: ignore[attr-defined]
    tracks = await feed_tracks(provider)

    assert tracks == []
