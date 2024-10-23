"""AirPlay Player definition."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant.common.models.enums import PlayerState

if TYPE_CHECKING:
    from zeroconf.asyncio import AsyncServiceInfo

    from .provider import AirplayProvider
    from .raop import RaopStream


class AirPlayPlayer:
    """Holds the details of the (discovered) Airplay (RAOP) player."""

    def __init__(
        self, prov: AirplayProvider, player_id: str, discovery_info: AsyncServiceInfo, address: str
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

    async def cmd_stop(self, update_state: bool = True) -> None:
        """Send STOP command to player."""
        if self.raop_stream:
            # forward stop to the entire stream session
            await self.raop_stream.session.stop()
        if update_state and (mass_player := self.mass.players.get(self.player_id)):
            mass_player.state = PlayerState.IDLE
            self.mass.players.update(mass_player.player_id)

    async def cmd_play(self) -> None:
        """Send PLAY (unpause) command to player."""
        if self.raop_stream and self.raop_stream.running:
            await self.raop_stream.send_cli_command("ACTION=PLAY")

    async def cmd_pause(self) -> None:
        """Send PAUSE command to player."""
        if not self.raop_stream or not self.raop_stream.running:
            return
        await self.raop_stream.send_cli_command("ACTION=PAUSE")
