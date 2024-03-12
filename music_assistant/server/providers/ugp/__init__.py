"""
Universal Group Player provider.

This is more like a "virtual" player provider,
allowing the user to create player groups from all players known in the system.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import shortuuid

from music_assistant.common.models.config_entries import (
    CONF_ENTRY_CROSSFADE_DURATION,
    ConfigEntry,
    ConfigValueOption,
    ConfigValueType,
)
from music_assistant.common.models.enums import (
    ConfigEntryType,
    PlayerFeature,
    PlayerState,
    PlayerType,
    ProviderFeature,
)
from music_assistant.common.models.player import DeviceInfo, Player
from music_assistant.constants import CONF_CROSSFADE, CONF_GROUP_MEMBERS, SYNCGROUP_PREFIX
from music_assistant.server.models.player_provider import PlayerProvider

if TYPE_CHECKING:
    from collections.abc import Iterable

    from music_assistant.common.models.config_entries import ProviderConfig
    from music_assistant.common.models.provider import ProviderManifest
    from music_assistant.common.models.queue_item import QueueItem
    from music_assistant.server import MusicAssistant
    from music_assistant.server.models import ProviderInstanceType

UGP_PREFIX = "ugp_"


# ruff: noqa: ARG002


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return UniversalGroupProvider(mass, manifest, config)


async def get_config_entries(
    mass: MusicAssistant,  # noqa: ARG001
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    instance_id: id of an existing provider instance (None if new instance setup).
    action: [optional] action key called from config entries UI.
    values: the (intermediate) raw values for config entries sent with the action.
    """
    return ()


