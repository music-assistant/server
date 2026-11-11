"""MPD Player Provider implementation."""

from __future__ import annotations

from typing import cast

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from music_assistant.constants import CONF_ENTRY_MANUAL_DISCOVERY_IPS
from music_assistant.models.player_provider import PlayerProvider

from .player import MPDPlayer

CONF_MANUAL_IPS = CONF_ENTRY_MANUAL_DISCOVERY_IPS.key


def _parse_host_entry(entry: str) -> tuple[str, int]:
    """
    Parse a single host entry into a (host, port) tuple.

    Accepted formats:
      - host
      - host:port

    Port defaults to 6600 if not specified.

    :param entry: A single host entry string.
    :return: Tuple of (host, port).
    """
    entry = entry.strip()
    if ":" in entry:
        host, port_str = entry.rsplit(":", 1)
        try:
            return host, int(port_str)
        except ValueError:
            return entry, 6600
    return entry, 6600


class MPDPlayerProvider(PlayerProvider):
    """
    MPD Player provider.

    One provider instance manages one or more MPD servers. Each server
    is registered as a separate MA player. Servers are specified as a
    list of host or host:port entries in the provider configuration.
    """

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

    async def loaded_in_mass(self) -> None:
        """Sync registered players against the current hosts config."""
        entries = cast("list[str]", self.config.get_value(CONF_MANUAL_IPS) or [])
        new_ids = {f"mpd_{h}_{p}" for h, p in (_parse_host_entry(e) for e in entries)}

        for player in self.protocols:
            if player.player_id not in new_ids:
                await self.mass.players.unregister(player.player_id)

        for entry in entries:
            host, port = _parse_host_entry(entry)
            player_id = f"mpd_{host}_{port}"
            if self.get_protocol(player_id):
                continue
            player = MPDPlayer(provider=self, player_id=player_id, host=host, port=port)
            await self.mass.players.register(player)

    async def discover_players(self) -> None:
        """Register one MPDPlayer per entry in the hosts config."""
        entries = cast("list[str]", self.config.get_value(CONF_MANUAL_IPS) or [])
        for entry in entries:
            host, port = _parse_host_entry(entry)
            player_id = f"mpd_{host}_{port}"
            if self.get_protocol(player_id):
                continue
            player = MPDPlayer(provider=self, player_id=player_id, host=host, port=port)
            await self.mass.players.register(player)

    def forget_host(self, host: str, port: int) -> None:
        """
        Take a device out of the hosts this provider is set up with.

        :param host: Hostname or IP address of the device.
        :param port: Port of the device.
        """
        entries = cast("list[str]", self.config.get_value(CONF_MANUAL_IPS) or [])
        new_entries = [entry for entry in entries if _parse_host_entry(entry) != (host, port)]
        if new_entries != entries:
            self._update_config_value(CONF_MANUAL_IPS, new_entries)
