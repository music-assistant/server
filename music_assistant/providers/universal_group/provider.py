"""Universal Player Group Provider implementation."""

from __future__ import annotations

from typing import TYPE_CHECKING

import shortuuid

from music_assistant.constants import CONF_DYNAMIC_GROUP_MEMBERS, CONF_GROUP_MEMBERS
from music_assistant.models.player_provider import PlayerProvider

from .constants import UGP_PREFIX
from .player import UniversalGroupPlayer

if TYPE_CHECKING:
    from music_assistant.models.player import Player


class UniversalGroupProvider(PlayerProvider):
    """Universal Group Player Provider."""

    async def create_group_player(
        self, name: str, members: list[str], dynamic: bool = True
    ) -> Player:
        """Create new Universal Group Player."""
        # filter out members that are not registered players
        # TODO: do we want to filter out groups here to prevent nested groups?
        members = [x for x in members if x in [y.player_id for y in self.mass.players]]
        # generate a new player_id for the group player
        player_id = f"{UGP_PREFIX}{shortuuid.random(8).lower()}"
        self.mass.config.create_default_player_config(
            player_id=player_id,
            provider=self.instance_id,
            name=name,
            enabled=True,
            values={
                CONF_GROUP_MEMBERS: members,
                CONF_DYNAMIC_GROUP_MEMBERS: dynamic,
            },
        )
        return await self._register_player(player_id)

    async def remove_group_player(self, player_id: str) -> None:
        """
        Remove a group player.

        Only called for providers that support REMOVE_GROUP_PLAYER feature.

        :param player_id: ID of the group player to remove.
        """
        # we simply permanently unregister the player and wipe its config
        await self.mass.players.unregister(player_id, True)

    async def discover_players(self) -> None:
        """Discover players."""
        for player_conf in await self.mass.config.get_player_configs(self.instance_id):
            if player_conf.player_id.startswith(UGP_PREFIX):
                await self._register_player(player_conf.player_id)

    async def _register_player(self, player_id: str) -> Player:
        """Register a universal group player."""
        group = UniversalGroupPlayer(self, player_id)
        await self.mass.players.register_or_update(group)
        return group
