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


def _protocol_id(host: str, port: int) -> str:
    """Return the protocol id of the MPD server at the given host and port."""
    return f"mpd_{host}_{port}"


class MPDPlayerProvider(PlayerProvider):
    """
    MPD Player provider.

    One provider instance manages one or more MPD servers. Each server
    is registered as a separate MA player. Servers are specified as a
    list of host or host:port entries in the provider configuration.
    """

    # the player of each configured server, also one that is not attached (its player is
    # disabled): the controller only remembers that one weakly, and a removal must reach it
    _host_players: dict[str, MPDPlayer]

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self._host_players = {}

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
        """Forget the servers that are no longer in the hosts config."""
        entries = cast("list[str]", self.config.get_value(CONF_MANUAL_IPS) or [])
        new_ids = {_protocol_id(h, p) for h, p in (_parse_host_entry(e) for e in entries)}
        for protocol_id in self.mass.config.get_protocol_ids_of_provider(self.instance_id):
            if protocol_id not in new_ids:
                await self.mass.players.remove_protocol(protocol_id)

    async def discover_players(self) -> None:
        """Register one MPDPlayer per entry in the hosts config that is not attached."""
        entries = cast("list[str]", self.config.get_value(CONF_MANUAL_IPS) or [])
        for entry in entries:
            host, port = _parse_host_entry(entry)
            protocol_id = _protocol_id(host, port)
            if self.get_protocol(protocol_id):
                continue
            if (player := self._host_players.get(protocol_id)) is None:
                player = MPDPlayer(provider=self, protocol_id=protocol_id, host=host, port=port)
                self._host_players[protocol_id] = player
            # connects only when the server wants the device
            await self.mass.players.register(player)

    def forget_host(self, host: str, port: int) -> None:
        """
        Take a device out of the hosts this provider is set up with.

        :param host: Hostname or IP address of the device.
        :param port: Port of the device.
        """
        self._host_players.pop(_protocol_id(host, port), None)
        entries = cast("list[str]", self.config.get_value(CONF_MANUAL_IPS) or [])
        new_entries = [entry for entry in entries if _parse_host_entry(entry) != (host, port)]
        if new_entries != entries:
            self._update_config_value(CONF_MANUAL_IPS, new_entries)
