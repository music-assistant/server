"""Model/base for a Metadata Provider implementation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.errors import PlayerUnavailableError

from .provider import Provider

if TYPE_CHECKING:
    from music_assistant.models.player import Player
    from music_assistant.models.player_protocol import PlayerProtocol


class PlayerProvider(Provider):
    """
    Base representation of a Player Provider (controller).

    Player Provider implementations should inherit from this base model.
    """

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""

    def get_protocol(
        self, protocol_id: str, raise_unavailable: bool = False
    ) -> PlayerProtocol | None:
        """
        Return the protocol object with the given id.

        Only pass ids of this provider's own protocol objects.

        :param protocol_id: ID of one of this provider's protocol objects.
        :param raise_unavailable: Raise if the protocol object is unknown, unavailable or disabled.
        :raises PlayerUnavailableError: If raise_unavailable is set and the protocol object is
            unknown, unavailable or disabled.
        """
        protocol = self.mass.players.get_player(protocol_id)
        if (
            protocol is not None
            # a mocked provider (tests) is no owner to compare against
            and isinstance(protocol.provider, PlayerProvider)
            and protocol.provider is not self
        ):
            # still returned: the warning only surfaces callers passing another provider's id
            self.logger.warning(
                "Provider %s looked up %s, which belongs to provider %s",
                self.instance_id,
                protocol_id,
                protocol.provider.instance_id,
            )
        if raise_unavailable and (
            protocol is None or not protocol.state.available or not protocol.state.enabled
        ):
            msg = f"Player {protocol_id} is not available"
            raise PlayerUnavailableError(msg)
        return protocol

    def on_player_enabled(self, player_id: str) -> None:
        """Call (by config manager) when a player gets enabled."""
        # default implementation: trigger discovery - feel free to override
        task_id = f"discover_players_{self.instance_id}"
        self.mass.call_later(5, self.mass.run_provider_discovery, self.instance_id, task_id=task_id)

    def on_player_disabled(self, player_id: str) -> None:
        """Call (by config manager) when a player gets disabled."""
        # default implementation: unregister player from player controller
        # which will also trigger an unload on the player instance
        # feel free to override with a better implementation
        self.mass.create_task(self.mass.players.unregister(player_id))

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
    def protocols(self) -> list[PlayerProtocol]:
        """Return all protocol objects belonging to this provider."""
        # a provider owns these players, so it always sees all of them - narrowing this
        # to what some user happens to see corrupts the provider's own bookkeeping, such
        # as the grouping options it derives from this list. Note this still hides
        # disabled and still-initializing players; teardown needs those too and therefore
        # walks the registry itself (see mass.unload_provider).
        return list(
            self.mass.players.iter_players(
                provider_filter=self.instance_id, return_protocol_players=True
            )
        )
