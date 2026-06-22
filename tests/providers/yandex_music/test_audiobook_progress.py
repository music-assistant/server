"""Tests for audiobook progress sync via play_audio in on_played/on_streamed."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import ResourceTemporarilyUnavailable
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.providers.yandex_music.provider import YandexMusicProvider


@pytest.fixture
def provider_mock() -> Mock:
    """Return a provider mock wired for audiobook progress reporting."""
    provider = Mock(spec=YandexMusicProvider)
    provider.domain = "yandex_music"
    provider.instance_id = "yandex_music_instance"
    provider.logger = Mock()
    provider.client = AsyncMock()
    provider.client.play_audio = AsyncMock(return_value=True)
    provider._audiobook_chapter_cache = {}
    provider._audiobook_play_ids = {}
    # real method so we don't have to replicate seek math in tests
    provider._resolve_audiobook_seek = YandexMusicProvider._resolve_audiobook_seek.__get__(
        provider, YandexMusicProvider
    )
    provider._audiobook_progress_point = YandexMusicProvider._audiobook_progress_point.__get__(
        provider, YandexMusicProvider
    )
    provider._resolve_audiobook_chapter_map = AsyncMock()
    return provider


@pytest.mark.asyncio
async def test_on_played_audiobook_reports_progress(provider_mock: Mock) -> None:
    """on_played(AUDIOBOOK) resolves chapter from cache and calls play_audio."""
    chapter_ids = ["c1", "c2", "c3"]
    chapter_durations_ms = [60_000, 120_000, 90_000]
    provider_mock._resolve_audiobook_chapter_map.return_value = (
        chapter_ids,
        chapter_durations_ms,
    )

    # position = 60 (end of c1) + 30 → middle of c2
    await YandexMusicProvider._report_audiobook_progress(provider_mock, "abook-42", 90)

    provider_mock.client.play_audio.assert_awaited_once()
    kwargs = provider_mock.client.play_audio.await_args.kwargs
    assert kwargs["track_id"] == "c2"
    assert kwargs["album_id"] == "abook-42"
    assert kwargs["track_length_seconds"] == 120
    assert kwargs["total_played_seconds"] == 30
    assert kwargs["end_position_seconds"] == 30
    # play_id created and persisted for the session
    assert provider_mock._audiobook_play_ids["abook-42"] == kwargs["play_id"]


@pytest.mark.asyncio
async def test_on_played_audiobook_skips_when_no_chapter_map(provider_mock: Mock) -> None:
    """No chapter map → skip play_audio (log at debug)."""
    provider_mock._resolve_audiobook_chapter_map.return_value = ([], [])

    await YandexMusicProvider._report_audiobook_progress(provider_mock, "abook-42", 90)

    provider_mock.client.play_audio.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_played_audiobook_reuses_play_id(provider_mock: Mock) -> None:
    """Successive on_played calls for the same audiobook share one play_id."""
    provider_mock._resolve_audiobook_chapter_map.return_value = (["c1"], [60_000])

    await YandexMusicProvider._report_audiobook_progress(provider_mock, "abook-1", 10)
    await YandexMusicProvider._report_audiobook_progress(provider_mock, "abook-1", 20)

    calls = provider_mock.client.play_audio.await_args_list
    assert calls[0].kwargs["play_id"] == calls[1].kwargs["play_id"]


@pytest.mark.asyncio
async def test_on_streamed_audiobook_sends_final_position(provider_mock: Mock) -> None:
    """on_streamed uses seek_position + seconds_streamed as absolute position."""
    sd = StreamDetails(
        provider="yandex_music",
        item_id="abook-7",
        audio_format=Mock(),
        media_type=MediaType.AUDIOBOOK,
        data={
            "chapter_ids": ["c1", "c2", "c3"],
            "chapter_durations_ms": [60_000, 120_000, 600_000],
        },
    )
    sd.seek_position = 300  # 5 minutes in — inside c3 (starts at 180s, 10min long)
    sd.seconds_streamed = 15.0  # stopped at 315s total
    # pre-stash a play_id so we can assert it pops
    provider_mock._audiobook_play_ids["abook-7"] = "stable-id"

    await YandexMusicProvider._report_audiobook_final(provider_mock, sd, sd.data)

    provider_mock.client.play_audio.assert_awaited_once()
    kwargs = provider_mock.client.play_audio.await_args.kwargs
    assert kwargs["track_id"] == "c3"
    assert kwargs["album_id"] == "abook-7"
    # absolute 315s → chapter 3 (starts at 180s) offset 135s
    assert kwargs["total_played_seconds"] == 135
    assert kwargs["end_position_seconds"] == 135
    assert kwargs["play_id"] == "stable-id"
    # session play_id cleared after final report
    assert "abook-7" not in provider_mock._audiobook_play_ids


@pytest.mark.asyncio
async def test_on_streamed_audiobook_ignores_empty_data(provider_mock: Mock) -> None:
    """Missing chapter_ids in data → no play_audio call."""
    sd = StreamDetails(
        provider="yandex_music",
        item_id="abook-9",
        audio_format=Mock(),
        media_type=MediaType.AUDIOBOOK,
        data={},
    )

    await YandexMusicProvider._report_audiobook_final(provider_mock, sd, sd.data)

    provider_mock.client.play_audio.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_played_audiobook_swallows_upstream_unavailable(
    provider_mock: Mock,
) -> None:
    """ResourceTemporarilyUnavailable from chapter_map resolution must not propagate."""
    provider_mock._resolve_audiobook_chapter_map.side_effect = ResourceTemporarilyUnavailable(
        "rate limited"
    )

    # Must not raise; progress report is advisory.
    await YandexMusicProvider._report_audiobook_progress(provider_mock, "abook-42", 30)

    provider_mock.client.play_audio.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_played_audiobook_swallows_unexpected_exception(
    provider_mock: Mock,
) -> None:
    """Any non-cancellation exception while resolving the chapter map is swallowed."""

    class SomeAuthError(Exception):
        pass

    provider_mock._resolve_audiobook_chapter_map.side_effect = SomeAuthError("token expired")

    await YandexMusicProvider._report_audiobook_progress(provider_mock, "abook-42", 30)

    provider_mock.client.play_audio.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_played_audiobook_propagates_cancellation(
    provider_mock: Mock,
) -> None:
    """asyncio.CancelledError must propagate — never suppressed."""
    provider_mock._resolve_audiobook_chapter_map.side_effect = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await YandexMusicProvider._report_audiobook_progress(provider_mock, "abook-42", 30)


@pytest.mark.asyncio
async def test_on_streamed_audiobook_evicts_cache_entry(provider_mock: Mock) -> None:
    """After on_streamed, the chapter-map cache entry for this book is dropped."""
    sd = StreamDetails(
        provider="yandex_music",
        item_id="abook-7",
        audio_format=Mock(),
        media_type=MediaType.AUDIOBOOK,
        data={"chapter_ids": ["c1"], "chapter_durations_ms": [60_000]},
    )
    provider_mock._audiobook_chapter_cache["abook-7"] = (["c1"], [60_000])
    provider_mock._audiobook_chapter_cache["abook-OTHER"] = (["x"], [1000])

    await YandexMusicProvider._report_audiobook_final(provider_mock, sd, sd.data)

    assert "abook-7" not in provider_mock._audiobook_chapter_cache
    # Other audiobooks in cache are untouched
    assert "abook-OTHER" in provider_mock._audiobook_chapter_cache


@pytest.mark.asyncio
async def test_on_streamed_audiobook_reports_end_of_last_chapter_at_eof(
    provider_mock: Mock,
) -> None:
    """Natural EOF must report end_position_seconds = last chapter's length, not 0."""
    sd = StreamDetails(
        provider="yandex_music",
        item_id="abook-eof",
        audio_format=Mock(),
        media_type=MediaType.AUDIOBOOK,
        data={
            "chapter_ids": ["c1", "c2"],
            "chapter_durations_ms": [60_000, 120_000],
        },
    )
    # Total duration = 180s. Reached exactly the end.
    sd.seek_position = 0
    sd.seconds_streamed = 180.0

    await YandexMusicProvider._report_audiobook_final(provider_mock, sd, sd.data)

    provider_mock.client.play_audio.assert_awaited_once()
    kwargs = provider_mock.client.play_audio.await_args.kwargs
    assert kwargs["track_id"] == "c2"
    assert kwargs["track_length_seconds"] == 120
    assert kwargs["end_position_seconds"] == 120
    assert kwargs["total_played_seconds"] == 120


