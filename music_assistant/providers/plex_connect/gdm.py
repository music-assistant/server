"""GDM (Plex Good Day Mate) advertising for player discovery."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket

LOGGER = logging.getLogger(__name__)

# GDM ports (see hippojay/plexgdm and songchenwen/plexdlnaplayer for reference)
GDM_BROADCAST_PORT = 32414  # Legacy broadcast target (server discovery port)
GDM_LISTEN_PORT = 32412  # Listen for (and reply to) M-SEARCH queries here
GDM_CLIENT_REGISTER_PORT = 32413  # Client register group: HELLO/BYE announcements
GDM_BROADCAST_ADDR = "255.255.255.255"  # Broadcast address
GDM_MULTICAST_ADDR = "239.0.0.250"  # GDM multicast group


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
        device_class: str = "speaker",
    ) -> None:
        """
        Initialize GDM advertiser.

        :param instance_id: Unique identifier for this instance.
        :param port: Port number for the server.
        :param publish_ip: IP address to advertise for this server.
        :param name: Display name for the device.
        :param product: Product name.
        :param version: Version string.
        :param device_class: Device class advertised to Plex (pc, speaker, phone, etc.).
        """
        self.instance_id = instance_id
        self.port = port
        self.name = name
        self.product = product
        self.version = version
        self.device_class = device_class
        self._running = False
        self._broadcast_task: asyncio.Task[None] | None = None
        self._listener_task: asyncio.Task[None] | None = None

        # Pre-build GDM messages (they're static)
        self._hello_message = self._build_message("HELLO * HTTP/1.0")
        self._response_message = self._build_message("HTTP/1.0 200 OK")
        self._bye_message = self._build_message("BYE * HTTP/1.0")

        # Sockets for reuse
        self._broadcast_socket: socket.socket | None = None
        self._listen_socket: socket.socket | None = None

        # Cached publish IP
        self._local_ip = publish_ip

    def start(self) -> None:
        """Start GDM advertising and listening."""
        if self._running:
            return
        self._running = True

        # Create reusable broadcast socket
        self._broadcast_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._broadcast_socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

        # Start broadcast task
        self._broadcast_task = asyncio.create_task(self._advertise_loop())

        # Create the shared listen/reply socket and start the listener task
        try:
            self._listen_socket = self._create_listen_socket()
        except OSError as e:
            LOGGER.error(f"Failed to create GDM listen socket: {e}")
        else:
            self._listener_task = asyncio.create_task(self._listen_loop())

        LOGGER.info(f"Started GDM advertising and listening at {self._local_ip}:{self.port}")

    async def stop(self) -> None:
        """Stop GDM advertising and listening."""
        self._running = False

        # Announce our departure to the client register group
        self._send_bye()

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

        if self._listen_socket:
            self._listen_socket.close()
            self._listen_socket = None

        LOGGER.info("Stopped GDM advertising")

    def _build_message(self, first_line: str) -> bytes:
        """
        Build a GDM message (static, built once).

        :param first_line: The GDM message start line (HELLO/BYE/M-SEARCH response).
        """
        message_lines = [
            first_line,
            f"Name: {self.name}",
            f"Port: {self.port}",
            f"Product: {self.product}",
            f"Version: {self.version}",
            "Protocol: plex",
            "Protocol-Version: 1",
            "Protocol-Capabilities: timeline,playback,navigation,playqueues",
            f"Device-Class: {self.device_class}",
            f"Resource-Identifier: {self.instance_id}",
            "Content-Type: plex/media-player",
            "Provides: client,player,pubsub-player",
        ]
        return "\r\n".join(message_lines).encode("utf-8")

    def _create_listen_socket(self) -> socket.socket:
        """Create the shared GDM listen/reply socket bound to the GDM client port."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # multiple plugin instances (and other local Plex software) share this port
        with contextlib.suppress(AttributeError, OSError):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)

        # Bind to the GDM client port so M-SEARCH replies originate from it
        # (strict clients ignore replies from an ephemeral source port)
        sock.bind(("0.0.0.0", GDM_LISTEN_PORT))

        # Join the GDM multicast group to also receive multicast M-SEARCH queries
        # (may be denied in restricted network namespaces - broadcast still works)
        mreq = socket.inet_aton(GDM_MULTICAST_ADDR) + socket.inet_aton("0.0.0.0")
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        except OSError as e:
            LOGGER.debug(f"Could not join GDM multicast group: {e}")

        sock.settimeout(1.0)  # 1 second timeout for checking _running
        return sock

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
        """Listen for GDM discovery requests and respond."""

        def listen() -> None:
            sock = self._listen_socket
            if not sock:
                return

            while self._running:
                try:
                    data, addr = sock.recvfrom(1024)
                    message = data.decode("utf-8", errors="ignore")

                    # Check if this is a discovery request (M-SEARCH) not our own HELLO
                    if "M-SEARCH" in message:
                        # Send response - addr contains the actual client's IP and port
                        self._send_discovery_response(addr)

                except TimeoutError:
                    continue
                except OSError:
                    # socket closed (or broken) - only relevant while still running
                    if self._running:
                        LOGGER.debug("GDM listen socket closed unexpectedly")
                    return
                except Exception as e:
                    if self._running:
                        LOGGER.debug(f"Error receiving GDM request: {e}")

        await asyncio.to_thread(listen)

    def _send_discovery_response(self, addr: tuple[str, int]) -> None:
        """Send GDM response to a discovery request."""
        if not self._listen_socket:
            LOGGER.warning("Listen socket not available")
            return

        try:
            # reply from the listen socket so the source port is the GDM port
            self._listen_socket.sendto(self._response_message, addr)

        except Exception as e:
            LOGGER.warning(f"Failed to send GDM response to {addr}: {e}")

    async def _send_announcement(self) -> None:
        """Send a GDM announcement broadcast (uses pre-built message)."""
        await asyncio.get_event_loop().run_in_executor(None, self._send_udp)

    def _send_udp(self) -> None:
        """Send UDP HELLO announcement (uses cached socket and message)."""
        self._send_message(self._hello_message)

    def _send_bye(self) -> None:
        """Send a best-effort BYE announcement on shutdown."""
        self._send_message(self._bye_message)

    def _send_message(self, message: bytes) -> None:
        """
        Send a GDM announcement to the client register group.

        :param message: The pre-built GDM message to send.
        """
        if not self._broadcast_socket:
            LOGGER.warning("Broadcast socket not available")
            return

        # Send to the multicast client register group (spec behavior) and keep the
        # legacy broadcast for lenient clients/setups where multicast is filtered.
        for target in (
            (GDM_MULTICAST_ADDR, GDM_CLIENT_REGISTER_PORT),
            (GDM_BROADCAST_ADDR, GDM_BROADCAST_PORT),
        ):
            try:
                self._broadcast_socket.sendto(message, target)
            except Exception as e:
                LOGGER.debug(f"Failed to send GDM announcement to {target}: {e}")
