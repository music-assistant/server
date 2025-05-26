"""Model/base for a Metadata Provider implementation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from zeroconf import ServiceStateChange
from zeroconf.asyncio import AsyncServiceInfo

from .provider import Provider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import PlayerConfig
    from music_assistant_models.player import Player

# ruff: noqa: ARG001, ARG002


class PlayerProvider(Provider):
    """
    Base representation of a Player Provider (controller).

    Player Provider implementations should inherit from this base model.
    """

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        await self.discover_players()

    async def on_player_config_change(self, config: PlayerConfig, changed_keys: set[str]) -> None:
        """Call (by config manager) when the configuration of a player changes."""
        # default implementation: feel free to override
        if (
            "enabled" in changed_keys
            and config.enabled
            and not self.mass.players.get(config.player_id)
        ):
            # if a player gets enabled, trigger discovery
            task_id = f"discover_players_{self.instance_id}"
            self.mass.call_later(5, self.discover_players, task_id=task_id)
        else:
            await self.poll_player(config.player_id)

    async def remove_player(self, player_id: str) -> None:
        """Remove a player from this provider."""
        # will only be called for providers with REMOVE_PLAYER feature set.
        raise NotImplementedError

    async def discover_players(self) -> None:
        """Discover players for this provider."""
        # This will be called (once) when the player provider is loaded into MA.
        # Default implementation is mdns discovery, which will also automatically
        # discovery players during runtime. If a provider overrides this method and
        # doesn't use mdns, it is responsible for periodically searching for new players.
        if not self.available:
            return
        for mdns_type in self.manifest.mdns_discovery or []:
            for mdns_name in set(self.mass.aiozc.zeroconf.cache.cache):
                if mdns_type not in mdns_name or mdns_type == mdns_name:
                    continue
                info = AsyncServiceInfo(mdns_type, mdns_name)
                if await info.async_request(self.mass.aiozc.zeroconf, 3000):
                    await self.on_mdns_service_state_change(
                        mdns_name, ServiceStateChange.Added, info
                    )

    @property
    def players(self) -> list[Player]:
        """Return all players belonging to this provider."""
        return self.mass.players.all(provider_filter=self.lookup_key)
