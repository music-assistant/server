"""Tests for shared (memoized) event serialization towards websocket clients."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from music_assistant_models.auth import User, UserRole
from music_assistant_models.enums import EventType
from music_assistant_models.event import MassEvent

from music_assistant.controllers.webserver.controller import WebserverController
from music_assistant.controllers.webserver.websocket_client import WebsocketClientHandler

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


async def drain_event_callbacks() -> None:
    """Yield to the event loop so event subscriber callbacks (call_soon) run."""
    await asyncio.sleep(0)


@pytest.fixture
def webserver(mass_minimal: MusicAssistant) -> WebserverController:
    """Return a WebserverController with stubbed serialization dependencies."""
    # stub the controllers referenced by the serialization resolvers
    # (mass_minimal does not set up metadata/translations/tasks)
    mass_minimal.metadata = SimpleNamespace(  # type: ignore[assignment]
        compute_image_id=lambda provider, path: f"{provider}--{path}"
    )
    mass_minimal.translations = SimpleNamespace(  # type: ignore[assignment]
        get_translation=lambda _key, **_kwargs: None
    )
    webserver = WebserverController(mass_minimal)
    mass_minimal.webserver = webserver
    return webserver


def create_ws_client(
    webserver: WebserverController, username: str, locale: str | None
) -> WebsocketClientHandler:
    """Create an authenticated + event-subscribed websocket client handler (no real socket)."""
    request = make_mocked_request("GET", "/ws", app=web.Application())
    client = WebsocketClientHandler(webserver, request)
    client._authenticated_user = User(user_id=username, username=username, role=UserRole.ADMIN)
    client._locale = locale
    client._subscribe_to_events()
    return client


def get_written_message(client: WebsocketClientHandler) -> str:
    """Pop the next message queued for the client's writer."""
    message = client._to_write.get_nowait()
    assert message is not None
    return message


@pytest.fixture
def to_json_spy(monkeypatch: pytest.MonkeyPatch) -> list[MassEvent]:
    """Spy on MassEvent.to_json, recording the event instance for every serialization."""
    calls: list[MassEvent] = []
    orig_to_json = MassEvent.to_json

    def _spy(self: MassEvent, *args: Any, **kwargs: Any) -> Any:
        calls.append(self)
        return orig_to_json(self, *args, **kwargs)

    monkeypatch.setattr(MassEvent, "to_json", _spy)
    return calls


async def test_event_serialized_once_per_locale(
    mass_minimal: MusicAssistant,
    webserver: WebserverController,
    to_json_spy: list[MassEvent],
) -> None:
    """Clients sharing a locale share one render; each extra locale renders once."""
    client1 = create_ws_client(webserver, "user1", "nl_NL")
    client2 = create_ws_client(webserver, "user2", "nl_NL")
    client3 = create_ws_client(webserver, "user3", "de_DE")

    mass_minimal.signal_event(EventType.PLAYER_UPDATED, "player1", {"name": "Test Player"})
    await drain_event_callbacks()

    # one serialization per locale, not per client
    assert len(to_json_spy) == 2
    # every client still received the event
    msg1 = get_written_message(client1)
    msg2 = get_written_message(client2)
    msg3 = get_written_message(client3)
    assert msg1 == msg2 == msg3
    assert "player1" in msg1

    # a new event is rendered again (cache holds only the most recent event)
    mass_minimal.signal_event(EventType.PLAYER_UPDATED, "player1", {"name": "Renamed Player"})
    await drain_event_callbacks()
    assert len(to_json_spy) == 4
    assert "Renamed Player" in get_written_message(client1)


async def test_event_serialized_once_for_default_locale(
    mass_minimal: MusicAssistant,
    webserver: WebserverController,
    to_json_spy: list[MassEvent],
) -> None:
    """Clients without a declared locale (None) also share a single render."""
    client1 = create_ws_client(webserver, "user1", None)
    client2 = create_ws_client(webserver, "user2", None)

    mass_minimal.signal_event(EventType.QUEUE_UPDATED, "queue1", {"queue_id": "queue1"})
    await drain_event_callbacks()

    assert len(to_json_spy) == 1
    assert get_written_message(client1) == get_written_message(client2)


async def test_tasks_updated_rendered_per_user(
    mass_minimal: MusicAssistant,
    webserver: WebserverController,
    to_json_spy: list[MassEvent],
) -> None:
    """TASKS_UPDATED events carry a per-user payload and are rendered per client."""
    mass_minimal.tasks = SimpleNamespace(  # type: ignore[assignment]
        list_tasks_for_user=lambda user: [{"task_id": f"task-for-{user.username}"}]
    )
    client1 = create_ws_client(webserver, "user1", "nl_NL")
    client2 = create_ws_client(webserver, "user2", "nl_NL")

    mass_minimal.signal_event(EventType.TASKS_UPDATED)
    await drain_event_callbacks()

    # rendered once per client (same locale!) with user-specific data
    assert len(to_json_spy) == 2
    assert "task-for-user1" in get_written_message(client1)
    assert "task-for-user2" in get_written_message(client2)


async def test_render_event_message_memoization(
    webserver: WebserverController,
    to_json_spy: list[MassEvent],
) -> None:
    """render_event_message serializes once per (event, locale) and resets per event."""
    event1 = MassEvent(event=EventType.PLAYER_UPDATED, object_id="player1", data={"foo": "bar"})
    event2 = MassEvent(event=EventType.PLAYER_UPDATED, object_id="player1", data={"foo": "baz"})

    rendered = webserver.render_event_message(event1, "nl_NL")
    assert webserver.render_event_message(event1, "nl_NL") == rendered
    assert len(to_json_spy) == 1

    webserver.render_event_message(event1, "de_DE")
    assert len(to_json_spy) == 2

    # a new event invalidates all cached renders of the previous one
    webserver.render_event_message(event2, "nl_NL")
    assert len(to_json_spy) == 3
    webserver.render_event_message(event1, "nl_NL")
    assert len(to_json_spy) == 4
