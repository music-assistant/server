"""Tests for moving a track inside a Music Assistant playlist."""

from __future__ import annotations

import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.errors import InvalidDataError

from music_assistant.helpers.playlists import PlaylistItem, generate_m3u
from music_assistant.providers.builtin import BuiltinProvider

TITLES = ["One", "Two", "Three", "Four"]


def _make_provider(titles: list[str]) -> BuiltinProvider:
    """
    Create a BuiltinProvider whose playlist file holds one entry per title.

    :param titles: The titles of the entries, in file order.
    """
    prov = object.__new__(BuiltinProvider)
    prov.mass = MagicMock()
    prov.logger = MagicMock()
    prov._playlist_locks = {}
    prov._playlist_lock = asyncio.Lock()
    entries = [
        PlaylistItem(path=f"spotify://track/{title.lower()}", title=title, length="120")
        for title in titles
    ]
    playlist = MagicMock()
    playlist.name = "My Playlist"
    playlist.image = None
    prov_any = cast("Any", prov)
    prov_any._read_m3u_file = AsyncMock(return_value=generate_m3u("My Playlist", entries))
    prov_any.get_playlist = AsyncMock(return_value=playlist)
    prov_any._write_m3u_file = AsyncMock()
    return prov


def _written_titles(prov: BuiltinProvider) -> list[str]:
    """Return the titles of the entries the provider wrote back, in file order."""
    write = cast("Any", prov)._write_m3u_file
    write.assert_awaited_once()
    assert write.await_args.args[0] == "playlist_1"
    return [entry.title for entry in write.await_args.args[2]]


@pytest.mark.parametrize(
    ("from_position", "to_position", "expected"),
    [
        pytest.param(3, 2, ["One", "Three", "Two", "Four"], id="up"),
        pytest.param(2, 3, ["One", "Three", "Two", "Four"], id="down"),
        pytest.param(4, 1, ["Four", "One", "Two", "Three"], id="to-top"),
        pytest.param(1, 4, ["Two", "Three", "Four", "One"], id="to-bottom"),
    ],
)
async def test_move_places_the_track_at_its_final_position(
    from_position: int, to_position: int, expected: list[str]
) -> None:
    """The moved track ends up at to_position and the others keep their order."""
    prov = _make_provider(TITLES)

    await prov.move_playlist_track("playlist_1", from_position, to_position)

    assert _written_titles(prov) == expected
    assert expected[to_position - 1] == TITLES[from_position - 1]


@pytest.mark.parametrize(("from_position", "to_position"), [(5, 1), (1, 5)])
async def test_move_outside_the_playlist_is_refused(from_position: int, to_position: int) -> None:
    """A position outside the file raises and leaves the file untouched."""
    prov = _make_provider(TITLES)

    with pytest.raises(InvalidDataError):
        await prov.move_playlist_track("playlist_1", from_position, to_position)

    cast("Any", prov)._write_m3u_file.assert_not_awaited()
