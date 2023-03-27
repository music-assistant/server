"""
Universal Group Player provider.

This is more like a "virtual" player provider,
allowing the user to create player groups from all players known in the system.
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from music_assistant.common.models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant.common.models.enums import ConfigEntryType, PlayerState
from music_assistant.common.models.player import Player
from music_assistant.common.models.queue_item import QueueItem
from music_assistant.constants import CONF_HIDE_GROUP_CHILDS
from music_assistant.server.models.player_provider import PlayerProvider

if TYPE_CHECKING:
    from music_assistant.common.models.config_entries import ProviderConfig
    from music_assistant.common.models.provider import ProviderManifest
    from music_assistant.server import MusicAssistant
    from music_assistant.server.models import ProviderInstanceType
    from music_assistant.server.providers.slimproto import SlimprotoProvider

CONF_GROUP_MEMBERS = "group_members"

# ruff: noqa: ARG002


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    prov = SlimprotoProvider(mass, manifest, config)
    await prov.handle_setup()
    return prov


async def get_config_entries(
    mass: MusicAssistant, manifest: ProviderManifest  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    return tuple()  # we do not have any config entries (yet)


class UniversalGroupProvider(PlayerProvider):
    """Base/builtin provider for universally grouping players."""

    async def handle_setup(self) -> None:
        """Handle async initialization of the provider."""
        self.player_id = self.instance_id

    async def unload(self) -> None:
        """Handle close/cleanup of the provider."""

    def get_player_config_entries(self, player_id: str) -> tuple[ConfigEntry]:  # noqa: ARG002
        """Return all (provider/player specific) Config Entries for the given player (if any)."""
        all_players = tuple(
            ConfigValueOption(x.display_name, x.player_id)
            for x in self.mass.players.all(True, True, False)
            if x.player_id != self.player_id
        )
        return (
            ConfigEntry(
                key=CONF_GROUP_MEMBERS,
                label="Group members",
                default_value=[],
                options=all_players,
                description="Select all players you want to be part of this group",
                multi_value=True,
            ),
            ConfigEntry(
                key=CONF_HIDE_GROUP_CHILDS,
                type=ConfigEntryType.STRING,
                options=[
                    ConfigValueOption("Always", "always"),
                    ConfigValueOption("Only if the group is active/powered", "active"),
                    ConfigValueOption("Never", "never"),
                ],
                default_value="active",
                label="Hide playergroup members in UI",
                description="Hide the individual player entry for the members of this group "
                "in the user interface.",
                advanced=True,
            ),
        )

    async def cmd_stop(self, player_id: str) -> None:
        """Send STOP command to given player."""
        # forward command to player and any connected sync child's
        async with asyncio.TaskGroup() as tg:
            for member in self._get_active_members(only_powered=True):
                if member.state == PlayerState.IDLE:
                    continue
                tg.create_task(self.mass.players.cmd_stop(member.player_id))

    async def cmd_play(self, player_id: str) -> None:
        """Send PLAY command to given player."""
        async with asyncio.TaskGroup() as tg:
            for member in self._get_active_members(only_powered=True):
                tg.create_task(self.mass.players.cmd_play(member.player_id))

    async def cmd_play_media(
        self,
        player_id: str,
        queue_item: QueueItem,
        seek_position: int = 0,
        fade_in: bool = False,
        flow_mode: bool = False,
    ) -> None:
        """Send PLAY MEDIA command to given player.

        This is called when the Queue wants the player to start playing a specific QueueItem.
        The player implementation can decide how to process the request, such as playing
        queue items one-by-one or enqueue all/some items.

            - player_id: player_id of the player to handle the command.
            - queue_item: the QueueItem to start playing on the player.
            - seek_position: start playing from this specific position.
            - fade_in: fade in the music at start (e.g. at resume).
        """
        assert player_id == self.instance_id
        # send stop first
        await self.cmd_stop(player_id)

        # power ON
        await self.cmd_power(player_id, True)

        player = self.mass.players.get(player_id)
        # make sure that the (master) player is powered
        # powering any client players must be done in other ways
        if not player.synced_to:
            await self._socket_clients[player_id].power(True)

        # forward command to player and any connected sync child's
        for client in self._get_sync_clients(player_id):
            await self._handle_play_media(
                client,
                queue_item=queue_item,
                seek_position=seek_position,
                fade_in=fade_in,
                send_flush=True,
                flow_mode=flow_mode,
            )

    async def cmd_pause(self, player_id: str) -> None:
        """Send PAUSE command to given player."""
        async with asyncio.TaskGroup() as tg:
            for member in self._get_active_members(only_powered=True):
                tg.create_task(self.mass.players.pause(member.player_id))

    async def cmd_power(self, player_id: str, powered: bool) -> None:
        """Send POWER command to given player."""
        # TODO: define strategy for power on
        async with asyncio.TaskGroup() as tg:
            for member in self._get_active_members(
                only_powered=not powered, skip_sync_childs=False
            ):
                tg.create_task(self.mass.players.cmd_power(member.player_id))

    async def cmd_volume_set(self, player_id: str, volume_level: int) -> None:
        """Send VOLUME_SET command to given player."""

    async def cmd_volume_mute(self, player_id: str, muted: bool) -> None:
        """Send VOLUME MUTE command to given player."""

    def _get_active_members(
        self, only_powered: bool = False, skip_sync_childs: bool = True
    ) -> list[Player]:
        """Get (child) players attached to a grouped player."""
        child_players: list[Player] = []
        conf_members: list[str] = self.config.get_value(CONF_GROUP_MEMBERS)
        for child_id in conf_members:
            if child_player := self.mass.players.get(child_id, False):
                if not (not only_powered or child_player.powered):
                    continue
                if child_player.synced_to and skip_sync_childs:
                    continue
                child_players.append(child_player)
        return child_players
