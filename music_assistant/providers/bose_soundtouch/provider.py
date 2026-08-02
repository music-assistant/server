"""Bose SoundTouch player provider implementation."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import aiohttp
from aiobosesoundtouch.client import SoundtouchDevice
from aiobosesoundtouch.client.session_configuration import SessionConfiguration
from zeroconf import ServiceStateChange

from music_assistant.constants import CONF_ENTRY_MANUAL_DISCOVERY_IPS
from music_assistant.helpers.util import get_primary_ip_address_from_zeroconf
from music_assistant.models.player_provider import PlayerProvider

from .config import (
    PRESET_KEY_PREFIX,
    build_preset_config_entries,
    parse_preset_action,
    preset_media_key,
    preset_selected_media_key,
)
from .const import PLAYER_ID_PREFIX
from .player import BoseSoundTouchPlayer

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry, ProviderConfig
    from zeroconf.asyncio import AsyncServiceInfo


class BoseSoundTouchProvider(PlayerProvider):
    """Player provider for Bose SoundTouch speakers."""

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to configure this provider."""
        return (CONF_ENTRY_MANUAL_DISCOVERY_IPS, *await build_preset_config_entries(self))

    async def handle_config_action(self, action: str) -> tuple[ConfigEntry, ...]:
        """
        Handle a preset search/select button press and re-render the entries.

        A select button persists the currently selected search result as that preset's
        media URI; a search button only re-runs that preset's media search. Values are
        read from the (already persisted) stored config; no in-flight form is passed.

        :param action: The action id of the pressed button.
        """
        preset_id, is_select = parse_preset_action(action)
        if preset_id is None:
            return await super().handle_config_action(action)
        if is_select and (
            selected := str(self.get_config_value(preset_selected_media_key(preset_id), "") or "")
        ):
            self.mass.config.set_raw_provider_config_value(
                self.instance_id, preset_media_key(preset_id), selected
            )
        # only the search button runs a (slow) media search; selecting merely persists the
        # already fetched choice, which stays selectable without searching again
        return (
            CONF_ENTRY_MANUAL_DISCOVERY_IPS,
            *await build_preset_config_entries(
                self, refresh_preset_id=None if is_select else preset_id
            ),
        )

    async def update_config(self, config: ProviderConfig, changed_keys: set[str]) -> None:
        """Handle logic when the config is updated."""
        # the preset mappings are read on demand when a button is pressed, so hide those
        # keys from the base implementation: reloading the provider for a preset edit
        # would needlessly drop and rediscover every speaker
        await super().update_config(
            config,
            {key for key in changed_keys if not key.startswith(f"values/{PRESET_KEY_PREFIX}")},
        )

    def get_preset_media(self, preset_id: int) -> str:
        """
        Return the media URI mapped to the given physical preset button (empty if unset).

        :param preset_id: The physical preset button number (1-6).
        """
        return str(self.get_config_value(preset_media_key(preset_id), "") or "")

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
        client = SoundtouchDevice(
            session_configuration=SessionConfiguration(
                session=self.mass.http_session, ip=ip_address
            )
        )
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
