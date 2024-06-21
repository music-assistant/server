"""Tests for the core Music Assistant server object."""

import asyncio

from music_assistant.common.models.enums import EventType
from music_assistant.common.models.event import MassEvent
from music_assistant.server.server import MusicAssistant


async def test_start_and_stop_server(mass: MusicAssistant) -> None:
    """Test that music assistant starts and stops cleanly."""
    domains = frozenset(p.domain for p in mass.get_provider_manifests())
    core_providers = frozenset(
        ("builtin", "cache", "metadata", "music", "player_queues", "players", "streams")
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
