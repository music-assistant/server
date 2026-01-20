"""Model/base for a Metadata Provider implementation."""

from __future__ import annotations

from typing import TYPE_CHECKING

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
        raise NotImplementedError

    async def remove_group_player(self, player_id: str) -> None:
        """
        Remove a group player.

        Only called for providers that support REMOVE_GROUP_PLAYER feature.

        :param player_id: ID of the group player to remove.
        """
        raise NotImplementedError

    async def discover_players(self) -> None:
        """Discover players for this provider."""
        # This will be called when the player provider is (re)loaded into MA.
        # For providers that support dynamic discovery of players via mdns,
        # there is no need to implement this method.

    @property
    def players(self) -> list[Player]:
        """Return all players belonging to this provider."""
        return self.mass.players.all(provider_filter=self.instance_id, return_sync_groups=False)
