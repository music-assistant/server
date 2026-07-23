"""Tests for MusicBrainz recommendations (birthdays and memorials)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.enums import ExternalID
from music_assistant_models.media_items import (
    Artist,
    MediaItemMetadata,
    ProviderMapping,
)

from music_assistant.providers.musicbrainz.models import (
    MusicBrainzArtist,
    MusicBrainzLifeSpan,
)
from music_assistant.providers.musicbrainz.recommendations import (
    RECOMMENDATIONS_CACHE_KEY,
    MusicBrainzRecommendationManager,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_artist(
    item_id: str,
    name: str,
    mbid: str | None = None,
    genres: set[str] | None = None,
) -> Artist:
    """Return a minimal library Artist, optionally with an MB external-ID."""
    pm = ProviderMapping(
        item_id=item_id,
        provider_domain="test",
        provider_instance="test",
    )
    artist = Artist(item_id=item_id, provider="test", name=name, provider_mappings={pm})
    if mbid:
        artist.add_external_id(ExternalID.MB_ARTIST, mbid)
    if genres is not None:
        artist.metadata = MediaItemMetadata(genres=genres)
    return artist


def _make_mb_artist(
    mbid: str, begin: str | None, end: str | None = None, ended: bool = False
) -> MusicBrainzArtist:
    """Return a MusicBrainzArtist with the given life-span."""
    life_span = MusicBrainzLifeSpan(begin=begin, end=end, ended=ended) if (begin or end) else None
    return MusicBrainzArtist(id=mbid, name="stub", sort_name="stub", life_span=life_span)


async def _async_iter(items: Sequence[object]) -> AsyncIterator[object]:
    """Yield items from a list as an async iterator."""
    for item in items:
        yield item


def _today_mmdd() -> str:
    """Return today's MM-DD string in UTC."""
    return _mmdd_for_offset(0)


def _mmdd_for_offset(offset: int) -> str:
    """Return the MM-DD string for today + offset days (UTC)."""
    target = datetime.now(UTC).date() + timedelta(days=offset)
    return f"{target.month:02d}-{target.day:02d}"


def _match_entry(kind: str, offset: int, artist: Artist) -> dict[str, object]:
    """Return a cached-match dict as stored by _refresh."""
    return {"kind": kind, "mmdd": _mmdd_for_offset(offset), "artist": artist.to_dict()}


def _set_library(provider_mock: Mock, artists: list[Artist]) -> None:
    """Wire the library artist iterator on the provider mock."""
    provider_mock.mass.music.artists.iter_library_items = Mock(return_value=_async_iter(artists))


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def provider_mock() -> Mock:
    """Return a minimal MusicbrainzProvider mock."""
    provider = Mock()
    provider.instance_id = "musicbrainz"
    provider.logger = Mock()
    provider.config.get_value = Mock(return_value=3)
    provider.mass = Mock()
    provider.mass.create_task = Mock()
    provider.mass.call_later = Mock()
    return provider


@pytest.fixture
def manager(provider_mock: Mock) -> MusicBrainzRecommendationManager:
    """Return a MusicBrainzRecommendationManager backed by the mock provider."""
    return MusicBrainzRecommendationManager(provider_mock)


# ---------------------------------------------------------------------------
# _scan_matches — birthdays
# ---------------------------------------------------------------------------


async def test_scan_matches_birthday(
    manager: MusicBrainzRecommendationManager,
    provider_mock: Mock,
) -> None:
    """Only artists whose full birth date matches a window MM-DD are returned."""
    today_mmdd = _today_mmdd()
    mbid_match = "20ff3303-4fe2-4a47-a1b6-291e26aa3438"
    mbid_no_match = "f59c5520-5f46-4d2c-b2c4-822eabf53419"
    mbid_year_only = "c3c82bdc-d9e7-4836-9746-c24ead47ca19"

    _set_library(
        provider_mock,
        [
            _make_artist("1", "Birthday Artist", mbid=mbid_match),
            _make_artist("2", "Other Artist", mbid=mbid_no_match),
            _make_artist("3", "Year-Only Artist", mbid=mbid_year_only),
        ],
    )
    far_mmdd = _mmdd_for_offset(180)

    async def fake_get_artist_details(mbid: str) -> MusicBrainzArtist:
        dates = {
            mbid_match: f"1980-{today_mmdd}",
            mbid_no_match: f"1975-{far_mmdd}",
            mbid_year_only: "1990",  # partial date, must be skipped
        }
        return _make_mb_artist(mbid, dates[mbid])

    provider_mock.get_artist_details = fake_get_artist_details

    matches = await manager._scan_matches()

    assert [(kind, mmdd, a.name) for kind, mmdd, a in matches] == [
        ("birthday", today_mmdd, "Birthday Artist")
    ]


