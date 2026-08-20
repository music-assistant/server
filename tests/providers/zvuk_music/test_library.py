"""Tests for ZvukMusicProvider library methods (get_library_*)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import InvalidDataError

from music_assistant.providers.zvuk_music.constants import DEFAULT_LIMIT
from music_assistant.providers.zvuk_music.provider import ZvukMusicProvider

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PROVIDER_MODULE = "music_assistant.providers.zvuk_music.provider"


def _make_provider() -> Any:
    """Create a ZvukMusicProvider mock with library methods and _iter_batched bound."""
    provider = Mock(spec=ZvukMusicProvider)
    provider.client = Mock()
    provider.logger = Mock()
    provider.instance_id = "zvuk_music"
    provider._iter_batched = ZvukMusicProvider._iter_batched.__get__(provider, ZvukMusicProvider)
    provider.get_library_artists = ZvukMusicProvider.get_library_artists.__get__(
        provider, ZvukMusicProvider
    )
    provider.get_library_albums = ZvukMusicProvider.get_library_albums.__get__(
        provider, ZvukMusicProvider
    )
    provider.get_library_tracks = ZvukMusicProvider.get_library_tracks.__get__(
        provider, ZvukMusicProvider
    )
    provider.get_library_playlists = ZvukMusicProvider.get_library_playlists.__get__(
        provider, ZvukMusicProvider
    )
    return provider


def _make_item(item_id: int) -> Mock:
    """Create a minimal collection item mock with an id."""
    item = Mock()
    item.id = item_id
    return item


# ---------------------------------------------------------------------------
# Tests for get_library_artists()
# ---------------------------------------------------------------------------


class TestGetLibraryArtists:
    """Tests for ZvukMusicProvider.get_library_artists()."""

    @pytest.mark.asyncio
    async def test_none_collection_yields_nothing(self) -> None:
        """When get_collection() returns None, nothing is yielded."""
        provider = _make_provider()
        provider.client.get_collection = AsyncMock(return_value=None)

        results = [a async for a in provider.get_library_artists()]

        assert results == []

    @pytest.mark.asyncio
    async def test_empty_artists_list_yields_nothing(self) -> None:
        """When collection.artists is empty, nothing is yielded."""
        provider = _make_provider()
        collection = Mock()
        collection.artists = []
        provider.client.get_collection = AsyncMock(return_value=collection)

        results = [a async for a in provider.get_library_artists()]

        assert results == []

    @pytest.mark.asyncio
    async def test_items_are_fetched_and_parsed(self) -> None:
        """Artists in the collection are fetched in batch and parsed."""
        provider = _make_provider()
        collection = Mock()
        collection.artists = [_make_item(1), _make_item(2)]
        provider.client.get_collection = AsyncMock(return_value=collection)

        raw_1, raw_2 = _make_item(1), _make_item(2)
        provider.client.get_artists = AsyncMock(return_value=[raw_1, raw_2])

        parsed_1, parsed_2 = Mock(), Mock()

        with patch(f"{_PROVIDER_MODULE}.parse_artist", side_effect=[parsed_1, parsed_2]):
            results = [a async for a in provider.get_library_artists()]

        assert results == [parsed_1, parsed_2]

    @pytest.mark.asyncio
    async def test_invalid_data_error_is_skipped(self) -> None:
        """InvalidDataError from parse_artist is reported and the item is skipped."""
        provider = _make_provider()
        collection = Mock()
        collection.artists = [_make_item(1), _make_item(2)]
        provider.client.get_collection = AsyncMock(return_value=collection)

        raw_1, raw_2 = _make_item(1), _make_item(2)
        provider.client.get_artists = AsyncMock(return_value=[raw_1, raw_2])

        parsed_2 = Mock()
        err = InvalidDataError("bad data")

        with patch(
            f"{_PROVIDER_MODULE}.parse_artist",
            side_effect=[err, parsed_2],
        ):
            results = [a async for a in provider.get_library_artists()]

        assert results == [parsed_2]
        provider.report_skipped_sync_item.assert_called_once_with(
            MediaType.ARTIST, str(raw_1.id), err
        )

    @pytest.mark.asyncio
    async def test_large_collection_fetches_multiple_batches(self) -> None:
        """Collections larger than DEFAULT_LIMIT trigger multiple fetcher calls."""
        provider = _make_provider()
        total = DEFAULT_LIMIT + 10  # 60 items → 2 batches
        collection = Mock()
        collection.artists = [_make_item(i) for i in range(1, total + 1)]
        provider.client.get_collection = AsyncMock(return_value=collection)
        mock_get_artists = AsyncMock(return_value=[])
        provider.client.get_artists = mock_get_artists

        with patch(f"{_PROVIDER_MODULE}.parse_artist"):
            [a async for a in provider.get_library_artists()]

        assert mock_get_artists.await_count == 2
        first_batch = mock_get_artists.call_args_list[0][0][0]
        second_batch = mock_get_artists.call_args_list[1][0][0]
        assert len(first_batch) == DEFAULT_LIMIT
        assert len(second_batch) == 10


# ---------------------------------------------------------------------------
# Tests for get_library_albums()
# ---------------------------------------------------------------------------


class TestGetLibraryAlbums:
    """Tests for ZvukMusicProvider.get_library_albums()."""

    @pytest.mark.asyncio
    async def test_none_collection_yields_nothing(self) -> None:
        """When get_collection() returns None, nothing is yielded."""
        provider = _make_provider()
        provider.client.get_collection = AsyncMock(return_value=None)

        results = [a async for a in provider.get_library_albums()]

        assert results == []

    @pytest.mark.asyncio
    async def test_items_are_fetched_and_parsed(self) -> None:
        """Releases in the collection are fetched and parsed as albums."""
        provider = _make_provider()
        collection = Mock()
        collection.releases = [_make_item(10), _make_item(20)]
        provider.client.get_collection = AsyncMock(return_value=collection)

        raw_1, raw_2 = _make_item(10), _make_item(20)
        provider.client.get_releases = AsyncMock(return_value=[raw_1, raw_2])

        parsed_1, parsed_2 = Mock(), Mock()

        with patch(f"{_PROVIDER_MODULE}.parse_album", side_effect=[parsed_1, parsed_2]):
            results = [a async for a in provider.get_library_albums()]

        assert results == [parsed_1, parsed_2]

    @pytest.mark.asyncio
    async def test_invalid_data_error_is_skipped(self) -> None:
        """InvalidDataError from parse_album is reported and the item is skipped."""
        provider = _make_provider()
        collection = Mock()
        collection.releases = [_make_item(10)]
        provider.client.get_collection = AsyncMock(return_value=collection)
        raw = _make_item(10)
        provider.client.get_releases = AsyncMock(return_value=[raw])

        err = InvalidDataError("bad release")
        with patch(f"{_PROVIDER_MODULE}.parse_album", side_effect=err):
            results = [a async for a in provider.get_library_albums()]

        assert results == []
        provider.report_skipped_sync_item.assert_called_once_with(MediaType.ALBUM, str(raw.id), err)


# ---------------------------------------------------------------------------
# Tests for get_library_tracks()
# ---------------------------------------------------------------------------


class TestGetLibraryTracks:
    """Tests for ZvukMusicProvider.get_library_tracks()."""

    @pytest.mark.asyncio
    async def test_none_collection_yields_nothing(self) -> None:
        """When get_collection() returns None, nothing is yielded."""
        provider = _make_provider()
        provider.client.get_collection = AsyncMock(return_value=None)

        results = [t async for t in provider.get_library_tracks()]

        assert results == []

    @pytest.mark.asyncio
    async def test_items_are_fetched_and_parsed(self) -> None:
        """Tracks in the collection are fetched and parsed."""
        provider = _make_provider()
        collection = Mock()
        collection.tracks = [_make_item(100), _make_item(200)]
        provider.client.get_collection = AsyncMock(return_value=collection)

        raw_1, raw_2 = _make_item(100), _make_item(200)
        provider.client.get_tracks = AsyncMock(return_value=[raw_1, raw_2])

        parsed_1, parsed_2 = Mock(), Mock()

        with patch(f"{_PROVIDER_MODULE}.parse_track", side_effect=[parsed_1, parsed_2]):
            results = [t async for t in provider.get_library_tracks()]

        assert results == [parsed_1, parsed_2]

    @pytest.mark.asyncio
    async def test_invalid_data_error_is_skipped(self) -> None:
        """InvalidDataError from parse_track causes the item to be skipped."""
        provider = _make_provider()
        collection = Mock()
        collection.tracks = [_make_item(100), _make_item(200)]
        provider.client.get_collection = AsyncMock(return_value=collection)

        provider.client.get_tracks = AsyncMock(return_value=[Mock(), Mock()])
        good_track = Mock()

        with patch(
            f"{_PROVIDER_MODULE}.parse_track",
            side_effect=[InvalidDataError("bad track"), good_track],
        ):
            results = [t async for t in provider.get_library_tracks()]

        assert results == [good_track]


# ---------------------------------------------------------------------------
# Tests for get_library_playlists()
# ---------------------------------------------------------------------------


class TestGetLibraryPlaylists:
    """Tests for ZvukMusicProvider.get_library_playlists()."""

    @pytest.mark.asyncio
    async def test_empty_user_playlists_still_yields_synthesis(self) -> None:
        """When get_user_playlists() returns None, synthesis playlists are still yielded."""
        provider = _make_provider()
        provider.client.get_user_playlists = AsyncMock(return_value=None)
        raw_synth = Mock()
        provider.client.get_short_playlists = AsyncMock(return_value=[raw_synth])

        parsed_synth = Mock()
        with patch(f"{_PROVIDER_MODULE}.parse_playlist", return_value=parsed_synth):
            results = [p async for p in provider.get_library_playlists()]

        assert results == [parsed_synth]

    @pytest.mark.asyncio
    async def test_user_playlists_are_fetched_and_parsed(self) -> None:
        """User playlists are fetched in batch and parsed."""
        provider = _make_provider()
        provider.client.get_user_playlists = AsyncMock(return_value=[_make_item(1), _make_item(2)])
        raw_1, raw_2 = _make_item(1), _make_item(2)
        provider.client.get_playlists = AsyncMock(return_value=[raw_1, raw_2])
        provider.client.get_short_playlists = AsyncMock(return_value=[])

        parsed_1, parsed_2 = Mock(), Mock()

        with patch(f"{_PROVIDER_MODULE}.parse_playlist", side_effect=[parsed_1, parsed_2]):
            results = [p async for p in provider.get_library_playlists()]

        assert results == [parsed_1, parsed_2]

    @pytest.mark.asyncio
    async def test_synthesis_playlists_are_also_yielded(self) -> None:
        """Synthesis (personalized) playlists are yielded after user playlists."""
        provider = _make_provider()
        provider.client.get_user_playlists = AsyncMock(return_value=[_make_item(1)])
        raw_user = Mock()
        provider.client.get_playlists = AsyncMock(return_value=[raw_user])
        raw_synth = Mock()
        provider.client.get_short_playlists = AsyncMock(return_value=[raw_synth])

        user_parsed = Mock()
        synth_parsed = Mock()

        with patch(f"{_PROVIDER_MODULE}.parse_playlist", side_effect=[user_parsed, synth_parsed]):
            results = [p async for p in provider.get_library_playlists()]

        assert results == [user_parsed, synth_parsed]

    @pytest.mark.asyncio
    async def test_synthesis_invalid_data_error_is_skipped(self) -> None:
        """InvalidDataError from a synthesis playlist parser is reported and skipped."""
        provider = _make_provider()
        # Need at least one user playlist so the method doesn't return early
        provider.client.get_user_playlists = AsyncMock(return_value=[_make_item(1)])
        provider.client.get_playlists = AsyncMock(return_value=[_make_item(1)])
        raw_synth = Mock()
        provider.client.get_short_playlists = AsyncMock(return_value=[raw_synth])

        # First call (user playlist) succeeds; second call (synthesis) raises
        good_parsed = Mock()
        err = InvalidDataError("bad synth")
        with patch(
            f"{_PROVIDER_MODULE}.parse_playlist",
            side_effect=[good_parsed, err],
        ):
            results = [p async for p in provider.get_library_playlists()]

        # User playlist is yielded; synthesis item is skipped
        assert results == [good_parsed]
        provider.report_skipped_sync_item.assert_called_once_with(
            MediaType.PLAYLIST, str(raw_synth.id), err
        )

    @pytest.mark.asyncio
    async def test_invalid_data_error_in_user_playlist_is_skipped(self) -> None:
        """InvalidDataError from parse_playlist on a user playlist is skipped."""
        provider = _make_provider()
        provider.client.get_user_playlists = AsyncMock(return_value=[_make_item(1), _make_item(2)])
        raw_1, raw_2 = _make_item(1), _make_item(2)
        provider.client.get_playlists = AsyncMock(return_value=[raw_1, raw_2])
        provider.client.get_short_playlists = AsyncMock(return_value=[])

        good_parsed = Mock()

        with patch(
            f"{_PROVIDER_MODULE}.parse_playlist",
            side_effect=[InvalidDataError("bad pl"), good_parsed],
        ):
            results = [p async for p in provider.get_library_playlists()]

        assert results == [good_parsed]


class TestBatchFetchGaps:
    """Tests for id's a batch fetch does not return."""

    @pytest.mark.asyncio
    async def test_id_the_fetch_left_out_is_reported(self) -> None:
        """
        An id the batch fetch did not return is reported instead of looking removed.

        The fetch returns an empty list for a batch it cannot find, which would otherwise
        drop every item in that batch out of the library.
        """
        provider = _make_provider()
        collection = Mock()
        collection.artists = [_make_item(1), _make_item(2)]
        provider.client.get_collection = AsyncMock(return_value=collection)
        provider.client.get_artists = AsyncMock(return_value=[])

        with patch(f"{_PROVIDER_MODULE}.parse_artist"):
            results = [a async for a in provider.get_library_artists()]

        assert results == []
        reported = {call.args[1] for call in provider.report_skipped_sync_item.call_args_list}
        assert reported == {"1", "2"}

    @pytest.mark.asyncio
    async def test_id_the_fetch_returned_is_not_reported(self) -> None:
        """An item that came back fine is not reported as missing."""
        provider = _make_provider()
        collection = Mock()
        collection.artists = [_make_item(1), _make_item(2)]
        provider.client.get_collection = AsyncMock(return_value=collection)
        provider.client.get_artists = AsyncMock(return_value=[_make_item(1)])

        with patch(f"{_PROVIDER_MODULE}.parse_artist"):
            results = [a async for a in provider.get_library_artists()]

        assert len(results) == 1
        reported = {call.args[1] for call in provider.report_skipped_sync_item.call_args_list}
        assert reported == {"2"}