@pytest.mark.asyncio
async def test_on_streamed_audiobook_clamps_zero_duration_chapter(
    provider_mock: Mock,
) -> None:
    """Missing duration_ms (coerced to 0) must never send track_length_seconds=0."""
    sd = StreamDetails(
        provider="yandex_music",
        item_id="abook-bad",
        audio_format=Mock(),
        media_type=MediaType.AUDIOBOOK,
        data={"chapter_ids": ["c1"], "chapter_durations_ms": [0]},
    )
    sd.seek_position = 0
    sd.seconds_streamed = 0.0

    await YandexMusicProvider._report_audiobook_final(provider_mock, sd, sd.data)

    kwargs = provider_mock.client.play_audio.await_args.kwargs
    assert kwargs["track_length_seconds"] >= 1
    assert 0 <= kwargs["end_position_seconds"] <= kwargs["track_length_seconds"]


@pytest.mark.asyncio
async def test_on_streamed_audiobook_without_data_still_cleans_up(
    provider_mock: Mock,
) -> None:
    """StreamDetails.data missing or stripped → caches still evicted, no play_audio call."""
    sd = StreamDetails(
        provider="yandex_music",
        item_id="abook-x",
        audio_format=Mock(),
        media_type=MediaType.AUDIOBOOK,
    )
    provider_mock._audiobook_chapter_cache["abook-x"] = (["c"], [1000])
    provider_mock._audiobook_play_ids["abook-x"] = "sess-id"

    await YandexMusicProvider._report_audiobook_final(provider_mock, sd, {})

    assert "abook-x" not in provider_mock._audiobook_chapter_cache
    assert "abook-x" not in provider_mock._audiobook_play_ids
    provider_mock.client.play_audio.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_streamed_audiobook_branch_taken_even_without_chapter_data(
    provider_mock: Mock,
) -> None:
    """
    AUDIOBOOK stream without chapter_ids still routes to audiobook cleanup.

    Previously the gate required ``"chapter_ids" in data`` and fell through
    to the radio path when data was missing, leaving caches stale.
    """
    provider_mock._report_audiobook_final = AsyncMock()
    provider_mock.client.send_rotor_station_feedback = AsyncMock()
    sd = StreamDetails(
        provider="yandex_music",
        item_id="abook-y",
        audio_format=Mock(),
        media_type=MediaType.AUDIOBOOK,
        # data=None (not a dict) — previously would fall through to radio
    )

    await YandexMusicProvider.on_streamed(provider_mock, sd)

    provider_mock._report_audiobook_final.assert_awaited_once()
    provider_mock.client.send_rotor_station_feedback.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_played_routes_audiobook_and_skips_radio_branch(
    provider_mock: Mock,
) -> None:
    """on_played with MediaType.AUDIOBOOK early-returns before radio feedback."""
    provider_mock._resolve_audiobook_chapter_map.return_value = (["c1"], [60_000])
    provider_mock._report_audiobook_progress = AsyncMock()
    provider_mock.client.send_rotor_station_feedback = AsyncMock()

    await YandexMusicProvider.on_played(
        provider_mock,
        MediaType.AUDIOBOOK,
        "abook-1",
        fully_played=False,
        position=30,
        media_item=Mock(),
        is_playing=True,
    )

    provider_mock._report_audiobook_progress.assert_awaited_once_with("abook-1", 30)
    provider_mock.client.send_rotor_station_feedback.assert_not_awaited()
