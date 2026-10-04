"""Test the catch-up broadcasts of the ORF Radiothek provider."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from datetime import date, timedelta
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from music_assistant_models.enums import StreamType
from music_assistant_models.errors import UnplayableMediaError
from music_assistant_models.streamdetails import MultiPartPath, StreamDetails

from music_assistant.helpers.datetime import utc
from music_assistant.providers.orf_radiothek import (
    BROADCAST_FINISHED_CACHE,
    BROADCAST_UNFINISHED_CACHE,
    CATCHUP_PAST_DAYS_CACHE,
    CATCHUP_RECENT_DAYS_CACHE,
    SUPPORTED_FEATURES,
    RadiothekProvider,
)
from music_assistant.providers.orf_radiothek.helpers import OrfStation

SEGMENT_URL = "https://loopstream01.apa.at/?channel=oe1&id=2026-10-02_0600_tl_51_7DaysFri3_1.mp3"


class _FakeCache:
    """In-memory stand-in for the cache controller that records expirations."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self.expirations: dict[str, int] = {}

    async def get(self, key: str, **kwargs: Any) -> Any:
        return self.data.get(key)

    async def set(self, key: str, data: Any, expiration: int = 0, **kwargs: Any) -> None:
        self.data[key] = data
        self.expirations[key] = expiration


def _provider() -> RadiothekProvider:
    """Return a provider with an in-memory cache and no network access."""
    config = Mock(instance_id="orf_radiothek--test", enabled=True)
    config.get_value.side_effect = lambda key, default=None: (
        "INFO" if key == "log_level" else default
    )
    mass = Mock()
    mass.cache = _FakeCache()
    provider = RadiothekProvider(mass, Mock(domain="orf_radiothek"), config, SUPPORTED_FEATURES)
    provider._get_bundle = AsyncMock(return_value={})  # type: ignore[method-assign]
    provider._iter_orf_stations = Mock(  # type: ignore[method-assign]
        return_value=[OrfStation(id="oe1", name="Ö1", live_stream_url_template="")]
    )
    return provider


def _cache(provider: RadiothekProvider) -> _FakeCache:
    """Return the in-memory cache of a provider made by _provider."""
    return cast("_FakeCache", provider.mass.cache)


def _broadcast(bid: int, day: date, hour: int, state: str = "C") -> dict[str, Any]:
    """Return a minimal day listing entry of a broadcast."""
    return {
        "id": bid,
        "title": f"Broadcast {bid}",
        "niceTime": f"{day.isoformat()}T{hour:02d}:00:00+02:00",
        "duration": 3600 * 1000,
        "state": state,
    }


def _segment(offset_ms: int, duration_ms: int) -> dict[str, Any]:
    """Return a broadcast detail stream segment."""
    return {
        "duration": duration_ms,
        "urls": {
            "progressive": f"{SEGMENT_URL}&offset={offset_ms}{{&duration}}",
            "hls": "https://example.com/playlist.m3u8",
        },
    }


def _audio_session(requested: list[str]) -> MagicMock:
    """Return an http session that records requested urls and serves the url as audio."""

    def _get(url: str, **_kwargs: Any) -> MagicMock:
        requested.append(url)

        async def _chunks(_size: int) -> AsyncGenerator[bytes]:
            yield url.encode()

        resp = MagicMock()
        resp.raise_for_status = Mock()
        resp.content.iter_chunked = _chunks
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=resp)
        ctx.__aexit__ = AsyncMock(return_value=None)
        return ctx

    return MagicMock(get=Mock(side_effect=_get))


def test_segment_url_from_moves_the_offset() -> None:
    """Seeking within a segment moves its millisecond offset parameter."""
    url = f"{SEGMENT_URL}&offset=1000"
    moved = RadiothekProvider._segment_url_from(url, 5000)
    assert "offset=6000" in moved
    assert "channel=oe1" in moved
    assert RadiothekProvider._segment_url_from(url, 0) == url
    assert RadiothekProvider._segment_url_from(SEGMENT_URL, 5000) == SEGMENT_URL


async def test_audio_stream_skips_whole_segments_and_plays_the_rest() -> None:
    """A seek skips whole segments, starts mid-segment, then plays later segments in full."""
    provider = _provider()
    requested: list[str] = []
    cast("Mock", provider.mass).http_session = _audio_session(requested)
    parts = [
        MultiPartPath(path=f"{SEGMENT_URL}&offset=0", duration=100),
        MultiPartPath(path=f"{SEGMENT_URL}&offset=200000", duration=200),
        MultiPartPath(path=f"{SEGMENT_URL}&offset=500000", duration=300),
    ]
    streamdetails = Mock(spec=StreamDetails, data=parts)

    chunks = [c async for c in provider.get_audio_stream(streamdetails, seek_position=150)]

    assert len(chunks) == 2
    assert "offset=250000" in requested[0]
    assert requested[1] == f"{SEGMENT_URL}&offset=500000"


