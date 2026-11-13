"""Model/base for a Player Provider implementation."""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, final

from music_assistant_models.enums import ProviderFeature
from music_assistant_models.errors import PlayerUnavailableError

from .provider import Provider

if TYPE_CHECKING:
    from music_assistant.models.player import Player
    from music_assistant.models.player_protocol import PlayerProtocol


class PlayerProvider(Provider):
    """
    Base representation of a Player Provider.

    A player provider owns protocols (PlayerProtocol), not players: it registers one per
    device it reaches with the players controller, which owns the player of that device.
    Player Provider implementations should inherit from this base model.
    """

    @property
    @final
    def supported_features(self) -> set[ProviderFeature]:
        """Return the features supported by this Provider."""
        # every player can be removed, which the players controller handles itself
        return {*self._supported_features, ProviderFeature.REMOVE_PLAYER}

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
        protocol = self.mass.players.get_protocol(protocol_id)
        if (
            protocol is not None
            # a mocked provider (tests) is no owner to compare against
            and isinstance(protocol.provider, PlayerProvider)
            and protocol.provider is not self
        ):
            # still returned: the warning only surfaces callers passing another provider's id,
            # so it names the call site
            caller = frame.f_back if (frame := inspect.currentframe()) else None
            self.logger.warning(
                "Provider %s looked up %s, which belongs to provider %s (called from %s:%s)",
                self.instance_id,
                protocol_id,
                protocol.provider.instance_id,
                caller.f_globals.get("__name__") if caller else "?",
                caller.f_lineno if caller else "?",
            )
            del frame, caller
        if raise_unavailable and (protocol is None or not protocol.available_for_playback):
            msg = f"Player {protocol_id} is not available"
            raise PlayerUnavailableError(msg)
        return protocol

    def schedule_discovery(self) -> None:
        """Run the discovery of this provider in a few seconds; repeated requests run it once."""
        task_id = f"discover_players_{self.instance_id}"
        self.mass.call_later(5, self.mass.run_provider_discovery, self.instance_id, task_id=task_id)

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

    async def discover_players(self) -> None:
        """
        Register every device this provider knows of that has no attached protocol.

        Called when the provider is (re)loaded and on every discovery request of the players
        controller: when one of its players is enabled or, unless it is a group, removed, and
        when one of its protocols that is not attached moves to another player (in a merge, or
        when it is taken off its player) or is switched on again as an output. A provider that discovers its devices through mdns only does not need to
        implement this.
        """

    @property
    def protocols(self) -> list[PlayerProtocol]:
        """
        Return the protocol objects of this provider that the players controller holds.

        One that still waits for the player it belongs to is included (its ``player``
        is None).
        """
        # a provider owns these protocols, so it always sees all of them - narrowing this
        # to what some user happens to see corrupts the provider's own bookkeeping, such
        # as the grouping options it derives from this list.
        return list(self.mass.players.iter_protocols(self.instance_id))
