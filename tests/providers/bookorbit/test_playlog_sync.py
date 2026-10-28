"""Tests for BookOrbit playlog sync helpers."""

from __future__ import annotations

import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch

import pytest
from aiohttp import ClientError
from music_assistant_models.enums import MediaType

from music_assistant.constants import PLAYBACK_REPORT_INTERVAL_SECONDS
from music_assistant.providers.bookorbit import BookOrbitProvider
from music_assistant.providers.bookorbit.playlog_sync import (
    InboundProgressGuard,
    apply_book_progress,
    resolve_remote_progress_seconds,
    sync_playlog,
)
from tests.providers.bookorbit.conftest import manifest_from_book


def test_inbound_guard_blocks_immediately_after_outbound() -> None:
    """Inbound apply is blocked right after an outbound progress write."""
    guard = InboundProgressGuard(min_interval_ms=8_000)
    guard.note_outbound(42)
    assert guard.allow_inbound(42) is False


def test_inbound_guard_allows_after_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inbound apply is allowed once the cool-down interval has elapsed."""
    guard = InboundProgressGuard(min_interval_ms=8_000)
    now = {"ms": 1_000_000}

    def fake_time_ms() -> int:
        return now["ms"]

    monkeypatch.setattr(guard, "_now_ms", fake_time_ms)
    guard.note_outbound(7)
    now["ms"] = 1_000_000 + 8_000
    assert guard.allow_inbound(7) is True


def test_inbound_guard_allows_unknown_book() -> None:
    """Books with no recent outbound write always allow inbound apply."""
    assert InboundProgressGuard().allow_inbound(99) is True


@pytest.mark.asyncio
async def test_resolve_prefers_playback_state(book_multifile: dict[str, Any]) -> None:
    """Playback state wins over legacy progress; seconds are floored from ms."""
    client = Mock()
    manifest = manifest_from_book(book_multifile)
    client.get_audiobook_manifest = AsyncMock(return_value=manifest)
    client.get_playback_state = AsyncMock(
        return_value={
            "assetId": manifest["assets"][1]["assetId"],
            "positionMs": 10_000,
            "percentage": 40.0,
            "completed": False,
        }
    )
    client.get_audio_progress = AsyncMock()
    client.get_book = AsyncMock()
    result = await resolve_remote_progress_seconds(client, int(book_multifile["id"]))
    assert result == (False, 110)
    client.get_audio_progress.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_falls_back_to_legacy(book_multifile: dict[str, Any]) -> None:
    """Legacy file progress uses manifest playback order for absolute seconds."""
    client = Mock()
    manifest = manifest_from_book(book_multifile)
    client.get_audiobook_manifest = AsyncMock(return_value=manifest)
    client.get_playback_state = AsyncMock(return_value=None)
    client.get_book = AsyncMock(return_value=book_multifile)
    client.get_audio_progress = AsyncMock(
        return_value={
            "currentFileId": 772,
            "positionSeconds": 50.0,
            "percentage": 50.0,
        }
    )
    result = await resolve_remote_progress_seconds(client, int(book_multifile["id"]))
    assert result == (False, 150)


@pytest.mark.asyncio
async def test_resolve_returns_none_when_empty(book_audiobook: dict[str, Any]) -> None:
    """No playback state and no legacy progress yields None."""
    client = Mock()
    client.get_audiobook_manifest = AsyncMock(return_value=manifest_from_book(book_audiobook))
    client.get_playback_state = AsyncMock(return_value=None)
    client.get_book = AsyncMock(return_value=book_audiobook)
    client.get_audio_progress = AsyncMock(return_value=None)
    assert await resolve_remote_progress_seconds(client, int(book_audiobook["id"])) is None


def _client_with_state(book: dict[str, Any], position_ms: int, percentage: float) -> Mock:
    client = Mock()
    manifest = manifest_from_book(book)
    client.get_audiobook_manifest = AsyncMock(return_value=manifest)
    client.get_playback_state = AsyncMock(
        return_value={
            "assetId": manifest["assets"][0]["assetId"],
            "positionMs": position_ms,
            "percentage": percentage,
            "completed": False,
        }
    )
    return client


def _mass() -> Mock:
    mass = Mock()
    mass.music.mark_item_played = AsyncMock()
    mass.music.get_library_item_by_prov_id = AsyncMock(return_value=Mock(name="book"))
    return mass


@pytest.mark.asyncio
async def test_apply_marks_played(book_audiobook: dict[str, Any]) -> None:
    """Remote progress is stamped into the playlog as non-user-initiated."""
    mass = _mass()
    manifest = manifest_from_book(book_audiobook)
    duration_ms = manifest["assets"][0]["durationMs"]
    client = _client_with_state(
        book_audiobook,
        max(PLAYBACK_REPORT_INTERVAL_SECONDS * 1000, duration_ms // 4),
        25.0,
    )
    has = await apply_book_progress(
        mass,
        provider_instance_id="bookorbit--test",
        client=client,
        book_id=int(book_audiobook["id"]),
        guard=InboundProgressGuard(),
    )
    assert has is True
    mass.music.mark_item_played.assert_awaited()
    kwargs = mass.music.mark_item_played.await_args.kwargs
    assert kwargs["user_initiated"] is False
    assert kwargs["provider_instance_id"] == "bookorbit--test"
    assert kwargs["fully_played"] is False


@pytest.mark.asyncio
async def test_apply_skips_short_position_but_counts_as_progress(
    book_audiobook: dict[str, Any],
) -> None:
    """Positions under the report interval are not stamped but still count as progress."""
    mass = _mass()
    client = _client_with_state(book_audiobook, 5_000, 1.0)
    has = await apply_book_progress(
        mass,
        provider_instance_id="bookorbit--test",
        client=client,
        book_id=int(book_audiobook["id"]),
        guard=InboundProgressGuard(),
    )
    assert has is True
    mass.music.mark_item_played.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_respects_outbound_guard(book_audiobook: dict[str, Any]) -> None:
    """A recent outbound write suppresses the inbound stamp."""
    mass = _mass()
    client = _client_with_state(book_audiobook, 60_000, 10.0)
    guard = InboundProgressGuard()
    guard.note_outbound(int(book_audiobook["id"]))
    has = await apply_book_progress(
        mass,
        provider_instance_id="bookorbit--test",
        client=client,
        book_id=int(book_audiobook["id"]),
        guard=guard,
    )
    assert has is True
    mass.music.mark_item_played.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_returns_false_without_remote_progress() -> None:
    """No remote progress yields False and no playlog write."""
    mass = _mass()
    with patch(
        "music_assistant.providers.bookorbit.playlog_sync.resolve_remote_progress_seconds",
        new=AsyncMock(return_value=None),
    ):
        has = await apply_book_progress(
            mass,
            provider_instance_id="bookorbit--test",
            client=Mock(),
            book_id=1,
            guard=InboundProgressGuard(),
        )
    assert has is False
    mass.music.mark_item_played.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_transport_error_returns_none() -> None:
    """Transport errors are logged and reported as unknown (None), not absent."""
    mass = _mass()
    with patch(
        "music_assistant.providers.bookorbit.playlog_sync.resolve_remote_progress_seconds",
        new=AsyncMock(side_effect=ClientError("boom")),
    ):
        has = await apply_book_progress(
            mass,
            provider_instance_id="bookorbit--test",
            client=Mock(),
            book_id=1,
            guard=InboundProgressGuard(),
        )
    assert has is None


@pytest.mark.asyncio
async def test_sync_playlog_discards_cleared_progress() -> None:
    """Candidates in the playlog with no remote progress are marked unplayed."""
    mass = Mock()
    mass.music.get_playlog_provider_item_ids = AsyncMock(
        return_value=[
            (MediaType.AUDIOBOOK, "5"),
            (MediaType.AUDIOBOOK, "9"),
            (MediaType.AUDIOBOOK, "77"),  # not a candidate: must be left alone
        ]
    )
    cleared = Mock(name="cleared")
    kept = Mock(name="kept")
    mass.music.get_library_item_by_prov_id = AsyncMock(
        side_effect=lambda **kwargs: {"5": cleared, "9": kept}[kwargs["item_id"]]
    )
    mass.music.mark_item_unplayed = AsyncMock()
    mass.music.mark_item_played = AsyncMock()

    async def fake_resolve(_client: Any, book_id: int) -> tuple[bool, int] | None:
        if book_id == 9:
            return (False, 120)
        return None

    with patch(
        "music_assistant.providers.bookorbit.playlog_sync.resolve_remote_progress_seconds",
        new=AsyncMock(side_effect=fake_resolve),
    ):
        await sync_playlog(
            mass=mass,
            provider_instance_id="bookorbit--test",
            client=Mock(),
            book_ids={5, 9},
            guard=InboundProgressGuard(),
            concurrency=2,
        )
    mass.music.mark_item_unplayed.assert_awaited_once()
    assert mass.music.mark_item_unplayed.await_args.args[0] is cleared
    assert mass.music.mark_item_unplayed.await_args.kwargs == {
        "provider_instance_id": "bookorbit--test"
    }


@pytest.mark.asyncio
async def test_sync_playlog_does_not_unplay_on_fetch_failure() -> None:
    """A transport failure must not be treated as cleared remote progress."""
    mass = Mock()
    mass.music.get_playlog_provider_item_ids = AsyncMock(return_value=[(MediaType.AUDIOBOOK, "5")])
    mass.music.get_library_item_by_prov_id = AsyncMock(return_value=Mock(name="book"))
    mass.music.mark_item_unplayed = AsyncMock()
    mass.music.mark_item_played = AsyncMock()
    with patch(
        "music_assistant.providers.bookorbit.playlog_sync.resolve_remote_progress_seconds",
        new=AsyncMock(side_effect=ClientError("boom")),
    ):
        await sync_playlog(
            mass=mass,
            provider_instance_id="bookorbit--test",
            client=Mock(),
            book_ids={5},
            guard=InboundProgressGuard(),
        )
    mass.music.mark_item_unplayed.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_playlog_isolates_unexpected_book_failure() -> None:
    """An unexpected error for one book neither aborts the batch nor discards it."""
    mass = Mock()
    mass.music.get_playlog_provider_item_ids = AsyncMock(
        return_value=[(MediaType.AUDIOBOOK, "5"), (MediaType.AUDIOBOOK, "9")]
    )
    mass.music.get_library_item_by_prov_id = AsyncMock(return_value=Mock(name="book"))
    mass.music.mark_item_unplayed = AsyncMock()
    mass.music.mark_item_played = AsyncMock()

    async def fake_resolve(_client: Any, book_id: int) -> tuple[bool, int] | None:
        if book_id == 5:
            raise RuntimeError("unexpected")
        return None

    with patch(
        "music_assistant.providers.bookorbit.playlog_sync.resolve_remote_progress_seconds",
        new=AsyncMock(side_effect=fake_resolve),
    ):
        await sync_playlog(
            mass=mass,
            provider_instance_id="bookorbit--test",
            client=Mock(),
            book_ids={5, 9},
            guard=InboundProgressGuard(),
        )
    mass.music.mark_item_unplayed.assert_awaited_once()


@pytest.mark.asyncio
async def test_provider_sync_playlog_uses_catalog_ids(
    provider: BookOrbitProvider, book_audiobook: dict[str, Any]
) -> None:
    """Provider playlog sync defaults to the catalog's numeric book ids."""
    provider._catalog = {str(book_audiobook["id"]): book_audiobook}
    with patch(
        "music_assistant.providers.bookorbit.sync_playlog",
        new_callable=AsyncMock,
    ) as sync_mock:
        await provider._sync_playlog_from_bookorbit()
    assert sync_mock.await_count == 1
    assert sync_mock.await_args is not None
    kwargs = sync_mock.await_args.kwargs
    assert int(book_audiobook["id"]) in set(kwargs["book_ids"])
    assert kwargs["guard"] is provider._inbound_progress_guard


