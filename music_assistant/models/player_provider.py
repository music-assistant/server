"""Model/base for a Metadata Provider implementation."""

from __future__ import annotations

from typing import TYPE_CHECKING

import shortuuid
from music_assistant_models.enums import ProviderFeature
from zeroconf import ServiceStateChange
from zeroconf.asyncio import AsyncServiceInfo

from music_assistant.constants import (
    CONF_DYNAMIC_GROUP_MEMBERS,
    CONF_GROUP_MEMBERS,
    SYNCGROUP_PREFIX,
)
from music_assistant.models.player import SyncGroupPlayer

from .provider import Provider

if TYPE_CHECKING:
    from music_assistant.models.player import Player


class PlayerProvider(Provider):
    """
    Base representation of a Player Provider (controller).

    Player Provider implementations should inherit from this base model.
    """

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        await self.discover_players()

    def on_player_enabled(self, player_id: str) -> None:
        """Call (by config manager) when a player gets enabled."""
        # default implementation: trigger discovery - feel free to override
        task_id = f"discover_players_{self.instance_id}"
        self.mass.call_later(5, self.discover_players, task_id=task_id)

    def on_player_disabled(self, player_id: str) -> None:
        """Call (by config manager) when a player gets disabled."""

    async def remove_player(self, player_id: str) -> None:
        """Remove a player from this provider."""
        # will only be called for providers with REMOVE_PLAYER feature set.
        raise NotImplementedError

    async def create_group_player(
        self, name: str, members: list[str], dynamic: bool = True
    ) -> Player:
        """
        Create new Group Player.

        Only called for providers that support CREATE_GROUP_PLAYER feature.

        :param name: Name of the group player
        :param members: List of player ids to add to the group
        :param dynamic: Whether the group is dynamic (members can change)
        """
        # default implementation for providers that support syncing players
        if ProviderFeature.SYNC_PLAYERS in self.supported_features:
            # we simply create a new syncgroup player with the given members
            # feel free to override or extend this method in your provider
            members = [x for x in members if x in [y.player_id for y in self.players]]
            player_id = f"{SYNCGROUP_PREFIX}{shortuuid.random(8).lower()}"
            self.mass.config.create_default_player_config(
                player_id=player_id,
                provider=self.lookup_key,
                name=name,
                enabled=True,
                values={
                    CONF_GROUP_MEMBERS: members,
                    CONF_DYNAMIC_GROUP_MEMBERS: dynamic,
                },
            )
            return await self._register_syncgroup_player(player_id)
        # all other providers should implement this method
        raise NotImplementedError

    async def remove_group_player(self, player_id: str) -> None:
        """
        Remove a group player.

        Only called for providers that support REMOVE_GROUP_PLAYER feature.

        :param player_id: ID of the group player to remove.
        """
        # default implementation for providers that support syncing players
        if ProviderFeature.SYNC_PLAYERS in self.supported_features and player_id.startswith(
            SYNCGROUP_PREFIX
        ):
            # we simply permanently unregister the syncgroup player and wipe its config
            await self.mass.players.unregister(player_id, True)
            return
        # all other providers should implement this method
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
        # discover syncgroup players
        if (
            ProviderFeature.SYNC_PLAYERS in self.supported_features
            and ProviderFeature.CREATE_GROUP_PLAYER in self.supported_features
        ):
            for player_conf in await self.mass.config.get_player_configs(self.lookup_key):
                if player_conf.player_id.startswith(SYNCGROUP_PREFIX):
                    await self._register_syncgroup_player(player_conf.player_id)

    async def _register_syncgroup_player(self, player_id: str) -> Player:
        """Register a syncgroup player."""
        syncgroup = SyncGroupPlayer(self, player_id)
        await self.mass.players.register_or_update(syncgroup)
        return syncgroup

    @property
    def players(self) -> list[Player]:
        """Return all players belonging to this provider."""
        return self.mass.players.all(provider_filter=self.lookup_key)
