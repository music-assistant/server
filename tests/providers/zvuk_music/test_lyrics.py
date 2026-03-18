"""Tests for Zvuk Music lyrics feature (get_track_metadata)."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from music_assistant.providers.zvuk_music.provider import ZvukMusicProvider


def _make_provider(lyrics_result: MagicMock | None) -> ZvukMusicProvider:
    """Create a ZvukMusicProvider mock with a preset lyrics result."""
    provider = Mock(spec=ZvukMusicProvider)
    provider.client = Mock()
    provider.client.get_lyrics = AsyncMock(return_value=lyrics_result)
    provider.get_track_metadata = ZvukMusicProvider.get_track_metadata.__get__(
        provider, ZvukMusicProvider
    )
    return provider


def _make_track(item_id: str = "173663389") -> Mock:
    """Create a minimal MA Track mock."""
    track = MagicMock()
    track.item_id = item_id
    return track


class TestGetTrackMetadata:
    """Tests for ZvukMusicProvider.get_track_metadata()."""

    @pytest.mark.asyncio
    async def test_synced_lyrics_returned_as_lrc(self) -> None:
        """LRC synced lyrics (is_synced=True) go into metadata.lrc_lyrics."""
        lrc_text = "[00:00.68]Честное слово мы с тобой лишние\n[00:04.71]В мире весёлых сетей\n"  # noqa: RUF001
        provider = _make_provider(MagicMock(lyrics=lrc_text, is_synced=True))

        result = await provider.get_track_metadata(_make_track("173663389"))

        assert result is not None
        assert result.lrc_lyrics == lrc_text
        assert result.lyrics is None

    @pytest.mark.asyncio
    async def test_plain_lyrics_returned_as_lyrics(self) -> None:
        """Plain lyrics (is_synced=False) go into metadata.lyrics."""
        plain_text = "Some plain lyrics text\nSecond line\n"
        provider = _make_provider(MagicMock(lyrics=plain_text, is_synced=False))

        result = await provider.get_track_metadata(_make_track("99999"))

        assert result is not None
        assert result.lyrics == plain_text
        assert result.lrc_lyrics is None

    @pytest.mark.asyncio
    async def test_no_lyrics_returns_none(self) -> None:
        """When API returns None (track has no lyrics), return None."""
        provider = _make_provider(None)

        result = await provider.get_track_metadata(_make_track("174649396"))

        assert result is None

    @pytest.mark.asyncio
    async def test_empty_lyrics_text_returns_none(self) -> None:
        """When lyrics object has empty/falsy lyrics text, return None."""
        provider = _make_provider(MagicMock(lyrics="", is_synced=False))

        result = await provider.get_track_metadata(_make_track("999"))

        assert result is None

    @pytest.mark.asyncio
    async def test_correct_track_id_passed_to_client(self) -> None:
        """The correct track item_id is forwarded to the API client."""
        provider = _make_provider(None)
        track = _make_track("12345678")

        await provider.get_track_metadata(track)

        cast("AsyncMock", provider.client.get_lyrics).assert_awaited_once_with("12345678")
