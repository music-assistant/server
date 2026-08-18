"""Tests for AlbumsController provider matching (explicit, IO-capable enrichment)."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, patch

from music_assistant_models.enums import ExternalID
from music_assistant_models.media_items import (
    Album,
    Artist,
    ProviderMapping,
    Track,
    UniqueList,
)

from music_assistant.controllers.music.media.albums import AlbumsController

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

MB_ALBUM_ID = "11111111-1111-1111-1111-111111111111"
BASE_BARCODE = "888072439412"
OTHER_BARCODE = "075678643224"


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------


def _artist() -> Artist:
    """Return the single album artist shared by every fixture album."""
    return Artist(
        item_id="artist",
        provider="library",
        name="Sigur Rós",
        provider_mappings={
            ProviderMapping(item_id="artist", provider_domain="test", provider_instance="library")
        },
    )


def _album(
    item_id: str = "1",
    provider: str = "library",
    *,
    name: str = "( )",
    version: str = "",
    year: int | None = 2022,
    external_ids: set[tuple[ExternalID, str]] | None = None,
    barcode: str | None = None,
) -> Album:
    """Build an Album for match-provider tests."""
    ext = set(external_ids or set())
    if barcode:
        ext.add((ExternalID.BARCODE, barcode))
    return Album(
        item_id=item_id,
        provider=provider,
        name=name,
        version=version,
        year=year,
        external_ids=ext,
        artists=UniqueList([_artist()]),
        provider_mappings={
            ProviderMapping(item_id=item_id, provider_domain=provider, provider_instance=provider)
        },
    )


def _track(number: int, *, isrc_prefix: str = "USRC17607", duration: int = 200) -> Track:
    """Build one ordered album track with a distinct ISRC per position."""
    return Track(
        item_id=str(number),
        provider="spotify_1",
        name=f"Track {number}",
        duration=duration,
        disc_number=1,
        track_number=number,
        external_ids={(ExternalID.ISRC, f"{isrc_prefix}{number:03d}")},
        provider_mappings={
            ProviderMapping(
                item_id=str(number), provider_domain="spotify", provider_instance="spotify_1"
            )
        },
    )


def _tracklist(count: int, *, isrc_prefix: str = "USRC17607") -> list[Track]:
    """Build an ordered tracklist of the given length."""
    return [_track(number, isrc_prefix=isrc_prefix) for number in range(1, count + 1)]


def _mb_release(release_id: str, release_group_id: str) -> SimpleNamespace:
    """Return a stand-in for a parsed MusicBrainz release."""
    return SimpleNamespace(id=release_id, release_group=SimpleNamespace(id=release_group_id))


def _provider() -> Mock:
    """Return a mock streaming MusicProvider for matching."""
    provider = Mock()
    provider.name = "Spotify"
    provider.instance_id = "spotify_1"
    provider.domain = "spotify"
    return provider


@dataclass
class _Harness:
    """A controller under test together with its mocked IO boundaries."""

    ctrl: AlbumsController
    search: AsyncMock
    get_provider_item: AsyncMock
    get_library_album_tracks: AsyncMock
    get_provider_album_tracks: AsyncMock


@contextmanager
def _harness(
    *,
    search_results: Sequence[Album],
    provider_items: dict[str, Album],
    library_tracks: list[Track] | None = None,
    provider_album_tracks: dict[str, list[Track]] | None = None,
    musicbrainz: Mock | None = None,
) -> Iterator[_Harness]:
    """
    Yield an AlbumsController with every IO boundary mocked.

    :param search_results: Sparse album search results returned to the prefilter.
    :param provider_items: Full provider albums keyed by their item id.
    :param library_tracks: Stored library tracks returned for the base album.
    :param provider_album_tracks: Provider album tracks keyed by album item id.
    :param musicbrainz: Optional mock MusicBrainz provider.
    """
    mass = Mock()
    mass.get_provider = Mock(
        side_effect=lambda domain: musicbrainz if domain == "musicbrainz" else None
    )
    ctrl = AlbumsController.__new__(AlbumsController)
    ctrl.logger = logging.getLogger("test.albums.match")
    ctrl.mass = mass
    search = AsyncMock(return_value=list(search_results))
    get_provider_item = AsyncMock(
        side_effect=lambda item_id, _provider, **_kwargs: provider_items[item_id]
    )
    library = AsyncMock(return_value=list(library_tracks or []))
    album_tracks = provider_album_tracks or {}
    provider_tracks = AsyncMock(
        side_effect=lambda item_id, _provider: list(album_tracks.get(item_id, []))
    )
    with patch.multiple(
        ctrl,
        search=search,
        get_provider_item=get_provider_item,
        get_library_album_tracks=library,
        _get_provider_album_tracks=provider_tracks,
    ):
        yield _Harness(ctrl, search, get_provider_item, library, provider_tracks)


# ---------------------------------------------------------------------------
# search-result prefilter
# ---------------------------------------------------------------------------


async def test_clear_no_match_search_result_skips_full_fetch() -> None:
    """A confidently non-matching search result is dropped before any full fetch."""
    other = _album(item_id="s1", provider="spotify_1", name="Takk...")
    with _harness(search_results=[other], provider_items={}) as harness:
        matches = await harness.ctrl.match_provider(_album(), _provider())

    assert matches == []
    harness.get_provider_item.assert_not_awaited()


async def test_insufficient_search_result_proceeds_to_one_full_fetch() -> None:
    """An ambiguous search result is confirmed against exactly one full provider album."""
    base = _album(version="", external_ids={(ExternalID.MB_ALBUM, MB_ALBUM_ID)})
    sparse = _album(item_id="s1", provider="spotify_1", version="Remaster")
    full = _album(
        item_id="s1",
        provider="spotify_1",
        version="",
        external_ids={(ExternalID.MB_ALBUM, MB_ALBUM_ID)},
    )
    with _harness(search_results=[sparse], provider_items={"s1": full}) as harness:
        matches = await harness.ctrl.match_provider(base, _provider())

    assert [mapping.item_id for mapping in matches] == ["s1"]
    harness.get_provider_item.assert_awaited_once()


async def test_full_item_match_uses_no_track_or_musicbrainz_calls() -> None:
    """A full album that matches on metadata never fetches tracks or hits MusicBrainz."""
    musicbrainz = Mock()
    musicbrainz.get_releases_by_barcode = AsyncMock()
    base = _album(external_ids={(ExternalID.MB_ALBUM, MB_ALBUM_ID)})
    sparse = _album(item_id="s1", provider="spotify_1", version="Remaster")
    full = _album(
        item_id="s1", provider="spotify_1", external_ids={(ExternalID.MB_ALBUM, MB_ALBUM_ID)}
    )
    with _harness(
        search_results=[sparse], provider_items={"s1": full}, musicbrainz=musicbrainz
    ) as harness:
        matches = await harness.ctrl.match_provider(base, _provider())

    assert [mapping.item_id for mapping in matches] == ["s1"]
    harness.get_library_album_tracks.assert_not_awaited()
    harness.get_provider_album_tracks.assert_not_awaited()
    musicbrainz.get_releases_by_barcode.assert_not_awaited()


# ---------------------------------------------------------------------------
# track-fingerprint resolution
# ---------------------------------------------------------------------------


async def test_ambiguous_albums_resolve_via_track_fingerprints() -> None:
    """The 14-track remaster vs deluxe-remaster case matches once tracklists agree."""
    musicbrainz = Mock()
    musicbrainz.get_releases_by_barcode = AsyncMock()
    base = _album(version="2022 Remaster")
    sparse = _album(item_id="s1", provider="spotify_1", version="Deluxe 2022 Remaster")
    full = _album(item_id="s1", provider="spotify_1", version="Deluxe 2022 Remaster")
    with _harness(
        search_results=[sparse],
        provider_items={"s1": full},
        library_tracks=_tracklist(14),
        provider_album_tracks={"s1": _tracklist(14)},
        musicbrainz=musicbrainz,
    ) as harness:
        matches = await harness.ctrl.match_provider(base, _provider())

    assert [mapping.item_id for mapping in matches] == ["s1"]
    harness.get_library_album_tracks.assert_awaited_once()
    harness.get_provider_album_tracks.assert_awaited_once_with("s1", "spotify_1")
    # a decisive fingerprint match must not fall through to MusicBrainz
    musicbrainz.get_releases_by_barcode.assert_not_awaited()


async def test_different_track_counts_do_not_map() -> None:
    """An 8-track album and a 14-track edition are never merged by fingerprints."""
    base = _album(version="2022 Remaster")
    sparse = _album(item_id="s1", provider="spotify_1", version="Deluxe 2022 Remaster")
    full = _album(item_id="s1", provider="spotify_1", version="Deluxe 2022 Remaster")
    with _harness(
        search_results=[sparse],
        provider_items={"s1": full},
        library_tracks=_tracklist(8),
        provider_album_tracks={"s1": _tracklist(14)},
    ) as harness:
        assert await harness.ctrl.match_provider(base, _provider()) == []


async def test_conflicting_isrc_fingerprints_do_not_map() -> None:
    """Same-length tracklists with conflicting ISRCs are a different recording."""
    base = _album(version="2022 Remaster")
    sparse = _album(item_id="s1", provider="spotify_1", version="Deluxe 2022 Remaster")
    full = _album(item_id="s1", provider="spotify_1", version="Deluxe 2022 Remaster")
    with _harness(
        search_results=[sparse],
        provider_items={"s1": full},
        library_tracks=_tracklist(14, isrc_prefix="USRC17607"),
        provider_album_tracks={"s1": _tracklist(14, isrc_prefix="USRC28718")},
    ) as harness:
        assert await harness.ctrl.match_provider(base, _provider()) == []


async def test_base_tracks_fetched_once_across_candidates() -> None:
    """The base tracklist is resolved once even when several candidates need it."""
    base = _album(version="2022 Remaster")
    search_results = [
        _album(item_id=f"s{index}", provider="spotify_1", version="Deluxe 2022 Remaster")
        for index in range(1, 4)
    ]
    provider_items = {
        album.item_id: _album(
            item_id=album.item_id, provider="spotify_1", version="Deluxe 2022 Remaster"
        )
        for album in search_results
    }
    with _harness(
        search_results=search_results,
        provider_items=provider_items,
        library_tracks=_tracklist(14),
        # every candidate stays ambiguous (no tracks to compare against)
        provider_album_tracks={},
    ) as harness:
        await harness.ctrl.match_provider(base, _provider())

    harness.get_library_album_tracks.assert_awaited_once()
    assert harness.get_provider_album_tracks.await_count == len(search_results)


# ---------------------------------------------------------------------------
# MusicBrainz last-resort evidence
# ---------------------------------------------------------------------------


@contextmanager
def _ambiguous_harness(
    musicbrainz: Mock | None,
    *,
    base_barcode: str = BASE_BARCODE,
    compare_barcode: str = BASE_BARCODE,
) -> Iterator[tuple[_Harness, Album]]:
    """Yield a harness whose only candidate is ambiguous through the fingerprint stage."""
    base = _album(version="2022 Remaster", barcode=base_barcode)
    sparse = _album(
        item_id="s1", provider="spotify_1", version="Deluxe 2022 Remaster", barcode=compare_barcode
    )
    full = _album(
        item_id="s1", provider="spotify_1", version="Deluxe 2022 Remaster", barcode=compare_barcode
    )
    with _harness(
        search_results=[sparse],
        provider_items={"s1": full},
        library_tracks=_tracklist(14),
        # no candidate tracks: the fingerprint stays inconclusive and MusicBrainz decides
        provider_album_tracks={"s1": []},
        musicbrainz=musicbrainz,
    ) as harness:
        yield harness, base


async def test_musicbrainz_shared_release_resolves_after_fingerprint_insufficiency() -> None:
    """A shared specific MusicBrainz release confirms the match once tracks fall short."""
    musicbrainz = Mock()
    musicbrainz.get_releases_by_barcode = AsyncMock(return_value=[_mb_release("rel-1", "rg-1")])
    with _ambiguous_harness(musicbrainz) as (harness, base):
        matches = await harness.ctrl.match_provider(base, _provider())

    assert [mapping.item_id for mapping in matches] == ["s1"]
    # the tracklist is consulted before MusicBrainz is ever asked
    harness.get_provider_album_tracks.assert_awaited_once_with("s1", "spotify_1")
    musicbrainz.get_releases_by_barcode.assert_awaited()


async def test_musicbrainz_shared_release_group_alone_does_not_match() -> None:
    """Different specific releases in one release group must not identify an edition."""
    musicbrainz = Mock()

    async def _releases(barcode: str) -> list[SimpleNamespace]:
        if barcode == BASE_BARCODE:
            return [_mb_release("rel-1", "rg-9")]
        return [_mb_release("rel-2", "rg-9")]

    musicbrainz.get_releases_by_barcode = AsyncMock(side_effect=_releases)
    with _ambiguous_harness(musicbrainz, compare_barcode=OTHER_BARCODE) as (harness, base):
        assert await harness.ctrl.match_provider(base, _provider()) == []


async def test_musicbrainz_disjoint_release_groups_do_not_match() -> None:
    """Barcodes in entirely different release groups are negative evidence."""
    musicbrainz = Mock()

    async def _releases(barcode: str) -> list[SimpleNamespace]:
        if barcode == BASE_BARCODE:
            return [_mb_release("rel-1", "rg-1")]
        return [_mb_release("rel-2", "rg-2")]

    musicbrainz.get_releases_by_barcode = AsyncMock(side_effect=_releases)
    with _ambiguous_harness(musicbrainz, compare_barcode=OTHER_BARCODE) as (harness, base):
        assert await harness.ctrl.match_provider(base, _provider()) == []


async def test_musicbrainz_unresolved_barcode_abstains() -> None:
    """A barcode MusicBrainz cannot resolve abstains instead of guessing."""
    musicbrainz = Mock()
    musicbrainz.get_releases_by_barcode = AsyncMock(return_value=[])
    with _ambiguous_harness(musicbrainz) as (harness, base):
        assert await harness.ctrl.match_provider(base, _provider()) == []


async def test_musicbrainz_skipped_without_a_configured_provider() -> None:
    """With no MusicBrainz provider configured the match simply abstains."""
    with _ambiguous_harness(None) as (harness, base):
        assert await harness.ctrl.match_provider(base, _provider()) == []


# ---------------------------------------------------------------------------
# bulk-sync guard
# ---------------------------------------------------------------------------


async def test_get_library_item_by_match_is_io_free() -> None:
    """The DB-only sync match path must never reach a provider or MusicBrainz."""
    mass = Mock()
    mass.get_provider = Mock(side_effect=AssertionError("provider lookup during sync"))
    ctrl = AlbumsController.__new__(AlbumsController)
    ctrl.logger = logging.getLogger("test.albums.sync")
    ctrl.mass = mass
    ctrl.db_table = "albums"
    item = _album(item_id="a1", provider="spotify_1", barcode=BASE_BARCODE)

    with patch.multiple(
        ctrl,
        # every DB lookup returns nothing; a real provider/MusicBrainz call would raise
        get_library_item_by_prov_id=AsyncMock(return_value=None),
        get_library_item_by_prov_mappings=AsyncMock(return_value=None),
        get_library_items_by_external_ids=AsyncMock(return_value=[]),
        get_library_items_by_query=AsyncMock(return_value=[]),
        search=AsyncMock(side_effect=AssertionError("search during sync")),
        get_provider_item=AsyncMock(side_effect=AssertionError("provider fetch during sync")),
    ):
        assert await ctrl._get_library_item_by_match(item) is None

    mass.get_provider.assert_not_called()