async def test_split_broadcast_plays_all_segments() -> None:
    """A broadcast split into segments plays all of them, even when HLS is configured."""
    provider = _provider()
    provider.catchup_proto = "hls"
    provider._fetch_broadcast_detail = AsyncMock(  # type: ignore[method-assign]
        return_value={"state": "C", "streams": [_segment(0, 600_000), _segment(900_000, 300_000)]}
    )

    details = await provider._get_broadcast_episode_stream_details("br:oe1:1")

    assert details.stream_type == StreamType.CUSTOM
    assert [p.duration for p in details.data] == [600, 300]
    assert details.duration == 900
    assert all("{" not in p.path for p in details.data)


async def test_split_broadcast_with_a_missing_segment_url_is_unplayable() -> None:
    """A broadcast is not played with a segment left out."""
    provider = _provider()
    provider._fetch_broadcast_detail = AsyncMock(  # type: ignore[method-assign]
        return_value={"state": "C", "streams": [_segment(0, 600_000), {"duration": 300_000}]}
    )

    with pytest.raises(UnplayableMediaError):
        await provider._get_broadcast_episode_stream_details("br:oe1:1")


@pytest.mark.parametrize(
    ("segment", "can_seek"),
    [
        (_segment(900_000, 300_000), True),
        ({**_segment(900_000, 300_000), "duration": None}, False),
        ({"duration": 300_000, "urls": {"progressive": f"{SEGMENT_URL}{{&offset}}"}}, False),
    ],
)
async def test_split_broadcast_seeks_natively_only_with_offsets_and_durations(
    segment: dict[str, Any], can_seek: bool
) -> None:
    """Without the offset and duration of every segment, seeking is left to the decoder."""
    provider = _provider()
    provider._fetch_broadcast_detail = AsyncMock(  # type: ignore[method-assign]
        return_value={"state": "C", "streams": [_segment(0, 600_000), segment]}
    )

    details = await provider._get_broadcast_episode_stream_details("br:oe1:1")

    assert details.can_seek is can_seek


async def test_single_segment_broadcast_uses_hls() -> None:
    """A broadcast with a single segment is still played over HLS when configured."""
    provider = _provider()
    provider.catchup_proto = "hls"
    provider._fetch_broadcast_detail = AsyncMock(  # type: ignore[method-assign]
        return_value={"state": "C", "streams": [_segment(0, 600_000)]}
    )

    details = await provider._get_broadcast_episode_stream_details("br:oe1:1")

    assert details.stream_type == StreamType.HLS


@pytest.mark.parametrize(
    ("state", "expiration"),
    [("C", BROADCAST_FINISHED_CACHE), ("L", BROADCAST_UNFINISHED_CACHE)],
)
async def test_broadcast_detail_cache_follows_state(state: str, expiration: int) -> None:
    """A broadcast still on air is cached briefly, a finished one for a day."""
    provider = _provider()
    provider._fetch_broadcast_detail = AsyncMock(  # type: ignore[method-assign]
        return_value={"state": state, "streams": []}
    )

    await provider._get_broadcast_detail("oe1", 1)

    assert _cache(provider).expirations["broadcast.oe1.1"] == expiration


async def test_episode_duration_follows_the_broadcast_detail() -> None:
    """Opening an episode reports the real audio duration, not a cached older one."""
    provider = _provider()
    today = utc().date()
    on_air = {**_broadcast(1, today, 6, state="L"), "streams": [_segment(0, 600_000)]}
    finished = {
        **_broadcast(1, today, 6),
        "streams": [_segment(0, 600_000), _segment(900_000, 300_000)],
    }
    provider._fetch_broadcast_detail = AsyncMock(  # type: ignore[method-assign]
        side_effect=[on_air, finished]
    )

    assert (await provider.get_podcast_episode("br:oe1:1")).duration == 600
    # the on-air detail expires from the cache, the episode itself is not cached
    _cache(provider).data.clear()
    assert (await provider.get_podcast_episode("br:oe1:1")).duration == 900


@pytest.mark.parametrize(
    ("days_ago", "expiration"),
    [(0, CATCHUP_RECENT_DAYS_CACHE), (1, CATCHUP_RECENT_DAYS_CACHE), (2, CATCHUP_PAST_DAYS_CACHE)],
)
async def test_day_listing_cache_depends_on_age(days_ago: int, expiration: int) -> None:
    """Recent days are cached briefly, older days are final and cached for a week."""
    provider = _provider()
    day = utc().date() - timedelta(days=days_ago)
    provider._http_get_json = AsyncMock(  # type: ignore[method-assign]
        return_value={"payload": [_broadcast(1, day, 6)]}
    )

    items = await provider._get_broadcasts_for_day("oe1", day)

    assert items is not None
    assert len(items) == 1
    assert _cache(provider).expirations[f"broadcasts.oe1.{day:%Y%m%d}"] == expiration


