"""VBAN subclasses to workaround issues in aiovban 0.6.3."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from aiovban.asyncio import AsyncVBANClient
from aiovban.packet import VBANPacket
from aiovban.packet.headers import VBANHeaderException

if TYPE_CHECKING:
    from . import VBANReceiverProvider

logger = logging.getLogger(__name__)
_aiovban_log_level = os.environ.get("AIOVBAN_LOG_LEVEL", "info").upper()
logging.getLogger("aiovban.asyncio.aiovban.asyncio.util").setLevel(_aiovban_log_level)


class VBANListenerProtocolMod(asyncio.DatagramProtocol):
    """VBANListenerProcotol workaround."""

    def __init__(self, client: AsyncVBANClientMod) -> None:
        """Initialize."""
        # WORKAROUND: each instance gets it's own Future.
        self.done: asyncio.Future[Any] = asyncio.get_event_loop().create_future()
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._client = client

    def error_received(self, exc: Exception) -> None:
        """Handle error."""
        self.done.set_exception(exc)

    def connection_lost(self, exc: Exception | None) -> None:
        """Handle lost connection."""
        if self.done.done():
            return
        # WORKAROUND: handle exc properly.
        if exc:
            self.done.set_exception(exc)
        else:
            self.done.set_result(None)

    def connection_made(self, transport) -> None:  # type: ignore[no-untyped-def]
        """Handle connection made."""
        logger.debug(f"Connection made to {transport}")

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        """Handle received datagram."""
        sender_ip, sender_port = addr

        if self._client.quick_reject(sender_ip) or not self._client.active_player:
            return

        try:
            packet = VBANPacket.unpack(data)
        except VBANHeaderException as exc:
            logger.error(f"Error unpacking packet: {exc}")
            return
        except ValueError as exc:
            # Handle odd packet sent when Voicemeeter start/stops stream
            error_msg = "6000 is not a valid VBANSampleRate"
            if str(exc) == error_msg:
                return
            raise

        task = asyncio.create_task(self._client.process_packet(sender_ip, sender_port, packet))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)


@dataclass
class AsyncVBANClientMod(AsyncVBANClient):  # type: ignore[misc]
    """AsyncVBANClient workaround."""

    _controller: VBANReceiverProvider | None = None

    @property
    def active_player(self) -> bool:
        """Report the active player status."""
        return False if not self._controller else self._controller.active_player

    async def listen(
        self,
        address: str = "0.0.0.0",
        port: int = 6980,
        loop: asyncio.AbstractEventLoop | None = None,
        controller: VBANReceiverProvider | None = None,
    ) -> None:
        """Create UDP listener."""
        loop = loop or asyncio.get_running_loop()
        self._controller = controller

        # Create a socket and set the options
        self._transport, proto = await loop.create_datagram_endpoint(
            lambda: VBANListenerProtocolMod(self),
            local_addr=(address, port),
            allow_broadcast=not self.ignore_audio_streams,
        )

        # WORKAROUND: await, not return.
        await proto.done

    def close(self) -> None:
        """Close down the connection."""
        self._controller = None
        if self._transport:
            self._transport.close()
            self._transport = None  # type: ignore[assignment]
