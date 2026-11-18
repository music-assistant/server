"""Tests for playlist track positions and profile reuse."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from music_assistant.providers.zvuk_music.api_client import ZvukMusicClient
from music_assistant.providers.zvuk_music.provider import ZvukMusicProvider


def _playlist_provider(track_ids: list[str]) -> Any:
    """
    Create a provider whose playlist holds the given track IDs in order.

    :param track_ids: Playlist track IDs in playlist order.
    """
    provider = Mock(spec=ZvukMusicProvider)
    provider.logger = Mock()
    provider.instance_id = "zvuk_music--test"
    provider.domain = "zvuk_music"
    provider.client = Mock()
    provider.client.user_id = "1"
    playlist = Mock()
    playlist.tracks = [Mock(id=tid) for tid in track_ids]
    provider.client.get_playlist = AsyncMock(return_value=playlist)

    async def get_tracks(ids: list[str]) -> list[Mock]:
        # The API does not guarantee order; return them reversed.
        return [Mock(id=tid) for tid in reversed(ids)]

    provider.client.get_tracks = AsyncMock(side_effect=get_tracks)
    provider.client.update_playlist = AsyncMock(return_value=True)
    provider._get_playlist_track_ids = ZvukMusicProvider._get_playlist_track_ids.__get__(
        provider, ZvukMusicProvider
    )
    return provider


# get_playlist_tracks is wrapped by MA's cache decorator, which needs a running mass.
_get_playlist_tracks = ZvukMusicProvider.get_playlist_tracks.__wrapped__  # type: ignore[attr-defined]


def _fake_parse_track(_provider: Any, track_obj: Any) -> Mock:
    parsed = Mock()
    parsed.item_id = str(track_obj.id)
    parsed.position = None
    return parsed


class TestPlaylistTrackPositions:
    """Playlist tracks carry 1-based positions in playlist order."""

    @pytest.mark.asyncio
    async def test_first_page_positions_follow_playlist_order(self) -> None:
        """Positions start at 1 and follow the playlist, not the API batch order."""
        provider = _playlist_provider(["10", "20", "30"])

        with patch("music_assistant.providers.zvuk_music.provider.parse_track", _fake_parse_track):
            tracks = await _get_playlist_tracks(provider, "pl", page=0)

        assert [(t.item_id, t.position) for t in tracks] == [("10", 1), ("20", 2), ("30", 3)]

    @pytest.mark.asyncio
    async def test_first_page_returns_whole_playlist_in_batches(self) -> None:
        """Page 0 reads the playlist once and returns every track, fetching details in batches."""
        ids = [str(i) for i in range(1, 76)]
        provider = _playlist_provider(ids)

        with patch("music_assistant.providers.zvuk_music.provider.parse_track", _fake_parse_track):
            tracks = await _get_playlist_tracks(provider, "pl", page=0)

        assert [t.item_id for t in tracks] == ids
        assert [t.position for t in tracks] == list(range(1, 76))
        provider.client.get_playlist.assert_awaited_once_with("pl")
        assert [len(c.args[0]) for c in provider.client.get_tracks.await_args_list] == [50, 25]

    @pytest.mark.asyncio
    async def test_later_pages_are_empty_without_api_calls(self) -> None:
        """Pages after the first end the listing without re-reading the playlist."""
        provider = _playlist_provider([str(i) for i in range(1, 76)])

        tracks = await _get_playlist_tracks(provider, "pl", page=1)

        assert tracks == []
        provider.client.get_playlist.assert_not_awaited()
        provider.client.get_tracks.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_remove_uses_one_based_positions(self) -> None:
        """Removing positions 1 and 3 drops the first and third tracks."""
        provider = _playlist_provider(["10", "20", "30", "40"])

        await ZvukMusicProvider.remove_playlist_tracks(provider, "pl", (1, 3))

        provider.client.update_playlist.assert_awaited_once_with("pl", ["20", "40"])


class TestConnectReusesProfile:
    """connect() reads the profile loaded during init()."""

    @pytest.mark.asyncio
    async def test_connect_uses_loaded_profile(self) -> None:
        """No second profile request is made after init()."""
        client = ZvukMusicClient(token="valid")
        inner = MagicMock()
        inner.init = AsyncMock(return_value=inner)
        inner.is_authorized = AsyncMock(return_value=True)
        inner.profile = Mock(id=777)
        inner.get_profile = AsyncMock()

        with patch(
            "music_assistant.providers.zvuk_music.api_client.ClientAsync",
            return_value=inner,
        ):
            await client.connect()

        assert client.user_id == "777"
        inner.get_profile.assert_not_awaited()
