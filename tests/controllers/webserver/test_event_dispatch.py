"""Tests for event dispatch and fan-out towards websocket clients."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from music_assistant_models.auth import User, UserRole
from music_assistant_models.enums import EventType

from music_assistant.constants import GUEST_ACCESS_RESTRICTED_PLAYER_ID
from music_assistant.controllers.webserver.controller import WebserverController
from music_assistant.controllers.webserver.websocket_client import WebsocketClientHandler

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


async def drain_event_callbacks() -> None:
    """Yield to the event loop so pending event subscriber callbacks run."""
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
    webserver: WebserverController,
    username: str,
    role: UserRole = UserRole.ADMIN,
    player_filter: list[str] | None = None,
    sendspin_player_id: str | None = None,
) -> WebsocketClientHandler:
    """Create an authenticated + event-subscribed websocket client handler (no real socket)."""
    request = make_mocked_request("GET", "/ws", app=web.Application())
    client = WebsocketClientHandler(webserver, request)
    client._authenticated_user = User(
        user_id=username,
        username=username,
        role=role,
        player_filter=player_filter or [],
    )
    client._sendspin_player_id = sendspin_player_id
    client._subscribe_to_events()
    return client


def get_written_message(client: WebsocketClientHandler) -> str:
    """Pop the next message queued for the client's writer."""
    message = client._to_write.get_nowait()
    assert message is not None
    return message


async def test_event_delivered_to_all_clients(
    mass_minimal: MusicAssistant,
    webserver: WebserverController,
) -> None:
    """An event signalled on the loop thread reaches every subscribed websocket client."""
    client1 = create_ws_client(webserver, "user1")
    client2 = create_ws_client(webserver, "user2")

    mass_minimal.signal_event(EventType.PLAYER_UPDATED, "player1", {"name": "Test Player"})
    await drain_event_callbacks()

    msg1 = get_written_message(client1)
    msg2 = get_written_message(client2)
    assert msg1 == msg2
    assert "player1" in msg1


async def test_tasks_updated_payload_per_user(
    mass_minimal: MusicAssistant,
    webserver: WebserverController,
) -> None:
    """TASKS_UPDATED events carry a per-user payload."""
    mass_minimal.tasks = SimpleNamespace(  # type: ignore[assignment]
        list_tasks_for_user=lambda user: [{"task_id": f"task-for-{user.username}"}]
    )
    client1 = create_ws_client(webserver, "user1")
    client2 = create_ws_client(webserver, "user2")

    mass_minimal.signal_event(EventType.TASKS_UPDATED)
    await drain_event_callbacks()

    msg1 = get_written_message(client1)
    msg2 = get_written_message(client2)
    assert "task-for-user1" in msg1
    assert "task-for-user2" not in msg1
    assert "task-for-user2" in msg2
    assert "task-for-user1" not in msg2


async def test_provider_event_delivered_to_guest_clients(
    mass_minimal: MusicAssistant,
    webserver: WebserverController,
) -> None:
    """PROVIDER_EVENT reaches all clients, including guest-scoped ones."""
    guest = create_ws_client(webserver, "guest1", role=UserRole.GUEST)
    admin = create_ws_client(webserver, "admin1")

    mass_minimal.signal_event(EventType.PROVIDER_EVENT, "music_quiz--abcd/game_state", {"round": 1})
    await drain_event_callbacks()

    msg_guest = get_written_message(guest)
    msg_admin = get_written_message(admin)
    assert msg_guest == msg_admin
    assert "music_quiz--abcd/game_state" in msg_guest


async def test_restricted_guest_events_exclude_dynamic_queues(
    mass_minimal: MusicAssistant,
    webserver: WebserverController,
) -> None:
    """Restricted guests receive own-player and provider events, but no dynamic queues."""
    guest = create_ws_client(
        webserver,
        "guest1",
        role=UserRole.GUEST,
        player_filter=[GUEST_ACCESS_RESTRICTED_PLAYER_ID, "allowed"],
        sendspin_player_id="web_player",
    )

    mass_minimal.signal_event(EventType.PLAYER_UPDATED, "host_player", {"name": "Host"})
    mass_minimal.signal_event(EventType.QUEUE_UPDATED, "host_player", {"name": "Host Queue"})
    mass_minimal.signal_event(EventType.PLAYER_UPDATED, "web_player", {"name": "Web Player"})
    mass_minimal.signal_event(EventType.QUEUE_UPDATED, "web_player", {"name": "Web Queue"})
    mass_minimal.signal_event(EventType.QUEUE_UPDATED, "allowed", {"name": "Allowed Queue"})
    mass_minimal.signal_event(
        EventType.PROVIDER_EVENT,
        "music_quiz--abcd/game_state",
        {"round": 1},
    )
    await drain_event_callbacks()

    messages = [get_written_message(guest) for _ in range(3)]
    assert guest._to_write.empty()
    assert any("Web Player" in message for message in messages)
    assert any("Allowed Queue" in message for message in messages)
    assert any("game_state" in message for message in messages)
    assert all("Host Queue" not in message for message in messages)
    assert all("Web Queue" not in message for message in messages)


@pytest.mark.parametrize(
    "event_type",
    [
        EventType.QUEUE_ADDED,
        EventType.QUEUE_ITEMS_UPDATED,
        EventType.QUEUE_TIME_UPDATED,
        EventType.QUEUE_UPDATED,
    ],
)
async def test_restricted_guest_cannot_claim_host_queue_events_with_sendspin_player(
    mass_minimal: MusicAssistant,
    webserver: WebserverController,
    event_type: EventType,
) -> None:
    """A managed guest cannot claim host queue events through its Sendspin player ID."""
    guest = create_ws_client(
        webserver,
        "guest1",
        role=UserRole.GUEST,
        player_filter=[GUEST_ACCESS_RESTRICTED_PLAYER_ID],
        sendspin_player_id="host_player",
    )

    mass_minimal.signal_event(event_type, "host_player", {"name": "Host Queue"})
    await drain_event_callbacks()

    assert guest._to_write.empty()


async def test_filtered_user_receives_own_sendspin_queue_event(
    mass_minimal: MusicAssistant,
    webserver: WebserverController,
) -> None:
    """An ordinary filtered user receives queue events for their own Sendspin player."""
    user = create_ws_client(
        webserver,
        "user1",
        role=UserRole.USER,
        player_filter=["allowed"],
        sendspin_player_id="web_player",
    )

    mass_minimal.signal_event(EventType.QUEUE_UPDATED, "host_player", {"name": "Host Queue"})
    mass_minimal.signal_event(EventType.QUEUE_UPDATED, "web_player", {"name": "Web Queue"})
    await drain_event_callbacks()

    message = get_written_message(user)
    assert user._to_write.empty()
    assert "Web Queue" in message
