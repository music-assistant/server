"""Tests for AlbumsController provider matching (explicit, IO-capable enrichment)."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, patch

from music_assistant_models.enums import ExternalID
from music_assistant_models.errors import MediaNotFoundError, RetriesExhausted
from music_assistant_models.media_items import (
    Album,
    Artist,
    ProviderMapping,
    Track,
    UniqueList,
)

from music_assistant.controllers.music.media.albums import AlbumsController
from music_assistant.helpers.compare import AlbumMatchEvidence

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

MB_ALBUM_ID = "11111111-1111-1111-1111-111111111111"
BASE_BARCODE = "888072439412"
OTHER_BARCODE = "075678643224"
THIRD_BARCODE = "093624912514"
# the base library album is linked to one existing (already-loaded) provider
BASE_MAPPING = ProviderMapping(
    item_id="base-prov", provider_domain="tidal", provider_instance="tidal_1"
)


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
    item_id: str,
    provider: str,
    *,
    name: str = "( )",
    version: str = "",
    year: int | None = 2022,
    external_ids: set[tuple[ExternalID, str]] | None = None,
    barcodes: Sequence[str] = (),
    mappings: Sequence[ProviderMapping] | None = None,
) -> Album:
    """Build an Album for match-provider tests."""
    ext = set(external_ids or set())
    ext.update((ExternalID.BARCODE, barcode) for barcode in barcodes)
    if mappings is None:
        mappings = [
            ProviderMapping(item_id=item_id, provider_domain=provider, provider_instance=provider)
        ]
    return Album(
        item_id=item_id,
        provider=provider,
        name=name,
        version=version,
        year=year,
        external_ids=ext,
        artists=UniqueList([_artist()]),
        provider_mappings=set(mappings),
    )


def _library_album(
    *,
    version: str = "",
    external_ids: set[tuple[ExternalID, str]] | None = None,
    barcodes: Sequence[str] = (),
    mappings: Sequence[ProviderMapping] = (BASE_MAPPING,),
) -> Album:
    """Build the base (library) album under match."""
    return _album(
        "lib1",
        "library",
        version=version,
        external_ids=external_ids,
        barcodes=barcodes,
        mappings=mappings,
    )


def _track(
    number: int, *, isrc_prefix: str = "USRC17607", duration: int = 200, disc_number: int = 1
) -> Track:
    """Build one ordered album track with a distinct ISRC per position."""
    return Track(
        item_id=str(number),
        provider="spotify_1",
        name=f"Track {number}",
        duration=duration,
        disc_number=disc_number,
        track_number=number,
        external_ids={(ExternalID.ISRC, f"{isrc_prefix}{number:03d}")},
        provider_mappings={
            ProviderMapping(
                item_id=str(number), provider_domain="spotify", provider_instance="spotify_1"
            )
        },
    )


def _tracklist(count: int, *, isrc_prefix: str = "USRC17607", disc_number: int = 1) -> list[Track]:
    """Build an ordered tracklist of the given length."""
    return [
        _track(number, isrc_prefix=isrc_prefix, disc_number=disc_number)
        for number in range(1, count + 1)
    ]


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


def _mb(**releases_by_barcode: list[SimpleNamespace]) -> Mock:
    """Return a mock MusicBrainz provider answering barcode lookups from a mapping."""
    lookup = {BASE_BARCODE: [], OTHER_BARCODE: [], THIRD_BARCODE: [], **releases_by_barcode}
    musicbrainz = Mock()
    musicbrainz.get_releases_by_barcode = AsyncMock(side_effect=lambda barcode: lookup[barcode])
    return musicbrainz


def _loaded_provider(instance_id: str, *, available: bool = True) -> Mock:
    """Return a mock provider instance for the loaded-provider registry."""
    provider = Mock()
    provider.instance_id = instance_id
    provider.available = available
    return provider


@dataclass
class _Harness:
    """A controller under test together with its mocked IO boundaries."""

    ctrl: AlbumsController
    search: AsyncMock
    get_provider_item: AsyncMock
    get_provider_album_tracks: AsyncMock
    get_provider: Mock
    provider: Mock

    async def match(self, db_album: Album, *, strict: bool = True) -> list[ProviderMapping]:
        """Match against the single (streaming) provider the harness owns."""
        return await self.ctrl.match_provider(db_album, self.provider, strict)

    def album_track_calls(self) -> list[str]:
        """Return the album item ids passed to each provider album-track lookup."""
        return [call.args[0] for call in self.get_provider_album_tracks.await_args_list]


@contextmanager
def _harness(
    *,
    search_results: Sequence[Album],
    provider_items: dict[str, Album],
    provider_album_tracks: dict[str, list[Track] | Exception] | None = None,
    musicbrainz: Mock | None = None,
    loaded_instances: Sequence[str] = ("tidal_1",),
    provider_registry: dict[str, Mock] | None = None,
) -> Iterator[_Harness]:
    """
    Yield an AlbumsController with every IO boundary mocked.

    :param search_results: Sparse album search results returned to the prefilter.
    :param provider_items: Full provider albums keyed by their item id.
    :param provider_album_tracks: Provider album tracks (or an error to raise) keyed by
        album item id; the base album's mapping ids resolve here too.
    :param musicbrainz: Optional mock MusicBrainz provider.
    :param loaded_instances: Provider instance ids considered currently loaded and available.
    :param provider_registry: Explicit instance-id -> provider mock overrides.
    """
    album_tracks = provider_album_tracks or {}
    registry = {instance: _loaded_provider(instance) for instance in loaded_instances}
    registry.update(provider_registry or {})

    def _get_provider(domain: str, return_unavailable: bool = False) -> object | None:
        if domain == "musicbrainz":
            return musicbrainz
        provider = registry.get(domain)
        if provider is None:
            return None
        return provider if (return_unavailable or provider.available) else None

    mass = Mock()
    mass.get_provider = Mock(side_effect=_get_provider)
    ctrl = AlbumsController.__new__(AlbumsController)
    ctrl.logger = logging.getLogger("test.albums.match")
    ctrl.mass = mass
    search = AsyncMock(return_value=list(search_results))
    get_provider_item = AsyncMock(
        side_effect=lambda item_id, _provider, **_kwargs: provider_items[item_id]
    )

    async def _album_tracks(item_id: str, *_rest: object) -> list[Track]:
        result = album_tracks.get(item_id, [])
        if isinstance(result, Exception):
            raise result
        return list(result)

    # a single recorder backs both the base mapping lookup (_get_provider_album_tracks)
    # and the candidate lookup (provider.get_album_tracks), so their calls stay ordered
    get_provider_album_tracks = AsyncMock(side_effect=_album_tracks)
    provider = _provider()
    provider.get_album_tracks = get_provider_album_tracks
    with patch.multiple(
        ctrl,
        search=search,
        get_provider_item=get_provider_item,
        _get_provider_album_tracks=get_provider_album_tracks,
    ):
        yield _Harness(
            ctrl, search, get_provider_item, get_provider_album_tracks, mass.get_provider, provider
        )


# ---------------------------------------------------------------------------
# search-result prefilter
# ---------------------------------------------------------------------------


async def test_clear_no_match_search_result_skips_full_fetch() -> None:
    """A confidently non-matching search result is dropped before any full fetch."""
    other = _album("s1", "spotify_1", name="Takk...")
    with _harness(search_results=[other], provider_items={}) as harness:
        matches = await harness.match(_library_album())

    assert matches == []
    harness.get_provider_item.assert_not_awaited()


async def test_insufficient_search_result_proceeds_to_one_full_fetch() -> None:
    """An ambiguous search result is confirmed against exactly one full provider album."""
    base = _library_album(external_ids={(ExternalID.MB_ALBUM, MB_ALBUM_ID)})
    sparse = _album("s1", "spotify_1", version="Remaster")
    full = _album("s1", "spotify_1", external_ids={(ExternalID.MB_ALBUM, MB_ALBUM_ID)})
    with _harness(search_results=[sparse], provider_items={"s1": full}) as harness:
        matches = await harness.match(base)

    assert [mapping.item_id for mapping in matches] == ["s1"]
    harness.get_provider_item.assert_awaited_once()


async def test_full_item_match_uses_no_track_or_musicbrainz_calls() -> None:
    """A full album that matches on metadata never fetches tracks or hits MusicBrainz."""
    musicbrainz = _mb()
    base = _library_album(external_ids={(ExternalID.MB_ALBUM, MB_ALBUM_ID)})
    sparse = _album("s1", "spotify_1", version="Remaster")
    full = _album("s1", "spotify_1", external_ids={(ExternalID.MB_ALBUM, MB_ALBUM_ID)})
    with _harness(
        search_results=[sparse], provider_items={"s1": full}, musicbrainz=musicbrainz
    ) as harness:
        matches = await harness.match(base)

    assert [mapping.item_id for mapping in matches] == ["s1"]
    harness.get_provider_album_tracks.assert_not_awaited()
    musicbrainz.get_releases_by_barcode.assert_not_awaited()


# ---------------------------------------------------------------------------
# track-fingerprint resolution
# ---------------------------------------------------------------------------


async def test_ambiguous_albums_resolve_via_track_fingerprints() -> None:
    """The 14-track remaster vs deluxe-remaster case matches once tracklists agree."""
    musicbrainz = _mb()
    base = _library_album(version="2022 Remaster")
    sparse = _album("s1", "spotify_1", version="Deluxe 2022 Remaster")
    full = _album("s1", "spotify_1", version="Deluxe 2022 Remaster")
    with _harness(
        search_results=[sparse],
        provider_items={"s1": full},
        provider_album_tracks={"base-prov": _tracklist(14), "s1": _tracklist(14)},
        musicbrainz=musicbrainz,
    ) as harness:
        matches = await harness.match(base)

    assert [mapping.item_id for mapping in matches] == ["s1"]
    # base tracklist comes from the existing provider mapping, candidate from the match target
    assert harness.album_track_calls() == ["base-prov", "s1"]
    # a decisive fingerprint match must not fall through to MusicBrainz
    musicbrainz.get_releases_by_barcode.assert_not_awaited()


async def test_different_track_counts_do_not_map() -> None:
    """A complete 8-track album and a 14-track edition are never merged by fingerprints."""
    base = _library_album(version="2022 Remaster")
    sparse = _album("s1", "spotify_1", version="Deluxe 2022 Remaster")
    full = _album("s1", "spotify_1", version="Deluxe 2022 Remaster")
    with _harness(
        search_results=[sparse],
        provider_items={"s1": full},
        provider_album_tracks={"base-prov": _tracklist(8), "s1": _tracklist(14)},
    ) as harness:
        assert await harness.match(base) == []


async def test_conflicting_isrc_fingerprints_do_not_map() -> None:
    """Same-length tracklists with conflicting ISRCs are a different recording."""
    base = _library_album(version="2022 Remaster")
    sparse = _album("s1", "spotify_1", version="Deluxe 2022 Remaster")
    full = _album("s1", "spotify_1", version="Deluxe 2022 Remaster")
    with _harness(
        search_results=[sparse],
        provider_items={"s1": full},
        provider_album_tracks={
            "base-prov": _tracklist(14, isrc_prefix="USRC17607"),
            "s1": _tracklist(14, isrc_prefix="USRC28718"),
        },
    ) as harness:
        assert await harness.match(base) == []


async def test_base_tracks_fetched_once_across_candidates() -> None:
    """The base tracklist is fetched once even when several candidates need it."""
    base = _library_album(version="2022 Remaster")
    search_results = [
        _album(f"s{index}", "spotify_1", version="Deluxe 2022 Remaster") for index in range(1, 4)
    ]
    provider_items = {album.item_id: album for album in search_results}
    with _harness(
        search_results=search_results,
        provider_items=provider_items,
        # every candidate stays ambiguous (no tracks to compare against)
        provider_album_tracks={"base-prov": _tracklist(14)},
    ) as harness:
        await harness.match(base)

    # the base mapping is only fetched once, regardless of the number of candidates
    assert harness.album_track_calls().count("base-prov") == 1


async def test_base_tracks_use_first_trustworthy_loaded_mapping() -> None:
    """Base resolution deterministically skips unloaded/untrustworthy mappings."""
    base = _library_album(
        version="2022 Remaster",
        mappings=[
            ProviderMapping(item_id="a", provider_domain="aaa", provider_instance="aaa_1"),
            ProviderMapping(item_id="b", provider_domain="bbb", provider_instance="bbb_1"),
            ProviderMapping(item_id="c", provider_domain="ccc", provider_instance="ccc_1"),
        ],
    )
    sparse = _album("s1", "spotify_1", version="Deluxe 2022 Remaster")
    full = _album("s1", "spotify_1", version="Deluxe 2022 Remaster")
    with _harness(
        search_results=[sparse],
        provider_items={"s1": full},
        provider_album_tracks={
            # "a" is on an unloaded instance and must never be fetched
            "a": _tracklist(14),
            # "b" is loaded but its positions are untrustworthy (no disc numbers)
            "b": _tracklist(14, disc_number=0),
            "c": _tracklist(14),
            "s1": _tracklist(14),
        },
        loaded_instances=("bbb_1", "ccc_1", "tidal_1"),
    ) as harness:
        matches = await harness.match(base)

    assert [mapping.item_id for mapping in matches] == ["s1"]
    # unloaded "a" skipped before any fetch; "b" fetched but rejected; "c" used
    assert harness.album_track_calls() == ["b", "c", "s1"]


async def test_base_mapping_not_found_is_skipped() -> None:
    """A base mapping whose tracklist lookup 404s is skipped for the next mapping."""
    base = _library_album(
        version="2022 Remaster",
        mappings=[
            ProviderMapping(item_id="gone", provider_domain="aaa", provider_instance="aaa_1"),
            ProviderMapping(item_id="ok", provider_domain="bbb", provider_instance="bbb_1"),
        ],
    )
    sparse = _album("s1", "spotify_1", version="Deluxe 2022 Remaster")
    full = _album("s1", "spotify_1", version="Deluxe 2022 Remaster")
    with _harness(
        search_results=[sparse],
        provider_items={"s1": full},
        provider_album_tracks={
            "gone": MediaNotFoundError("gone"),
            "ok": _tracklist(14),
            "s1": _tracklist(14),
        },
        loaded_instances=("aaa_1", "bbb_1"),
    ) as harness:
        matches = await harness.match(base)

    assert [mapping.item_id for mapping in matches] == ["s1"]
    assert harness.album_track_calls() == ["gone", "ok", "s1"]


async def test_base_mapping_transient_error_is_skipped() -> None:
    """A transient base tracklist failure skips to the next mapping."""
    base = _library_album(
        version="2022 Remaster",
        mappings=[
            ProviderMapping(item_id="flaky", provider_domain="aaa", provider_instance="aaa_1"),
            ProviderMapping(item_id="ok", provider_domain="bbb", provider_instance="bbb_1"),
        ],
    )
    sparse = _album("s1", "spotify_1", version="Deluxe 2022 Remaster")
    full = _album("s1", "spotify_1", version="Deluxe 2022 Remaster")
    with _harness(
        search_results=[sparse],
        provider_items={"s1": full},
        provider_album_tracks={
            "flaky": RetriesExhausted("flaky"),
            "ok": _tracklist(14),
            "s1": _tracklist(14),
        },
        loaded_instances=("aaa_1", "bbb_1"),
    ) as harness:
        matches = await harness.match(base)

    assert [mapping.item_id for mapping in matches] == ["s1"]
    assert harness.album_track_calls() == ["flaky", "ok", "s1"]


async def test_base_mapping_skipped_when_exact_instance_not_loaded() -> None:
    """A mapping whose exact instance is not loaded is skipped, never a same-domain fallback."""
    base = _library_album(
        version="2022 Remaster",
        mappings=[
            ProviderMapping(item_id="wrong", provider_domain="aaa", provider_instance="aaa_1"),
            ProviderMapping(item_id="ok", provider_domain="bbb", provider_instance="bbb_1"),
        ],
    )
    sparse = _album("s1", "spotify_1", version="Deluxe 2022 Remaster")
    full = _album("s1", "spotify_1", version="Deluxe 2022 Remaster")
    with _harness(
        search_results=[sparse],
        provider_items={"s1": full},
        provider_album_tracks={"wrong": _tracklist(14), "ok": _tracklist(14), "s1": _tracklist(14)},
        loaded_instances=("bbb_1",),
        # aaa_1 resolves (same-domain fallback) to a different instance, so it must be skipped
        provider_registry={"aaa_1": _loaded_provider("other_1")},
    ) as harness:
        matches = await harness.match(base)

    assert [mapping.item_id for mapping in matches] == ["s1"]
    # "wrong" is never fetched because its exact instance isn't the one that resolved
    assert harness.album_track_calls() == ["ok", "s1"]


async def test_candidate_tracklist_not_found_falls_through_to_musicbrainz() -> None:
    """A candidate tracklist that 404s is treated as absent so MusicBrainz can decide."""
    musicbrainz = _mb(
        **{
            BASE_BARCODE: [_mb_release("rel-1", "rg-1")],
            OTHER_BARCODE: [_mb_release("rel-2", "rg-2")],
        }
    )
    base = _library_album(version="2022 Remaster", barcodes=[BASE_BARCODE])
    sparse = _album("s1", "spotify_1", version="Deluxe 2022 Remaster", barcodes=[OTHER_BARCODE])
    full = _album("s1", "spotify_1", version="Deluxe 2022 Remaster", barcodes=[OTHER_BARCODE])
    with _harness(
        search_results=[sparse],
        provider_items={"s1": full},
        provider_album_tracks={"base-prov": _tracklist(14), "s1": MediaNotFoundError("s1")},
        musicbrainz=musicbrainz,
    ) as harness:
        matches = await harness.match(base)

    # MusicBrainz was consulted despite the tracklist failure and its verdict
    # (disjoint release groups) was applied
    assert matches == []
    musicbrainz.get_releases_by_barcode.assert_awaited()


async def test_candidate_tracklist_transient_error_falls_through_to_musicbrainz() -> None:
    """A transient candidate tracklist failure is treated as absent so MusicBrainz can decide."""
    musicbrainz = _mb(
        **{
            BASE_BARCODE: [_mb_release("rel-1", "rg-1")],
            OTHER_BARCODE: [_mb_release("rel-2", "rg-2")],
        }
    )
    base = _library_album(version="2022 Remaster", barcodes=[BASE_BARCODE])
    sparse = _album("s1", "spotify_1", version="Deluxe 2022 Remaster", barcodes=[OTHER_BARCODE])
    full = _album("s1", "spotify_1", version="Deluxe 2022 Remaster", barcodes=[OTHER_BARCODE])
    with _harness(
        search_results=[sparse],
        provider_items={"s1": full},
        provider_album_tracks={"base-prov": _tracklist(14), "s1": TimeoutError()},
        musicbrainz=musicbrainz,
    ) as harness:
        matches = await harness.match(base)

    # MusicBrainz was consulted despite the tracklist failure and its verdict
    # (disjoint release groups) was applied
    assert matches == []
    musicbrainz.get_releases_by_barcode.assert_awaited()


async def test_candidate_tracklist_uses_matched_provider_not_same_domain_fallback() -> None:
    """The candidate tracklist is fetched from the matched provider, never a same-domain fallback."""
    # a second instance of the candidate's domain is registered in the loaded-provider
    # registry; re-resolving the candidate through it would fingerprint against the wrong
    # account and reject the correct match
    fallback = _loaded_provider("spotify_other")
    fallback.get_album_tracks = AsyncMock(return_value=_tracklist(14, isrc_prefix="USRC28718"))
    base = _library_album(version="2022 Remaster")
    sparse = _album("s1", "spotify_1", version="Deluxe 2022 Remaster")
    full = _album("s1", "spotify_1", version="Deluxe 2022 Remaster")
    with _harness(
        search_results=[sparse],
        provider_items={"s1": full},
        provider_album_tracks={"base-prov": _tracklist(14)},
        provider_registry={"spotify_1": fallback},
    ) as harness:
        # the matched provider itself returns the correct, agreeing tracklist
        harness.provider.get_album_tracks = AsyncMock(return_value=_tracklist(14))
        matches = await harness.match(base)

    assert [mapping.item_id for mapping in matches] == ["s1"]
    harness.provider.get_album_tracks.assert_awaited_once_with("s1")
    # the same-domain fallback is never consulted and the candidate domain is never re-resolved
    fallback.get_album_tracks.assert_not_awaited()
    assert all(call.args[0] != "spotify_1" for call in harness.get_provider.call_args_list)


# ---------------------------------------------------------------------------
# MusicBrainz last-resort evidence
# ---------------------------------------------------------------------------


@contextmanager
def _mb_harness(
    musicbrainz: Mock | None,
    *,
    base_barcodes: Sequence[str] = (BASE_BARCODE,),
    compare_barcodes: Sequence[str] = (OTHER_BARCODE,),
) -> Iterator[tuple[_Harness, Album]]:
    """Yield a harness plus base album whose only candidate is ambiguous past fingerprints."""
    base = _library_album(version="2022 Remaster", barcodes=base_barcodes)
    sparse = _album("s1", "spotify_1", version="Deluxe 2022 Remaster", barcodes=compare_barcodes)
    full = _album("s1", "spotify_1", version="Deluxe 2022 Remaster", barcodes=compare_barcodes)
    with _harness(
        search_results=[sparse],
        provider_items={"s1": full},
        # base tracklist present but candidate has none: fingerprint stays inconclusive
        provider_album_tracks={"base-prov": _tracklist(14), "s1": []},
        musicbrainz=musicbrainz,
    ) as harness:
        yield harness, base


async def test_shared_barcode_resolves_ambiguity_without_musicbrainz() -> None:
    """A shared barcode resolves an ambiguous edition wording without any lookups."""
    musicbrainz = _mb()
    with _mb_harness(musicbrainz, compare_barcodes=(BASE_BARCODE,)) as (harness, base):
        matches = await harness.match(base)

    assert [mapping.item_id for mapping in matches] == ["s1"]
    musicbrainz.get_releases_by_barcode.assert_not_awaited()


async def test_musicbrainz_multiple_releases_per_barcode_abstains() -> None:
    """A barcode resolving to several releases is ambiguous and must not match."""
    musicbrainz = _mb(
        **{
            BASE_BARCODE: [_mb_release("rel-1", "rg-1"), _mb_release("rel-2", "rg-1")],
            OTHER_BARCODE: [_mb_release("rel-3", "rg-1")],
        }
    )
    with _mb_harness(musicbrainz) as (harness, base):
        assert await harness.match(base) == []


async def test_musicbrainz_multiple_barcodes_are_all_considered() -> None:
    """Every canonical barcode on each album is used, not just the first."""
    musicbrainz = _mb(
        **{
            BASE_BARCODE: [_mb_release("rel-1", "rg-1")],
            THIRD_BARCODE: [_mb_release("rel-9", "rg-9")],
            OTHER_BARCODE: [_mb_release("rel-2", "rg-9")],
        }
    )
    with _mb_harness(
        musicbrainz, base_barcodes=(BASE_BARCODE, THIRD_BARCODE), compare_barcodes=(OTHER_BARCODE,)
    ) as (harness, base):
        assert await harness.match(base) == []
    queried = {call.args[0] for call in musicbrainz.get_releases_by_barcode.await_args_list}
    assert queried == {BASE_BARCODE, OTHER_BARCODE, THIRD_BARCODE}


async def test_musicbrainz_shared_release_group_alone_does_not_match() -> None:
    """Different specific releases in one release group must not identify an edition."""
    musicbrainz = _mb(
        **{
            BASE_BARCODE: [_mb_release("rel-1", "rg-9")],
            OTHER_BARCODE: [_mb_release("rel-2", "rg-9")],
        }
    )
    with _mb_harness(musicbrainz, compare_barcodes=(OTHER_BARCODE,)) as (harness, base):
        assert await harness.match(base) == []


async def test_musicbrainz_disjoint_release_groups_do_not_match() -> None:
    """Barcodes in entirely different release groups are negative evidence."""
    musicbrainz = _mb(
        **{
            BASE_BARCODE: [_mb_release("rel-1", "rg-1")],
            OTHER_BARCODE: [_mb_release("rel-2", "rg-2")],
        }
    )
    with _mb_harness(musicbrainz, compare_barcodes=(OTHER_BARCODE,)) as (harness, base):
        assert await harness.match(base) == []


async def test_musicbrainz_unresolved_barcode_abstains() -> None:
    """A barcode MusicBrainz cannot resolve abstains instead of guessing."""
    with _mb_harness(_mb()) as (harness, base):
        assert await harness.match(base) == []


async def test_musicbrainz_transport_error_abstains() -> None:
    """A MusicBrainz outage abstains rather than aborting the whole match."""
    musicbrainz = Mock()
    musicbrainz.get_releases_by_barcode = AsyncMock(side_effect=TimeoutError())
    with _mb_harness(musicbrainz) as (harness, base):
        assert await harness.match(base) == []


async def test_musicbrainz_skipped_without_a_configured_provider() -> None:
    """With no MusicBrainz provider configured the match simply abstains."""
    with _mb_harness(None) as (harness, base):
        assert await harness.match(base) == []


def _mb_evidence_ctrl(musicbrainz: Mock) -> AlbumsController:
    """Return a bare controller wired to a mock MusicBrainz provider."""
    mass = Mock()
    mass.get_provider = Mock(
        side_effect=lambda domain: musicbrainz if domain == "musicbrainz" else None
    )
    ctrl = AlbumsController.__new__(AlbumsController)
    ctrl.logger = logging.getLogger("test.albums.mb")
    ctrl.mass = mass
    return ctrl


async def test_musicbrainz_evidence_shared_single_release_matches() -> None:
    """A shared barcode resolving to exactly one specific release is positive evidence."""
    musicbrainz = _mb(**{BASE_BARCODE: [_mb_release("rel-1", "rg-1")]})
    ctrl = _mb_evidence_ctrl(musicbrainz)
    base = _library_album(barcodes=(BASE_BARCODE,))
    compare = _album("s1", "spotify_1", barcodes=(BASE_BARCODE,))

    assert await ctrl._musicbrainz_album_evidence(base, compare) == AlbumMatchEvidence.MATCH


async def test_musicbrainz_evidence_ambiguous_barcode_is_not_release_identity() -> None:
    """A shared barcode resolving to several releases never identifies a specific release."""
    musicbrainz = _mb(
        **{BASE_BARCODE: [_mb_release("rel-1", "rg-1"), _mb_release("rel-2", "rg-1")]}
    )
    ctrl = _mb_evidence_ctrl(musicbrainz)
    base = _library_album(barcodes=(BASE_BARCODE,))
    compare = _album("s1", "spotify_1", barcodes=(BASE_BARCODE,))

    assert await ctrl._musicbrainz_album_evidence(base, compare) == AlbumMatchEvidence.INSUFFICIENT


async def test_musicbrainz_evidence_all_resolved_disjoint_groups_reject() -> None:
    """Disjoint release groups are negative evidence when every barcode resolved."""
    musicbrainz = _mb(
        **{
            BASE_BARCODE: [_mb_release("rel-1", "rg-1")],
            OTHER_BARCODE: [_mb_release("rel-2", "rg-2")],
        }
    )
    ctrl = _mb_evidence_ctrl(musicbrainz)
    base = _library_album(barcodes=(BASE_BARCODE,))
    compare = _album("s1", "spotify_1", barcodes=(OTHER_BARCODE,))

    assert await ctrl._musicbrainz_album_evidence(base, compare) == AlbumMatchEvidence.NO_MATCH


async def test_musicbrainz_evidence_unresolved_barcode_blocks_disjoint_rejection() -> None:
    """An unresolved barcode leaves the group sets incomplete, so it cannot reject."""
    musicbrainz = _mb(
        **{
            # BASE_BARCODE stays unresolved ([]); THIRD_BARCODE resolves to a distinct group
            THIRD_BARCODE: [_mb_release("rel-1", "rg-1")],
            OTHER_BARCODE: [_mb_release("rel-2", "rg-2")],
        }
    )
    ctrl = _mb_evidence_ctrl(musicbrainz)
    base = _library_album(barcodes=(BASE_BARCODE, THIRD_BARCODE))
    compare = _album("s1", "spotify_1", barcodes=(OTHER_BARCODE,))

    assert await ctrl._musicbrainz_album_evidence(base, compare) == AlbumMatchEvidence.INSUFFICIENT


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
    item = _album("a1", "spotify_1", barcodes=[BASE_BARCODE])

    with patch.multiple(
        ctrl,
        # every DB lookup returns nothing; a real provider/MusicBrainz call would raise
        get_library_item_by_prov_id=AsyncMock(return_value=None),
        get_library_item_by_prov_mappings=AsyncMock(return_value=None),
        get_library_items_by_external_id=AsyncMock(return_value=[]),
        get_library_items_by_query=AsyncMock(return_value=[]),
        search=AsyncMock(side_effect=AssertionError("search during sync")),
        get_provider_item=AsyncMock(side_effect=AssertionError("provider fetch during sync")),
        _get_provider_album_tracks=AsyncMock(side_effect=AssertionError("track fetch during sync")),
    ):
        assert await ctrl._get_library_item_by_match(item) is None

    mass.get_provider.assert_not_called()
