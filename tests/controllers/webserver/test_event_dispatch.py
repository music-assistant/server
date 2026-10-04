"""Tests for event dispatch and fan-out towards websocket clients."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import TYPE_CHECKING

from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from music_assistant_models.auth import User, UserRole
from music_assistant_models.enums import EventType, MediaType
from music_assistant_models.favorite_update import FavoriteUpdate
from music_assistant_models.media_items import Track

from music_assistant.controllers.webserver.controller import WebserverController
from music_assistant.controllers.webserver.websocket_client import WebsocketClientHandler

if TYPE_CHECKING:
    import pytest

    from music_assistant.mass import MusicAssistant


async def drain_event_callbacks() -> None:
    """Yield to the event loop so pending event subscriber callbacks run."""
    await asyncio.sleep(0)


def create_ws_client(
    webserver: WebserverController, username: str, role: UserRole = UserRole.ADMIN
) -> WebsocketClientHandler:
    """Create an authenticated + event-subscribed websocket client handler (no real socket)."""
    request = make_mocked_request("GET", "/ws", app=web.Application())
    client = WebsocketClientHandler(webserver, request)
    client._authenticated_user = User(user_id=username, username=username, role=role)
    client._subscribe_to_events()
    return client


def get_written_message(client: WebsocketClientHandler) -> str:
    """Pop the next message queued for the client's writer."""
    message = client._to_write.get_nowait()
    assert isinstance(message, str)
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


def _restricted_client(
    webserver: WebserverController,
    *,
    player_filter: list[str],
) -> WebsocketClientHandler:
    """Create a websocket client for a restricted (non-admin) user with a player filter."""
    client = create_ws_client(webserver, "restricted", role=UserRole.USER)
    client._authenticated_user = User(
        user_id="restricted",
        username="restricted",
        role=UserRole.USER,
        player_filter=player_filter,
    )
    return client


def _stub_players(monkeypatch: pytest.MonkeyPatch, mass: MusicAssistant, **players: bool) -> None:
    """Stub the player registry with the given players, mapping id -> is_private."""
    registry = {
        player_id: SimpleNamespace(player_id=player_id, private=private)
        for player_id, private in players.items()
    }
    monkeypatch.setattr(mass, "players", SimpleNamespace(get_player=registry.get), raising=False)


async def test_player_events_honor_the_user_player_filter(
    mass_minimal: MusicAssistant,
    webserver: WebserverController,
) -> None:
    """A restricted user only receives events for players in their filter."""
    client = _restricted_client(webserver, player_filter=["kitchen"])

    mass_minimal.signal_event(EventType.PLAYER_UPDATED, "kitchen", {"name": "Kitchen"})
    await drain_event_callbacks()
    assert "kitchen" in get_written_message(client)

    mass_minimal.signal_event(EventType.PLAYER_UPDATED, "living_room", {"name": "Living room"})
    await drain_event_callbacks()
    assert client._to_write.empty()


