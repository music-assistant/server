"""SSDP Server for DLNA device advertisement.

This module handles SSDP (Simple Service Discovery Protocol) multicast announcements
to make the DLNA server discoverable on the local network.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket

# SSDP constants
SSDP_MULTICAST_ADDR = "239.255.255.250"
SSDP_PORT = 1900
SSDP_MX = 3  # Maximum wait time in seconds
SSDP_ALIVE_INTERVAL = 1800  # Send alive messages every 30 minutes

SUPPORTED_SEARCH_TARGETS = {
    "ssdp:all",
    "upnp:rootdevice",
    "urn:schemas-upnp-org:device:MediaServer:1",
    "urn:schemas-upnp-org:service:ContentDirectory:1",
    "urn:schemas-upnp-org:service:ConnectionManager:1",
}


class SSDPServer:
    """SSDP Server for device discovery via multicast."""

    def __init__(
        self,
        location: str,
        server_uuid: str,
        friendly_name: str,
        logger: logging.Logger,
    ) -> None:
        """Initialize SSDP server.

        Args:
            location: URL to the device description XML
            server_uuid: Unique device identifier (UUID)
            friendly_name: Human-readable device name
            logger: Logger instance
        """
        self.location = location
        self.server_uuid = server_uuid
        self.friendly_name = friendly_name
        self.logger = logger

        self._transport: asyncio.DatagramTransport | None = None
        self._protocol: SSDPProtocol | None = None
        self._alive_task: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        """Start the SSDP server."""
        if self._running:
            return

        self.logger.debug("Starting SSDP server")

        # Create UDP socket for multicast
        loop = asyncio.get_event_loop()

        # Create the protocol instance
        protocol = SSDPProtocol(
            location=self.location,
            server_uuid=self.server_uuid,
            friendly_name=self.friendly_name,
            logger=self.logger,
        )

        # Create datagram endpoint
        try:
            self._transport, _ = await loop.create_datagram_endpoint(
                lambda: protocol,  # Use local variable instead of self._protocol
                local_addr=("0.0.0.0", SSDP_PORT),
                reuse_port=True,
            )

            # Store it after successful creation
            self._protocol = protocol

            # Join multicast group
            sock = self._transport.get_extra_info("socket")
            group = socket.inet_aton(SSDP_MULTICAST_ADDR)
            mreq = group + socket.inet_aton("0.0.0.0")
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

            self._running = True

            # Send initial ALIVE messages
            await self._send_alive_messages()

            # Start periodic ALIVE task
            self._alive_task = asyncio.create_task(self._periodic_alive())

            self.logger.info("SSDP server started - advertising device at %s", self.location)

        except OSError as err:
            self.logger.error("Failed to start SSDP server: %s", err)
            raise

    async def stop(self) -> None:
        """Stop the SSDP server."""
        if not self._running:
            return

        self.logger.debug("Stopping SSDP server")

        # Cancel periodic alive task
        if self._alive_task and not self._alive_task.done():
            self._alive_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._alive_task

        # Send byebye messages
        await self._send_byebye_messages()

        # Close transport
        if self._transport:
            self._transport.close()

        self._running = False
        self.logger.info("SSDP server stopped")

    async def _send_alive_messages(self) -> None:
        """Send SSDP ALIVE messages for all service types."""
        if not self._protocol:
            return

        # Send alive for root device
        await self._protocol.send_alive(
            f"uuid:{self.server_uuid}",
            "upnp:rootdevice",
        )

        # Send alive for device UUID
        await self._protocol.send_alive(
            f"uuid:{self.server_uuid}",
            f"uuid:{self.server_uuid}",
        )

        # Send alive for MediaServer device
        await self._protocol.send_alive(
            f"uuid:{self.server_uuid}",
            "urn:schemas-upnp-org:device:MediaServer:1",
        )

        # Send alive for ContentDirectory service
        await self._protocol.send_alive(
            f"uuid:{self.server_uuid}",
            "urn:schemas-upnp-org:service:ContentDirectory:1",
        )

        # Send alive for ConnectionManager service
        await self._protocol.send_alive(
            f"uuid:{self.server_uuid}",
            "urn:schemas-upnp-org:service:ConnectionManager:1",
        )

    async def _send_byebye_messages(self) -> None:
        """Send SSDP BYEBYE messages for all service types."""
        if not self._protocol:
            return

        # Send byebye for root device
        await self._protocol.send_byebye(
            f"uuid:{self.server_uuid}",
            "upnp:rootdevice",
        )

        # Send byebye for device UUID
        await self._protocol.send_byebye(
            f"uuid:{self.server_uuid}",
            f"uuid:{self.server_uuid}",
        )

        # Send byebye for MediaServer device
        await self._protocol.send_byebye(
            f"uuid:{self.server_uuid}",
            "urn:schemas-upnp-org:device:MediaServer:1",
        )

        # Send byebye for ContentDirectory service
        await self._protocol.send_byebye(
            f"uuid:{self.server_uuid}",
            "urn:schemas-upnp-org:service:ContentDirectory:1",
        )

        # Send byebye for ConnectionManager service
        await self._protocol.send_byebye(
            f"uuid:{self.server_uuid}",
            "urn:schemas-upnp-org:service:ConnectionManager:1",
        )

    async def _periodic_alive(self) -> None:
        """Periodically send ALIVE messages."""
        while self._running:
            try:
                await asyncio.sleep(SSDP_ALIVE_INTERVAL)
                await self._send_alive_messages()
            except asyncio.CancelledError:
                break
            except Exception as err:
                self.logger.exception("Error in periodic ALIVE task: %s", err)


class SSDPProtocol(asyncio.DatagramProtocol):
    """SSDP protocol handler."""

    def __init__(
        self,
        location: str,
        server_uuid: str,
        friendly_name: str,
        logger: logging.Logger,
    ) -> None:
        """Initialize protocol.

        Args:
            location: URL to the device description XML
            server_uuid: Unique device identifier (UUID)
            friendly_name: Human-readable device name
            logger: Logger instance
        """
        super().__init__()
        self.location = location
        self.server_uuid = server_uuid
        self.friendly_name = friendly_name
        self.logger = logger
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        """Call when connection is made."""
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        """Handle incoming SSDP datagram."""
        try:
            message = data.decode("utf-8")

            # Parse the message
            lines = message.split("\r\n")
            if not lines:
                return

            # Check if this is an M-SEARCH request
            if lines[0].startswith("M-SEARCH"):
                asyncio.create_task(self._handle_search(message, addr))

        except Exception as err:
            self.logger.debug("Error processing SSDP datagram: %s", err)

    async def _handle_search(self, message: str, addr: tuple[str, int]) -> None:
        """Handle M-SEARCH discovery request."""
        # Parse search target
        st = None
        for line in message.split("\r\n"):
            if line.lower().startswith("st:"):
                st = line.split(":", 1)[1].strip()
                break

        if not st:
            return

        # Respond if the search target matches our services
        should_respond = st in SUPPORTED_SEARCH_TARGETS or st == f"uuid:{self.server_uuid}"

        if should_respond:
            # Small delay to avoid response flooding
            await asyncio.sleep(0.1)
            await self.send_search_response(st, addr)

    async def send_search_response(self, st: str, addr: tuple[str, int]) -> None:
        """Send response to M-SEARCH request."""
        if not self.transport:
            return

        usn = f"uuid:{self.server_uuid}"
        if st not in {f"uuid:{self.server_uuid}", "upnp:rootdevice"}:
            usn = f"{usn}::{st}"

        response = (
            "HTTP/1.1 200 OK\r\n"
            f"CACHE-CONTROL: max-age={SSDP_ALIVE_INTERVAL}\r\n"
            f"EXT:\r\n"
            f"LOCATION: {self.location}\r\n"
            f"SERVER: Music Assistant UPnP/1.0\r\n"
            f"ST: {st}\r\n"
            f"USN: {usn}\r\n"
            "\r\n"
        )

        self.transport.sendto(response.encode("utf-8"), addr)

    async def send_alive(self, usn: str, nt: str) -> None:
        """Send NOTIFY ALIVE message."""
        if not self.transport:
            return

        message = (
            "NOTIFY * HTTP/1.1\r\n"
            f"HOST: {SSDP_MULTICAST_ADDR}:{SSDP_PORT}\r\n"
            f"CACHE-CONTROL: max-age={SSDP_ALIVE_INTERVAL}\r\n"
            f"LOCATION: {self.location}\r\n"
            f"NT: {nt}\r\n"
            f"NTS: ssdp:alive\r\n"
            f"SERVER: Music Assistant UPnP/1.0\r\n"
            f"USN: {usn}::{nt}\r\n"
            "\r\n"
        )

        self.transport.sendto(
            message.encode("utf-8"),
            (SSDP_MULTICAST_ADDR, SSDP_PORT),
        )

    async def send_byebye(self, usn: str, nt: str) -> None:
        """Send NOTIFY BYEBYE message."""
        if not self.transport:
            return

        message = (
            "NOTIFY * HTTP/1.1\r\n"
            f"HOST: {SSDP_MULTICAST_ADDR}:{SSDP_PORT}\r\n"
            f"NT: {nt}\r\n"
            f"NTS: ssdp:byebye\r\n"
            f"USN: {usn}::{nt}\r\n"
            "\r\n"
        )

        self.transport.sendto(
            message.encode("utf-8"),
            (SSDP_MULTICAST_ADDR, SSDP_PORT),
        )
