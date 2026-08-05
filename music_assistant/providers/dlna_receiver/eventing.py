"""
UPnP GENA Eventing — subscription management and NOTIFY dispatch.

Implements the General Event Notification Architecture (GENA) protocol
for UPnP service state variable change notifications.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from dataclasses import dataclass, field
from time import monotonic
from xml.sax.saxutils import escape

import aiohttp

LOGGER = logging.getLogger(__name__)

DEFAULT_SUBSCRIPTION_TIMEOUT = 1800  # seconds


@dataclass
class Subscription:
    """Represents a single GENA event subscription."""

    sid: str
    callback_urls: list[str]
    timeout: int
    created_at: float = field(default_factory=monotonic)
    seq: int = 0
    notify_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    @property
    def is_expired(self) -> bool:
        """Check if this subscription has expired."""
        return (monotonic() - self.created_at) > self.timeout


class EventingManager:
    """
    Manages GENA subscriptions and sends NOTIFY events.

    Each UPnP service has its own EventingManager instance keyed by
    service name (e.g., "AVTransport", "RenderingControl").
    """

    MAX_SUBSCRIPTIONS = 32

    def __init__(self, session: aiohttp.ClientSession | None = None) -> None:
        """
        Initialize with no subscriptions and (optionally) a shared session.

        If ``session`` is provided, NOTIFY traffic reuses it and the manager
        does not own its lifecycle; this lets a single renderer instance
        share one connector/DNS cache across all three services, and lets
        the provider pass down ``mass.http_session`` in the typical case.
        When ``session`` is None we fall back to owning a private session
        (kept for standalone/test use) that is closed in ``stop()``.
        """
        self._subscriptions: dict[str, Subscription] = {}
        self._cleanup_task: asyncio.Task[None] | None = None
        self._pending_tasks: set[asyncio.Task[None]] = set()
        self._session: aiohttp.ClientSession | None = session
        self._owns_session: bool = session is None

    async def start(self) -> None:
        """Start the periodic subscription cleanup task."""
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=5),
            )
            self._owns_session = True
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def stop(self) -> None:
        """Stop the cleanup task and clear all subscriptions."""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._cleanup_task
            self._cleanup_task = None
        if self._pending_tasks:
            # Snapshot: done-callbacks mutate self._pending_tasks.
            pending = list(self._pending_tasks)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            self._pending_tasks.clear()
        # Only close the session if we created it ourselves; an injected
        # (shared) session is owned by the caller (e.g. Music Assistant).
        if self._session is not None and self._owns_session:
            await self._session.close()
            self._session = None
        self._subscriptions.clear()

    def subscribe(
        self,
        callback_header: str,
        timeout_header: str | None = None,
    ) -> tuple[str, int]:
        """
        Register a new subscription.

        :param callback_header: CALLBACK header value, e.g.
            ``<http://host:port/path>``.
        :param timeout_header: TIMEOUT header value, e.g.
            ``Second-1800``.
        :returns: Tuple of (SID, actual timeout in seconds).
        """
        callback_urls = self._parse_callback_header(callback_header)
        if not callback_urls:
            raise ValueError("No valid callback URLs in CALLBACK header")

        self._remove_expired_subscriptions()
        if len(self._subscriptions) >= self.MAX_SUBSCRIPTIONS:
            raise ValueError("GENA subscription limit reached")

        timeout = self._parse_timeout(timeout_header)
        sid = f"uuid:{uuid.uuid4()}"

        self._subscriptions[sid] = Subscription(
            sid=sid,
            callback_urls=callback_urls,
            timeout=timeout,
        )
        LOGGER.info(
            "New GENA subscription: SID=%s, callbacks=%s, timeout=%ds",
            sid,
            callback_urls,
            timeout,
        )
        return sid, timeout

    def renew(
        self,
        sid: str,
        timeout_header: str | None = None,
    ) -> int:
        """
        Renew an existing subscription.

        :param sid: Subscription ID to renew.
        :param timeout_header: New TIMEOUT header value.
        :returns: Actual timeout in seconds.
        :raises KeyError: If ``sid`` is not found.
        """
        sub = self._subscriptions.get(sid)
        if sub is None:
            raise KeyError(f"Unknown SID: {sid}")
        if sub.is_expired:
            # Per UPnP spec, renew of an expired SID is a 412 Precondition Failed
            self._subscriptions.pop(sid, None)
            raise KeyError(f"Expired SID: {sid}")

        timeout = self._parse_timeout(timeout_header)
        sub.timeout = timeout
        sub.created_at = monotonic()
        LOGGER.debug("Renewed subscription %s, timeout=%ds", sid, timeout)
        return timeout

    def unsubscribe(self, sid: str) -> None:
        """Remove a subscription."""
        removed = self._subscriptions.pop(sid, None)
        if removed:
            LOGGER.info("Removed GENA subscription: %s", sid)
        else:
            LOGGER.debug("Unsubscribe for unknown SID: %s", sid)

    async def notify(self, changed_vars: dict[str, str]) -> None:
        """
        Send NOTIFY to all active subscribers with changed state variables.

        :param changed_vars: Mapping of state variable name to new
            value.
        """
        if not changed_vars or not self._subscriptions:
            return

        xml_body = self._build_propertyset(changed_vars)

        expired_sids: list[str] = []
        for sid, sub in self._subscriptions.items():
            if sub.is_expired:
                expired_sids.append(sid)
                continue
            task: asyncio.Task[None] = asyncio.create_task(
                self._send_notify(sub, xml_body),
            )
            self._pending_tasks.add(task)
            task.add_done_callback(self._pending_tasks.discard)

        for sid in expired_sids:
            self._subscriptions.pop(sid, None)

    async def notify_initial(self, sid: str, all_vars: dict[str, str]) -> None:
        """Send initial event (SEQ=0) to a newly subscribed control point."""
        sub = self._subscriptions.get(sid)
        if sub is None:
            return
        xml_body = self._build_propertyset(all_vars)
        await self._send_notify(sub, xml_body)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _send_notify(self, sub: Subscription, xml_body: str) -> None:
        """Send a GENA NOTIFY to a single subscriber."""
        async with sub.notify_lock:
            session = self._session
            if session is None or session.closed:
                LOGGER.debug("NOTIFY skipped — session not started")
                return

            headers = {
                "Content-Type": 'text/xml; charset="utf-8"',
                "NT": "upnp:event",
                "NTS": "upnp:propchange",
                "SID": sub.sid,
                "SEQ": str(sub.seq),
            }
            sub.seq += 1

            # Per-request timeout so we don't depend on the injected session's
            # default (mass.http_session has no 5s cap) and stay consistent with
            # the timeout we used when owning our own session.
            timeout = aiohttp.ClientTimeout(total=5)

            # UDA §4.1.2: CALLBACK lists URLs in preference order. Use the first
            # one that actually accepts the NOTIFY (2xx); on HTTP errors or network
            # failures, fall through to the next URL.
            for url in sub.callback_urls:
                try:
                    async with session.request(
                        "NOTIFY",
                        url,
                        headers=headers,
                        data=xml_body,
                        timeout=timeout,
                    ) as resp:
                        if resp.status >= 300:
                            LOGGER.warning(
                                "NOTIFY to %s returned %s",
                                url,
                                resp.status,
                            )
                            continue
                        LOGGER.debug("NOTIFY sent to %s (SEQ=%d)", url, sub.seq - 1)
                        return
                except aiohttp.ClientError, TimeoutError:
                    LOGGER.debug("NOTIFY to %s failed, trying next callback", url)

            LOGGER.warning("All NOTIFY callbacks failed for SID %s", sub.sid)

    @staticmethod
    def _build_propertyset(variables: dict[str, str]) -> str:
        """Build a GENA propertyset XML body."""
        props = ""
        for name, value in variables.items():
            props += f"<e:property><{name}>{escape(value)}</{name}></e:property>"
        return (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<e:propertyset xmlns:e="urn:schemas-upnp-org:event-1-0">'
            f"{props}"
            "</e:propertyset>"
        )

    @staticmethod
    def _parse_callback_header(header: str) -> list[str]:
        """
        Parse CALLBACK header: '<url1><url2>' -> ['url1', 'url2'].

        Per UDA §4.1.2 a GENA CALLBACK URL must use the http/https scheme;
        a bare ``startswith("http")`` check would also let ``httpx://`` or
        ``httpfake://`` through and then fail at NOTIFY time.
        """
        urls: list[str] = []
        for raw in header.split(">"):
            part = raw.strip()
            if part.startswith("<"):
                url = part[1:]
                if url.startswith(("http://", "https://")):
                    urls.append(url)
        return urls

    @staticmethod
    def _parse_timeout(header: str | None) -> int:
        """Parse TIMEOUT header: 'Second-1800' -> 1800."""
        if not header:
            return DEFAULT_SUBSCRIPTION_TIMEOUT
        header = header.strip()
        if header.lower() == "infinite":
            return DEFAULT_SUBSCRIPTION_TIMEOUT
        if header.lower().startswith("second-"):
            try:
                value = int(header.split("-", 1)[1])
                return min(max(value, 1), DEFAULT_SUBSCRIPTION_TIMEOUT)
            except ValueError, IndexError:
                pass
        return DEFAULT_SUBSCRIPTION_TIMEOUT

    def _remove_expired_subscriptions(self) -> None:
        """Remove subscriptions whose timeout has elapsed."""
        expired = [sid for sid, sub in self._subscriptions.items() if sub.is_expired]
        for sid in expired:
            self._subscriptions.pop(sid, None)
            LOGGER.debug("Cleaned up expired subscription: %s", sid)

    async def _cleanup_loop(self) -> None:
        """Periodically remove expired subscriptions."""
        while True:
            await asyncio.sleep(60)
            self._remove_expired_subscriptions()
