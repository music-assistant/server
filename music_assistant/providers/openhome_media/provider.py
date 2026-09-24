"""Linn/OpenHome Media Player Provider."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from async_upnp_client.aiohttp import AiohttpSessionRequester
from async_upnp_client.client_factory import UpnpFactory
from music_assistant_models.player import DeviceInfo

from music_assistant.constants import CONF_PLAYERS, VERBOSE_LOG_LEVEL
from music_assistant.helpers.json import SerializableType
from music_assistant.models.player_provider import PlayerProvider
from music_assistant.providers.openhome_media.constants import CALLBACK_URL
from music_assistant.providers.openhome_media.helpers import (
    OpenHomeNotifyServer,
    create_short_player_id,
)
from music_assistant.providers.openhome_media.player import OpenHomePlayer

if TYPE_CHECKING:
    from async_upnp_client.client import UpnpRequester
    from async_upnp_client.utils import CaseInsensitiveDict
    from music_assistant_models.config_entries import ConfigEntry


class OpenHomePlayerProvider(PlayerProvider):
    """Linn/OpenHome Media Player provider."""

    _ignored_udns: set[str]

    lock: asyncio.Lock
    requester: UpnpRequester
    upnp_factory: UpnpFactory
    notify_server: OpenHomeNotifyServer

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to configure this provider."""
        return ()

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        if self.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
            logging.getLogger("async_upnp_client").setLevel(logging.DEBUG)
        else:
            logging.getLogger("async_upnp_client").setLevel(self.logger.level + 10)

        self.lock = asyncio.Lock()
        self._ignored_udns = set()
        self.requester = AiohttpSessionRequester(self.mass.http_session, with_sleep=True)
        self.upnp_factory = UpnpFactory(self.requester, non_strict=True)
        self.notify_server = OpenHomeNotifyServer(self.requester, self.mass)

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        self.mass.streams.unregister_dynamic_route(path=CALLBACK_URL, method="NOTIFY")
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
            if isinstance(player, OpenHomePlayer)
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
        # if not ssdp_st or "Product" not in ssdp_st:
        if not ssdp_st:
            return

        ssdp_usn: str | None = discovery_info.get("usn")
        if not ssdp_usn:
            return
        if not (
            ("urn:linn-co-uk:device:Source:1" in ssdp_usn)
            or ("urn:av-openhome-org:device:Source:1" in ssdp_usn)
        ):
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
        """Handle discovered Linn/OpenHome Media player."""
        async with self.lock:
            # skip devices that we've already determined should be ignored
            if udn in self._ignored_udns:
                self.logger.debug("Ignoring device with udn: %s", udn)
                return

            # generate a short player id based on udn
            short_id = create_short_player_id(udn)
            player_id: str = f"ohm{short_id}"

            if openhome_player := self.mass.players.get_player(udn):
                # existing player
                assert isinstance(openhome_player, OpenHomePlayer)
                if openhome_player.description_url == description_url and openhome_player.available:
                    # nothing to do, device is already connected
                    return
                # update description url to newly discovered one
                openhome_player.description_url = description_url
            else:
                # new player detected, setup our OpenHomePlayer wrapper
                conf_key = f"{CONF_PLAYERS}/{udn}/enabled"
                enabled = self.mass.config.get(conf_key, True)
                # ignore disabled players
                if not enabled:
                    self.logger.debug("Ignoring disabled player: %s", udn)
                    return

                openhome_player = OpenHomePlayer(
                    provider=self,
                    player_id=player_id,
                    description_url=description_url,
                    device=None,
                )
                # will be updated later when device connects
                openhome_player._attr_device_info = DeviceInfo(
                    model="unknown",
                    manufacturer="unknown",
                )

            # Setup will return False if the device should be ignored (e.g., passive speaker)
            if not await openhome_player.setup():
                self.logger.debug("Setup failed for %s", udn)
                self._ignored_udns.add(udn)
