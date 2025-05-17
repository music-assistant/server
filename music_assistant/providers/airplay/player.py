"""AirPlay Player definition."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from zeroconf.asyncio import AsyncServiceInfo

    from .provider import AirPlayProvider
    from .raop import RaopStream


class AirPlayPlayer:
    """Holds the details of the (discovered) AirPlay (RAOP) player."""

    def __init__(
        self, prov: AirPlayProvider, player_id: str, discovery_info: AsyncServiceInfo, address: str
    ) -> None:
        """Initialize AirPlayPlayer."""
        self.prov = prov
        self.mass = prov.mass
        self.player_id = player_id
        self.discovery_info = discovery_info
        self.address = address
        self.logger = prov.logger.getChild(player_id)
        self.raop_stream: RaopStream | None = None
        self.last_command_sent = 0.0
        self._lock = asyncio.Lock()

    async def cmd_stop(self) -> None:
        """Send STOP command to player."""
        if self.raop_stream and self.raop_stream.session:
            # forward stop to the entire stream session
            await self.raop_stream.session.stop()

    async def cmd_play(self) -> None:
        """Send PLAY (unpause) command to player."""
        async with self._lock:
            if self.raop_stream and self.raop_stream.running:
                await self.raop_stream.send_cli_command("ACTION=PLAY")

    async def cmd_pause(self) -> None:
        """Send PAUSE command to player."""
        async with self._lock:
            if not self.raop_stream or not self.raop_stream.running:
                return
            await self.raop_stream.send_cli_command("ACTION=PAUSE")