@pytest.mark.asyncio
async def test_provider_sync_playlog_swallows_errors(
    provider: BookOrbitProvider, book_audiobook: dict[str, Any]
) -> None:
    """A failing playlog sync never propagates to sync_library."""
    provider._catalog = {str(book_audiobook["id"]): book_audiobook}
    with patch(
        "music_assistant.providers.bookorbit.sync_playlog",
        new_callable=AsyncMock,
        side_effect=RuntimeError("boom"),
    ):
        await provider._sync_playlog_from_bookorbit()


@pytest.mark.asyncio
async def test_on_played_notes_inbound_guard(
    provider: BookOrbitProvider, book_multifile: dict[str, Any]
) -> None:
    """Outbound play reports block inbound playlog apply briefly."""
    provider._inbound_progress_guard = InboundProgressGuard()
    book_id = int(book_multifile["id"])
    with patch.object(provider, "_sync_playback_state", new_callable=AsyncMock):
        await provider.on_played(
            MediaType.AUDIOBOOK,
            str(book_id),
            fully_played=False,
            position=150,
            media_item=Mock(),
        )
    assert provider._inbound_progress_guard.allow_inbound(book_id) is False


@pytest.mark.asyncio
async def test_sync_library_audiobook_syncs_playlog(provider: BookOrbitProvider) -> None:
    """Audiobook library sync finishes by pushing BookOrbit progress to the playlog."""
    with (
        patch.object(provider, "_load_applied_mapping_details", new_callable=AsyncMock),
        patch.object(provider, "_save_applied_mapping_details", new_callable=AsyncMock),
        patch.object(provider, "_sync_playlog_from_bookorbit", new_callable=AsyncMock) as sync_mock,
        patch("music_assistant.models.music_provider.MusicProvider.sync_library", AsyncMock()),
    ):
        await provider.sync_library(MediaType.AUDIOBOOK)
        assert sync_mock.await_count == 1
        await provider.sync_library(MediaType.ARTIST)
        assert sync_mock.await_count == 1


