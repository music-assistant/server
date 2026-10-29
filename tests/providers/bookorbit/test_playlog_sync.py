"""Tests for BookOrbit playlog sync helpers."""

from __future__ import annotations

import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch

import pytest
from aiohttp import ClientError
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import LoginFailed

from music_assistant.constants import PLAYBACK_REPORT_INTERVAL_SECONDS
from music_assistant.providers.bookorbit import BookOrbitProvider
from music_assistant.providers.bookorbit.playlog_sync import (
    InboundProgressGuard,
    PlaylogProgressMemory,
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
    client.get_audiobook_manifest.assert_not_awaited()
    client.get_book.assert_not_awaited()


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
async def test_apply_rechecks_guard_after_library_lookup(book_audiobook: dict[str, Any]) -> None:
    """An outbound stamp during the library lookup must still block mark_item_played."""
    mass = _mass()
    book_id = int(book_audiobook["id"])
    guard = InboundProgressGuard()
    client = _client_with_state(book_audiobook, 60_000, 10.0)

    async def lookup(**_kwargs: Any) -> Mock:
        guard.note_outbound(book_id)
        return Mock(name="book", fully_played=False, resume_position_ms=0)

    mass.music.get_library_item_by_prov_id = AsyncMock(side_effect=lookup)
    has = await apply_book_progress(
        mass,
        provider_instance_id="bookorbit--test",
        client=client,
        book_id=book_id,
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
async def test_apply_absent_during_outbound_guard_returns_none() -> None:
    """Absent remote progress during an outbound cool-down is unknown, not clearable."""
    mass = _mass()
    guard = InboundProgressGuard()
    guard.note_outbound(1)
    with patch(
        "music_assistant.providers.bookorbit.playlog_sync.resolve_remote_progress_seconds",
        new=AsyncMock(return_value=None),
    ):
        has = await apply_book_progress(
            mass,
            provider_instance_id="bookorbit--test",
            client=Mock(),
            book_id=1,
            guard=guard,
        )
    assert has is None
    mass.music.mark_item_played.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_conversion_error_returns_none() -> None:
    """Unusable remote progress is unknown (None), not confirmed absent."""
    mass = _mass()
    with patch(
        "music_assistant.providers.bookorbit.playlog_sync.resolve_remote_progress_seconds",
        new=AsyncMock(side_effect=ValueError("unknown duration")),
    ):
        has = await apply_book_progress(
            mass,
            provider_instance_id="bookorbit--test",
            client=Mock(),
            book_id=1,
            guard=InboundProgressGuard(),
        )
    assert has is None
    mass.music.mark_item_played.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_skips_unchanged_progress_via_memory() -> None:
    """Identical last-applied progress does not re-stamp the playlog timestamp."""
    mass = _mass()
    seconds = max(PLAYBACK_REPORT_INTERVAL_SECONDS, 60)
    memory = PlaylogProgressMemory()
    memory.note_progress(1, False, seconds)
    with patch(
        "music_assistant.providers.bookorbit.playlog_sync.resolve_remote_progress_seconds",
        new=AsyncMock(return_value=(False, seconds)),
    ):
        has = await apply_book_progress(
            mass,
            provider_instance_id="bookorbit--test",
            client=Mock(),
            book_id=1,
            guard=InboundProgressGuard(),
            memory=memory,
        )
    assert has is True
    mass.music.mark_item_played.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_stamps_when_library_hydrate_matches_but_memory_empty() -> None:
    """Do not trust hydrated resume fields; stamp until memory records provider progress."""
    mass = _mass()
    seconds = max(PLAYBACK_REPORT_INTERVAL_SECONDS, 60)
    library_item = Mock(name="book", fully_played=False, resume_position_ms=seconds * 1000)
    mass.music.get_library_item_by_prov_id = AsyncMock(return_value=library_item)
    with patch(
        "music_assistant.providers.bookorbit.playlog_sync.resolve_remote_progress_seconds",
        new=AsyncMock(return_value=(False, seconds)),
    ):
        has = await apply_book_progress(
            mass,
            provider_instance_id="bookorbit--test",
            client=Mock(),
            book_id=1,
            guard=InboundProgressGuard(),
            memory=PlaylogProgressMemory(),
        )
    assert has is True
    mass.music.mark_item_played.assert_awaited_once()


@pytest.mark.asyncio
async def test_resolve_skips_manifest_when_playback_state_unchanged(
    book_audiobook: dict[str, Any],
) -> None:
    """Unchanged playback-state fingerprint reuses memory without a manifest fetch."""
    book_id = int(book_audiobook["id"])
    seconds = max(PLAYBACK_REPORT_INTERVAL_SECONDS, 60)
    memory = PlaylogProgressMemory()
    memory.note_progress(book_id, False, seconds)
    client = _client_with_state(book_audiobook, seconds * 1000, 10.0)
    state = await client.get_playback_state(book_id)
    memory.note_remote_fp(
        book_id,
        (
            "state",
            state["assetId"],
            state["positionMs"],
            state["percentage"],
            state["completed"],
        ),
    )
    client.get_audiobook_manifest = AsyncMock()
    result = await resolve_remote_progress_seconds(client, book_id, memory=memory)
    assert result == (False, seconds)
    client.get_audiobook_manifest.assert_not_awaited()


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

    async def fake_resolve(_client: Any, book_id: int, **_kwargs: Any) -> tuple[bool, int] | None:
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
async def test_sync_playlog_does_not_unplay_on_conversion_failure() -> None:
    """Unusable remote progress must not clear the local playlog entry."""
    mass = Mock()
    mass.music.get_playlog_provider_item_ids = AsyncMock(return_value=[(MediaType.AUDIOBOOK, "5")])
    mass.music.get_library_item_by_prov_id = AsyncMock(return_value=Mock(name="book"))
    mass.music.mark_item_unplayed = AsyncMock()
    mass.music.mark_item_played = AsyncMock()
    with patch(
        "music_assistant.providers.bookorbit.playlog_sync.resolve_remote_progress_seconds",
        new=AsyncMock(side_effect=ValueError("unknown duration")),
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
async def test_sync_playlog_skips_unplay_during_outbound_guard() -> None:
    """Absent remote progress during an outbound cool-down must not unplay."""
    mass = Mock()
    mass.music.get_playlog_provider_item_ids = AsyncMock(return_value=[(MediaType.AUDIOBOOK, "5")])
    mass.music.get_library_item_by_prov_id = AsyncMock(return_value=Mock(name="book"))
    mass.music.mark_item_unplayed = AsyncMock()
    mass.music.mark_item_played = AsyncMock()
    guard = InboundProgressGuard()
    guard.note_outbound(5)
    with patch(
        "music_assistant.providers.bookorbit.playlog_sync.resolve_remote_progress_seconds",
        new=AsyncMock(return_value=None),
    ):
        await sync_playlog(
            mass=mass,
            provider_instance_id="bookorbit--test",
            client=Mock(),
            book_ids={5},
            guard=guard,
        )
    mass.music.mark_item_unplayed.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_playlog_rechecks_guard_before_unplay() -> None:
    """An outbound stamp after apply still blocks the clear path."""
    mass = Mock()
    guard = InboundProgressGuard()

    async def playlog_ids(_provider_instance_id: str) -> list[tuple[MediaType, str]]:
        # Outbound write lands after apply confirmed absent, before discard.
        guard.note_outbound(5)
        return [(MediaType.AUDIOBOOK, "5")]

    mass.music.get_playlog_provider_item_ids = AsyncMock(side_effect=playlog_ids)
    mass.music.get_library_item_by_prov_id = AsyncMock(return_value=Mock(name="book"))
    mass.music.mark_item_unplayed = AsyncMock()
    mass.music.mark_item_played = AsyncMock()
    with patch(
        "music_assistant.providers.bookorbit.playlog_sync.resolve_remote_progress_seconds",
        new=AsyncMock(return_value=None),
    ):
        await sync_playlog(
            mass=mass,
            provider_instance_id="bookorbit--test",
            client=Mock(),
            book_ids={5},
            guard=guard,
        )
    mass.music.mark_item_unplayed.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_playlog_rechecks_guard_after_discard_lookup() -> None:
    """An outbound stamp during discard library lookup must still block unplay."""
    mass = Mock()
    guard = InboundProgressGuard()
    mass.music.get_playlog_provider_item_ids = AsyncMock(return_value=[(MediaType.AUDIOBOOK, "5")])

    async def lookup(**_kwargs: Any) -> Mock:
        guard.note_outbound(5)
        return Mock(name="book")

    mass.music.get_library_item_by_prov_id = AsyncMock(side_effect=lookup)
    mass.music.mark_item_unplayed = AsyncMock()
    mass.music.mark_item_played = AsyncMock()
    with patch(
        "music_assistant.providers.bookorbit.playlog_sync.resolve_remote_progress_seconds",
        new=AsyncMock(return_value=None),
    ):
        await sync_playlog(
            mass=mass,
            provider_instance_id="bookorbit--test",
            client=Mock(),
            book_ids={5},
            guard=guard,
        )
    mass.music.mark_item_unplayed.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_playlog_reresolves_before_unplay() -> None:
    """Discard skips clear when remote regained progress after the apply pass."""
    mass = Mock()
    mass.music.get_playlog_provider_item_ids = AsyncMock(return_value=[(MediaType.AUDIOBOOK, "5")])
    mass.music.get_library_item_by_prov_id = AsyncMock(return_value=Mock(name="book"))
    mass.music.mark_item_unplayed = AsyncMock()
    mass.music.mark_item_played = AsyncMock()
    resolve = AsyncMock(side_effect=[None, (False, 120)])
    with patch(
        "music_assistant.providers.bookorbit.playlog_sync.resolve_remote_progress_seconds",
        new=resolve,
    ):
        await sync_playlog(
            mass=mass,
            provider_instance_id="bookorbit--test",
            client=Mock(),
            book_ids={5},
            guard=InboundProgressGuard(),
        )
    assert resolve.await_count == 2
    mass.music.mark_item_unplayed.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_playlog_aborts_on_login_failed(caplog: pytest.LogCaptureFixture) -> None:
    """LoginFailed aborts the batch: one log line, no further fetches, no unplay."""
    mass = Mock()
    mass.music.get_playlog_provider_item_ids = AsyncMock(
        return_value=[(MediaType.AUDIOBOOK, str(i)) for i in range(1, 6)]
    )
    mass.music.get_library_item_by_prov_id = AsyncMock(return_value=Mock(name="book"))
    mass.music.mark_item_unplayed = AsyncMock()
    mass.music.mark_item_played = AsyncMock()
    resolve = AsyncMock(side_effect=LoginFailed("nope"))
    with (
        patch(
            "music_assistant.providers.bookorbit.playlog_sync.resolve_remote_progress_seconds",
            new=resolve,
        ),
        caplog.at_level("WARNING"),
    ):
        await sync_playlog(
            mass=mass,
            provider_instance_id="bookorbit--test",
            client=Mock(),
            book_ids=range(1, 6),
            guard=InboundProgressGuard(),
            concurrency=1,
        )
    assert resolve.await_count == 1
    assert len([r for r in caplog.records if "authentication failed" in r.getMessage()]) == 1
    mass.music.mark_item_unplayed.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_playlog_discard_failure_does_not_skip_rest() -> None:
    """One failing unplay does not prevent the remaining discards."""
    mass = Mock()
    mass.music.get_playlog_provider_item_ids = AsyncMock(
        return_value=[(MediaType.AUDIOBOOK, "5"), (MediaType.AUDIOBOOK, "9")]
    )
    mass.music.get_library_item_by_prov_id = AsyncMock(return_value=Mock(name="book"))
    mass.music.mark_item_played = AsyncMock()
    mass.music.mark_item_unplayed = AsyncMock(side_effect=[RuntimeError("db"), None])
    with patch(
        "music_assistant.providers.bookorbit.playlog_sync.resolve_remote_progress_seconds",
        new=AsyncMock(return_value=None),
    ):
        await sync_playlog(
            mass=mass,
            provider_instance_id="bookorbit--test",
            client=Mock(),
            book_ids={5, 9},
            guard=InboundProgressGuard(),
        )
    assert mass.music.mark_item_unplayed.await_count == 2


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

    async def fake_resolve(_client: Any, book_id: int, **_kwargs: Any) -> tuple[bool, int] | None:
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
async def test_provider_sync_playlog_uses_playlog_and_continue_listening(
    provider: BookOrbitProvider,
) -> None:
    """Provider playlog sync defaults to playlog plus continue-listening, not the catalog."""
    mass = cast("Any", provider.mass)
    client = cast("Any", provider._client)
    provider._catalog = {"1": {"id": 1}, "2": {"id": 2}, "99": {"id": 99}}
    mass.music.get_playlog_provider_item_ids = AsyncMock(
        return_value=[(MediaType.AUDIOBOOK, "1"), (MediaType.PODCAST_EPISODE, "2")]
    )
    client.get_continue_listening_book_ids = AsyncMock(return_value=[3])
    with (
        patch(
            "music_assistant.providers.bookorbit.sync_playlog",
            new_callable=AsyncMock,
        ) as sync_mock,
        patch.object(provider, "_save_playlog_progress_memory", new_callable=AsyncMock),
    ):
        await provider._sync_playlog_from_bookorbit()
    assert sync_mock.await_count == 1
    assert sync_mock.await_args is not None
    kwargs = sync_mock.await_args.kwargs
    assert set(kwargs["book_ids"]) == {1, 3}
    assert kwargs["guard"] is provider._inbound_progress_guard
    assert kwargs["memory"] is provider._playlog_progress_memory


@pytest.mark.asyncio
async def test_provider_sync_playlog_swallows_errors(provider: BookOrbitProvider) -> None:
    """A failing playlog sync never propagates to sync_library."""
    with (
        patch.object(provider, "_playlog_candidate_book_ids", AsyncMock(return_value={1})),
        patch(
            "music_assistant.providers.bookorbit.sync_playlog",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ),
    ):
        await provider._sync_playlog_from_bookorbit()


@pytest.mark.asyncio
async def test_on_played_notes_inbound_guard_and_progress_memory(
    provider: BookOrbitProvider, book_multifile: dict[str, Any]
) -> None:
    """Successful outbound reports arm cool-down and record sent progress for skip-stamps."""
    provider._inbound_progress_guard = InboundProgressGuard()
    provider._playlog_progress_memory = PlaylogProgressMemory()
    book_id = int(book_multifile["id"])
    with (
        patch.object(provider, "_sync_playback_state", new_callable=AsyncMock),
        patch.object(provider, "_save_playlog_progress_memory", new_callable=AsyncMock) as save,
    ):
        await provider.on_played(
            MediaType.AUDIOBOOK,
            str(book_id),
            fully_played=False,
            position=150,
            media_item=Mock(),
        )
    assert provider._inbound_progress_guard.allow_inbound(book_id) is False
    assert provider._playlog_progress_memory.matches_progress(book_id, False, 150)
    save.assert_awaited_once()


@pytest.mark.asyncio
async def test_on_played_skips_progress_memory_when_upload_fails(
    provider: BookOrbitProvider, book_multifile: dict[str, Any]
) -> None:
    """Failed BookOrbit uploads must not record progress as sent."""
    provider._inbound_progress_guard = InboundProgressGuard()
    provider._playlog_progress_memory = PlaylogProgressMemory()
    book_id = int(book_multifile["id"])
    with (
        patch.object(
            provider,
            "_sync_playback_state",
            new_callable=AsyncMock,
            side_effect=ClientError("bo down"),
        ),
        patch.object(provider, "_save_playlog_progress_memory", new_callable=AsyncMock) as save,
    ):
        await provider.on_played(
            MediaType.AUDIOBOOK,
            str(book_id),
            fully_played=False,
            position=150,
            media_item=Mock(),
        )
    assert provider._inbound_progress_guard.allow_inbound(book_id) is False
    assert provider._playlog_progress_memory.get_progress(book_id) is None
    save.assert_not_awaited()


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
async def test_playlog_poll_loop_uses_continue_listening_candidates(
    provider: BookOrbitProvider,
) -> None:
    """Each poll tick syncs continue-listening ids only, not the full playlog."""
    sync_mock = AsyncMock(side_effect=asyncio.CancelledError)
    with (
        patch("music_assistant.providers.bookorbit.asyncio.sleep", AsyncMock()),
        patch.object(provider, "_playlog_poll_candidate_book_ids", AsyncMock(return_value={3, 7})),
        patch.object(provider, "_sync_playlog_from_bookorbit", sync_mock),
        pytest.raises(asyncio.CancelledError),
    ):
        await provider._playlog_poll_loop()
    sync_mock.assert_awaited_once_with({3, 7})


@pytest.mark.asyncio
async def test_unload_cancels_playlog_poll_task(provider: BookOrbitProvider) -> None:
    """Unload cancels the background poll task."""
    cast("Any", provider._client).close = AsyncMock()
    task = asyncio.create_task(asyncio.sleep(3600))
    provider._playlog_poll_task = task
    await provider.unload()
    assert task.cancelled()
    assert provider._playlog_poll_task is None