async def test_scan_matches_no_life_span(
    manager: MusicBrainzRecommendationManager,
    provider_mock: Mock,
) -> None:
    """Artists with no life_span are silently skipped."""
    mbid = "89ad4ac3-39f7-470e-963a-56509c546377"
    _set_library(provider_mock, [_make_artist("1", "No Lifespan", mbid=mbid)])
    provider_mock.get_artist_details = AsyncMock(return_value=_make_mb_artist(mbid, None))

    assert await manager._scan_matches() == []


async def test_scan_matches_api_error_skipped(
    manager: MusicBrainzRecommendationManager,
    provider_mock: Mock,
) -> None:
    """API errors for individual artists are swallowed; remaining artists still checked."""
    today_mmdd = _today_mmdd()
    mbid_error = "20ff3303-4fe2-4a47-a1b6-291e26aa3438"
    mbid_ok = "f59c5520-5f46-4d2c-b2c4-822eabf53419"
    _set_library(
        provider_mock,
        [
            _make_artist("1", "Error Artist", mbid=mbid_error),
            _make_artist("2", "OK Artist", mbid=mbid_ok),
        ],
    )

    async def fake_get_artist_details(mbid: str) -> MusicBrainzArtist:
        if mbid == mbid_error:
            msg = "MB API unavailable"
            raise RuntimeError(msg)
        return _make_mb_artist(mbid, f"1985-{today_mmdd}")

    provider_mock.get_artist_details = fake_get_artist_details

    matches = await manager._scan_matches()
    assert [a.name for _, _, a in matches] == ["OK Artist"]


# ---------------------------------------------------------------------------
# _scan_matches — in memoriam
# ---------------------------------------------------------------------------


async def test_scan_matches_memoriam(
    manager: MusicBrainzRecommendationManager,
    provider_mock: Mock,
) -> None:
    """Artists who passed away on a window date (ended=True) are returned as memoriam."""
    today_mmdd = _today_mmdd()
    mbid = "20ff3303-4fe2-4a47-a1b6-291e26aa3438"
    _set_library(provider_mock, [_make_artist("1", "Late Artist", mbid=mbid)])
    provider_mock.get_artist_details = AsyncMock(
        return_value=_make_mb_artist(mbid, begin="1933-01-01", end=f"2006-{today_mmdd}", ended=True)
    )

    matches = await manager._scan_matches()
    assert [(kind, a.name) for kind, _, a in matches] == [("memoriam", "Late Artist")]


async def test_scan_matches_skips_living_artists(
    manager: MusicBrainzRecommendationManager,
    provider_mock: Mock,
) -> None:
    """Artists where ended=False are not included even if end date matches."""
    today_mmdd = _today_mmdd()
    mbid = "f59c5520-5f46-4d2c-b2c4-822eabf53419"
    _set_library(provider_mock, [_make_artist("1", "Living Artist", mbid=mbid)])
    # ended=False simulates a band that broke up but members are alive
    provider_mock.get_artist_details = AsyncMock(
        return_value=_make_mb_artist(
            mbid, begin="1996-01-01", end=f"2023-{today_mmdd}", ended=False
        )
    )

    assert await manager._scan_matches() == []


# ---------------------------------------------------------------------------
# _scan_matches — empty / no match
# ---------------------------------------------------------------------------


async def test_scan_matches_empty_library(
    manager: MusicBrainzRecommendationManager,
    provider_mock: Mock,
) -> None:
    """Empty library returns no matches."""
    _set_library(provider_mock, [])
    assert await manager._scan_matches() == []


async def test_scan_matches_no_mbid_artists_skipped(
    manager: MusicBrainzRecommendationManager,
    provider_mock: Mock,
) -> None:
    """Library artists without an MBID are not looked up and produce no matches."""
    _set_library(provider_mock, [_make_artist("1", "No MBID Artist")])
    provider_mock.get_artist_details = AsyncMock(side_effect=AssertionError("should not be called"))
    assert await manager._scan_matches() == []


async def test_scan_matches_out_of_window(
    manager: MusicBrainzRecommendationManager,
    provider_mock: Mock,
) -> None:
    """A birth date outside the +/- window is not matched."""
    mbid = "c3c82bdc-d9e7-4836-9746-c24ead47ca19"
    _set_library(provider_mock, [_make_artist("1", "Wrong Birthday", mbid=mbid)])
    provider_mock.get_artist_details = AsyncMock(
        return_value=_make_mb_artist(mbid, f"1985-{_mmdd_for_offset(180)}")
    )

    assert await manager._scan_matches() == []


