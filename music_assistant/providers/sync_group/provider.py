"""Sync Group Player Provider implementation."""

from __future__ import annotations

from typing import TYPE_CHECKING

import shortuuid
from music_assistant_models.enums import PlayerType

from music_assistant.constants import CONF_DYNAMIC_GROUP_MEMBERS, CONF_GROUP_MEMBERS
from music_assistant.models.player_provider import PlayerProvider

from .constants import SGP_PREFIX
from .player import SyncGroupPlayer

if TYPE_CHECKING:
    from music_assistant.models.player import Player


class SyncGroupProvider(PlayerProvider):
    """Sync Group Player Provider."""

    async def create_group_player(
        self, name: str, members: list[str], dynamic: bool = True
    ) -> Player:
        """
        Create new Sync Group Player.

        :param name: Name of the group player.
        :param members: List of player ids to add to the group.
        :param dynamic: Whether the group is dynamic (members can change).
        """
        # validation to ensure all members are compatible (can_group_with check)
        members = [x for x in members if x in [y.player_id for y in self.mass.players]]
        final_members: list[str] = []
        can_group_with: set[str] = set()
        for member_id in members:
            member = self.mass.players.get_player(member_id)
            if member is None or not member.available:
                continue
            if not can_group_with:
                # first member, add all its compatible players to the can_group_with set
                can_group_with = set(member.state.can_group_with)
            if member_id not in can_group_with:
                # member is not compatible with the current group, skip it
                continue
            final_members.append(member_id)
        # generate a new player_id for the group player
        player_id = f"{SGP_PREFIX}{shortuuid.random(8).lower()}"
        self.mass.config.create_default_player_config(
            player_id=player_id,
            provider=self.instance_id,
            player_type=PlayerType.GROUP,
            name=name,
            enabled=True,
            values={
                CONF_GROUP_MEMBERS: final_members,
                CONF_DYNAMIC_GROUP_MEMBERS: dynamic,
            },
        )
        return await self._register_player(player_id)

    async def remove_group_player(self, player_id: str) -> None:
        """
        Remove a group player.

        :param player_id: ID of the group player to remove.
        """
        # we simply permanently unregister the player and wipe its config
        await self.mass.players.unregister(player_id, True)

    async def discover_players(self) -> None:
        """Discover players."""
        for player_conf in await self.mass.config.get_player_configs(self.instance_id):
            if player_conf.player_id.startswith(SGP_PREFIX):
                await self._register_player(player_conf.player_id)

    async def _register_player(self, player_id: str) -> Player:
        """Register a sync group player."""
        group = SyncGroupPlayer(self, player_id)
        await self.mass.players.register_or_update(group)
        return group
