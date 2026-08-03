"""Tests for MusicBrainz recommendations (birthdays and memorials)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.enums import ExternalID, RecommendationFolderType
from music_assistant_models.media_items import (
    Artist,
    MediaItemMetadata,
    ProviderMapping,
    RecommendationFolder,
)
from music_assistant_models.media_items.metadata import LifeSpan

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
    life_span: LifeSpan | None = None,
) -> Artist:
    """Return a minimal library Artist, optionally with an MB external-ID and life_span."""
    pm = ProviderMapping(
        item_id=item_id,
        provider_domain="test",
        provider_instance="test",
    )
    artist = Artist(item_id=item_id, provider="test", name=name, provider_mappings={pm})
    if mbid:
        artist.add_external_id(ExternalID.MB_ARTIST, mbid)
    if life_span is not None:
        artist.metadata = MediaItemMetadata(life_span=life_span)
    return artist


def _make_mb_artist(
    mbid: str,
    begin: str | None,
    end: str | None = None,
    ended: bool = False,
    artist_type: str | None = None,
) -> MusicBrainzArtist:
    """Return a MusicBrainzArtist with the given life-span and optional type."""
    life_span = MusicBrainzLifeSpan(begin=begin, end=end, ended=ended) if (begin or end) else None
    return MusicBrainzArtist(
        id=mbid, name="stub", sort_name="stub", life_span=life_span, type=artist_type
    )


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


def _set_library(provider_mock: Mock, artists: list[Artist]) -> None:
    """Wire the library artist iterator on the provider mock."""
    provider_mock.mass.music.artists.iter_library_items = Mock(return_value=_async_iter(artists))


def _get_timeline_folder(folders: list[RecommendationFolder]) -> RecommendationFolder:
    """Extract and validate the single timeline folder."""
    assert len(folders) == 1, "Expected single timeline folder"
    folder = folders[0]
    assert folder.type == RecommendationFolderType.TIMELINE
    assert folder.item_id == "musicbrainz_timeline"
    assert folder.translation_key == "artist_timeline"
    return folder


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
# _scan_matches
# ---------------------------------------------------------------------------


async def test_scan_matches_birthday_in_window(
    manager: MusicBrainzRecommendationManager,
    provider_mock: Mock,
) -> None:
    """Artists whose birth date falls within the window are returned."""
    today_mmdd = _today_mmdd()
    mbid = "20ff3303-4fe2-4a47-a1b6-291e26aa3438"
    _set_library(provider_mock, [_make_artist("1", "Birthday Artist", mbid=mbid)])
    provider_mock.get_artist_details = AsyncMock(
        return_value=_make_mb_artist(mbid, f"1980-{today_mmdd}")
    )

    artists = await manager._scan_matches()
    assert [a.name for a in artists] == ["Birthday Artist"]


async def test_scan_matches_memoriam_in_window(
    manager: MusicBrainzRecommendationManager,
    provider_mock: Mock,
) -> None:
    """Artists who passed away on a window date (ended=True) are returned."""
    today_mmdd = _today_mmdd()
    mbid = "20ff3303-4fe2-4a47-a1b6-291e26aa3438"
    _set_library(provider_mock, [_make_artist("1", "Late Artist", mbid=mbid)])
    provider_mock.get_artist_details = AsyncMock(
        return_value=_make_mb_artist(mbid, begin="1933-01-01", end=f"2006-{today_mmdd}", ended=True)
    )

    artists = await manager._scan_matches()
    assert [a.name for a in artists] == ["Late Artist"]


async def test_scan_matches_out_of_window(
    manager: MusicBrainzRecommendationManager,
    provider_mock: Mock,
) -> None:
    """Artists whose event date is outside the window are not returned."""
    mbid = "c3c82bdc-d9e7-4836-9746-c24ead47ca19"
    _set_library(provider_mock, [_make_artist("1", "Wrong Date", mbid=mbid)])
    provider_mock.get_artist_details = AsyncMock(
        return_value=_make_mb_artist(mbid, f"1985-{_mmdd_for_offset(180)}")
    )

    assert await manager._scan_matches() == []


async def test_scan_matches_no_life_span(
    manager: MusicBrainzRecommendationManager,
    provider_mock: Mock,
) -> None:
    """Artists with no life_span are silently skipped."""
    mbid = "89ad4ac3-39f7-470e-963a-56509c546377"
    _set_library(provider_mock, [_make_artist("1", "No Lifespan", mbid=mbid)])
    provider_mock.get_artist_details = AsyncMock(return_value=_make_mb_artist(mbid, None))

    assert await manager._scan_matches() == []


async def test_scan_matches_partial_date_skipped(
    manager: MusicBrainzRecommendationManager,
    provider_mock: Mock,
) -> None:
    """Partial dates like '1990' (no MM-DD) are ignored."""
    mbid = "c3c82bdc-d9e7-4836-9746-c24ead47ca19"
    _set_library(provider_mock, [_make_artist("1", "Partial Date", mbid=mbid)])
    provider_mock.get_artist_details = AsyncMock(return_value=_make_mb_artist(mbid, "1990"))

    assert await manager._scan_matches() == []


async def test_scan_matches_living_artist_not_in_memoriam(
    manager: MusicBrainzRecommendationManager,
    provider_mock: Mock,
) -> None:
    """Artists where ended=False are not included even if end date matches."""
    today_mmdd = _today_mmdd()
    mbid = "f59c5520-5f46-4d2c-b2c4-822eabf53419"
    _set_library(provider_mock, [_make_artist("1", "Living Artist", mbid=mbid)])
    provider_mock.get_artist_details = AsyncMock(
        return_value=_make_mb_artist(
            mbid, begin="1996-01-01", end=f"2023-{today_mmdd}", ended=False
        )
    )

    assert await manager._scan_matches() == []


async def test_scan_matches_excludes_character_and_other(
    manager: MusicBrainzRecommendationManager,
    provider_mock: Mock,
) -> None:
    """Character and Other artist types are excluded."""
    today_mmdd = _today_mmdd()
    mbid_char = "aaaaaaaa-0000-0000-0000-000000000001"
    mbid_other = "aaaaaaaa-0000-0000-0000-000000000002"
    mbid_person = "aaaaaaaa-0000-0000-0000-000000000003"
    _set_library(
        provider_mock,
        [
            _make_artist("1", "Character", mbid=mbid_char),
            _make_artist("2", "Other", mbid=mbid_other),
            _make_artist("3", "Real Person", mbid=mbid_person),
        ],
    )

    async def fake_details(mbid: str) -> MusicBrainzArtist:
        types = {mbid_char: "Character", mbid_other: "Other", mbid_person: "Person"}
        return _make_mb_artist(mbid, f"1980-{today_mmdd}", artist_type=types[mbid])

    provider_mock.get_artist_details = fake_details

    artists = await manager._scan_matches()
    assert [a.name for a in artists] == ["Real Person"]


async def test_scan_matches_no_mbid_skipped(
    manager: MusicBrainzRecommendationManager,
    provider_mock: Mock,
) -> None:
    """Artists without an MBID are not looked up."""
    _set_library(provider_mock, [_make_artist("1", "No MBID Artist")])
    provider_mock.get_artist_details = AsyncMock(side_effect=AssertionError("should not call"))

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

    async def fake_details(mbid: str) -> MusicBrainzArtist:
        if mbid == mbid_error:
            msg = "MB API unavailable"
            raise RuntimeError(msg)
        return _make_mb_artist(mbid, f"1985-{today_mmdd}")

    provider_mock.get_artist_details = fake_details

    artists = await manager._scan_matches()
    assert [a.name for a in artists] == ["OK Artist"]


async def test_scan_matches_uses_metadata_when_available(
    manager: MusicBrainzRecommendationManager,
    provider_mock: Mock,
) -> None:
    """Artists with pre-populated life_span metadata skip the API call."""
    today_mmdd = _today_mmdd()
    mbid = "20ff3303-4fe2-4a47-a1b6-291e26aa3438"
    life_span = LifeSpan(begin=f"1980-{today_mmdd}")
    _set_library(
        provider_mock, [_make_artist("1", "Cached Artist", mbid=mbid, life_span=life_span)]
    )
    provider_mock.get_artist_details = AsyncMock(side_effect=AssertionError("should not call"))

    artists = await manager._scan_matches()
    assert [a.name for a in artists] == ["Cached Artist"]


# ---------------------------------------------------------------------------
# get_recommendations / _build_folder
# ---------------------------------------------------------------------------


async def test_get_recommendations_returns_flat_timeline_folder(
    manager: MusicBrainzRecommendationManager,
    provider_mock: Mock,
) -> None:
    """A fresh cache hit returns folder metadata without items."""
    today_mmdd = _today_mmdd()
    artist = _make_artist("1", "Cached Artist", life_span=LifeSpan(begin=f"1980-{today_mmdd}"))
    provider_mock.mass.cache.get = AsyncMock(return_value=[artist.to_dict()])

    result = await manager.get_recommendations()

    timeline = _get_timeline_folder(result)
    assert len(timeline.items) == 0, "get_recommendations should return empty items"
    provider_mock.mass.create_task.assert_not_called()


async def test_get_recommendations_serves_stale_and_schedules(
    manager: MusicBrainzRecommendationManager,
    provider_mock: Mock,
) -> None:
    """When nothing fresh is cached, folder metadata is served and a refresh is scheduled."""
    artist = _make_artist("1", "Stale Artist")
    provider_mock.mass.cache.get = AsyncMock(side_effect=[None, [artist.to_dict()]])

    result = await manager.get_recommendations()

    timeline = _get_timeline_folder(result)
    assert len(timeline.items) == 0, "get_recommendations should return empty items"
    provider_mock.mass.create_task.assert_called_once()


async def test_get_recommendations_empty_when_no_cache(
    manager: MusicBrainzRecommendationManager,
    provider_mock: Mock,
) -> None:
    """With no cached data at all, returns empty and schedules a refresh."""
    provider_mock.mass.cache.get = AsyncMock(return_value=None)

    result = await manager.get_recommendations()

    assert result == []
    provider_mock.mass.create_task.assert_called_once()


async def test_get_recommendation_items_returns_cached_artists(
    manager: MusicBrainzRecommendationManager,
    provider_mock: Mock,
) -> None:
    """get_recommendation_items returns artists from cache."""
    today_mmdd = _today_mmdd()
    artist = _make_artist("1", "Cached Artist", life_span=LifeSpan(begin=f"1980-{today_mmdd}"))
    provider_mock.mass.cache.get = AsyncMock(return_value=[artist.to_dict()])

    result = await manager.get_recommendation_items("musicbrainz_timeline")

    assert len(result) == 1
    assert result[0].name == "Cached Artist"


async def test_get_recommendation_items_returns_empty_for_wrong_id(
    manager: MusicBrainzRecommendationManager,
) -> None:
    """get_recommendation_items returns empty list for unknown folder ID."""
    result = await manager.get_recommendation_items("unknown_folder")
    assert len(result) == 0


async def test_get_recommendation_items_returns_empty_when_no_cache(
    manager: MusicBrainzRecommendationManager,
    provider_mock: Mock,
) -> None:
    """get_recommendation_items returns empty when no cache exists."""
    provider_mock.mass.cache.get = AsyncMock(return_value=None)

    result = await manager.get_recommendation_items("musicbrainz_timeline")

    assert len(result) == 0


# ---------------------------------------------------------------------------
# _refresh
# ---------------------------------------------------------------------------


async def test_refresh_caches_artist_dicts(
    manager: MusicBrainzRecommendationManager,
    provider_mock: Mock,
) -> None:
    """_refresh caches a list of serialized artist dicts."""
    today_mmdd = _today_mmdd()
    mbid = "20ff3303-4fe2-4a47-a1b6-291e26aa3438"
    _set_library(provider_mock, [_make_artist("1", "Birthday Star", mbid=mbid)])
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
    assert stored[0]["name"] == "Birthday Star"
    # Verify life_span round-trips through the cache (frontend relies on it for event derivation).
    cached_artist = Artist.from_dict(stored[0])
    assert cached_artist.metadata is not None
    assert cached_artist.metadata.life_span is not None
    assert cached_artist.metadata.life_span.begin == f"1990-{today_mmdd}"
    assert kwargs["provider"] == "musicbrainz"
    assert kwargs["allow_expired_cache"] is True


async def test_refresh_swallows_errors(
    manager: MusicBrainzRecommendationManager,
    provider_mock: Mock,
) -> None:
    """A failure during scan does not write the cache or raise."""
    provider_mock.mass.music.artists.iter_library_items = Mock(
        side_effect=RuntimeError("library unavailable")
    )
    provider_mock.mass.cache.set = AsyncMock()

    await manager._refresh()

    provider_mock.mass.cache.set.assert_not_awaited()


# ---------------------------------------------------------------------------
# End of tests
