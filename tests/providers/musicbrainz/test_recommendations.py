"""Tests for MusicBrainz recommendations (birthdays and memorials)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.enums import ExternalID
from music_assistant_models.media_items import (
    Album,
    Artist,
    MediaItemMetadata,
    ProviderMapping,
)

from music_assistant.providers.musicbrainz.models import (
    MusicBrainzArtist,
    MusicBrainzLifeSpan,
    MusicBrainzReleaseGroup,
)
from music_assistant.providers.musicbrainz.recommendations import MusicBrainzRecommendationManager

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


def _make_album(
    item_id: str,
    name: str,
    mb_rg_id: str | None = None,
    year: int | None = None,
) -> Album:
    """Return a minimal library Album, optionally with a MB release-group external-ID."""
    pm = ProviderMapping(
        item_id=item_id,
        provider_domain="test",
        provider_instance="test",
    )
    album = Album(item_id=item_id, provider="test", name=name, provider_mappings={pm})
    if mb_rg_id:
        album.add_external_id(ExternalID.MB_RELEASEGROUP, mb_rg_id)
    if year is not None:
        album.year = year
    return album


def _make_mb_artist(
    mbid: str, begin: str | None, end: str | None = None, ended: bool = False
) -> MusicBrainzArtist:
    """Return a MusicBrainzArtist with the given life-span."""
    life_span = MusicBrainzLifeSpan(begin=begin, end=end, ended=ended) if (begin or end) else None
    return MusicBrainzArtist(id=mbid, name="stub", sort_name="stub", life_span=life_span)


def _make_mb_release_group(
    rg_id: str, title: str, first_release_date: str | None
) -> MusicBrainzReleaseGroup:
    """Return a MusicBrainzReleaseGroup with the given first release date."""
    return MusicBrainzReleaseGroup(id=rg_id, title=title, first_release_date=first_release_date)


async def _async_iter(items: list[object]) -> AsyncIterator[object]:
    """Yield items from a list as an async iterator."""
    for item in items:
        yield item


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
    return provider


@pytest.fixture
def manager(provider_mock: Mock) -> MusicBrainzRecommendationManager:
    """Return a MusicBrainzRecommendationManager backed by the mock provider."""
    return MusicBrainzRecommendationManager(provider_mock)


# ---------------------------------------------------------------------------
# _find_artist_mbids_by_date — begin (birthday)
# ---------------------------------------------------------------------------


async def test_find_birthday_mbids_matches_exact_date(
    manager: MusicBrainzRecommendationManager,
    provider_mock: Mock,
) -> None:
    """Artists whose birth date matches today's MM-DD are returned."""
    today = datetime.now(UTC).date()
    today_mmdd = f"{today.month:02d}-{today.day:02d}"

    mbid_match = "20ff3303-4fe2-4a47-a1b6-291e26aa3438"
    mbid_no_match = "f59c5520-5f46-4d2c-b2c4-822eabf53419"
    mbid_year_only = "c3c82bdc-d9e7-4836-9746-c24ead47ca19"

    mbid_to_artist = {
        mbid_match: _make_artist("1", "Birthday Artist", mbid=mbid_match),
        mbid_no_match: _make_artist("2", "Other Artist", mbid=mbid_no_match),
        mbid_year_only: _make_artist("3", "Year-Only Artist", mbid=mbid_year_only),
    }

    async def fake_get_artist_details(mbid: str) -> MusicBrainzArtist:
        match_date = f"1980-{today_mmdd}"
        dates = {
            mbid_match: match_date,
            mbid_no_match: "1975-01-15",
            mbid_year_only: "1990",
        }
        return _make_mb_artist(mbid, dates[mbid])

    provider_mock.get_artist_details = fake_get_artist_details

    result = await manager._find_artist_mbids_by_date(
        mbid_to_artist, today_mmdd, date_field="begin"
    )

    assert result == [mbid_match]


async def test_find_birthday_mbids_no_life_span(
    manager: MusicBrainzRecommendationManager,
    provider_mock: Mock,
) -> None:
    """Artists with no life_span are silently skipped."""
    today = datetime.now(UTC).date()
    today_mmdd = f"{today.month:02d}-{today.day:02d}"

    mbid = "89ad4ac3-39f7-470e-963a-56509c546377"
    mbid_to_artist = {mbid: _make_artist("1", "No Lifespan", mbid=mbid)}

    provider_mock.get_artist_details = AsyncMock(return_value=_make_mb_artist(mbid, None))

    result = await manager._find_artist_mbids_by_date(
        mbid_to_artist, today_mmdd, date_field="begin"
    )

    assert result == []


async def test_find_birthday_mbids_api_error_skipped(
    manager: MusicBrainzRecommendationManager,
    provider_mock: Mock,
) -> None:
    """API errors for individual artists are swallowed; remaining artists still checked."""
    today = datetime.now(UTC).date()
    today_mmdd = f"{today.month:02d}-{today.day:02d}"

    mbid_error = "20ff3303-4fe2-4a47-a1b6-291e26aa3438"
    mbid_ok = "f59c5520-5f46-4d2c-b2c4-822eabf53419"
    mbid_to_artist = {
        mbid_error: _make_artist("1", "Error Artist", mbid=mbid_error),
        mbid_ok: _make_artist("2", "OK Artist", mbid=mbid_ok),
    }

    async def fake_get_artist_details(mbid: str) -> MusicBrainzArtist:
        if mbid == mbid_error:
            msg = "MB API unavailable"
            raise RuntimeError(msg)
        return _make_mb_artist(mbid, f"1985-{today_mmdd}")

    provider_mock.get_artist_details = fake_get_artist_details

    result = await manager._find_artist_mbids_by_date(
        mbid_to_artist, today_mmdd, date_field="begin"
    )

    assert result == [mbid_ok]