@pytest.mark.asyncio
async def test_playlog_poll_loop_syncs_playlog_and_continue_listening(
    provider: BookOrbitProvider,
) -> None:
    """Each poll tick merges MA playlog ids with BookOrbit continue-listening ids."""
    mass = cast("Any", provider.mass)
    client = cast("Any", provider._client)
    mass.music.get_playlog_provider_item_ids = AsyncMock(
        return_value=[(MediaType.AUDIOBOOK, "1"), (MediaType.PODCAST_EPISODE, "2")]
    )
    client.get_continue_listening_book_ids = AsyncMock(return_value=[3])
    sync_mock = AsyncMock(side_effect=asyncio.CancelledError)
    with (
        patch("music_assistant.providers.bookorbit.asyncio.sleep", AsyncMock()),
        patch.object(provider, "_sync_playlog_from_bookorbit", sync_mock),
        pytest.raises(asyncio.CancelledError),
    ):
        await provider._playlog_poll_loop()
    sync_mock.assert_awaited_once_with({1, 3})


@pytest.mark.asyncio
async def test_unload_cancels_playlog_poll_task(provider: BookOrbitProvider) -> None:
    """Unload cancels the background poll task."""
    cast("Any", provider._client).close = AsyncMock()
    task = asyncio.create_task(asyncio.sleep(3600))
    provider._playlog_poll_task = task
    await provider.unload()
    assert task.cancelled()
    assert provider._playlog_poll_task is None