class UniversalGroupProvider(PlayerProvider):
    """Base/builtin provider for universally grouping players."""

    prev_sync_leaders: dict[str, tuple[str]] | None = None
    debounce_id: str | None = None

    @property
    def supported_features(self) -> tuple[ProviderFeature, ...]:
        """Return the features supported by this Provider."""
        return (ProviderFeature.PLAYER_GROUP_CREATE,)

    def __init__(
        self, mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
    ) -> None:
        """Initialize MusicProvider."""
        super().__init__(mass, manifest, config)
        self.prev_sync_leaders = {}

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        await self._register_all_players()

    async def get_player_config_entries(self, player_id: str) -> tuple[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the given player (if any)."""
        base_entries = await super().get_player_config_entries(player_id)
        return (
            *base_entries,
            ConfigEntry(
                key=CONF_GROUP_MEMBERS,
                type=ConfigEntryType.STRING,
                label="Group members",
                default_value=[],
                options=tuple(
                    ConfigValueOption(x.display_name, x.player_id)
                    for x in self.mass.players.all(True, False)
                    if x.player_id != player_id
                ),
                description="Select all players you want to be part of this universal group",
                multi_value=True,
                required=True,
            ),
            ConfigEntry(
                key="ugp_note",
                type=ConfigEntryType.LABEL,
                label="Please note that although the universal group "
                "allows you to group any player, it will not enable audio sync "
                "between players of different ecosystems.",
            ),
            ConfigEntry(
                key=CONF_CROSSFADE,
                type=ConfigEntryType.BOOLEAN,
                label="Enable crossfade",
                default_value=False,
                description="Enable a crossfade transition between (queue) tracks. \n\n"
                "Note that DLNA does not natively support crossfading so you need to enable "
                "the 'flow mode' workaround to use crossfading with DLNA players.",
                advanced=False,
            ),
            CONF_ENTRY_CROSSFADE_DURATION,
        )

    async def cmd_stop(self, player_id: str) -> None:
        """Send STOP command to given player."""
        group_player = self.mass.players.get(player_id)
        group_player.state = PlayerState.IDLE
        # forward command to player and any connected sync child's
        async with asyncio.TaskGroup() as tg:
            for member in self.mass.players.iter_group_members(group_player, only_powered=True):
                if member.state == PlayerState.IDLE:
                    continue
                tg.create_task(self.mass.players.cmd_stop(member.player_id))

    async def cmd_play(self, player_id: str) -> None:
        """Send PLAY command to given player."""

    async def cmd_pause(self, player_id: str) -> None:
        """Send PAUSE command to given player."""

    async def cmd_power(self, player_id: str, powered: bool) -> None:
        """Send POWER command to given player."""
        await self.mass.players.cmd_group_power(player_id, powered)

    async def cmd_volume_set(self, player_id: str, volume_level: int) -> None:
        """Send VOLUME_SET command to given player."""
        # group volume is already handled in the player manager

    async def cmd_volume_mute(self, player_id: str, muted: bool) -> None:
        """Send VOLUME MUTE command to given player."""

    async def play_media(
        self,
        player_id: str,
        queue_item: QueueItem,
        seek_position: int,
        fade_in: bool,
    ) -> None:
        """Handle PLAY MEDIA on given player.

        This is called by the Queue controller to start playing a queue item on the given player.
        The provider's own implementation should work out how to handle this request.

            - player_id: player_id of the player to handle the command.
            - queue_item: The QueueItem that needs to be played on the player.
            - seek_position: Optional seek to this position.
            - fade_in: Optionally fade in the item at playback start.
        """
        # power ON
        await self.cmd_power(player_id, True)
        group_player = self.mass.players.get(player_id)

        # create multi-client stream job
        stream_job = await self.mass.streams.create_multi_client_stream_job(
            player_id,
            start_queue_item=queue_item,
            seek_position=seek_position,
            fade_in=fade_in,
        )

        # forward the stream job to all group members
        async with asyncio.TaskGroup() as tg:
            for member in self.mass.players.iter_group_members(group_player, only_powered=True):
                player_prov = self.mass.players.get_player_provider(member.player_id)
                if member.player_id.startswith(SYNCGROUP_PREFIX):
                    member = self.mass.players.get_sync_leader(member)  # noqa: PLW2901
                    if member is None:
                        continue
                tg.create_task(player_prov.play_stream(member.player_id, stream_job))

    async def poll_player(self, player_id: str) -> None:
        """Poll player for state updates."""
        self.update_attributes(player_id)
        self.mass.players.update(player_id, skip_forward=True)

    async def create_group(self, name: str, members: list[str]) -> Player:
        """Create new PlayerGroup on this provider.

        Create a new PlayerGroup with given name and members.

            - name: Name for the new group to create.
            - members: A list of player_id's that should be part of this group.
        """
        new_group_id = f"{UGP_PREFIX}{shortuuid.random(8).lower()}"
        # cleanup list, filter groups (should be handled by frontend, but just in case)
        members = [
            x.player_id
            for x in self.mass.players
            if x.player_id in members
            if x.provider != self.instance_id
        ]
        # create default config with the user chosen name
        self.mass.config.create_default_player_config(
            new_group_id,
            self.instance_id,
            name=name,
            enabled=True,
            values={CONF_GROUP_MEMBERS: members},
        )
        return self._register_group_player(new_group_id, name=name, members=members)

    async def _register_all_players(self) -> None:
        """Register all (virtual/fake) group players in the Player controller."""
        player_configs = await self.mass.config.get_player_configs(
            self.instance_id, include_values=True
        )
        for player_config in player_configs:
            members = player_config.get_value(CONF_GROUP_MEMBERS)
            self._register_group_player(
                player_config.player_id,
                player_config.name or player_config.default_name,
                members,
            )

    def _register_group_player(
        self, group_player_id: str, name: str, members: Iterable[str]
    ) -> Player:
        """Register a UGP group player in the Player controller."""
        player = Player(
            player_id=group_player_id,
            provider=self.instance_id,
            type=PlayerType.SYNC_GROUP,
            name=name,
            available=True,
            powered=False,
            device_info=DeviceInfo(model="Group", manufacturer=self.name),
            supported_features=(PlayerFeature.VOLUME_SET, PlayerFeature.POWER),
            group_childs=set(members),
        )
        self.mass.players.register_or_update(player)
        return player

    def update_attributes(self, player_id: str) -> None:
        """Update player attributes."""
        group_player = self.mass.players.get(player_id)
        if not group_player.powered:
            group_player.state = PlayerState.IDLE
            return

        # read the state from the first active group member
        for member in self.mass.players.iter_group_members(group_player, only_powered=True):
            group_player.current_item_id = member.current_item_id
            group_player.elapsed_time = member.elapsed_time
            group_player.elapsed_time_last_updated = member.elapsed_time_last_updated
            group_player.state = member.state
            break

    def on_child_power(self, player_id: str, child_player_id: str, new_power: bool) -> None:
        """
        Call when a power command was executed on one of the child player of a PlayerGroup.

        This is used to handle special actions such as (re)syncing.
        """
        group_player = self.mass.players.get(player_id)
        child_player = self.mass.players.get(child_player_id)

        if not group_player.powered:
            # guard, this should be caught in the player controller but just in case...
            return None

        powered_childs = [
            x
            for x in self.mass.players.iter_group_members(group_player, True)
            if not (not new_power and x.player_id == child_player_id)
        ]
        if new_power and child_player not in powered_childs:
            powered_childs.append(child_player)

        # if the last player of a group turned off, turn off the group
        if len(powered_childs) == 0:
            self.logger.debug(
                "Group %s has no more powered members, turning off group player",
                group_player.display_name,
            )
            self.mass.create_task(self.cmd_power(player_id, False))
            return False

        # if a child player turned ON while the group player is already playing
        # we need to resync/resume
        if new_power and group_player.state == PlayerState.PLAYING:
            self.logger.warning(
                "Player %s turned on while syncgroup is playing, "
                "a forced resume for %s will be performed...",
                child_player.display_name,
                group_player.display_name,
            )
            self.mass.loop.call_later(
                1,
                self.mass.create_task,
                self.mass.player_queues.resume(group_player.player_id),
            )
            return None
        return None
