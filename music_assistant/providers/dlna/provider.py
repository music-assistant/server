"""DLNA Player Provider."""

import asyncio
import time
from typing import TYPE_CHECKING

from async_upnp_client.aiohttp import AiohttpSessionRequester
from async_upnp_client.client_factory import UpnpFactory
from music_assistant_models.player import DeviceInfo

from music_assistant.constants import CONF_PLAYERS
from music_assistant.helpers.json import SerializableType
from music_assistant.models.player_provider import PlayerProvider

from .helpers import DLNANotifyServer
from .player import DLNAPlayer

if TYPE_CHECKING:
    from async_upnp_client.client import UpnpRequester
    from async_upnp_client.utils import CaseInsensitiveDict
    from music_assistant_models.config_entries import ConfigEntry


class DLNAPlayerProvider(PlayerProvider):
    """DLNA Player provider."""

    _ignored_udns: set[str]

    lock: asyncio.Lock
    requester: UpnpRequester
    upnp_factory: UpnpFactory
    notify_server: DLNANotifyServer

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to configure this provider."""
        return ()

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self.lock = asyncio.Lock()
        self._ignored_udns = set()
        self.requester = AiohttpSessionRequester(self.mass.http_session, with_sleep=True)
        self.upnp_factory = UpnpFactory(self.requester, non_strict=True)
        self.notify_server = DLNANotifyServer(self.requester, self.mass)

    async def unload(self, is_removed: bool = False) -> None:
        """
        Handle unload/close of the provider.

        Called when provider is deregistered (e.g. MA exiting or config reloading).
        """
        self.mass.streams.unregister_dynamic_route("/notify", "NOTIFY")
        self._ignored_udns = set()

    async def get_diagnostics(self) -> dict[str, SerializableType]:
        """Return diagnostics info for this provider to include in diagnostics reports."""
        now = time.time()
        devices: list[SerializableType] = [
            {
                "model": player.device_info.model,
                "manufacturer": player.device_info.manufacturer,
                "available": player.available,
                "eventing": not player.force_poll,
                "last_seen_age_sec": round(now - player.last_seen),
            }
            for player in self.players
            if isinstance(player, DLNAPlayer)
        ]
        return {
            "ignored_devices": len(self._ignored_udns),
            "devices": devices,
        }

    async def on_upnp_service_discovered(
        self, search_target: str, discovery_info: CaseInsensitiveDict
    ) -> None:
        """Handle SSDP discovery callbacks."""
        del search_target
        ssdp_st: str | None = discovery_info.get("st", discovery_info.get("nt"))
        if not ssdp_st or "MediaRenderer" not in ssdp_st:
            return

        ssdp_usn: str | None = discovery_info.get("usn")
        if not ssdp_usn:
            return

        ssdp_udn: str | None = discovery_info.get("_udn")
        if not ssdp_udn and ssdp_usn.startswith("uuid:"):
            ssdp_udn = ssdp_usn.split("::")[0]
        if not ssdp_udn:
            return

        description_url: str | None = discovery_info.get("location")
        if not description_url:
            return

        await self._device_discovered(ssdp_udn, description_url)

    async def _device_discovered(self, udn: str, description_url: str) -> None:
        """Handle discovered DLNA player."""
        async with self.lock:
            # skip devices that we've already determined should be ignored
            if udn in self._ignored_udns:
                return

            if dlna_player := self.mass.players.get_player(udn):
                # existing player
                assert isinstance(dlna_player, DLNAPlayer)
                if dlna_player.description_url == description_url and dlna_player.available:
                    # nothing to do, device is already connected
                    return
                # update description url to newly discovered one
                dlna_player.description_url = description_url
            else:
                # new player detected, setup our DLNAPlayer wrapper
                conf_key = f"{CONF_PLAYERS}/{udn}/enabled"
                enabled = self.mass.config.get(conf_key, True)
                # ignore disabled players
                if not enabled:
                    self.logger.debug("Ignoring disabled player: %s", udn)
                    return

                dlna_player = DLNAPlayer(
                    provider=self,
                    player_id=udn,
                    description_url=description_url,
                )
                # will be updated later when device connects
                dlna_player._attr_device_info = DeviceInfo(
                    model="unknown",
                    manufacturer="unknown",
                )

            # Setup will return False if the device should be ignored (e.g., passive speaker)
            if not await dlna_player.setup():
                self._ignored_udns.add(udn)
