"""Tests for Soundcloud playlist parsing and playlist track listings."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from music_assistant_models.errors import MediaNotFoundError

from music_assistant.providers.soundcloud import TRACK_IDS_BATCH_SIZE, SoundcloudMusicProvider


def _track_obj(track_id: int, title: str) -> dict[str, Any]:
    """Build a playable Soundcloud API track object."""
    return {
        "id": track_id,
        "title": title,
        "duration": 235818,
        "permalink_url": f"https://soundcloud.com/artist/{track_id}",
        "user": {"id": 1, "username": "Some Artist", "permalink": "some-artist"},
    }


def _playlist_obj(**extra: Any) -> dict[str, Any]:
    """Build a Soundcloud API playlist object."""
    return {"id": 10, "title": "Summer Mix", **extra}


async def test_parse_playlist_basics(provider: SoundcloudMusicProvider) -> None:
    """A playlist is parsed with its provider mapping and is not editable."""
    playlist = await provider._parse_playlist(_playlist_obj())

    assert playlist.item_id == "10"
    assert playlist.name == "Summer Mix"
    assert playlist.provider == "soundcloud"
    assert playlist.is_editable is False
    mapping = next(iter(playlist.provider_mappings))
    assert mapping.item_id == "10"
    assert mapping.provider_instance == provider.instance_id


async def test_get_playlist_unparsable_is_not_found(provider: SoundcloudMusicProvider) -> None:
    """A playlist that cannot be parsed is reported as not found."""
    get_playlist: Any = SoundcloudMusicProvider.get_playlist.__wrapped__  # type: ignore[attr-defined]
    provider._soundcloud.get_playlist_details.return_value = {"title": "No id"}

    with pytest.raises(MediaNotFoundError):
        await get_playlist(provider, "10")


async def test_parse_playlist_strips_related_tracks_prefix(
    provider: SoundcloudMusicProvider,
) -> None:
    """The 'Related tracks: ' prefix Soundcloud adds is removed from the name."""
    playlist = await provider._parse_playlist(_playlist_obj(title="Related tracks: Summer Mix"))

    assert playlist.name == "Summer Mix"


async def test_parse_playlist_metadata(provider: SoundcloudMusicProvider) -> None:
    """Description, artwork, genre and tags are carried over into the metadata."""
    playlist = await provider._parse_playlist(
        _playlist_obj(
            description="Sunny tunes",
            artwork_url="https://i1.sndcdn.com/artworks-abc-large.jpg",
            genre="House",
            tag_list="house dance",
        )
    )

    assert playlist.metadata.description == "Sunny tunes"
    assert playlist.metadata.genres == {"House"}
    assert playlist.metadata.style == "house dance"
    # artwork is upgraded to the high resolution variant
    assert playlist.metadata.images is not None
    assert playlist.metadata.images[0].path.endswith("artworks-abc-t500x500.jpg")
    assert playlist.metadata.images[0].remotely_accessible is True


async def test_get_playlist(provider: SoundcloudMusicProvider) -> None:
    """A playlist is fetched and parsed by its provider id."""
    provider._soundcloud.get_playlist_details.return_value = _playlist_obj()

    get_playlist: Any = SoundcloudMusicProvider.get_playlist.__wrapped__  # type: ignore[attr-defined]
    playlist = await get_playlist(provider, "10")

    provider._soundcloud.get_playlist_details.assert_awaited_once_with("10")
    assert playlist.item_id == "10"
    assert playlist.name == "Summer Mix"


async def test_playlist_tracks_returns_parsed_tracks(provider: SoundcloudMusicProvider) -> None:
    """Tracks embedded in the playlist response are parsed in order."""
    provider._soundcloud.get_playlist_details.return_value = _playlist_obj(
        tracks=[_track_obj(1, "First"), _track_obj(2, "Second")]
    )

    get_playlist_tracks: Any = SoundcloudMusicProvider.get_playlist_tracks.__wrapped__  # type: ignore[attr-defined]
    tracks = await get_playlist_tracks(provider, "10")

    assert [track.item_id for track in tracks] == ["1", "2"]
    assert [track.position for track in tracks] == [1, 2]


async def test_playlist_tracks_fetches_details_for_partial_entries(
    provider: SoundcloudMusicProvider,
) -> None:
    """Entries without a title are resolved through a track details call."""
    provider._soundcloud.get_playlist_details.return_value = _playlist_obj(
        tracks=[{"id": 5, "kind": "track", "monetization_model": "NOT_APPLICABLE"}]
    )
    provider._soundcloud.get_track_details.return_value = [_track_obj(5, "Resolved")]

    get_playlist_tracks: Any = SoundcloudMusicProvider.get_playlist_tracks.__wrapped__  # type: ignore[attr-defined]
    tracks = await get_playlist_tracks(provider, "10")

    provider._soundcloud.get_track_details.assert_awaited_once_with("5")
    assert [track.name for track in tracks] == ["Resolved"]


async def test_playlist_tracks_resolves_stubs_in_batches(
    provider: SoundcloudMusicProvider,
) -> None:
    """Stub entries are resolved in batched requests, keeping the playlist order."""
    stub_ids = list(range(100, 100 + TRACK_IDS_BATCH_SIZE * 2 + 1))
    provider._soundcloud.get_playlist_details.return_value = _playlist_obj(
        tracks=[_track_obj(1, "First"), *({"id": track_id} for track_id in stub_ids)]
    )

    async def _get_track_details(track_ids: str) -> list[dict[str, Any]]:
        # Soundcloud returns the tracks in its own order and leaves out unavailable ones
        ids = [int(track_id) for track_id in track_ids.split(",") if track_id != "150"]
        return [_track_obj(track_id, f"Track {track_id}") for track_id in reversed(ids)]

    provider._soundcloud.get_track_details = AsyncMock(side_effect=_get_track_details)

    get_playlist_tracks: Any = SoundcloudMusicProvider.get_playlist_tracks.__wrapped__  # type: ignore[attr-defined]
    tracks = await get_playlist_tracks(provider, "10")

    requested = [
        call.args[0].split(",") for call in provider._soundcloud.get_track_details.await_args_list
    ]
    assert [len(batch) for batch in requested] == [TRACK_IDS_BATCH_SIZE, TRACK_IDS_BATCH_SIZE, 1]
    assert [track_id for batch in requested for track_id in batch] == [str(i) for i in stub_ids]
    expected_ids = [1, *(track_id for track_id in stub_ids if track_id != 150)]
    assert [track.item_id for track in tracks] == [str(i) for i in expected_ids]
    # positions follow the playlist, so the unavailable track leaves a gap
    assert [track.position for track in tracks[50:52]] == [51, 53]


async def test_playlist_tracks_without_stubs_makes_no_track_requests(
    provider: SoundcloudMusicProvider,
) -> None:
    """A playlist that holds full track objects needs no track or user requests."""
    provider._soundcloud.get_playlist_details.return_value = _playlist_obj(
        tracks=[_track_obj(1, "First"), _track_obj(2, "Second")]
    )

    get_playlist_tracks: Any = SoundcloudMusicProvider.get_playlist_tracks.__wrapped__  # type: ignore[attr-defined]
    await get_playlist_tracks(provider, "10")

    provider._soundcloud.get_track_details.assert_not_awaited()
    provider._soundcloud.get_user_details.assert_not_awaited()


async def test_playlist_tracks_skips_unparsable_entry(provider: SoundcloudMusicProvider) -> None:
    """A broken entry is skipped without dropping the rest of the playlist."""
    provider._soundcloud.get_playlist_details.return_value = _playlist_obj(
        tracks=[{"title": "Broken"}, _track_obj(2, "Second")]
    )

    get_playlist_tracks: Any = SoundcloudMusicProvider.get_playlist_tracks.__wrapped__  # type: ignore[attr-defined]
    tracks = await get_playlist_tracks(provider, "10")

    assert [track.item_id for track in tracks] == ["2"]


async def test_playlist_tracks_without_tracks_key(provider: SoundcloudMusicProvider) -> None:
    """A playlist response without tracks yields an empty list."""
    provider._soundcloud.get_playlist_details.return_value = _playlist_obj()

    get_playlist_tracks: Any = SoundcloudMusicProvider.get_playlist_tracks.__wrapped__  # type: ignore[attr-defined]
    tracks = await get_playlist_tracks(provider, "10")

    assert tracks == []


async def test_playlist_tracks_second_page_is_empty(provider: SoundcloudMusicProvider) -> None:
    """Soundcloud has no paging for playlist tracks, so later pages are empty."""
    get_playlist_tracks: Any = SoundcloudMusicProvider.get_playlist_tracks.__wrapped__  # type: ignore[attr-defined]
    tracks = await get_playlist_tracks(provider, "10", 1)

    assert tracks == []
    provider._soundcloud.get_playlist_details.assert_not_awaited()


async def test_playlist_tracks_uses_system_playlist_endpoint(
    provider: SoundcloudMusicProvider,
) -> None:
    """System playlists are fetched through their own endpoint."""
    playlist_id = "soundcloud:system-playlists:artist-stations:1"
    provider._soundcloud.get_system_playlist_details.return_value = _playlist_obj(
        tracks=[_track_obj(1, "First")]
    )

    get_playlist_tracks: Any = SoundcloudMusicProvider.get_playlist_tracks.__wrapped__  # type: ignore[attr-defined]
    tracks = await get_playlist_tracks(provider, playlist_id)

    provider._soundcloud.get_system_playlist_details.assert_awaited_once_with(playlist_id)
    provider._soundcloud.get_playlist_details.assert_not_awaited()
    assert [track.item_id for track in tracks] == ["1"]
