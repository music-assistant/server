"""Bose SoundTouch player provider implementation."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import aiohttp
from zeroconf import ServiceStateChange

from music_assistant.constants import CONF_ENTRY_MANUAL_DISCOVERY_IPS
from music_assistant.helpers.util import get_primary_ip_address_from_zeroconf
from music_assistant.models.player_provider import PlayerProvider

from .client import SoundTouchClient
from .config import (
    ACTION_ASSIGN,
    ACTION_SEARCH,
    CONF_SEARCH_MEDIA_TYPE,
    CONF_SEARCH_QUERY,
    CONF_SEARCH_RESULT,
    CONF_SEARCH_TARGET,
    PRESET_KEY_PREFIX,
    build_preset_config_entries,
    preset_media_key,
)
from .const import PLAYER_ID_PREFIX, PRESET_IDS
from .player import BoseSoundTouchPlayer

if TYPE_CHECKING:
    from music_assistant_models.config_entries import (
        ConfigActionResult,
        ConfigEntry,
        ProviderConfig,
    )
    from zeroconf.asyncio import AsyncServiceInfo


def _search_values_to_reset(changed_keys: set[str]) -> tuple[str, ...]:
    """Return search fields invalidated by an earlier step changing."""
    if f"values/{CONF_SEARCH_MEDIA_TYPE}" in changed_keys:
        return (CONF_SEARCH_QUERY, CONF_SEARCH_RESULT, CONF_SEARCH_TARGET)
    if f"values/{CONF_SEARCH_QUERY}" in changed_keys:
        return (CONF_SEARCH_RESULT, CONF_SEARCH_TARGET)
    if f"values/{CONF_SEARCH_RESULT}" in changed_keys:
        return (CONF_SEARCH_TARGET,)
    return ()


class BoseSoundTouchProvider(PlayerProvider):
    """Player provider for Bose SoundTouch speakers."""

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to configure this provider."""
        return (CONF_ENTRY_MANUAL_DISCOVERY_IPS, *await build_preset_config_entries(self))

    async def handle_config_action(
        self, action: str
    ) -> tuple[ConfigEntry, ...] | ConfigActionResult | None:
        """
        Handle a preset search/assignment button press and re-render the entries.

        The search button refreshes the shared result list. The assignment button copies
        the selected result to the chosen physical preset.

        :param action: The action id of the pressed button.
        """
        if action not in (ACTION_SEARCH, ACTION_ASSIGN):
            return await super().handle_config_action(action)
        if action == ACTION_SEARCH:
            self._reset_search_values(CONF_SEARCH_RESULT, CONF_SEARCH_TARGET)
        if action == ACTION_ASSIGN:
            selected = str(self.get_config_value(CONF_SEARCH_RESULT, "") or "")
            target = str(self.get_config_value(CONF_SEARCH_TARGET, "") or "")
            if selected and target.isdigit() and (preset_id := int(target)) in PRESET_IDS:
                self._update_config_value(preset_media_key(preset_id), selected, immediate=True)
                self._reset_search_values(
                    CONF_SEARCH_MEDIA_TYPE,
                    CONF_SEARCH_QUERY,
                    CONF_SEARCH_RESULT,
                    CONF_SEARCH_TARGET,
                )
        return (
            CONF_ENTRY_MANUAL_DISCOVERY_IPS,
            *await build_preset_config_entries(self, refresh_results=action == ACTION_SEARCH),
        )

    async def update_config(self, config: ProviderConfig, changed_keys: set[str]) -> None:
        """Handle logic when the config is updated."""
        for key in _search_values_to_reset(changed_keys):
            self._update_config_value(key, "", immediate=True)
            if entry := config.values.get(key):
                entry.value = ""
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

    def _reset_search_values(self, *keys: str) -> None:
        """Reset persisted fields that belong to later search steps."""
        for key in keys:
            self._update_config_value(key, "", immediate=True)

    def _get_player_by_ip(self, ip_address: str) -> BoseSoundTouchPlayer | None:
        """Return an existing SoundTouch player with the given IP address (if any)."""
        for player in self.players:
            if (
                isinstance(player, BoseSoundTouchPlayer)
                and player.device_info.ip_address == ip_address
            ):
                return player
        return None
