"""Tests for moving a track inside an Open Subsonic playlist."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest
from libopensonic.errors import DataNotFoundError, SonicError
from music_assistant_models.enums import ProviderFeature
from music_assistant_models.errors import (
    InvalidDataError,
    MediaNotFoundError,
    ProviderPermissionDenied,
)

from music_assistant.providers.opensubsonic import SUPPORTED_FEATURES
from music_assistant.providers.opensubsonic.sonic_provider import OpenSonicProvider

SONG_IDS = ["s1", "s2", "s3", "s4"]


def _connect(provider: OpenSonicProvider, song_ids: list[str]) -> Mock:
    """
    Give the provider a mocked connection whose playlist holds the given songs.

    :param provider: The provider under test.
    :param song_ids: The song ids of the playlist, in playlist order.
    """
    playlist = Mock()
    playlist.entry = [Mock(id=song_id) for song_id in song_ids] or None
    conn = Mock()
    conn.get_playlist = AsyncMock(return_value=playlist)
    conn.create_playlist = AsyncMock(return_value=True)
    provider.conn = conn
    return conn


def test_opensubsonic_advertises_the_move_feature() -> None:
    """The controller only asks providers that advertise the feature."""
    assert ProviderFeature.PLAYLIST_TRACKS_MOVE in SUPPORTED_FEATURES


@pytest.mark.parametrize(
    ("positions", "to_position", "expected"),
    [
        pytest.param([3, 4], 1, ["s3", "s4", "s1", "s2"], id="block"),
        pytest.param([2], None, ["s1", "s3", "s4", "s2"], id="to-end"),
    ],
)
async def test_move_writes_the_whole_new_order_back_once(
    provider: OpenSonicProvider,
    positions: list[int],
    to_position: int | None,
    expected: list[str],
) -> None:
    """A move is one write, so a failure cannot leave the playlist half moved."""
    conn = _connect(provider, SONG_IDS)

    await provider.move_playlist_tracks("pl-1", positions, to_position)

    conn.get_playlist.assert_awaited_once_with("pl-1")
    conn.create_playlist.assert_awaited_once_with(playlist_id="pl-1", song_ids=expected)


async def test_single_move_keeps_duplicate_songs_apart(provider: OpenSonicProvider) -> None:
    """With the same song listed twice, only the entry at from_position moves."""
    conn = _connect(provider, ["s1", "s2", "s1", "s3"])

    await provider.move_playlist_track("pl-1", 1, 4)

    conn.create_playlist.assert_awaited_once_with(
        playlist_id="pl-1", song_ids=["s2", "s1", "s3", "s1"]
    )


@pytest.mark.parametrize("song_ids", [SONG_IDS, []], ids=["outside", "empty-playlist"])
async def test_move_that_cannot_apply_is_refused(
    provider: OpenSonicProvider, song_ids: list[str]
) -> None:
    """An invalid move raises and nothing is written."""
    conn = _connect(provider, song_ids)

    with pytest.raises(InvalidDataError):
        await provider.move_playlist_track("pl-1", 5, 1)

    conn.create_playlist.assert_not_awaited()


async def test_move_in_a_missing_playlist_raises_media_not_found(
    provider: OpenSonicProvider,
) -> None:
    """A playlist the server does not know is reported as not found."""
    conn = _connect(provider, SONG_IDS)
    conn.get_playlist.side_effect = DataNotFoundError("not found")

    with pytest.raises(MediaNotFoundError):
        await provider.move_playlist_track("pl-1", 1, 2)

    conn.create_playlist.assert_not_awaited()


async def test_move_refused_by_the_server_raises_permission_denied(
    provider: OpenSonicProvider,
) -> None:
    """A server that refuses the write, for example for a playlist of another user, says so."""
    conn = _connect(provider, SONG_IDS)
    conn.create_playlist.side_effect = SonicError("not allowed")

    with pytest.raises(ProviderPermissionDenied):
        await provider.move_playlist_track("pl-1", 1, 2)
