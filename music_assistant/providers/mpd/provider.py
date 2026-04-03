"""MPD Player Provider implementation."""

from __future__ import annotations

from typing import cast

from music_assistant.models.player_provider import PlayerProvider

from .constants import CONF_HOSTS, DEFAULT_MPD_PORT
from .player import MPDPlayer


def _parse_hosts(hosts_str: str) -> list[tuple[str, int, str | None]]:
    """Parse the hosts config string into a list of (host, port, password) tuples.

    Accepted formats per entry (comma-separated):
      - host
      - host:port
      - host#password
      - host:port#password

    :param hosts_str: Raw value from the CONF_HOSTS config entry.
    :return: List of (host, port, password) tuples.
    """
    result = []
    for raw_entry in hosts_str.split(","):
        entry = raw_entry.strip()
        if not entry:
            continue
        password: str | None = None
        if "#" in entry:
            entry, password = entry.split("#", 1)
        if ":" in entry:
            host, port_str = entry.rsplit(":", 1)
            try:
                port = int(port_str)
            except ValueError:
                # colon was not a port separator (e.g. IPv6 without brackets)
                host = entry
                port = DEFAULT_MPD_PORT
        else:
            host = entry
            port = DEFAULT_MPD_PORT
        result.append((host, port, password))
    return result


class MPDPlayerProvider(PlayerProvider):
    """MPD Player provider.

    One provider instance manages one or more MPD servers. Servers are
    configured as a comma-separated list of host:port#password entries.
    Each server is registered as a separate MA player.
    """

    async def loaded_in_mass(self) -> None:
        """Sync registered players against the current hosts config."""
        hosts_str = cast("str", self.config.get_value(CONF_HOSTS))
        new_entries = _parse_hosts(hosts_str)
        new_ids = {f"mpd_{host}_{port}" for host, port, _ in new_entries}

        # Remove players that are no longer in the config
        for player in self.players:
            if player.player_id not in new_ids:
                await self.mass.players.unregister(player.player_id)

        # Register any new players
        for host, port, password in new_entries:
            player_id = f"mpd_{host}_{port}"
            if self.mass.players.get_player(player_id):
                continue
            player = MPDPlayer(
                provider=self,
                player_id=player_id,
                host=host,
                port=port,
                password=password,
            )
            await self.mass.players.register(player)

    async def discover_players(self) -> None:
        """Register one MPDPlayer per entry in the hosts config."""
        hosts_str = cast("str", self.config.get_value(CONF_HOSTS))
        for host, port, password in _parse_hosts(hosts_str):
            player_id = f"mpd_{host}_{port}"
            if self.mass.players.get_player(player_id):
                continue
            player = MPDPlayer(
                provider=self,
                player_id=player_id,
                host=host,
                port=port,
                password=password,
            )
            await self.mass.players.register(player)

    async def remove_player(self, player_id: str) -> None:
        """Remove a player and unregister it from MA."""
        await self.mass.players.unregister(player_id)
