"""Universal Player Group Provider implementation."""

from __future__ import annotations

from typing import TYPE_CHECKING

import shortuuid
from music_assistant_models.errors import PlayerCommandFailed

from music_assistant.constants import CONF_DYNAMIC_GROUP_MEMBERS, CONF_GROUP_MEMBERS
from music_assistant.models.player_provider import PlayerProvider

from .constants import UGP_PREFIX
from .player import UniversalGroupPlayer

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry

    from music_assistant.models.player import Player


class UniversalGroupProvider(PlayerProvider):
    """Universal Group Player Provider."""

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to setup this provider."""
        # nothing to configure (for now)
        return ()

    async def create_group_player(
        self, name: str, members: list[str], dynamic: bool = True
    ) -> Player:
        """
        Create new Universal Group Player.

        :param name: Name of the group player.
        :param members: List of player ids to add to the group.
        :param dynamic: Whether the group is dynamic (members can change).
        """
        # filter out members that are not registered players
        # TODO: do we want to filter out groups here to prevent nested groups?
        members = [x for x in members if x in [y.player_id for y in self.mass.players]]
        protocol_id = f"{UGP_PREFIX}{shortuuid.random(8).lower()}"
        group = UniversalGroupPlayer(self, protocol_id, name=name)
        # attaching creates the player of the group and the records of both
        await self.mass.players.attach(group)
        if (player := group.player) is None:
            raise PlayerCommandFailed(f"Could not create group player {name}")
        await self.mass.config.save_protocol_config(
            protocol_id, {CONF_GROUP_MEMBERS: members, CONF_DYNAMIC_GROUP_MEMBERS: dynamic}
        )
        return player

    async def discover_players(self) -> None:
        """Attach every stored universal group that has no attached protocol."""
        for player_conf in await self.mass.config.get_player_configs(self.instance_id):
            for protocol_id in self.mass.config.get_protocol_ids_of_player(player_conf.player_id):
                if self.get_protocol(protocol_id):
                    continue  # already attached
                record = self.mass.config.get_protocol_record(protocol_id) or {}
                if record.get("provider") != self.instance_id:
                    continue
                await self.mass.players.attach(UniversalGroupPlayer(self, protocol_id))