async def test_empty_and_failed_days_are_not_cached() -> None:
    """An empty day may not be published yet and a failed one must be retried."""
    provider = _provider()
    day = utc().date()
    provider._http_get_json = AsyncMock(  # type: ignore[method-assign]
        side_effect=[{"payload": []}, TimeoutError()]
    )

    assert await provider._get_broadcasts_for_day("oe1", day) == []
    assert await provider._get_broadcasts_for_day("oe1", day) is None
    assert _cache(provider).data == {}


async def test_listing_orders_by_broadcast_time_and_survives_a_failed_day() -> None:
    """Positions follow the broadcast start, and a failed day does not end the listing."""
    provider = _provider()
    provider.mass.create_task = Mock()  # type: ignore[method-assign]
    provider._fill_broadcast_durations = Mock()  # type: ignore[method-assign]
    today = utc().date()
    days: dict[date, list[dict[str, Any]] | None] = {
        today: [_broadcast(1, today, 6), _broadcast(2, today, 9)],
        today - timedelta(days=1): None,
        today - timedelta(days=2): [_broadcast(3, today - timedelta(days=2), 20)],
    }
    provider._get_broadcasts_for_day = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda _station, day: days.get(day, [])
    )

    episodes = [ep async for ep in provider.get_podcast_episodes("br:oe1")]

    assert [ep.item_id for ep in episodes] == ["br:oe1:1", "br:oe1:2", "br:oe1:3"]
    newest_first = sorted(episodes, key=lambda ep: ep.position, reverse=True)
    assert [ep.item_id for ep in newest_first] == ["br:oe1:2", "br:oe1:1", "br:oe1:3"]
    # the fill must not drop the durations of the day that failed to load
    provider._fill_broadcast_durations.assert_called_once_with("oe1", [1, 2, 3], None)


async def test_listing_uses_known_durations() -> None:
    """Broadcasts with a known real duration report it instead of the scheduled one."""
    provider = _provider()
    provider.mass.create_task = Mock()  # type: ignore[method-assign]
    today = utc().date()
    _cache(provider).data["broadcast_durations.oe1"] = {"1": 1800}
    provider._get_broadcasts_for_day = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda _station, day: [_broadcast(1, today, 6)] if day == today else []
    )

    episodes = [ep async for ep in provider.get_podcast_episodes("br:oe1")]

    assert episodes[0].duration == 1800
    provider.mass.create_task.assert_not_called()


@pytest.mark.parametrize(
    ("current", "kept"),
    [({2}, {"2", "3"}), (None, {"1", "2", "3"})],
)
async def test_fill_prunes_only_after_a_complete_listing(
    current: set[int] | None, kept: set[str]
) -> None:
    """Durations of broadcasts no longer listed are dropped, unless the listing was incomplete."""
    provider = _provider()
    _cache(provider).data["broadcast_durations.oe1"] = {"1": 100, "2": 200}
    provider._fetch_broadcast_detail = AsyncMock(  # type: ignore[method-assign]
        return_value={"state": "C", "streams": [_segment(0, 300_000)]}
    )

    await provider._fill_broadcast_durations("oe1", [3], current)

    assert set(_cache(provider).data["broadcast_durations.oe1"]) == kept


async def test_fill_reuses_cached_details_and_skips_unfinished() -> None:
    """A detail cached by playback is reused, and broadcasts still on air are skipped."""
    provider = _provider()
    _cache(provider).data["broadcast.oe1.1"] = {
        "state": "C",
        "streams": [_segment(0, 300_000)],
    }
    provider._fetch_broadcast_detail = AsyncMock(  # type: ignore[method-assign]
        return_value={"state": "L", "streams": [_segment(0, 60_000)]}
    )

    await provider._fill_broadcast_durations("oe1", [1, 2], {1, 2})

    provider._fetch_broadcast_detail.assert_awaited_once_with("oe1", 2)
    assert _cache(provider).data["broadcast_durations.oe1"] == {"1": 300}


async def test_unload_cancels_running_fills() -> None:
    """Unloading the provider stops its background duration lookups."""
    provider = _provider()
    fill = asyncio.create_task(asyncio.sleep(3600))
    provider._fill_tasks["oe1"] = fill

    await provider.unload()

    assert fill.cancelled()
    assert provider._fill_tasks == {}
