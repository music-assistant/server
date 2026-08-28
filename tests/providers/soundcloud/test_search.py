"""Tests for Soundcloud search."""

from __future__ import annotations

from typing import Any

from music_assistant_models.enums import MediaType

from music_assistant.providers.soundcloud import SoundcloudMusicProvider

ALL_MEDIA_TYPES = [MediaType.ARTIST, MediaType.TRACK, MediaType.PLAYLIST]


def _track_hit(
    track_id: int, duration: int = 235818, full_duration: int = 235771
) -> dict[str, Any]:
    """Build a track search hit."""
    return {
        "kind": "track",
        "id": track_id,
        "title": "Some Track",
        "duration": duration,
        "full_duration": full_duration,
        "permalink_url": f"https://soundcloud.com/artist/{track_id}",
        "user": {"id": 1, "username": "Some Artist", "permalink": "some-artist"},
    }


async def _search(
    provider: SoundcloudMusicProvider,
    collection: list[dict[str, Any]],
    media_types: list[MediaType],
) -> Any:
    """Run a search against the given API collection."""
    provider._soundcloud.search.return_value = {"collection": collection}
    search: Any = SoundcloudMusicProvider.search.__wrapped__  # type: ignore[attr-defined]
    return await search(provider, "query", media_types, 10)


async def test_search_returns_all_media_types(provider: SoundcloudMusicProvider) -> None:
    """Artists, tracks and playlists are all parsed from one search response."""
    result = await _search(
        provider,
        [
            {"kind": "user", "id": 5, "username": "Some Artist", "permalink": "some-artist"},
            _track_hit(1),
            {"kind": "playlist", "id": 10, "title": "Summer Mix"},
        ],
        ALL_MEDIA_TYPES,
    )

    assert [artist.item_id for artist in result.artists] == ["5"]
    assert [track.item_id for track in result.tracks] == ["1"]
    assert [playlist.item_id for playlist in result.playlists] == ["10"]


async def test_search_skips_preview_tracks(provider: SoundcloudMusicProvider) -> None:
    """A track that only offers a short preview is not returned as a full track."""
    result = await _search(
        provider, [_track_hit(1, duration=30000, full_duration=235771)], [MediaType.TRACK]
    )

    assert result.tracks == []


async def test_search_without_supported_media_types(provider: SoundcloudMusicProvider) -> None:
    """A search for unsupported media types does not hit the API."""
    search: Any = SoundcloudMusicProvider.search.__wrapped__  # type: ignore[attr-defined]
    result = await search(provider, "query", [MediaType.AUDIOBOOK], 10)

    assert result.tracks == []
    provider._soundcloud.search.assert_not_awaited()


async def test_search_ignores_unwanted_media_types(provider: SoundcloudMusicProvider) -> None:
    """Results outside the requested media types are left out."""
    result = await _search(
        provider,
        [
            {"kind": "user", "id": 5, "username": "Some Artist", "permalink": "some-artist"},
            _track_hit(1),
        ],
        [MediaType.TRACK],
    )

    assert result.artists == []
    assert [track.item_id for track in result.tracks] == ["1"]
