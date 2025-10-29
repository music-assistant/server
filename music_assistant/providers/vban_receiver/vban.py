"""VBAN subclasses to workaround issues in aiovban 0.6.3."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any

from aiovban.asyncio import AsyncVBANClient
from aiovban.packet import VBANPacket
from aiovban.packet.headers import VBANHeaderException

logger = logging.getLogger(__name__)
_aiovban_log_level = os.environ.get("AIOVBAN_LOG_LEVEL", "info").upper()
logging.getLogger("aiovban.asyncio.aiovban.asyncio.util").setLevel(_aiovban_log_level)


@dataclass
class VBANBaseProtocolMod(asyncio.DatagramProtocol):
    """VBANBaseProtocol workaround."""

    client: AsyncVBANClientMod

    def __post_init__(self) -> None:
        """Initialize."""
        # WORKAROUND: each instance gets it's own Future.
        self.done: asyncio.Future[Any] = asyncio.get_event_loop().create_future()
        # self.done = asyncio.get_event_loop().create_future()
        self.background_tasks: set[asyncio.Task[Any]] = set()

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


@dataclass
class VBANListenerProtocolMod(VBANBaseProtocolMod):
    """VBANListenerProcotol workaround."""

    def connection_made(self, transport) -> None:  # type: ignore[no-untyped-def]
        """Handle connection made."""
        logger.debug(f"Connection made to {transport}")

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        """Handle received datagram."""
        try:
            if self.client.quick_reject(addr[0]):
                return
            packet = VBANPacket.unpack(data)
            task = asyncio.create_task(self.client.process_packet(addr[0], addr[1], packet))
            self.background_tasks.add(task)
            task.add_done_callback(self.background_tasks.discard)
        except VBANHeaderException as e:
            logger.error(f"Error unpacking packet: {e}")


class AsyncVBANClientMod(AsyncVBANClient):  # type: ignore[misc]
    """AsyncVBANClient workaround."""

    async def listen(
        self,
        address: str = "0.0.0.0",
        port: int = 6980,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        """Create UDP listener."""
        loop = loop or asyncio.get_running_loop()

        # Create a socket and set the options
        self._transport, proto = await loop.create_datagram_endpoint(
            lambda: VBANListenerProtocolMod(self),
            local_addr=(address, port),
            allow_broadcast=not self.ignore_audio_streams,
        )

        # WORKAROUND: await, not return.
        await proto.done