# ---------------------------------------------------------------------------
# _find_artist_mbids_by_date — end (in memoriam)
# ---------------------------------------------------------------------------


async def test_find_memoriam_mbids_matches_death_date(
    manager: MusicBrainzRecommendationManager,
    provider_mock: Mock,
) -> None:
    """Artists who passed away on today's date (ended=True) are returned."""
    today = datetime.now(UTC).date()
    today_mmdd = f"{today.month:02d}-{today.day:02d}"

    mbid = "20ff3303-4fe2-4a47-a1b6-291e26aa3438"
    mbid_to_artist = {mbid: _make_artist("1", "Late Artist", mbid=mbid)}

    provider_mock.get_artist_details = AsyncMock(
        return_value=_make_mb_artist(mbid, begin="1933-01-01", end=f"2006-{today_mmdd}", ended=True)
    )

    result = await manager._find_artist_mbids_by_date(mbid_to_artist, today_mmdd, date_field="end")

    assert result == [mbid]


async def test_find_memoriam_skips_living_artists(
    manager: MusicBrainzRecommendationManager,
    provider_mock: Mock,
) -> None:
    """Artists where ended=False are not included even if end date matches."""
    today = datetime.now(UTC).date()
    today_mmdd = f"{today.month:02d}-{today.day:02d}"

    mbid = "f59c5520-5f46-4d2c-b2c4-822eabf53419"
    mbid_to_artist = {mbid: _make_artist("1", "Living Artist", mbid=mbid)}

    # ended=False simulates a band that broke up but members are alive
    provider_mock.get_artist_details = AsyncMock(
        return_value=_make_mb_artist(
            mbid, begin="1996-01-01", end=f"2023-{today_mmdd}", ended=False
        )
    )

    result = await manager._find_artist_mbids_by_date(mbid_to_artist, today_mmdd, date_field="end")

    assert result == []


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
# get_recommendations (integration)
# ---------------------------------------------------------------------------


async def test_get_recommendations_returns_folders_for_birthday_artists(
    manager: MusicBrainzRecommendationManager,
    provider_mock: Mock,
) -> None:
    """Full flow: library artist with today's birthday produces a folder."""
    today = datetime.now(UTC).date()
    today_mmdd = f"{today.month:02d}-{today.day:02d}"

    mbid = "20ff3303-4fe2-4a47-a1b6-291e26aa3438"
    birthday_artist = _make_artist("10", "Birthday Star", mbid=mbid, genres={"Pop"})

    provider_mock.mass.music.artists.iter_library_items = Mock(
        return_value=_async_iter([birthday_artist])
    )
    provider_mock.mass.music.albums.iter_library_items = Mock(return_value=_async_iter([]))
    provider_mock.get_artist_details = AsyncMock(
        return_value=_make_mb_artist(mbid, f"1990-{today_mmdd}")
    )

    folders = await manager.get_recommendations()

    birthday_folders = [
        f for f in folders if f.translation_key and "birthdays" in f.translation_key
    ]
    assert len(birthday_folders) == 1
    assert birthday_folders[0].translation_key == "artist_birthdays_today"
    assert birthday_folders[0].translation_params is None
    assert len(birthday_folders[0].items) == 1


async def test_get_recommendations_empty_library(
    manager: MusicBrainzRecommendationManager,
    provider_mock: Mock,
) -> None:
    """Empty library returns no folders."""
    provider_mock.mass.music.artists.iter_library_items = Mock(return_value=_async_iter([]))
    provider_mock.mass.music.albums.iter_library_items = Mock(return_value=_async_iter([]))

    folders = await manager.get_recommendations()

    assert folders == []


async def test_get_recommendations_no_mbid_artists_skipped(
    manager: MusicBrainzRecommendationManager,
    provider_mock: Mock,
) -> None:
    """Library artists without an MBID are not checked and produce no folders."""
    artist_no_mbid = _make_artist("1", "No MBID Artist")

    provider_mock.mass.music.artists.iter_library_items = Mock(
        return_value=_async_iter([artist_no_mbid])
    )
    provider_mock.mass.music.albums.iter_library_items = Mock(return_value=_async_iter([]))

    folders = await manager.get_recommendations()

    assert folders == []


async def test_get_recommendations_no_birthday_match(
    manager: MusicBrainzRecommendationManager,
    provider_mock: Mock,
) -> None:
    """Library artist with MBID but different birth date produces no folders."""
    today = datetime.now(UTC).date()
    today_mmdd = f"{today.month:02d}-{today.day:02d}"

    mbid = "c3c82bdc-d9e7-4836-9746-c24ead47ca19"
    artist = _make_artist("1", "Wrong Birthday", mbid=mbid, genres={"Rock"})

    other_mmdd = "01-01" if today_mmdd != "01-01" else "01-02"

    provider_mock.mass.music.artists.iter_library_items = Mock(return_value=_async_iter([artist]))
    provider_mock.mass.music.albums.iter_library_items = Mock(return_value=_async_iter([]))
    provider_mock.get_artist_details = AsyncMock(
        return_value=_make_mb_artist(mbid, f"1985-{other_mmdd}")
    )

    folders = await manager.get_recommendations()

    assert folders == []
