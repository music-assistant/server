"""FullyKiosk Player provider for Music Assistant."""

from __future__ import annotations

import logging
from typing import cast

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from music_assistant.constants import CONF_ENTRY_MANUAL_DISCOVERY_IPS, VERBOSE_LOG_LEVEL
from music_assistant.models.player_provider import PlayerProvider

from .dashboard import FullyKioskDashboards
from .player import DEFAULT_PORT, FullyKioskPlayer

CONF_MANUAL_IPS = CONF_ENTRY_MANUAL_DISCOVERY_IPS.key


def _parse_host_entry(entry: str) -> tuple[str, int]:
    """
    Parse a single host entry into a (host, port) tuple.

    Accepted formats:
      - host
      - host:port

    Port defaults to 2323 if not specified.

    :param entry: A single host entry string.
    :return: Tuple of (host, port).
    """
    entry = entry.strip()
    if ":" in entry:
        host, port_str = entry.rsplit(":", 1)
        try:
            return host, int(port_str)
        except ValueError:
            return entry, DEFAULT_PORT
    return entry, DEFAULT_PORT


class FullyKioskProvider(PlayerProvider):
    """
    Fully Kiosk Player provider.

    One provider instance manages one or more Fully Kiosk devices. Each device
    is registered as a separate MA player. Devices are specified as a list of
    host or host:port entries in the provider configuration; the password (and
    optional SSL options) are configured on the player itself.
    """

    dashboards: FullyKioskDashboards

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to configure this provider."""
        return (
            ConfigEntry(
                key="manual_discovery_ip_addresses",
                type=ConfigEntryType.STRING,
                default_value=[],
                required=True,
                multi_value=True,
            ),
        )

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self.dashboards = FullyKioskDashboards(self)
        if self.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
            logging.getLogger("fullykiosk").setLevel(logging.DEBUG)
        else:
            logging.getLogger("fullykiosk").setLevel(self.logger.level + 10)

    async def loaded_in_mass(self) -> None:
        """Sync registered players against the current hosts config."""
        entries = cast("list[str]", self.config.get_value(CONF_MANUAL_IPS) or [])
        new_ids = {f"fully_kiosk_{h}_{p}" for h, p in (_parse_host_entry(e) for e in entries)}

        for player in self.players:
            if player.player_id not in new_ids:
                await self.mass.players.unregister(player.player_id)

        for entry in entries:
            host, port = _parse_host_entry(entry)
            player_id = f"fully_kiosk_{host}_{port}"
            if self.mass.players.get_player(player_id):
                continue
            player = FullyKioskPlayer(provider=self, player_id=player_id, host=host, port=port)
            await self.mass.players.register(player)

    async def discover_players(self) -> None:
        """Register one FullyKioskPlayer per entry in the hosts config."""
        entries = cast("list[str]", self.config.get_value(CONF_MANUAL_IPS) or [])
        for entry in entries:
            host, port = _parse_host_entry(entry)
            player_id = f"fully_kiosk_{host}_{port}"
            if self.mass.players.get_player(player_id):
                continue
            player = FullyKioskPlayer(provider=self, player_id=player_id, host=host, port=port)
            await self.mass.players.register(player)

    async def remove_player(self, player_id: str) -> None:
        """Remove a player and persist that removal in the provider config."""
        host_port: tuple[str, int] | None = None
        if player := self.mass.players.get_player(player_id):
            if isinstance(player, FullyKioskPlayer):
                host_port = (player.host, player.port)
        if host_port is None:
            for entry in cast("list[str]", self.config.get_value(CONF_MANUAL_IPS) or []):
                host, port = _parse_host_entry(entry)
                if f"fully_kiosk_{host}_{port}" == player_id:
                    host_port = (host, port)
                    break
        if host_port is not None:
            entries = cast("list[str]", self.config.get_value(CONF_MANUAL_IPS) or [])
            new_entries = [entry for entry in entries if _parse_host_entry(entry) != host_port]
            if new_entries != entries:
                self._update_config_value(CONF_MANUAL_IPS, new_entries)
        await self.mass.players.unregister(player_id, True)

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        await self.dashboards.unload()
        await super().unload(is_removed)
