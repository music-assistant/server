"""EventSub WebSocket client for Twitch raid following."""

# mypy: disable-error-code="unreachable"
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable


logger = logging.getLogger(__name__)

EVENTSUB_WS_URL = "wss://eventsub.wss.twitch.tv/ws"
MAX_BACKOFF = 60.0


class EventSubClient:
    """Async EventSub WebSocket client for channel.raid subscriptions."""

    def __init__(
        self,
        http_session: Any,
        api_headers_fn: Callable[[], dict[str, str]],
    ) -> None:
        """Initialize EventSub client.

        Args:
            http_session: aiohttp ClientSession for WebSocket + API calls
            api_headers_fn: callable returning auth headers for Twitch API

        """
        self._http_session = http_session
        self._api_headers_fn = api_headers_fn

        self._ws: Any | None = None
        self._session_id: str | None = None
        self._subscriptions: dict[str, str] = {}  # broadcaster_user_id -> subscription_id
        self._reconnect_url: str | None = None

        self._ready = asyncio.Event()
        self._stopped = False
        self._backoff = 1.0
        self._listen_task: asyncio.Task[None] | None = None
        self._on_raid: Callable[[str, str], Any] | None = None
        self._subscribe_pending: set[str] = set()

    @property
    def is_connected(self) -> bool:
        """Return whether the WebSocket is connected."""
        return self._ws is not None and not self._stopped

    async def start(self, on_raid: Callable[[str, str], Any]) -> None:
        """Start the EventSub WebSocket connection.

        Args:
            on_raid: callback(from_login, to_login) when a raid is received

        """
        self._on_raid = on_raid
        self._stopped = False
        self._listen_task = asyncio.create_task(self._connect_loop())

    async def stop(self) -> None:
        """Stop the EventSub WebSocket and clean up."""
        logger.debug("EventSub: stopping WebSocket and cleaning up")
        self._stopped = True
        self._session_id = None
        self._subscriptions.clear()
        self._ready.clear()

        if self._ws is not None:
            await self._ws.close()
            self._ws = None

        if self._listen_task is not None:
            self._listen_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._listen_task
            self._listen_task = None

    async def subscribe_raids(self, broadcaster_user_id: str) -> None:
        """Subscribe to channel.raid events for a broadcaster. No-op if already subscribed."""
        if broadcaster_user_id in self._subscriptions:
            return

        # Wait for WebSocket to be ready
        self._subscribe_pending.add(broadcaster_user_id)
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=10.0)
        except TimeoutError:
            logger.warning(
                "EventSub not ready — cannot subscribe to raids for %s",
                broadcaster_user_id,
            )
            return
        finally:
            self._subscribe_pending.discard(broadcaster_user_id)

        # Check if welcome handler already re-subscribed (reconnect case)
        if broadcaster_user_id in self._subscriptions:
            return

        await self._create_subscription(broadcaster_user_id)

    async def unsubscribe_raids(self, broadcaster_user_id: str) -> None:
        """Unsubscribe from raid events for a specific broadcaster."""
        sub_id = self._subscriptions.pop(broadcaster_user_id, None)
        if not sub_id:
            return

        try:
            async with self._http_session.delete(
                "https://api.twitch.tv/helix/eventsub/subscriptions",
                headers=self._api_headers_fn(),
                params={"id": sub_id},
            ):
                pass
            logger.debug(
                "EventSub: unsubscribed %s for broadcaster %s",
                sub_id,
                broadcaster_user_id,
            )
        except Exception:
            logger.warning("EventSub: failed to unsubscribe %s", sub_id, exc_info=True)

    async def unsubscribe_all(self) -> None:
        """Unsubscribe from all active EventSub subscriptions."""
        broadcaster_ids = list(self._subscriptions.keys())
        for broadcaster_id in broadcaster_ids:
            await self.unsubscribe_raids(broadcaster_id)

    async def _create_subscription(self, broadcaster_user_id: str) -> None:
        """Create an EventSub subscription for channel.raid."""
        body = {
            "type": "channel.raid",
            "version": "1",
            "condition": {"from_broadcaster_user_id": broadcaster_user_id},
            "transport": {"method": "websocket", "session_id": self._session_id},
        }
        try:
            async with self._http_session.post(
                "https://api.twitch.tv/helix/eventsub/subscriptions",
                headers={**self._api_headers_fn(), "Content-Type": "application/json"},
                json=body,
            ) as response:
                if response.status in (200, 202):
                    data = await response.json()
                    self._subscriptions[broadcaster_user_id] = data["data"][0]["id"]
                    logger.debug(
                        "EventSub: subscribed to channel.raid for %s (sub=%s)",
                        broadcaster_user_id,
                        self._subscriptions[broadcaster_user_id],
                    )
                else:
                    text = await response.text()
                    logger.warning("EventSub: subscribe failed: %s %s", response.status, text)
        except Exception:
            logger.warning("EventSub: failed to create subscription", exc_info=True)

    async def _connect_loop(self) -> None:
        """Run the connection loop — connect, listen, reconnect with backoff."""
        while not self._stopped:
            url = self._reconnect_url or EVENTSUB_WS_URL
            self._reconnect_url = None  # consume after use

            try:
                self._ws = await self._http_session.ws_connect(url)
                async for msg in self._ws:
                    if self._stopped:
                        break
                    data = getattr(msg, "data", None)
                    if not isinstance(data, str):
                        continue
                    try:
                        self._handle_message(json.loads(data))
                    except (json.JSONDecodeError, KeyError, TypeError):
                        logger.debug("EventSub: ignoring malformed message")
            except asyncio.CancelledError:
                return
            except Exception:
                logger.debug("EventSub: WebSocket disconnected", exc_info=True)
            finally:
                self._ws = None
                self._ready.clear()

            if self._stopped:
                return

            # Backoff before reconnect
            logger.debug("EventSub: reconnecting in %.1fs", self._backoff)
            await asyncio.sleep(self._backoff)
            self._backoff = min(self._backoff * 2, MAX_BACKOFF)

    def _handle_message(self, msg: dict[str, Any]) -> None:
        """Dispatch an EventSub WebSocket message by type."""
        msg_type = msg.get("metadata", {}).get("message_type", "")

        if msg_type == "session_welcome":
            self._handle_welcome(msg)
        elif msg_type == "session_reconnect":
            self._handle_reconnect(msg)
        elif msg_type == "notification":
            self._handle_notification(msg)
        elif msg_type == "revocation":
            self._handle_revocation(msg)
        # session_keepalive is a no-op

    def _handle_welcome(self, msg: dict[str, Any]) -> None:
        """Handle session_welcome — store session ID, re-subscribe if needed."""
        self._session_id = msg["payload"]["session"]["id"]
        self._backoff = 1.0  # reset backoff

        # Old subscriptions are invalid on the new session. Keep the broadcaster
        # IDs (we need to re-subscribe) but clear the subscription IDs.
        stale_broadcasters = [
            bid for bid in self._subscriptions if bid not in self._subscribe_pending
        ]
        self._subscriptions.clear()

        # Re-subscribe for all broadcasters that aren't already being handled
        # by a concurrent subscribe_raids call.
        for broadcaster_id in stale_broadcasters:
            asyncio.create_task(self._create_subscription(broadcaster_id))

        self._ready.set()

    def _handle_reconnect(self, msg: dict[str, Any]) -> None:
        """Handle session_reconnect — store new URL, close current WS."""
        self._reconnect_url = msg["payload"]["session"]["reconnect_url"]
        if self._ws is not None:
            asyncio.create_task(self._ws.close())

    def _handle_notification(self, msg: dict[str, Any]) -> None:
        """Handle notification — fire raid callback if channel.raid."""
        sub_type = msg.get("metadata", {}).get("subscription_type", "")
        if sub_type != "channel.raid":
            return

        event = msg["payload"]["event"]
        from_login = event["from_broadcaster_user_login"]
        to_login = event["to_broadcaster_user_login"]

        if self._on_raid:
            self._on_raid(from_login, to_login)

    def _handle_revocation(self, msg: dict[str, Any]) -> None:
        """Handle revocation — clear subscription, log warning."""
        sub = msg.get("payload", {}).get("subscription", {})
        logger.warning(
            "EventSub: subscription revoked: type=%s status=%s",
            sub.get("type"),
            sub.get("status"),
        )
        # Remove the revoked subscription by its ID
        revoked_id = sub.get("id")
        if revoked_id:
            self._subscriptions = {
                bid: sid for bid, sid in self._subscriptions.items() if sid != revoked_id
            }
