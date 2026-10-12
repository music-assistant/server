"""
Tests for the whole-albums Autoplay mode.

When the queue runs out, Autoplay adds the next album of the last track's artist, in the artist
order. The tests resolve the albums through a real MusicAssistant with a real SQLite database,
reusing the artist order stubs.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from music_assistant_models.enums import AlbumType
from music_assistant_models.media_items import Album, Artist, ProviderMapping

from music_assistant.controllers.player_queues.autoplay import albums_mode_available
from music_assistant.controllers.player_queues.constants import ArtistOrder
from tests.controllers.player_queues.test_artist_album_order import (
    _add_album,
    _add_artist,
    _library_mapping,
    _providers_available,
    _set_order,
    _set_selection,
    _streaming_album,
    _stub_provider,
)

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

__all__ = ["_providers_available"]


def _album_names(albums: list[list[Album]] | None) -> list[str] | None:
    """Return the names of the albums' first copies, in order."""
    return None if albums is None else [copies[0].name for copies in albums]


def _provider_artist() -> Artist:
    """Build an artist as a streaming provider lists it, not in the library."""
    item_id = uuid4().hex
    return Artist(
        item_id=item_id,
        provider="streaming",
        name="ABBA",
        provider_mappings={
            ProviderMapping(
                item_id=item_id, provider_domain="streaming", provider_instance="streaming"
            )
        },
    )


async def _artist_with_years(
    mass: MusicAssistant, *years: int | None
) -> tuple[Artist, dict[int | None, Album]]:
    """Add a library artist with one album per year, named after it. Returns the albums too."""
    artist = await _add_artist(mass)
    albums = {year: await _add_album(mass, artist, f"Album {year}", year) for year in years}
    return artist, albums


async def test_release_order_plays_the_next_newer_album_then_wraps(mass: MusicAssistant) -> None:
    """By release, the albums after the one that played come first, then the oldest unplayed."""
    artist, albums = await _artist_with_years(mass, 1995, 1998, 2005, 2010, 2015)
    _set_order(mass, ArtistOrder.ALBUMS_BY_RELEASE)

    result = await mass.player_queues.artist_album_candidates(
        artist, [albums[2005], albums[2010]], albums[2005]
    )

    assert _album_names(result) == ["Album 2015", "Album 1995", "Album 1998"]


async def test_every_album_played_leaves_no_candidate(mass: MusicAssistant) -> None:
    """An album with a track in the queue counts as played, so all played leaves none."""
    artist, albums = await _artist_with_years(mass, 1995, 1998)
    _set_order(mass, ArtistOrder.ALBUMS_BY_RELEASE)

    result = await mass.player_queues.artist_album_candidates(
        artist, list(albums.values()), albums[1998]
    )

    assert result == []


async def test_a_queued_album_matches_its_other_copy(mass: MusicAssistant) -> None:
    """A queued provider copy of a library album counts as that album played."""
    artist, albums = await _artist_with_years(mass, 1995, 1998)
    _set_order(mass, ArtistOrder.ALBUMS_BY_RELEASE)
    copy = replace(
        albums[1995],
        item_id="copy",
        provider="streaming",
        provider_mappings={
            ProviderMapping(
                item_id="copy", provider_domain="streaming", provider_instance="streaming"
            )
        },
    )

    result = await mass.player_queues.artist_album_candidates(artist, [copy], None)

    assert _album_names(result) == ["Album 1998"]


@pytest.mark.parametrize("anchor_kind", ["single", "other artist"])
async def test_an_anchor_outside_the_list_is_placed_by_its_year(
    mass: MusicAssistant, anchor_kind: str
) -> None:
    """A single or another artist's album counts as before the albums of its year."""
    artist, _ = await _artist_with_years(mass, 1995, 1998, 2005)
    if anchor_kind == "single":
        anchor = await _add_album(mass, artist, "Zebra", 1998, album_type=AlbumType.SINGLE)
    else:
        other = await mass.music.artists.add_item_to_library(
            Artist(
                item_id="0", provider="library", name="Other", provider_mappings=_library_mapping()
            )
        )
        anchor = await _add_album(mass, other, "Zebra", 1998)
    _set_order(mass, ArtistOrder.ALBUMS_BY_RELEASE)

    result = await mass.player_queues.artist_album_candidates(artist, [anchor], anchor)

    assert _album_names(result) == ["Album 1998", "Album 2005", "Album 1995"]


