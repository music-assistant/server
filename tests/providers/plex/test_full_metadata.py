"""Tests for loading full metadata when listing a Plex library."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from music_assistant.providers.plex import PlexProvider
from music_assistant.providers.plex.constants import METADATA_BATCH_SIZE
from music_assistant.providers.plex.helpers import SUPPORTED_FEATURES


class _FakePlexItem:
    """Plex item stub that records whether it came from a full metadata fetch."""

    def __init__(self, rating_key: int, full: bool = False) -> None:
        self.ratingKey = rating_key
        self.key = f"/library/metadata/{rating_key}"
        self.full = full
        self._autoReload = True


def _make_provider(listing: list[_FakePlexItem], missing: set[int] | None = None) -> Any:
    """
    Create a PlexProvider whose library listings return the given items.

    The fake server answers a metadata request in reverse order and leaves out the keys
    in missing, so results only line up if the provider maps them back itself.

    :param listing: Items every library listing returns.
    :param missing: Rating keys the fake server no longer knows about.
    """
    mock_mass = MagicMock()
    mock_config = MagicMock()
    mock_config.instance_id = "plex_instance_1"
    config_values = {"library_type": "music", "log_level": "INFO"}
    mock_config.get_value = lambda key: config_values.get(key)
    setup_data = {"library_type": "music", "token": "local_auth"}
    mock_mass.config.get = lambda key, default=None: (
        setup_data if str(key).endswith("/setup_data") else default
    )
    mock_mass.config.get_raw_provider_config_value = lambda _instance_id, _key: None
    mock_mass.config.decrypt_string = lambda value: value
    mock_manifest = MagicMock()
    mock_manifest.type = "music"
    mock_manifest.domain = "plex"

    provider = PlexProvider(mock_mass, mock_manifest, mock_config, SUPPORTED_FEATURES)
    provider._plex_library = MagicMock()
    provider._plex_library.all = MagicMock(return_value=listing)
    provider._plex_library.albums = MagicMock(return_value=listing)
    provider._plex_library.searchTracks = MagicMock(side_effect=[listing, []])

    def fetch_items(keys: list[int], **_kwargs: Any) -> list[_FakePlexItem]:
        return [
            _FakePlexItem(key, full=True) for key in reversed(keys) if key not in (missing or ())
        ]

    provider._plex_library.fetchItems = MagicMock(side_effect=fetch_items)
    for parser in ("_parse_track", "_parse_album", "_parse_artist"):
        setattr(provider, parser, AsyncMock(side_effect=lambda item: item))
    return provider


@pytest.mark.parametrize(
    "listing", ["get_library_tracks", "get_library_albums", "get_library_artists"]
)
async def test_library_items_are_parsed_from_full_metadata(listing: str) -> None:
    """
    Listings only return partial objects, so each item is loaded in full before parsing.

    Parsing a partial object makes plexapi reload that item on its own, one request per item.
    """
    provider = _make_provider([_FakePlexItem(1), _FakePlexItem(2)])

    items = [item async for item in getattr(provider, listing)()]

    assert [item.ratingKey for item in items] == [1, 2]
    assert all(item.full for item in items)
    assert all(item._autoReload is False for item in items)
    call = provider._plex_library.fetchItems.call_args
    assert call.args[0] == [1, 2]
    assert call.kwargs["container_size"] == METADATA_BATCH_SIZE
    assert call.kwargs["params"]["includeChapters"] == 1


async def test_full_metadata_is_requested_in_batches() -> None:
    """A large listing is loaded in batches instead of a single request."""
    listing = [_FakePlexItem(key) for key in range(METADATA_BATCH_SIZE + 5)]
    provider = _make_provider(listing)

    items = [item async for item in provider.get_library_albums()]

    assert [item.ratingKey for item in items] == [item.ratingKey for item in listing]
    requested = [call.args[0] for call in provider._plex_library.fetchItems.call_args_list]
    assert len(requested) == 2
    assert requested[1] == list(range(METADATA_BATCH_SIZE, METADATA_BATCH_SIZE + 5))


async def test_item_missing_from_full_metadata_keeps_its_place() -> None:
    """An item the server no longer returns is parsed from the listing result instead."""
    provider = _make_provider([_FakePlexItem(1), _FakePlexItem(2), _FakePlexItem(3)], missing={2})

    items = [item async for item in provider.get_library_albums()]

    assert [item.ratingKey for item in items] == [1, 2, 3]
    assert [item.full for item in items] == [True, False, True]


async def test_playlist_tracks_keep_their_order_and_duplicates() -> None:
    """Playlist positions follow the playlist order, repeated tracks included."""
    items = [_FakePlexItem(3), _FakePlexItem(1), _FakePlexItem(3), _FakePlexItem(2)]
    provider = _make_provider([])
    plex_playlist = MagicMock()
    plex_playlist.items = MagicMock(return_value=items)
    provider._get_data = AsyncMock(return_value=plex_playlist)
    provider._parse_track = AsyncMock(
        side_effect=lambda item: SimpleNamespace(ratingKey=item.ratingKey, position=0)
    )

    get_playlist_tracks: Any = PlexProvider.get_playlist_tracks.__wrapped__  # type: ignore[attr-defined]
    tracks = await get_playlist_tracks(provider, "/playlists/1")

    assert [track.ratingKey for track in tracks] == [3, 1, 3, 2]
    assert [track.position for track in tracks] == [1, 2, 3, 4]
    assert provider._plex_library.fetchItems.call_args.args[0] == [3, 1, 2]
