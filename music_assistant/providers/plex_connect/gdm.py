"""GDM (Plex Good Day Mate) advertising for player discovery."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket

LOGGER = logging.getLogger(__name__)

# GDM broadcast and listen ports (matching test-client.py)
GDM_BROADCAST_PORT = 32414  # Send HELLO broadcasts here
GDM_LISTEN_PORT = 32412  # Listen for M-SEARCH queries here
GDM_BROADCAST_ADDR = "255.255.255.255"  # Broadcast address


class PlexGDMAdvertiser:
    """Advertise Music Assistant as a Plex player via GDM."""

    def __init__(
        self,
        instance_id: str,
        port: int,
        publish_ip: str,
        name: str = "Music Assistant",
        product: str = "Music Assistant",
        version: str = "1.0.0",
    ) -> None:
        """Initialize GDM advertiser.

        :param instance_id: Unique identifier for this instance.
        :param port: Port number for the server.
        :param publish_ip: IP address to advertise for this server.
        :param name: Display name for the device.
        :param product: Product name.
        :param version: Version string.
        """
        self.instance_id = instance_id
        self.port = port
        self.name = name
        self.product = product
        self.version = version
        self._running = False
        self._broadcast_task: asyncio.Task[None] | None = None
        self._listener_task: asyncio.Task[None] | None = None

        # Pre-build GDM messages (they're static)
        self._hello_message = self._build_hello_message()
        self._response_message = self._build_response_message()

        # Sockets for reuse
        self._broadcast_socket: socket.socket | None = None
        self._response_socket: socket.socket | None = None

        # Cached publish IP
        self._local_ip = publish_ip

    def _build_hello_message(self) -> bytes:
        """Build HELLO broadcast message (static, built once)."""
        message_lines = [
            "HELLO * HTTP/1.0",
            f"Name: {self.name}",
            f"Port: {self.port}",
            f"Product: {self.product}",
            f"Version: {self.version}",
            "Protocol: plex",
            "Protocol-Version: 1",
            "Protocol-Capabilities: timeline,playback,navigation,playqueues",
            "Device-Class: pc",
            f"Resource-Identifier: {self.instance_id}",
            "Content-Type: plex/media-player",
            "Provides: client,player,pubsub-player",
        ]
        return "\r\n".join(message_lines).encode("utf-8")

    def _build_response_message(self) -> bytes:
        """Build M-SEARCH response message (static, built once)."""
        message_lines = [
            "HTTP/1.0 200 OK",
            f"Name: {self.name}",
            f"Port: {self.port}",
            f"Product: {self.product}",
            f"Version: {self.version}",
            "Protocol: plex",
            "Protocol-Version: 1",
            "Protocol-Capabilities: timeline,playback,navigation,playqueues",
            "Device-Class: pc",
            f"Resource-Identifier: {self.instance_id}",
            "Content-Type: plex/media-player",
            "Provides: client,player,pubsub-player",
        ]
        return "\r\n".join(message_lines).encode("utf-8")

    def start(self) -> None:
        """Start GDM advertising and listening."""
        if self._running:
            return
        self._running = True

        # Create reusable broadcast socket
        self._broadcast_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._broadcast_socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

        # Create reusable response socket
        self._response_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        # Start broadcast task
        self._broadcast_task = asyncio.create_task(self._advertise_loop())

        # Start listener task
        self._listener_task = asyncio.create_task(self._listen_loop())

        LOGGER.info(f"Started GDM advertising and listening at {self._local_ip}:{self.port}")

    async def stop(self) -> None:
        """Stop GDM advertising and listening."""
        self._running = False

        if self._broadcast_task:
            self._broadcast_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._broadcast_task

        if self._listener_task:
            self._listener_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._listener_task

        # Close reusable sockets
        if self._broadcast_socket:
            self._broadcast_socket.close()
            self._broadcast_socket = None

        if self._response_socket:
            self._response_socket.close()
            self._response_socket = None

        LOGGER.info("Stopped GDM advertising")

    async def _advertise_loop(self) -> None:
        """Continuously advertise via GDM every 30 seconds."""
        # Send initial announcement immediately
        await self._send_announcement()

        while self._running:
            try:
                await asyncio.sleep(30)
                await self._send_announcement()
            except asyncio.CancelledError:
                break
            except Exception as e:
                LOGGER.exception(f"Error sending GDM announcement: {e}")
                await asyncio.sleep(30)

    async def _listen_loop(self) -> None:
        """Listen for GDM discovery requests and respond (matching test-client.py)."""

        def listen() -> None:
            try:
                # Create UDP socket
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

                # Bind to GDM listen port (like test-client.py)
                sock.bind(("", GDM_LISTEN_PORT))

                sock.settimeout(1.0)  # 1 second timeout for checking _running

                while self._running:
                    try:
                        data, addr = sock.recvfrom(1024)
                        message = data.decode("utf-8", errors="ignore")

                        # Check if this is a discovery request (M-SEARCH) not our own HELLO
                        if "M-SEARCH" in message:
                            # Send response - addr contains the actual client's IP and port
                            self._send_discovery_response(addr)

                    except socket.timeout:  # noqa: UP041
                        continue
                    except Exception as e:
                        if self._running:
                            LOGGER.debug(f"Error receiving GDM request: {e}")

                sock.close()

            except Exception as e:
                LOGGER.exception(f"Failed to start GDM listener: {e}")

        await asyncio.to_thread(listen)

    def _send_discovery_response(self, addr: tuple[str, int]) -> None:
        """Send GDM response to a discovery request."""
        if not self._response_socket:
            LOGGER.warning("Response socket not available")
            return

        try:
            self._response_socket.sendto(self._response_message, addr)

        except Exception as e:
            LOGGER.warning(f"Failed to send GDM response to {addr}: {e}")

    async def _send_announcement(self) -> None:
        """Send a GDM announcement broadcast (uses pre-built message)."""
        await asyncio.get_event_loop().run_in_executor(None, self._send_udp)

    def _send_udp(self) -> None:
        """Send UDP broadcast message (uses cached socket and message)."""
        if not self._broadcast_socket:
            LOGGER.warning("Broadcast socket not available")
            return

        try:
            self._broadcast_socket.sendto(
                self._hello_message, (GDM_BROADCAST_ADDR, GDM_BROADCAST_PORT)
            )

        except Exception as e:
            LOGGER.exception(f"Failed to send GDM announcement: {e}")