@pytest.mark.parametrize("anchor_year", ["no anchor", "no year"])
async def test_without_an_anchor_release_order_starts_from_the_oldest(
    mass: MusicAssistant, anchor_year: str
) -> None:
    """With no anchor, or an anchor without a year, the oldest album comes first."""
    artist, _ = await _artist_with_years(mass, 2005, None, 1995)
    anchor = None
    if anchor_year == "no year":
        anchor = await _add_album(mass, artist, "Zebra", None, album_type=AlbumType.SINGLE)
    _set_order(mass, ArtistOrder.ALBUMS_BY_RELEASE)

    result = await mass.player_queues.artist_album_candidates(artist, [], anchor)

    assert _album_names(result) == ["Album 1995", "Album 2005", "Album None"]


async def test_random_order_picks_only_unplayed_albums(mass: MusicAssistant) -> None:
    """Random albums leave out the played ones."""
    artist, albums = await _artist_with_years(mass, 1995, 1998, 2005)
    _set_order(mass, ArtistOrder.RANDOM_ALBUMS)

    result = await mass.player_queues.artist_album_candidates(artist, [albums[1998]], None)

    assert sorted(_album_names(result) or []) == ["Album 1995", "Album 2005"]


async def test_an_artist_with_only_singles_has_no_candidate(mass: MusicAssistant) -> None:
    """Singles are left out, so an artist with only singles has no album to play."""
    artist = await _add_artist(mass)
    await _add_album(mass, artist, "Waterloo", 1974, album_type=AlbumType.SINGLE)
    _set_order(mass, ArtistOrder.ALBUMS_BY_RELEASE)

    assert await mass.player_queues.artist_album_candidates(artist, [], None) == []


async def test_an_artist_outside_the_library_gets_all_provider_albums(
    mass: MusicAssistant,
) -> None:
    """An artist outside the library plays all its provider albums under a library selection."""
    _stub_provider(mass, [_streaming_album("Arrival", 1976), _streaming_album("Waterloo", 1974)])
    _set_order(mass, ArtistOrder.ALBUMS_BY_RELEASE)
    _set_selection(mass, "library_tracks")

    result = await mass.player_queues.artist_album_candidates(_provider_artist(), [], None)

    assert _album_names(result) == ["Waterloo", "Arrival"]


async def test_albums_that_cannot_be_listed_give_none(mass: MusicAssistant) -> None:
    """When no provider can list the albums, there are no candidates to tell apart from none."""
    resolver = mass.player_queues._media_resolver
    resolver._provider_artist_albums = AsyncMock(return_value=None)  # type: ignore[method-assign]
    _set_order(mass, ArtistOrder.ALBUMS_BY_RELEASE)

    assert await mass.player_queues.artist_album_candidates(_provider_artist(), [], None) is None


@pytest.mark.parametrize(
    ("order", "selection", "available"),
    [
        (ArtistOrder.ALBUMS_BY_RELEASE, "all_tracks", True),
        (ArtistOrder.RANDOM_ALBUMS, "library_tracks", True),
        (ArtistOrder.SHUFFLED_TRACKS, "all_tracks", False),
        (ArtistOrder.ALBUMS_BY_RELEASE, "top_tracks", False),
    ],
)
def test_the_albums_mode_needs_an_album_order_and_no_top_tracks(
    mass: MusicAssistant, order: ArtistOrder, selection: str, available: bool
) -> None:
    """Whole albums can play only when the artist order plays albums, and not top tracks."""
    _set_order(mass, order)
    _set_selection(mass, selection)

    assert albums_mode_available(mass) is available
