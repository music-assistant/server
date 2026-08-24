"""Tests for the core Music Assistant server object."""

import asyncio
import logging
from typing import TYPE_CHECKING

from music_assistant_models.enums import EventType

from music_assistant.constants import MASS_LOGGER_NAME
from music_assistant.mass import MusicAssistant

if TYPE_CHECKING:
    import pytest
    from music_assistant_models.event import MassEvent


async def test_start_and_stop_server(mass: MusicAssistant) -> None:
    """Test that music assistant starts and stops cleanly."""
    domains = frozenset(p.domain for p in mass.get_provider_manifests())
    core_providers = frozenset(
        (
            "builtin",
            "cache",
            "discovery",
            "metadata",
            "music",
            "player_queues",
            "players",
            "streams",
        )
    )
    assert domains.issuperset(core_providers)


async def test_events(mass: MusicAssistant) -> None:
    """Test that events sent by signal_event can be seen by subscribe."""
    filters: list[tuple[EventType | tuple[EventType, ...] | None, str | tuple[str, ...] | None]] = [
        (None, None),
        (EventType.UNKNOWN, None),
        ((EventType.UNKNOWN, EventType.PLAYER_ADDED), None),
        (None, "myid1"),
        (None, ("myid1", "myid2")),
        (EventType.UNKNOWN, "myid1"),
    ]

    for event_filter, id_filter in filters:
        flag = False

        def _ev(event: MassEvent) -> None:
            assert event.event == EventType.UNKNOWN
            assert event.data == "mytestdata"
            assert event.object_id == "myid1"
            nonlocal flag
            flag = True

        remove_cb = mass.subscribe(_ev, event_filter, id_filter)

        mass.signal_event(EventType.UNKNOWN, "myid1", "mytestdata")
        await asyncio.sleep(0)
        assert flag is True

        flag = False
        remove_cb()
        mass.signal_event(EventType.UNKNOWN)
        await asyncio.sleep(0)
        assert flag is False


async def test_create_task_failure_logged(
    mass_minimal: MusicAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Test that a failed task without awaiter is logged, also without debug logging."""

    async def _boom() -> None:
        raise ValueError("boom")

    with caplog.at_level(logging.INFO, logger=MASS_LOGGER_NAME):
        mass_minimal.create_task(_boom)
        # allow the task's done callback to run
        await asyncio.sleep(0)

    assert any(
        "boom" in record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING
    )


async def test_create_task_replacement_stays_tracked(mass_minimal: MusicAssistant) -> None:
    """Test that a finished task does not untrack a replacement with the same task_id."""
    task_id = "test_replacement"
    release = asyncio.Event()

    async def _instant() -> None:
        return

    async def _blocked() -> None:
        await release.wait()

    # a task that never suspends is already finished when create_task returns,
    # so its done callback is still queued while the caller continues
    first = mass_minimal.create_task(_instant(), task_id=task_id)
    assert first.done()
    second = mass_minimal.create_task(_blocked(), task_id=task_id)
    assert second is not first
    assert mass_minimal._tracked_tasks[task_id] is second

    # allow the first task's done callback to run
    await asyncio.sleep(0)

    assert mass_minimal._tracked_tasks.get(task_id) is second
    # a later caller must join the in-flight task instead of starting a duplicate
    assert mass_minimal.create_task(_blocked(), task_id=task_id) is second

    release.set()
    await second
    assert task_id not in mass_minimal._tracked_tasks


async def test_create_task_abort_existing_tracks_replacement(
    mass_minimal: MusicAssistant,
) -> None:
    """Test that a task aborted in favour of a replacement does not untrack it."""
    task_id = "test_abort_existing"

    async def _blocked() -> None:
        await asyncio.Event().wait()

    first = mass_minimal.create_task(_blocked(), task_id=task_id)
    second = mass_minimal.create_task(_blocked(), task_id=task_id, abort_existing=True)
    assert second is not first

    # the aborted task runs its done callback only once the cancellation is delivered
    await asyncio.wait((first,))
    assert first.cancelled()
    assert mass_minimal._tracked_tasks.get(task_id) is second

    second.cancel()
    await asyncio.wait((second,))
    assert task_id not in mass_minimal._tracked_tasks
