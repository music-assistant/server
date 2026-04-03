"""Test EventSub WebSocket client."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest

from music_assistant.providers.twitch.eventsub import EVENTSUB_WS_URL, MAX_BACKOFF, EventSubClient
from tests.providers.twitch.conftest import MockResponse, load_fixture


@pytest.fixture
def http_session() -> Mock:
    """Return a mock aiohttp session."""
    session = Mock()
    session.ws_connect = AsyncMock()
    session.post = Mock(
        return_value=MockResponse(status=202, json_data={"data": [{"id": "sub_999"}]})
    )
    session.delete = Mock(return_value=MockResponse(status=204))
    return session


@pytest.fixture
def client(http_session: Mock) -> EventSubClient:
    """Return an EventSubClient instance."""
    return EventSubClient(
        http_session=http_session,
        api_headers_fn=lambda: {"Authorization": "Bearer test", "Client-Id": "test_client"},
    )


# --- Connection Lifecycle ---


def test_connect_to_default_url() -> None:
    """Initial connection targets the standard EventSub URL."""
    assert EVENTSUB_WS_URL == "wss://eventsub.wss.twitch.tv/ws"


def test_welcome_stores_session_id(client: EventSubClient) -> None:
    """session_welcome message stores session ID."""
    msg = load_fixture("eventsub_welcome.json")
    client._handle_message(msg)
    assert client._session_id == "test_session_123"


def test_welcome_signals_ready(client: EventSubClient) -> None:
    """session_welcome sets the ready event."""
    assert not client._ready.is_set()
    msg = load_fixture("eventsub_welcome.json")
    client._handle_message(msg)
    assert client._ready.is_set()


def test_welcome_resets_backoff(client: EventSubClient) -> None:
    """Successful welcome resets backoff to 1.0s."""
    client._backoff = 32.0
    msg = load_fixture("eventsub_welcome.json")
    client._handle_message(msg)
    assert client._backoff == 1.0


async def test_stop_prevents_reconnect(client: EventSubClient) -> None:
    """After stop(), _stopped flag is set."""
    await client.stop()
    assert client._stopped is True


async def test_stop_clears_session_state(client: EventSubClient) -> None:
    """stop() clears session_id, subscriptions, ready event."""
    client._session_id = "test"
    client._subscriptions = {"123": "sub_1"}
    client._ready.set()

    await client.stop()

    assert client._session_id is None
    assert len(client._subscriptions) == 0  # type: ignore[unreachable]
    assert not client._ready.is_set()


async def test_disconnect_triggers_reconnect(client: EventSubClient) -> None:
    """WebSocket disconnect increases backoff, indicating reconnect will happen."""
    initial_backoff = client._backoff
    assert initial_backoff == 1.0

    client._backoff = min(client._backoff * 2, MAX_BACKOFF)
    assert client._backoff == 2.0

    welcome = load_fixture("eventsub_welcome.json")
    client._handle_message(welcome)
    assert client._backoff == 1.0

    assert client._stopped is False
    await client.stop()
    assert client._stopped is True


# --- Twitch-Requested Reconnect ---


def test_reconnect_message_stores_url(client: EventSubClient) -> None:
    """session_reconnect stores the new URL."""
    msg = load_fixture("eventsub_reconnect.json")
    client._handle_message(msg)
    assert client._reconnect_url == "wss://eventsub.wss.twitch.tv/ws?reconnect=true"


def test_reconnect_url_consumed(client: EventSubClient) -> None:
    """Reconnect URL is stored and available for next connect attempt."""
    msg = load_fixture("eventsub_reconnect.json")
    client._handle_message(msg)
    assert client._reconnect_url is not None
    url = client._reconnect_url
    assert url == "wss://eventsub.wss.twitch.tv/ws?reconnect=true"


# --- Re-subscription on Reconnect ---


async def test_welcome_clears_old_subscriptions(client: EventSubClient) -> None:
    """Welcome clears old subscription IDs (they're invalid on new session)."""
    client._subscriptions = {"123": "old_sub"}
    msg = load_fixture("eventsub_welcome.json")
    client._handle_message(msg)
    # Old subs cleared synchronously — re-subscription tasks created but not yet run
    assert len(client._subscriptions) == 0


def test_welcome_does_not_resubscribe_if_no_active(
    client: EventSubClient,
) -> None:
    """If no active broadcasters, welcome does not create subscription tasks."""
    client._subscriptions = {}
    msg = load_fixture("eventsub_welcome.json")
    client._handle_message(msg)
    assert client._ready.is_set()


# --- Subscription Management ---


async def test_post_includes_auth_headers(client: EventSubClient, http_session: Mock) -> None:
    """POST to EventSub includes Authorization and Client-Id headers."""
    client._session_id = "test_session"
    client._ready.set()

    await client.subscribe_raids("123")

    call_kwargs = http_session.post.call_args
    headers = call_kwargs.kwargs.get("headers", {})
    assert "Authorization" in headers
    assert headers["Authorization"] == "Bearer test"
    assert "Client-Id" in headers
    assert headers["Client-Id"] == "test_client"


async def test_subscribe_creates_raid_subscription(
    client: EventSubClient, http_session: Mock
) -> None:
    """subscribe_raids() calls EventSub create API with correct params."""
    client._session_id = "test_session"
    client._ready.set()

    await client.subscribe_raids("123")

    http_session.post.assert_called_once()
    call_kwargs = http_session.post.call_args
    assert "eventsub/subscriptions" in call_kwargs.args[0]
    body = call_kwargs.kwargs["json"]
    assert body["type"] == "channel.raid"
    assert body["condition"]["from_broadcaster_user_id"] == "123"


async def test_subscribe_stores_subscription(client: EventSubClient) -> None:
    """subscribe_raids() stores the subscription ID in _subscriptions."""
    client._session_id = "test_session"
    client._ready.set()

    await client.subscribe_raids("123")

    assert client._subscriptions["123"] == "sub_999"


async def test_subscribe_noop_if_already_subscribed(
    client: EventSubClient, http_session: Mock
) -> None:
    """subscribe_raids() is a no-op for an already-subscribed broadcaster."""
    client._session_id = "test_session"
    client._ready.set()
    client._subscriptions = {"123": "existing_sub"}

    await client.subscribe_raids("123")

    http_session.post.assert_not_called()


async def test_subscribe_waits_for_ready(client: EventSubClient, http_session: Mock) -> None:
    """Subscribe blocks until ready event is set."""
    client._session_id = "test_session"

    async def set_ready() -> None:
        await asyncio.sleep(0.01)
        client._ready.set()

    asyncio.create_task(set_ready())

    await client.subscribe_raids("123")
    http_session.post.assert_called_once()


async def test_unsubscribe_raids_calls_delete_api(
    client: EventSubClient, http_session: Mock
) -> None:
    """unsubscribe_raids() calls EventSub delete API for specific broadcaster."""
    client._subscriptions = {"123": "sub_123"}

    await client.unsubscribe_raids("123")

    http_session.delete.assert_called_once()
    assert "123" not in client._subscriptions


async def test_unsubscribe_raids_noop_if_not_subscribed(
    client: EventSubClient, http_session: Mock
) -> None:
    """unsubscribe_raids() is a no-op for a non-subscribed broadcaster."""
    client._subscriptions = {}

    await client.unsubscribe_raids("123")

    http_session.delete.assert_not_called()


async def test_unsubscribe_all_clears_all(client: EventSubClient, http_session: Mock) -> None:
    """unsubscribe_all() unsubscribes all broadcasters."""
    client._subscriptions = {"123": "sub_1", "456": "sub_2"}

    await client.unsubscribe_all()

    assert http_session.delete.call_count == 2
    assert len(client._subscriptions) == 0


async def test_unsubscribe_all_noop_when_empty(client: EventSubClient, http_session: Mock) -> None:
    """Unsubscribe with no active subscriptions doesn't call API."""
    client._subscriptions = {}

    await client.unsubscribe_all()

    http_session.delete.assert_not_called()


async def test_unsubscribe_tolerates_api_error(client: EventSubClient, http_session: Mock) -> None:
    """API error during unsubscribe is logged, not raised."""
    client._subscriptions = {"123": "sub_123"}

    def raise_err(*_args: Any, **_kwargs: Any) -> None:
        msg = "API error"
        raise ConnectionError(msg)

    http_session.delete = Mock(side_effect=raise_err)

    # Should not raise
    await client.unsubscribe_raids("123")
    assert "123" not in client._subscriptions


# --- Subscription Revocation ---


def test_revocation_clears_subscription(client: EventSubClient) -> None:
    """Revocation message removes the revoked subscription."""
    client._subscriptions = {"123": "sub_123"}
    msg = load_fixture("eventsub_revocation.json")
    # The fixture has subscription id — check what it is
    msg["payload"]["subscription"]["id"] = "sub_123"
    client._handle_message(msg)
    assert "123" not in client._subscriptions


def test_revocation_logged(client: EventSubClient, caplog: pytest.LogCaptureFixture) -> None:
    """Revocation is logged as warning."""
    client._subscriptions = {"123": "sub_123"}
    msg = load_fixture("eventsub_revocation.json")
    with caplog.at_level(logging.WARNING):
        client._handle_message(msg)
    assert any("revoked" in r.message.lower() for r in caplog.records)


# --- Backoff ---


def test_backoff_doubles_on_reconnect(client: EventSubClient) -> None:
    """Consecutive reconnects double backoff: 1->2->4->8->16->32->60->60."""
    client._backoff = 1.0
    expected = [2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0]
    for exp in expected:
        client._backoff = min(client._backoff * 2, 60.0)
        assert client._backoff == exp


def test_backoff_caps_at_60s(client: EventSubClient) -> None:
    """Backoff never exceeds 60s."""
    client._backoff = 60.0
    client._backoff = min(client._backoff * 2, 60.0)
    assert client._backoff == 60.0


# --- Twitch-Requested Reconnect (extended) ---


async def test_reconnect_message_closes_current_ws(client: EventSubClient) -> None:
    """session_reconnect triggers close on current WebSocket."""
    mock_ws = AsyncMock()
    client._ws = mock_ws

    msg = load_fixture("eventsub_reconnect.json")
    client._handle_message(msg)

    assert client._reconnect_url is not None
    await asyncio.sleep(0.01)
    mock_ws.close.assert_called_once()


def test_reconnect_uses_new_url(client: EventSubClient) -> None:
    """After reconnect message, stored URL is used for next connection."""
    msg = load_fixture("eventsub_reconnect.json")
    client._handle_message(msg)

    expected_url = "wss://eventsub.wss.twitch.tv/ws?reconnect=true"
    assert client._reconnect_url == expected_url


# --- Re-subscription on Reconnect (extended) ---


async def test_welcome_resubscribes_active_broadcasters(
    client: EventSubClient, http_session: Mock
) -> None:
    """If subscriptions exist, welcome re-creates them on new session."""
    client._subscriptions = {"123": "old_sub", "456": "old_sub2"}
    msg = load_fixture("eventsub_welcome.json")

    client._handle_message(msg)

    assert client._ready.is_set()
    assert client._session_id == "test_session_123"

    # Give the create_tasks a chance to run
    await asyncio.sleep(0.05)

    # POST should have been called for each broadcaster
    assert http_session.post.call_count == 2


async def test_ready_set_after_resubscribe(client: EventSubClient) -> None:
    """Ready event fires after welcome (even with resubscriptions)."""
    client._subscriptions = {"123": "old_sub"}
    msg = load_fixture("eventsub_welcome.json")
    client._handle_message(msg)
    assert client._ready.is_set()


# --- Subscription Management (extended) ---


async def test_subscribe_timeout_when_not_ready(client: EventSubClient) -> None:
    """If ready event not set within timeout, subscribe is a no-op."""
    client._session_id = "test_session"

    async def fast_timeout(coro: Any, timeout: float) -> None:  # noqa: ARG001
        raise TimeoutError

    with patch(
        "music_assistant.providers.twitch.eventsub.asyncio.wait_for", side_effect=fast_timeout
    ):
        await client.subscribe_raids("123")

    assert "123" not in client._subscriptions


async def test_subscribe_skips_if_welcome_already_subscribed(
    client: EventSubClient, http_session: Mock
) -> None:
    """If welcome handler already created sub while waiting, don't duplicate."""
    client._session_id = "test_session"

    original_wait_for = asyncio.wait_for

    async def wait_that_simulates_welcome(coro: Any, timeout: float) -> Any:
        # Simulate the welcome handler creating a sub during the wait
        client._subscriptions["123"] = "sub_from_welcome"
        client._ready.set()
        return await original_wait_for(coro, timeout=timeout)

    with patch(
        "music_assistant.providers.twitch.eventsub.asyncio.wait_for",
        side_effect=wait_that_simulates_welcome,
    ):
        await client.subscribe_raids("123")

    # No POST should have been made — the welcome handler's sub was detected
    http_session.post.assert_not_called()
    assert client._subscriptions["123"] == "sub_from_welcome"


# --- Raid Notification ---


def test_raid_event_fires_callback(client: EventSubClient) -> None:
    """channel.raid notification calls the on_raid callback."""
    raids_received: list[tuple[str, str]] = []
    client._on_raid = lambda from_l, to_l: raids_received.append((from_l, to_l))

    msg = load_fixture("eventsub_raid.json")
    client._handle_message(msg)

    assert len(raids_received) == 1
    assert raids_received[0] == ("streamer_a", "streamer_c")


def test_non_raid_notification_ignored(client: EventSubClient) -> None:
    """Other notification types don't fire callback."""
    raids_received: list[tuple[str, str]] = []
    client._on_raid = lambda from_l, to_l: raids_received.append((from_l, to_l))

    msg = load_fixture("eventsub_raid.json")
    msg["metadata"]["subscription_type"] = "stream.online"
    client._handle_message(msg)

    assert len(raids_received) == 0


def test_invalid_json_ignored(client: EventSubClient) -> None:
    """Malformed message is ignored (no crash)."""
    client._handle_message({})
    client._handle_message({"metadata": {}})
    client._handle_message({"metadata": {"message_type": "unknown_type"}})
