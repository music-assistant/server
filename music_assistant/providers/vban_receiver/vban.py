"""VBAN subclasses to prevent unnecessary packet processing when the plugin isn't in use."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from aiovban.asyncio import AsyncVBANClient
from aiovban.asyncio.protocol import VBANListenerProtocol

if TYPE_CHECKING:
    from .provider import VBANReceiverProvider


@dataclass
class VBANListenerProtocolMod(VBANListenerProtocol):  # type: ignore[misc]
    """VBANListenerProcotol workaround."""

    controller: VBANReceiverProvider | None = None

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        """Handle received datagram."""
        # No need to process the datagram if no queue is currently streaming us
        if self.controller and not self.controller._in_use_by_player:
            return
        super().datagram_received(data, addr)


@dataclass
class AsyncVBANClientMod(AsyncVBANClient):  # type: ignore[misc]
    """AsyncVBANClient workaround."""

    async def listen(
        self,
        address: str = "0.0.0.0",
        port: int = 6980,
        loop: asyncio.AbstractEventLoop | None = None,
        controller: VBANReceiverProvider | None = None,
    ) -> asyncio.Future[Any]:
        """Create UDP listener."""
        loop = loop or asyncio.get_running_loop()

        # Create a socket and set the options
        self._transport, proto = await loop.create_datagram_endpoint(
            lambda: VBANListenerProtocolMod(self, controller),
            local_addr=(address, port),
            allow_broadcast=True,
        )
        return proto.done  # type: ignore[no-any-return]