# ---------------------------------------------------------------------------
# _folders_from_matches — labelling for the current day
# ---------------------------------------------------------------------------


def test_folders_from_matches_today_birthday(
    manager: MusicBrainzRecommendationManager,
) -> None:
    """A birthday match for today produces a folder labelled for today."""
    matches = [_match_entry("birthday", 0, _make_artist("1", "Star"))]
    folders = manager._folders_from_matches(matches)

    assert len(folders) == 1
    assert folders[0].translation_key == "artist_birthdays_today"
    assert folders[0].translation_params is None
    assert [a.name for a in folders[0].items] == ["Star"]


def test_folders_from_matches_future_offset(
    manager: MusicBrainzRecommendationManager,
) -> None:
    """A match two days out is labelled with the in-N-days key and the day count param."""
    matches = [_match_entry("birthday", 2, _make_artist("1", "Future Star"))]
    folders = manager._folders_from_matches(matches)

    assert len(folders) == 1
    assert folders[0].translation_key == "artist_birthdays_in_n_days"
    assert folders[0].translation_params == ["2"]


def test_folders_from_matches_memoriam(
    manager: MusicBrainzRecommendationManager,
) -> None:
    """A memoriam match for today produces a memoriam folder."""
    matches = [_match_entry("memoriam", 0, _make_artist("1", "Late Star"))]
    folders = manager._folders_from_matches(matches)

    assert len(folders) == 1
    assert folders[0].translation_key == "artist_memoriam_today"
    assert [a.name for a in folders[0].items] == ["Late Star"]


def test_folders_from_matches_out_of_window_dropped(
    manager: MusicBrainzRecommendationManager,
) -> None:
    """Stale matches whose date is no longer in the current window are dropped."""
    far = datetime.now(UTC).date() + timedelta(days=180)
    matches = [
        {
            "kind": "birthday",
            "mmdd": f"{far.month:02d}-{far.day:02d}",
            "artist": _make_artist("1", "Stale Star").to_dict(),
        }
    ]
    assert manager._folders_from_matches(matches) == []


# ---------------------------------------------------------------------------
# _build_artist_folders
# ---------------------------------------------------------------------------


def test_build_artist_folders_returns_single_folder(
    manager: MusicBrainzRecommendationManager,
) -> None:
    """All artists are collected into a single folder regardless of genre."""
    rock_artist = _make_artist("1", "Rock Artist", genres={"Rock"})
    jazz_artist = _make_artist("2", "Jazz Artist", genres={"Jazz"})
    no_genre = _make_artist("3", "No Genre Artist")

    folders = manager._build_artist_folders(
        [rock_artist, jazz_artist, no_genre],
        folder_id_prefix="birthdays",
        translation_key="artist_birthdays_day",
        translation_params=["today"],
        icon="mdi-cake-variant",
    )

    assert len(folders) == 1
    assert folders[0].item_id == "birthdays"
    assert folders[0].translation_key == "artist_birthdays_day"
    assert folders[0].translation_params == ["today"]
    assert len(folders[0].items) == 3


def test_build_artist_folders_empty_returns_empty(
    manager: MusicBrainzRecommendationManager,
) -> None:
    """Empty artist list returns empty folder list."""
    assert (
        manager._build_artist_folders(
            [],
            folder_id_prefix="birthdays",
            translation_key="artist_birthdays_day",
            translation_params=["today"],
            icon="mdi-cake-variant",
        )
        == []
    )


# ---------------------------------------------------------------------------
# get_recommendations — rows without items (stale-while-revalidate cache read)
# ---------------------------------------------------------------------------


async def test_get_recommendations_returns_rows_without_items(
    manager: MusicBrainzRecommendationManager,
    provider_mock: Mock,
) -> None:
    """A fresh cache hit yields row descriptors with empty items and zero MB API calls."""
    matches = [
        _match_entry("memoriam", -1, _make_artist("1", "Late Star")),
        _match_entry("birthday", 0, _make_artist("2", "Birthday Star")),
        _match_entry("memoriam", 0, _make_artist("3", "Other Late Star")),
        _match_entry("birthday", 2, _make_artist("4", "Future Star")),
    ]
    provider_mock.mass.cache.get = AsyncMock(return_value=matches)
    provider_mock.get_artist_details = AsyncMock(side_effect=AssertionError("no backend calls"))

    rows = await manager.get_recommendations()

    assert [row.item_id for row in rows] == [
        "memoriam_-1",
        "birthdays_0",
        "memoriam_0",
        "birthdays_2",
    ]
    assert rows[1].translation_key == "artist_birthdays_today"
    assert rows[1].icon == "mdi-cake-variant"
    assert rows[0].icon == "mdi-candle"
    assert all(row.items == [] for row in rows)
    provider_mock.mass.cache.get.assert_awaited_once()
    provider_mock.mass.create_task.assert_not_called()


