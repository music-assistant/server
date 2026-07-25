"""Bose SoundTouch player provider implementation."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import aiohttp
from zeroconf import ServiceStateChange

from music_assistant.constants import CONF_ENTRY_MANUAL_DISCOVERY_IPS
from music_assistant.helpers.util import get_primary_ip_address_from_zeroconf
from music_assistant.models.player_provider import PlayerProvider

from .client import SoundTouchClient
from .const import PLAYER_ID_PREFIX
from .player import BoseSoundTouchPlayer

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry
    from zeroconf.asyncio import AsyncServiceInfo


class BoseSoundTouchProvider(PlayerProvider):
    """Player provider for Bose SoundTouch speakers."""

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to configure this provider."""
        return (CONF_ENTRY_MANUAL_DISCOVERY_IPS,)

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        manual_ips = cast("list[str]", self.config.get_value(CONF_ENTRY_MANUAL_DISCOVERY_IPS.key))
        for ip_address in manual_ips:
            if stripped := ip_address.strip():
                await self.try_add_player(stripped)

    async def on_mdns_service_state_change(
        self, name: str, state_change: ServiceStateChange, info: AsyncServiceInfo | None
    ) -> None:
        """Handle MDNS service state callback."""
        if not info or state_change == ServiceStateChange.Removed:
            # availability is tracked by the player itself (websocket + polling)
            return
        ip_address = get_primary_ip_address_from_zeroconf(info)
        if not ip_address:
            return
        # if we already know a player on this address, just trigger an update
        if existing := self._get_player_by_ip(ip_address):
            self.mass.players.trigger_player_update(existing.player_id)
            return
        # debounce setup to avoid duplicate work on rapid mDNS updates
        task_id = f"setup_soundtouch_{ip_address}"
        self.mass.call_later(2, self.try_add_player, ip_address, task_id=task_id)

    async def try_add_player(self, ip_address: str) -> None:
        """Try to add a Bose SoundTouch speaker as a player."""
        client = SoundTouchClient(self.mass.http_session, ip_address)
        try:
            info = await client.get_info()
        except (aiohttp.ClientError, TimeoutError, OSError) as err:
            self.logger.debug("Failed to query SoundTouch device at %s: %s", ip_address, err)
            return
        if not info.device_id:
            self.logger.debug("SoundTouch device at %s returned no device id", ip_address)
            return

        player_id = f"{PLAYER_ID_PREFIX}{info.device_id}"
        if existing := self.mass.players.get_player(player_id):
            # already known: refresh its address and bail out
            assert isinstance(existing, BoseSoundTouchPlayer)
            existing.update_ip_address(ip_address)
            return

        player = BoseSoundTouchPlayer(self, player_id, client, info)
        try:
            await player.setup()
            await self.mass.players.register_or_update(player)
        except Exception:
            self.logger.exception("Failed to register SoundTouch player %s", info.name)
            await player.on_unload()
            return
        self.logger.info("Registered Bose SoundTouch player: %s (%s)", info.name, ip_address)

    def _get_player_by_ip(self, ip_address: str) -> BoseSoundTouchPlayer | None:
        """Return an existing SoundTouch player with the given IP address (if any)."""
        for player in self.players:
            if (
                isinstance(player, BoseSoundTouchPlayer)
                and player.device_info.ip_address == ip_address
            ):
                return player
        return None