async def test_own_private_client_player_events_are_delivered(
    mass_minimal: MusicAssistant,
    webserver: WebserverController,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A restricted user receives events for the private client player they connected on."""
    _stub_players(monkeypatch, mass_minimal, browser=True)
    client = _restricted_client(webserver, player_filter=["kitchen"])
    client.bind_sendspin_player("browser")

    mass_minimal.signal_event(EventType.PLAYER_UPDATED, "browser", {"name": "Browser"})
    await drain_event_callbacks()
    assert "browser" in get_written_message(client)


async def test_events_reach_own_client_bound_before_registration(
    mass_minimal: MusicAssistant,
    webserver: WebserverController,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Binding can precede registration; once the player exists its events reach the owner."""
    _stub_players(monkeypatch, mass_minimal)  # player not registered yet at bind time
    client = _restricted_client(webserver, player_filter=["kitchen"])
    client.bind_sendspin_player("browser")

    _stub_players(monkeypatch, mass_minimal, browser=True)  # player now registered
    mass_minimal.signal_event(EventType.PLAYER_ADDED, "browser", {"name": "Browser"})
    await drain_event_callbacks()
    assert "browser" in get_written_message(client)


async def test_own_private_client_removal_event_is_delivered(
    mass_minimal: MusicAssistant,
    webserver: WebserverController,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The own private client still gets its removal event after it left the registry."""
    _stub_players(monkeypatch, mass_minimal, browser=True)
    client = _restricted_client(webserver, player_filter=["kitchen"])
    client.bind_sendspin_player("browser")
    # an event while the player exists latches its private status
    mass_minimal.signal_event(EventType.PLAYER_ADDED, "browser", {"name": "Browser"})
    await drain_event_callbacks()
    assert "browser" in get_written_message(client)

    _stub_players(monkeypatch, mass_minimal)  # player has left the registry
    mass_minimal.signal_event(EventType.PLAYER_REMOVED, "browser", {})
    await drain_event_callbacks()
    assert "browser" in get_written_message(client)


async def test_own_client_removal_delivered_after_becoming_restricted(
    mass_minimal: MusicAssistant,
    webserver: WebserverController,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A client restricted only after registering still receives its own removal event."""
    _stub_players(monkeypatch, mass_minimal, browser=True)
    client = _restricted_client(webserver, player_filter=[])  # unrestricted for now
    client.bind_sendspin_player("browser")
    # while unrestricted, the registration event latches the private status
    mass_minimal.signal_event(EventType.PLAYER_ADDED, "browser", {"name": "Browser"})
    await drain_event_callbacks()
    assert "browser" in get_written_message(client)

    # the user is restricted later and the player then leaves the registry
    client._authenticated_user = User(
        user_id="restricted", username="restricted", role=UserRole.USER, player_filter=["kitchen"]
    )
    _stub_players(monkeypatch, mass_minimal)
    mass_minimal.signal_event(EventType.PLAYER_REMOVED, "browser", {})
    await drain_event_callbacks()
    assert "browser" in get_written_message(client)


async def test_shared_speaker_claimed_as_client_stays_filtered(
    mass_minimal: MusicAssistant,
    webserver: WebserverController,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Announcing a shared speaker's id as the client id does not unlock its events."""
    _stub_players(monkeypatch, mass_minimal, living_room=False)
    client = _restricted_client(webserver, player_filter=["kitchen"])
    client.bind_sendspin_player("living_room")  # a shared, non-private speaker

    mass_minimal.signal_event(EventType.PLAYER_UPDATED, "living_room", {"name": "Living room"})
    await drain_event_callbacks()
    assert client._to_write.empty()


async def test_full_access_user_receives_events_outside_their_filter(
    mass_minimal: MusicAssistant,
    webserver: WebserverController,
) -> None:
    """A full-access (admin) user is never limited by a stored player filter."""
    admin = create_ws_client(webserver, "admin1")
    admin._authenticated_user = User(
        user_id="admin1", username="admin1", role=UserRole.ADMIN, player_filter=["kitchen"]
    )

    mass_minimal.signal_event(EventType.PLAYER_UPDATED, "living_room", {"name": "Living room"})
    await drain_event_callbacks()
    assert "living_room" in get_written_message(admin)


async def test_favorite_update_reaches_its_own_user_only(
    mass_minimal: MusicAssistant,
    webserver: WebserverController,
) -> None:
    """A like or dislike is announced to the connection of its user and to nobody else."""
    client1 = create_ws_client(webserver, "user1")
    client2 = create_ws_client(webserver, "user2")
    anonymous = WebsocketClientHandler(
        webserver, make_mocked_request("GET", "/ws", app=web.Application())
    )
    anonymous._subscribe_to_events()

    mass_minimal.signal_event(
        EventType.FAVORITE_UPDATED,
        "library://track/1",
        FavoriteUpdate(
            uri="library://track/1",
            media_type=MediaType.TRACK,
            item_id="1",
            favorite=False,
            user_id="user1",
        ),
    )
    await drain_event_callbacks()

    assert json.loads(get_written_message(client1))["data"]["user_id"] == "user1"
    assert client2._to_write.empty()
    assert anonymous._to_write.empty()


async def test_media_item_events_carry_no_favorite_state(
    mass_minimal: MusicAssistant,
    webserver: WebserverController,
) -> None:
    """A library item goes out without the favorite state of the user that touched it."""
    client = create_ws_client(webserver, "user1")

    for state in (True, False):
        track = Track(
            item_id="1",
            provider="library",
            name="Track",
            provider_mappings=set(),
            favorite=state,
        )
        mass_minimal.signal_event(EventType.MEDIA_ITEM_UPDATED, track.uri, track)
        await drain_event_callbacks()

        assert json.loads(get_written_message(client))["data"]["favorite"] is None