async def test_get_recommendations_serves_stale_and_schedules(
    manager: MusicBrainzRecommendationManager,
    provider_mock: Mock,
) -> None:
    """When nothing fresh is cached, stale rows are served and a refresh is scheduled."""
    matches = [_match_entry("birthday", 0, _make_artist("1", "Stale-but-shown"))]
    # first (fresh) lookup misses, second (allow_expired) returns stale data
    provider_mock.mass.cache.get = AsyncMock(side_effect=[None, matches])

    rows = await manager.get_recommendations()

    assert [row.item_id for row in rows] == ["birthdays_0"]
    assert rows[0].items == []
    provider_mock.mass.create_task.assert_called_once()
    assert provider_mock.mass.cache.get.await_count == 2


async def test_get_recommendations_empty_when_no_cache(
    manager: MusicBrainzRecommendationManager,
    provider_mock: Mock,
) -> None:
    """With no cached data at all, the rows call returns empty and schedules a refresh."""
    provider_mock.mass.cache.get = AsyncMock(return_value=None)

    result = await manager.get_recommendations()

    assert result == []
    provider_mock.mass.create_task.assert_called_once()


# ---------------------------------------------------------------------------
# get_recommendation_items — one row's items from the cached scan
# ---------------------------------------------------------------------------


async def test_get_recommendation_items_returns_matching_row(
    manager: MusicBrainzRecommendationManager,
    provider_mock: Mock,
) -> None:
    """Items for one row come from the cached scan; other rows' artists are excluded."""
    matches = [
        _match_entry("birthday", 0, _make_artist("1", "Birthday Star")),
        _match_entry("memoriam", 0, _make_artist("2", "Late Star")),
    ]
    provider_mock.mass.cache.get = AsyncMock(return_value=matches)
    provider_mock.get_artist_details = AsyncMock(side_effect=AssertionError("no backend calls"))

    items = await manager.get_recommendation_items("birthdays_0")

    assert [a.name for a in items] == ["Birthday Star"]
    provider_mock.mass.create_task.assert_not_called()


async def test_get_recommendation_items_unknown_id_empty(
    manager: MusicBrainzRecommendationManager,
    provider_mock: Mock,
) -> None:
    """An unknown row id returns an empty list."""
    matches = [_match_entry("birthday", 0, _make_artist("1", "Cached"))]
    provider_mock.mass.cache.get = AsyncMock(return_value=matches)

    assert await manager.get_recommendation_items("birthdays_5") == []


# ---------------------------------------------------------------------------
# _refresh — background compute + cache write
# ---------------------------------------------------------------------------


async def test_refresh_scans_and_caches(
    manager: MusicBrainzRecommendationManager,
    provider_mock: Mock,
) -> None:
    """_refresh scans the library and stores serialized matches with stale fallback enabled."""
    today_mmdd = _today_mmdd()
    mbid = "20ff3303-4fe2-4a47-a1b6-291e26aa3438"
    _set_library(provider_mock, [_make_artist("10", "Birthday Star", mbid=mbid)])
    provider_mock.get_artist_details = AsyncMock(
        return_value=_make_mb_artist(mbid, f"1990-{today_mmdd}")
    )
    provider_mock.mass.cache.set = AsyncMock()

    await manager._refresh()

    provider_mock.mass.cache.set.assert_awaited_once()
    args, kwargs = provider_mock.mass.cache.set.call_args
    assert args[0] == RECOMMENDATIONS_CACHE_KEY
    stored = args[1]
    assert isinstance(stored, list)
    assert stored[0]["kind"] == "birthday"
    assert stored[0]["mmdd"] == today_mmdd
    assert stored[0]["artist"]["name"] == "Birthday Star"
    assert kwargs["provider"] == "musicbrainz"
    assert kwargs["allow_expired_cache"] is True


async def test_refresh_swallows_compute_errors(
    manager: MusicBrainzRecommendationManager,
    provider_mock: Mock,
) -> None:
    """A failure during the scan does not write the cache or raise."""
    provider_mock.mass.music.artists.iter_library_items = Mock(
        side_effect=RuntimeError("library unavailable")
    )
    provider_mock.mass.cache.set = AsyncMock()

    await manager._refresh()

    provider_mock.mass.cache.set.assert_not_awaited()
