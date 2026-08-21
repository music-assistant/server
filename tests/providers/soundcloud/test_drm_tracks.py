"""Tests for skipping DRM protected Soundcloud tracks."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import InvalidDataError, MediaNotFoundError

from music_assistant.models.music_provider import SyncRunState
from music_assistant.providers.soundcloud import (
    DrmProtectedTrackError,
    SoundcloudMusicProvider,
    _is_drm_protected,
)

# transcodings as returned by the Soundcloud API for DRM protected content: the encrypted
# HLS variants are the only playable ones, while the mp3 entries are advertised but 404
DRM_TRANSCODINGS = [
    {
        "preset": "aac_160k",
        "url": "https://api/aac_cbc",
        "format": {"protocol": "cbc-encrypted-hls"},
    },
    {
        "preset": "aac_160k",
        "url": "https://api/aac_ctr",
        "format": {"protocol": "ctr-encrypted-hls"},
    },
    {"preset": "mp3_1_0", "url": "https://api/mp3_hls", "format": {"protocol": "hls"}},
    {"preset": "mp3_1_0", "url": "https://api/mp3_prog", "format": {"protocol": "progressive"}},
]
PLAIN_TRANSCODINGS = [
    {"preset": "mp3_0_1", "url": "https://api/mp3_hls", "format": {"protocol": "hls"}},
    {"preset": "mp3_0_1", "url": "https://api/mp3_prog", "format": {"protocol": "progressive"}},
]


def _track_obj(track_id: int, title: str, transcodings: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a Soundcloud API track object with the given transcodings."""
    return {
        "id": track_id,
        "title": title,
        "duration": 235818,
        "full_duration": 235771,
        "kind": "track",
        "permalink_url": f"https://soundcloud.com/artist/{track_id}",
        "policy": "MONETIZE",
        "monetization_model": "AD_SUPPORTED",
        "track_authorization": "auth-token",
        "user": {"id": 1, "username": "Some Artist", "permalink": "some-artist"},
        "media": {"transcodings": transcodings},
    }


def test_is_drm_protected_detects_encrypted_transcodings() -> None:
    """Encrypted HLS transcodings mark a track as DRM protected."""
    assert _is_drm_protected(_track_obj(1, "Danceteria", DRM_TRANSCODINGS)) is True


def test_is_drm_protected_allows_plain_transcodings() -> None:
    """A track with only plain transcodings is not DRM protected."""
    assert _is_drm_protected(_track_obj(2, "Lofi Beat", PLAIN_TRANSCODINGS)) is False


def test_is_drm_protected_allows_partial_track_object() -> None:
    """A partial track object without media details is not treated as DRM protected."""
    assert _is_drm_protected({"id": 3, "kind": "track"}) is False


async def test_parse_track_rejects_drm_track(provider: SoundcloudMusicProvider) -> None:
    """Parsing a DRM protected track raises an error the listing paths already skip."""
    with pytest.raises(DrmProtectedTrackError):
        await provider._parse_track(_track_obj(1, "Danceteria", DRM_TRANSCODINGS))
    assert issubclass(DrmProtectedTrackError, InvalidDataError)


async def test_parse_track_accepts_plain_track(provider: SoundcloudMusicProvider) -> None:
    """Parsing a track without DRM returns a Track."""
    track = await provider._parse_track(_track_obj(2, "Lofi Beat", PLAIN_TRANSCODINGS))
    assert track.item_id == "2"
    assert track.name == "Lofi Beat"


async def test_library_tracks_skip_drm_and_log_count(
    provider: SoundcloudMusicProvider, caplog: pytest.LogCaptureFixture
) -> None:
    """DRM protected tracks are not imported and the number skipped is logged."""

    async def _liked_tracks(_user_id: str) -> AsyncGenerator[dict[str, Any]]:
        yield _track_obj(1, "Danceteria", DRM_TRANSCODINGS)
        yield _track_obj(2, "Lofi Beat", PLAIN_TRANSCODINGS)
        yield _track_obj(3, "The Fate of Ophelia", DRM_TRANSCODINGS)

    provider._soundcloud.get_track_details_liked = _liked_tracks

    with caplog.at_level(logging.INFO):
        tracks = [track async for track in provider.get_library_tracks()]

    assert [track.item_id for track in tracks] == ["2"]
    assert "2" in caplog.text
    assert "DRM" in caplog.text


async def test_library_drm_tracks_are_not_reported_as_sync_failures(
    provider: SoundcloudMusicProvider, sync_run: SyncRunState
) -> None:
    """
    A DRM protected track is left out without counting as a failed sync item.

    Soundcloud never allows those to be imported, so they are a permanent and expected
    category rather than something that went wrong, and the count is logged in one go.
    """

    async def _liked_tracks(_user_id: str) -> AsyncGenerator[dict[str, Any]]:
        yield _track_obj(1, "Danceteria", DRM_TRANSCODINGS)
        yield _track_obj(2, "Lofi Beat", PLAIN_TRANSCODINGS)

    provider._soundcloud.get_track_details_liked = _liked_tracks

    tracks = [track async for track in provider.get_library_tracks()]

    assert [track.item_id for track in tracks] == ["2"]
    assert sync_run.skipped_item_ids == {}
    assert sync_run.failures == 0
    assert not sync_run.incomplete_media_types


async def test_playlist_tracks_skip_drm(provider: SoundcloudMusicProvider) -> None:
    """DRM protected tracks are left out of playlist listings."""
    provider._soundcloud.get_playlist_details.return_value = {
        "id": 10,
        "tracks": [
            _track_obj(1, "Danceteria", DRM_TRANSCODINGS),
            _track_obj(2, "Lofi Beat", PLAIN_TRANSCODINGS),
        ],
    }

    get_playlist_tracks: Any = SoundcloudMusicProvider.get_playlist_tracks.__wrapped__  # type: ignore[attr-defined]
    tracks = await get_playlist_tracks(provider, "10")

    assert [track.item_id for track in tracks] == ["2"]


async def test_search_skips_drm_track_and_keeps_others(
    provider: SoundcloudMusicProvider,
) -> None:
    """A DRM protected search hit is skipped without dropping the other results."""
    provider._soundcloud.search.return_value = {
        "collection": [
            _track_obj(1, "Danceteria", DRM_TRANSCODINGS),
            _track_obj(2, "Lofi Beat", PLAIN_TRANSCODINGS),
        ]
    }

    search: Any = SoundcloudMusicProvider.search.__wrapped__  # type: ignore[attr-defined]
    result = await search(provider, "madonna", [MediaType.TRACK], 10)

    assert [track.item_id for track in result.tracks] == ["2"]


async def test_get_stream_details_reports_drm(provider: SoundcloudMusicProvider) -> None:
    """Streaming an already imported DRM track fails with a message naming the cause."""
    provider._soundcloud.get_track_details.return_value = [
        _track_obj(1, "Danceteria", DRM_TRANSCODINGS)
    ]

    with pytest.raises(MediaNotFoundError, match="DRM"):
        await provider.get_stream_details("1", MediaType.TRACK)


async def test_get_track_raises_media_not_found_when_unparsable(
    provider: SoundcloudMusicProvider,
) -> None:
    """An unparsable track results in MediaNotFoundError instead of an internal error."""
    provider._soundcloud.get_track_details.return_value = [
        _track_obj(1, "Danceteria", DRM_TRANSCODINGS)
    ]

    get_track: Any = SoundcloudMusicProvider.get_track.__wrapped__  # type: ignore[attr-defined]
    with pytest.raises(MediaNotFoundError):
        await get_track(provider, "1")
