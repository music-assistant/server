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
    today = datetime.now(UTC).date()
    return f"{today.month:02d}-{today.day:02d}"


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
# _compute_folders — birthdays
# ---------------------------------------------------------------------------


async def test_compute_folders_birthday_match(
    manager: MusicBrainzRecommendationManager,
    provider_mock: Mock,
) -> None:
    """Only artists whose full birth date matches today's MM-DD produce a folder."""
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

    async def fake_get_artist_details(mbid: str) -> MusicBrainzArtist:
        dates = {
            mbid_match: f"1980-{today_mmdd}",
            mbid_no_match: "1975-01-15",
            mbid_year_only: "1990",  # partial date, must be skipped
        }
        return _make_mb_artist(mbid, dates[mbid])

    provider_mock.get_artist_details = fake_get_artist_details

    folders = await manager._compute_folders()

    birthday_folders = [f for f in folders if "birthdays" in (f.translation_key or "")]
    assert len(birthday_folders) == 1
    assert birthday_folders[0].translation_key == "artist_birthdays_today"
    assert birthday_folders[0].translation_params is None
    assert [a.name for a in birthday_folders[0].items] == ["Birthday Artist"]


async def test_compute_folders_no_life_span(
    manager: MusicBrainzRecommendationManager,
    provider_mock: Mock,
) -> None:
    """Artists with no life_span are silently skipped."""
    mbid = "89ad4ac3-39f7-470e-963a-56509c546377"
    _set_library(provider_mock, [_make_artist("1", "No Lifespan", mbid=mbid)])
    provider_mock.get_artist_details = AsyncMock(return_value=_make_mb_artist(mbid, None))

    assert await manager._compute_folders() == []


async def test_compute_folders_api_error_skipped(
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

    folders = await manager._compute_folders()
    birthday_folders = [f for f in folders if "birthdays" in (f.translation_key or "")]
    assert len(birthday_folders) == 1
    assert [a.name for a in birthday_folders[0].items] == ["OK Artist"]


# ---------------------------------------------------------------------------
# _compute_folders — in memoriam
# ---------------------------------------------------------------------------


async def test_compute_folders_memoriam_match(
    manager: MusicBrainzRecommendationManager,
    provider_mock: Mock,
) -> None:
    """Artists who passed away on today's date (ended=True) produce a memoriam folder."""
    today_mmdd = _today_mmdd()
    mbid = "20ff3303-4fe2-4a47-a1b6-291e26aa3438"
    _set_library(provider_mock, [_make_artist("1", "Late Artist", mbid=mbid)])
    provider_mock.get_artist_details = AsyncMock(
        return_value=_make_mb_artist(mbid, begin="1933-01-01", end=f"2006-{today_mmdd}", ended=True)
    )

    folders = await manager._compute_folders()
    memoriam_folders = [f for f in folders if "memoriam" in (f.translation_key or "")]
    assert len(memoriam_folders) == 1
    assert memoriam_folders[0].translation_key == "artist_memoriam_today"
    assert [a.name for a in memoriam_folders[0].items] == ["Late Artist"]


async def test_compute_folders_skips_living_artists(
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

    assert await manager._compute_folders() == []


# ---------------------------------------------------------------------------
# _compute_folders — empty / no match
# ---------------------------------------------------------------------------


async def test_compute_folders_empty_library(
    manager: MusicBrainzRecommendationManager,
    provider_mock: Mock,
) -> None:
    """Empty library returns no folders."""
    _set_library(provider_mock, [])
    assert await manager._compute_folders() == []


async def test_compute_folders_no_mbid_artists_skipped(
    manager: MusicBrainzRecommendationManager,
    provider_mock: Mock,
) -> None:
    """Library artists without an MBID are not looked up and produce no folders."""
    _set_library(provider_mock, [_make_artist("1", "No MBID Artist")])
    provider_mock.get_artist_details = AsyncMock(side_effect=AssertionError("should not be called"))
    assert await manager._compute_folders() == []


async def test_compute_folders_no_birthday_match(
    manager: MusicBrainzRecommendationManager,
    provider_mock: Mock,
) -> None:
    """Library artist with MBID but a non-window birth date produces no folders."""
    mbid = "c3c82bdc-d9e7-4836-9746-c24ead47ca19"
    _set_library(provider_mock, [_make_artist("1", "Wrong Birthday", mbid=mbid)])
    # a date ~6 months out is always outside the +/- window (max 15 days)
    far = datetime.now(UTC).date() + timedelta(days=180)
    other_mmdd = f"{far.month:02d}-{far.day:02d}"
    provider_mock.get_artist_details = AsyncMock(
        return_value=_make_mb_artist(mbid, f"1985-{other_mmdd}")
    )

    assert await manager._compute_folders() == []


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
# get_recommendations — cache-backed hot path
# ---------------------------------------------------------------------------


async def test_get_recommendations_returns_cached(
    manager: MusicBrainzRecommendationManager,
    provider_mock: Mock,
) -> None:
    """A cached result is returned as-is without triggering a background refresh."""
    cached = manager._build_artist_folders(
        [_make_artist("1", "Cached Artist")],
        folder_id_prefix="birthdays_0",
        translation_key="artist_birthdays_today",
        icon="mdi-cake-variant",
    )
    provider_mock.mass.cache.get = AsyncMock(return_value=cached)

    result = await manager.get_recommendations()

    assert result == cached
    provider_mock.mass.cache.get.assert_awaited_once()
    provider_mock.mass.create_task.assert_not_called()


async def test_get_recommendations_schedules_refresh_on_miss(
    manager: MusicBrainzRecommendationManager,
    provider_mock: Mock,
) -> None:
    """On a cache miss the hot path returns empty and schedules a background refresh."""
    provider_mock.mass.cache.get = AsyncMock(return_value=None)

    result = await manager.get_recommendations()

    assert result == []
    provider_mock.mass.create_task.assert_called_once()


# ---------------------------------------------------------------------------
# _refresh — background compute + cache write
# ---------------------------------------------------------------------------


async def test_refresh_computes_caches_and_rearms(
    manager: MusicBrainzRecommendationManager,
    provider_mock: Mock,
) -> None:
    """_refresh scans the library, stores serialized folders, and schedules the next run."""
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
    # stored payload is a JSON-serializable list of folder dicts
    stored = args[1]
    assert isinstance(stored, list)
    assert stored
    assert isinstance(stored[0], dict)
    assert kwargs["provider"] == "musicbrainz"
    # next-day refresh re-armed
    provider_mock.mass.call_later.assert_called_once()


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
