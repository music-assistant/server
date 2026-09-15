"""Tests for the AI Radio media item surface."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import ProviderMapping, Track

from music_assistant.providers.ai_radio.media import AIRadioMediaMixin

STATION = {
    "id": "morning_show",
    "name": "Morning Show",
    "source_playlist_id": "42",
    "source_playlist_provider": "library",
    "default_player_id": "",
    "max_duration_minutes": 0.0,
    "shuffle_source_tracks": True,
    "host_id": "amy",
}


class _Media(AIRadioMediaMixin):
    """Bare media harness."""

    def __init__(self, stations: dict[str, dict[str, Any]]) -> None:
        """Stamp the attrs AIRadioMediaMixin reads, skipping real provider init."""
        self._stations = stations
        self._show_library_ids: dict[str, str] = {}
        self.instance_id = "ai_radio"
        self.domain = "ai_radio"
        # the mixin declares `mass: MusicAssistant`; a mock stands in for tests
        self.mass: Any = MagicMock()
        self.logger = MagicMock()

    def _ai_radio_cover_image_path(self) -> str:
        """Return a fake cover image path."""
        return "/tmp/air.png"  # noqa: S108


async def test_get_radio_builds_dynamic_radio() -> None:
    """get_radio builds a finite dynamic Radio item with a unique provider mapping."""
    media = _Media({"morning_show": STATION})
    radio = await media.get_radio("morning_show")
    assert radio.item_id == "morning_show"
    assert radio.provider == "ai_radio"
    assert radio.is_dynamic is True
    assert radio.is_finite is True
    assert radio.uri == "ai_radio://radio/morning_show"
    mapping = next(iter(radio.provider_mappings))
    assert mapping.is_unique is True


async def test_get_radio_unknown_station_raises() -> None:
    """get_radio raises when the station id is unknown."""
    media = _Media({})
    with pytest.raises(MediaNotFoundError):
        await media.get_radio("nope")


async def test_library_upkeep_adds_missing_show() -> None:
    """_sync_show_library_items adds a show that has no library item yet."""
    media = _Media({"morning_show": STATION})
    radio_ctrl = media.mass.music.radio
    radio_ctrl.get_library_item_by_prov_mappings = AsyncMock(return_value=None)
    added = MagicMock(item_id="7", name="Morning Show")
    radio_ctrl.add_item_to_library = AsyncMock(return_value=added)

    async def _no_items() -> AsyncGenerator[Any]:
        return
        yield

    radio_ctrl.iter_library_items = MagicMock(return_value=_no_items())
    await media._sync_show_library_items()
    radio_ctrl.add_item_to_library.assert_awaited_once()
    # the sync also rebuilds the map that resolves a library row back to its station
    assert media._show_library_ids == {"7": "morning_show"}


def _track(item_id: str) -> Track:
    """Build a minimal Track with one provider mapping."""
    return Track(
        item_id=item_id,
        provider="library",
        name=f"Track {item_id}",
        provider_mappings={
            ProviderMapping(
                item_id=item_id,
                provider_domain="filesystem_local",
                provider_instance="filesystem_local",
            )
        },
    )


def _media_with_show(track_count: int, max_duration_minutes: float = 0.0) -> _Media:
    """Build a _Media harness with a station whose source playlist has track_count tracks."""
    station = {**STATION, "max_duration_minutes": max_duration_minutes}
    media = _Media({"morning_show": station})
    media._fetch_source_tracks = AsyncMock(  # type: ignore[method-assign]
        return_value=(
            [
                {
                    "index": i,
                    "item_id": f"t{i}",
                    "name": f"Track t{i}",
                    "artist": "",
                    "songinfo": f"Track t{i}",
                    "duration": 210,
                    "media_item": _track(f"t{i}"),
                }
                for i in range(track_count)
            ],
            "My Playlist",
        )
    )
    return media


async def test_call_returns_the_whole_tracklist() -> None:
    """Every call returns the show's complete tracklist, not a page of it."""
    media = _media_with_show(track_count=30)
    tracks = await media.get_dynamic_radio_tracks("morning_show")
    assert len(tracks) == 30


async def test_repeated_calls_each_re_snapshot() -> None:
    """The feed is stateless: each call re-fetches and re-shuffles the source playlist."""
    media = _media_with_show(track_count=10)
    first = await media.get_dynamic_radio_tracks("morning_show")
    second = await media.get_dynamic_radio_tracks("morning_show")
    assert len(first) == len(second) == 10
    assert media._fetch_source_tracks.await_count == 2  # type: ignore[attr-defined]


async def test_duration_cap_trims_snapshot() -> None:
    """The snapshot is trimmed to the station's configured maximum duration."""
    media = _media_with_show(track_count=10, max_duration_minutes=7.0)
    tracks = await media.get_dynamic_radio_tracks("morning_show")
    # 10 tracks of 210s; the cap keeps tracks until >= 7 minutes (2 tracks)
    assert len(tracks) == 2


async def test_unknown_station_raises() -> None:
    """get_dynamic_radio_tracks raises when the station id is unknown."""
    media = _Media({})
    with pytest.raises(MediaNotFoundError):
        await media.get_dynamic_radio_tracks("nope")
