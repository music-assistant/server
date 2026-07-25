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
        ((EventType.UNKNOWN, EventType.AUTH_SESSION), None),
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


async def test_background_task_failure_is_retrieved_and_logged(
    mass_minimal: MusicAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Background task failures remain visible outside debug logging."""

    async def raise_error() -> None:
        raise RuntimeError("background task failed")

    caplog.set_level(logging.INFO, logger=MASS_LOGGER_NAME)
    task = mass_minimal.create_task(raise_error())
    await asyncio.sleep(0)

    assert task.done()
    assert "background task failed" in caplog.text
