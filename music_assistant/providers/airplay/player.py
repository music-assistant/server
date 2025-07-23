"""AirPlay Player definition."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from zeroconf.asyncio import AsyncServiceInfo

    from .airplay2 import AirPlay2Stream
    from .provider import AirPlayProvider
    from .raop import RaopStream


class AirPlayPlayer:
    """Holds the details of the (discovered) AirPlay (RAOP or AirPlay2) player."""

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
        self.airplay2_stream: AirPlay2Stream | None = None
        self.last_command_sent = 0.0
        self._lock = asyncio.Lock()

    async def cmd_stop(self) -> None:
        """Send STOP command to player."""
        if self.raop_stream and self.raop_stream.session:
            # forward stop to the entire stream session
            await self.raop_stream.session.stop()
        elif self.airplay2_stream and self.airplay2_stream.session:
            # forward stop to the entire stream session
            await self.airplay2_stream.session.stop()

    async def cmd_play(self) -> None:
        """Send PLAY (unpause) command to player."""
        async with self._lock:
            if self.raop_stream and self.raop_stream.running:
                await self.raop_stream.send_cli_command("ACTION=PLAY")
            elif self.airplay2_stream and self.airplay2_stream.running:
                await self.airplay2_stream.send_cli_command("ACTION=PLAY")

    async def cmd_pause(self) -> None:
        """Send PAUSE command to player."""
        async with self._lock:
            if self.raop_stream and self.raop_stream.running:
                await self.raop_stream.send_cli_command("ACTION=PAUSE")
            elif self.airplay2_stream and self.airplay2_stream.running:
                await self.airplay2_stream.send_cli_command("ACTION=PAUSE")
