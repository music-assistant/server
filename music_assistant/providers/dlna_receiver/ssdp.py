"""DLNA Receiver — SSDP advertisement for the virtual MediaRenderer."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import socket

from .constants import (
    SSDP_MAX_AGE,
    SSDP_MULTICAST_ADDR,
    SSDP_PORT,
    UPNP_DEVICE_TYPE,
    UPNP_SERVICE_AV_TRANSPORT,
    UPNP_SERVICE_CONNECTION_MANAGER,
    UPNP_SERVICE_RENDERING_CONTROL,
)

LOGGER = logging.getLogger(__name__)

# UPnP spec allows MX up to 120, but honoring that would stall discovery;
# cap at 5 s so control points still see our response within their window.
_MX_MAX_SECONDS = 5


class SSDPAdvertiser:
    """Advertise a UPnP MediaRenderer via SSDP on the local network."""

    def __init__(
        self,
        udn: str,
        description_url: str,
        bind_ip: str,
    ) -> None:
        """Store config; sockets and tasks are created in start()."""
        self.udn = udn
        self.description_url = description_url
        self.bind_ip = bind_ip
        self._transport: asyncio.DatagramTransport | None = None
        self._recv_transport: asyncio.DatagramTransport | None = None
        self._advertise_task: asyncio.Task[None] | None = None
        self._pending_responses: set[asyncio.Task[None]] = set()
        self._running = False

    async def start(self) -> None:
        """Start SSDP advertisement."""
        self._running = True
        loop = asyncio.get_running_loop()

        # Sending socket — for NOTIFY alive/byebye to multicast group
        send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        send_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        send_sock.setsockopt(
            socket.IPPROTO_IP,
            socket.IP_MULTICAST_IF,
            socket.inet_aton(self.bind_ip),
        )
        send_sock.setblocking(False)
        self._transport, _ = await loop.create_datagram_endpoint(
            _SSDPSendProtocol,
            sock=send_sock,
        )

        # Receiving socket — join multicast group on port 1900 for M-SEARCH.
        # In multi-player mode each instance binds its own ("", 1900) socket,
        # which only works if SO_REUSEPORT is available: track whether we
        # managed to enable it so we can turn the subsequent bind() error
        # into a clear, actionable message instead of a raw EADDRINUSE.
        recv_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        recv_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        reuse_port_enabled = False
        try:
            recv_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            reuse_port_enabled = True
        except AttributeError, OSError:
            reuse_port_enabled = False
        try:
            recv_sock.bind(("", SSDP_PORT))
        except OSError as err:
            recv_sock.close()
            # EADDRINUSE: 48 on macOS/BSD, 98 on Linux, 10048 on Windows.
            if not reuse_port_enabled and err.errno in (48, 98, 10048):
                msg = (
                    f"Unable to bind SSDP receive socket to port {SSDP_PORT}: "
                    "this platform does not support sharing the SSDP port "
                    "across multiple DLNA receiver instances because "
                    "SO_REUSEPORT could not be enabled. Run with a single "
                    "target player, or use a platform that supports "
                    "SO_REUSEPORT."
                )
                raise RuntimeError(msg) from err
            raise
        mreq = socket.inet_aton(SSDP_MULTICAST_ADDR) + socket.inet_aton(self.bind_ip)
        recv_sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        recv_sock.setblocking(False)
        self._recv_transport, _ = await loop.create_datagram_endpoint(
            lambda: _SSDPRecvProtocol(self),
            sock=recv_sock,
        )

        # Send initial alive notifications
        await self._send_alive()

        # Periodic re-advertisement
        self._advertise_task = asyncio.create_task(self._periodic_alive())
        LOGGER.info("SSDP advertiser started for %s", self.udn)

    async def stop(self) -> None:
        """Stop SSDP advertisement and send byebye."""
        self._running = False
        if self._advertise_task:
            advertise_task = self._advertise_task
            self._advertise_task = None
            advertise_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await advertise_task
        if self._pending_responses:
            # Snapshot: done-callbacks mutate self._pending_responses.
            pending = list(self._pending_responses)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            self._pending_responses.clear()
        await self._send_byebye()
        if self._recv_transport:
            self._recv_transport.close()
            self._recv_transport = None
        if self._transport:
            self._transport.close()
            self._transport = None
        LOGGER.info("SSDP advertiser stopped")

    def handle_search(self, data: bytes, addr: tuple[str, int]) -> None:
        """
        Respond to M-SEARCH requests matching our device/service types.

        Per UPnP Device Architecture 1.1 §1.3.3, the responder must wait a
        random interval in [0, MX] seconds before replying so a roomful of
        devices doesn't flood the requester simultaneously. We cap MX at
        ``_MX_MAX_SECONDS`` to keep discovery snappy.
        """
        text = data.decode("utf-8", errors="ignore")
        if "M-SEARCH" not in text:
            return

        st = ""
        mx_raw = ""
        for line in text.splitlines():
            upper = line.upper()
            if upper.startswith("ST:"):
                st = line.split(":", 1)[1].strip()
            elif upper.startswith("MX:"):
                mx_raw = line.split(":", 1)[1].strip()

        # Check if the search target matches any of our types
        match_targets = {
            "ssdp:all",
            "upnp:rootdevice",
            UPNP_DEVICE_TYPE,
            UPNP_SERVICE_AV_TRANSPORT,
            UPNP_SERVICE_RENDERING_CONTROL,
            UPNP_SERVICE_CONNECTION_MANAGER,
            self.udn,
        }
        if st not in match_targets:
            return

        usn = f"{self.udn}::{st}" if st != self.udn else self.udn
        response = (
            "HTTP/1.1 200 OK\r\n"
            f"CACHE-CONTROL: max-age={SSDP_MAX_AGE}\r\n"
            "EXT:\r\n"
            f"LOCATION: {self.description_url}\r\n"
            f"ST: {st}\r\n"
            f"USN: {usn}\r\n"
            f"SERVER: Music Assistant DLNA Receiver/1.0 UPnP/1.0\r\n"
            "\r\n"
        ).encode()

        delay = _parse_mx_delay(mx_raw)
        if delay <= 0:
            self._send_response(response, addr, st)
            return
        try:
            task: asyncio.Task[None] = asyncio.create_task(
                self._delayed_response(response, addr, st, delay),
            )
        except RuntimeError:
            # No running loop (e.g. invoked from a test) — send immediately.
            self._send_response(response, addr, st)
            return
        self._pending_responses.add(task)
        task.add_done_callback(self._pending_responses.discard)

    async def _periodic_alive(self) -> None:
        """Re-send alive notifications periodically."""
        while self._running:
            await asyncio.sleep(SSDP_MAX_AGE // 2)
            if self._running:
                await self._send_alive()

    async def _send_alive(self) -> None:
        """Send ssdp:alive NOTIFY messages for all service types."""
        notification_types = [
            "upnp:rootdevice",
            self.udn,
            UPNP_DEVICE_TYPE,
            UPNP_SERVICE_AV_TRANSPORT,
            UPNP_SERVICE_RENDERING_CONTROL,
            UPNP_SERVICE_CONNECTION_MANAGER,
        ]
        for nt in notification_types:
            usn = f"{self.udn}::{nt}" if nt != self.udn else self.udn
            message = (
                "NOTIFY * HTTP/1.1\r\n"
                f"HOST: {SSDP_MULTICAST_ADDR}:{SSDP_PORT}\r\n"
                f"CACHE-CONTROL: max-age={SSDP_MAX_AGE}\r\n"
                f"LOCATION: {self.description_url}\r\n"
                f"NT: {nt}\r\n"
                "NTS: ssdp:alive\r\n"
                f"SERVER: Music Assistant DLNA Receiver/1.0 UPnP/1.0\r\n"
                f"USN: {usn}\r\n"
                "\r\n"
            )
            self._send_datagram(message)
        LOGGER.debug("Sent SSDP alive notifications")

    async def _send_byebye(self) -> None:
        """Send ssdp:byebye NOTIFY messages."""
        notification_types = [
            "upnp:rootdevice",
            self.udn,
            UPNP_DEVICE_TYPE,
        ]
        for nt in notification_types:
            usn = f"{self.udn}::{nt}" if nt != self.udn else self.udn
            message = (
                "NOTIFY * HTTP/1.1\r\n"
                f"HOST: {SSDP_MULTICAST_ADDR}:{SSDP_PORT}\r\n"
                f"NT: {nt}\r\n"
                "NTS: ssdp:byebye\r\n"
                f"USN: {usn}\r\n"
                "\r\n"
            )
            self._send_datagram(message)
        LOGGER.debug("Sent SSDP byebye notifications")

    def _send_datagram(self, message: str) -> None:
        """Send a UDP datagram to the SSDP multicast address."""
        if self._transport and not self._transport.is_closing():
            self._transport.sendto(
                message.encode("utf-8"),
                (SSDP_MULTICAST_ADDR, SSDP_PORT),
            )

    async def _delayed_response(
        self,
        response: bytes,
        addr: tuple[str, int],
        st: str,
        delay: float,
    ) -> None:
        """Sleep for the MX-derived jitter then send the prepared response."""
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        self._send_response(response, addr, st)

    def _send_response(self, response: bytes, addr: tuple[str, int], st: str) -> None:
        """Send a prebuilt M-SEARCH response datagram to the requester."""
        if self._recv_transport and not self._recv_transport.is_closing():
            self._recv_transport.sendto(response, addr)
            LOGGER.debug("Responded to M-SEARCH from %s for %s", addr, st)


def _parse_mx_delay(header: str) -> float:
    """
    Parse an SSDP ``MX:`` header into a jitter delay in seconds.

    Returns 0.0 if the header is missing, malformed, or non-positive — in
    which case we respond immediately. A valid MX is clamped to
    ``_MX_MAX_SECONDS`` and then multiplied by a uniform random in [0, 1)
    using ``secrets`` (we don't need cryptographic strength, but ``secrets``
    avoids pulling the ``random`` module just for this one spot and the
    quality is more than adequate for jitter).
    """
    if not header:
        return 0.0
    try:
        mx = int(header)
    except ValueError:
        return 0.0
    if mx <= 0:
        return 0.0
    capped = min(mx, _MX_MAX_SECONDS)
    # randbelow(1000)/1000 gives a float in [0, 1) without importing `random`.
    return capped * (secrets.randbelow(1000) / 1000)


class _SSDPSendProtocol(asyncio.DatagramProtocol):
    """Asyncio UDP protocol for the SSDP sending socket (NOTIFY)."""

    def error_received(self, exc: Exception) -> None:
        """Handle transport errors."""
        LOGGER.warning("SSDP send transport error: %s", exc)


class _SSDPRecvProtocol(asyncio.DatagramProtocol):
    """Asyncio UDP protocol for receiving SSDP M-SEARCH requests."""

    def __init__(self, advertiser: SSDPAdvertiser) -> None:
        """Hold a reference to the advertiser that receives M-SEARCH datagrams."""
        self._advertiser = advertiser

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        """Handle incoming UDP datagrams."""
        self._advertiser.handle_search(data, addr)

    def error_received(self, exc: Exception) -> None:
        """Handle transport errors."""
        LOGGER.warning("SSDP recv transport error: %s", exc)
