"""Tests for the core Music Assistant server object."""

import asyncio
import pathlib
from unittest.mock import AsyncMock, NonCallableMagicMock, patch

from music_assistant_models.enums import EventType
from music_assistant_models.event import MassEvent

from music_assistant.mass import MusicAssistant
from tests.conftest import _create_mock_zeroconf


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


async def test_mass_fixture_uses_unique_port(mass: MusicAssistant, tmp_path: pathlib.Path) -> None:
    """Two concurrent mass instances must not share a port."""
    storage2 = tmp_path / "data2"
    cache2 = tmp_path / "cache2"
    storage2.mkdir()
    cache2.mkdir()
    mass2 = MusicAssistant(str(storage2), str(cache2))
    mock_zc = _create_mock_zeroconf()
    mock_browser = NonCallableMagicMock()
    with (
        patch(
            "music_assistant.controllers.discovery.controller.AsyncZeroconf",
            return_value=mock_zc,
        ),
        patch(
            "music_assistant.controllers.discovery.controller.AsyncServiceBrowser",
            return_value=mock_browser,
        ),
        patch(
            "music_assistant.controllers.discovery.controller.async_upnp_search",
            new_callable=AsyncMock,
        ),
    ):
        await mass2.start()
        try:
            assert mass2.webserver.publish_port != mass.webserver.publish_port
        finally:
            await mass2.stop()
