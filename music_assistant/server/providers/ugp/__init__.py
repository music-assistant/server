"""
Universal Group Player provider.

This is more like a "virtual" player provider,
allowing the user to create player groups from all players known in the system.
"""
from __future__ import annotations

import asyncio
from collections.abc import Iterable, Iterator
from typing import TYPE_CHECKING

import shortuuid

from music_assistant.common.models.config_entries import (
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
from music_assistant.common.models.queue_item import QueueItem
from music_assistant.constants import CONF_GROUP_MEMBERS
from music_assistant.server.models.player_provider import PlayerProvider

if TYPE_CHECKING:
    from music_assistant.common.models.config_entries import ProviderConfig
    from music_assistant.common.models.provider import ProviderManifest
    from music_assistant.server import MusicAssistant
    from music_assistant.server.models import ProviderInstanceType

UGP_PREFIX = "ugp_"


# ruff: noqa: ARG002


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    prov = UniversalGroupProvider(mass, manifest, config)
    await prov.handle_setup()
    return prov


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
    return tuple()


class UniversalGroupProvider(PlayerProvider):
    """Base/builtin provider for universally grouping players."""

    prev_sync_leaders: dict[str, tuple[str]] | None = None
    debounce_id: str | None = None

    @property
    def supported_features(self) -> tuple[ProviderFeature, ...]:
        """Return the features supported by this Provider."""
        return (ProviderFeature.PLAYER_GROUP_CREATE,)

    async def handle_setup(self) -> None:
        """Handle async initialization of the provider."""
        self.prev_sync_leaders = {}
        self.mass.loop.create_task(self.register_group_players())

    async def get_player_config_entries(self, player_id: str) -> tuple[ConfigEntry]:  # noqa: ARG002
        """Return all (provider/player specific) Config Entries for the given player (if any)."""
        base_entries = await super().get_player_config_entries(player_id)
        return base_entries + (
            ConfigEntry(
                key=CONF_GROUP_MEMBERS,
                type=ConfigEntryType.STRING,
                label="Group members",
                default_value=[],
                options=tuple(
                    ConfigValueOption(x.display_name, x.player_id)
                    for x in self.mass.players.all(True, False)
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
        )

    async def cmd_stop(self, player_id: str) -> None:
        """Send STOP command to given player."""
        group_player = self.mass.players.get(player_id)
        group_player.state = PlayerState.IDLE
        # forward command to player and any connected sync child's
        async with asyncio.TaskGroup() as tg:
            for member in self._get_active_members(
                group_player, only_powered=True, skip_sync_childs=True
            ):
                if member.state == PlayerState.IDLE:
                    continue
                tg.create_task(self.mass.players.cmd_stop(member.player_id))

    async def cmd_play(self, player_id: str) -> None:
        """Send PLAY command to given player."""
        group_player = self.mass.players.get(player_id)
        group_player.state = PlayerState.PLAYING
        async with asyncio.TaskGroup() as tg:
            for member in self._get_active_members(
                group_player, only_powered=False, skip_sync_childs=True
            ):
                tg.create_task(self.mass.players.cmd_play(member.player_id))

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

        # forward the command to all (sync master) group child's
        members_powered = False
        async with asyncio.TaskGroup() as tg:
            for member in self._get_active_members(
                group_player, only_powered=True, skip_sync_childs=True
            ):
                player_prov = self.mass.players.get_player_provider(member.player_id)
                members_powered = True
                tg.create_task(
                    player_prov.play_media(
                        member.player_id,
                        queue_item=queue_item,
                        seek_position=seek_position,
                        fade_in=fade_in,
                    )
                )
        if members_powered:
            # set state optimistically
            group_player.state = PlayerState.PLAYING
        else:
            self.logger.warning(
                "Play media requested for player %s but no member players are powered!",
                group_player.display_name,
            )

    async def enqueue_next_queue_item(self, player_id: str, queue_item: QueueItem):
        """
        Handle enqueuing of the next queue item on the player.

        If the player supports PlayerFeature.ENQUE_NEXT:
          This will be called about 10 seconds before the end of the track.
        If the player does NOT report support for PlayerFeature.ENQUE_NEXT:
          This will be called when the end of the track is reached.

        A PlayerProvider implementation is in itself responsible for handling this
        so that the queue items keep playing until its empty or the player stopped.

        This will NOT be called if the end of the queue is reached (and repeat disabled).
        This will NOT be called if the player is using flow mode to playback the queue.
        """
        group_player = self.mass.players.get(player_id)
        # forward the command to all (sync master) group child's
        async with asyncio.TaskGroup() as tg:
            for member in self._get_active_members(
                group_player, only_powered=False, skip_sync_childs=True
            ):
                player_prov = self.mass.players.get_player_provider(member.player_id)
                tg.create_task(player_prov.enqueue_next_queue_item(member.player_id, queue_item))

    async def cmd_pause(self, player_id: str) -> None:
        """Send PAUSE command to given player."""
        group_player = self.mass.players.get(player_id)
        group_player.state = PlayerState.PAUSED
        async with asyncio.TaskGroup() as tg:
            for member in self._get_active_members(
                group_player, only_powered=True, skip_sync_childs=True
            ):
                tg.create_task(self.mass.players.cmd_pause(member.player_id))

    async def cmd_power(self, player_id: str, powered: bool) -> None:
        """Send POWER command to given player."""
        await self.mass.players.cmd_group_power(player_id, powered)
        if powered:
            # sync all players on power on
            await self._sync_players(player_id)

    async def cmd_volume_set(self, player_id: str, volume_level: int) -> None:
        """Send VOLUME_SET command to given player."""
        # group volume is already handled in the player manager

    async def cmd_volume_mute(self, player_id: str, muted: bool) -> None:
        """Send VOLUME MUTE command to given player."""

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
        # create default config with the user chosen name
        self.mass.config.create_default_player_config(
            new_group_id,
            self.instance_id,
            name=name,
            enabled=True,
            values={CONF_GROUP_MEMBERS: members},
        )
        player = self._register_group_player(new_group_id, name=name, members=members)
        return player

    async def register_group_players(self) -> None:
        """Register all (virtual/fake) group players in the Player controller."""
        player_configs = await self.mass.config.get_player_configs(self.instance_id)
        for player_config in player_configs:
            members = player_config.get_value(CONF_GROUP_MEMBERS)
            self._register_group_player(
                player_config.player_id, player_config.name or player_config.default_name, members
            )

    def _register_group_player(
        self, group_player_id: str, name: str, members: Iterable[str]
    ) -> Player:
        """Register a UGP group player in the Player controller."""
        # extract player features from first/random player
        # TODO: should we gather only the features that exist on ALL child players?
        for member in members:
            if first_player := self.mass.players.get(member):
                supported_features = first_player.supported_features
                break
        else:
            # edge case: no child player is (yet) available; use safe default feature set
            supported_features = (PlayerFeature.VOLUME_SET, PlayerFeature.POWER)
        player = Player(
            player_id=group_player_id,
            provider=self.instance_id,
            type=PlayerType.SYNC_GROUP,
            name=name,
            available=True,
            powered=False,
            device_info=DeviceInfo(model="Group", manufacturer=self.name),
            supported_features=supported_features,
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
        for member in self._get_active_members(
            group_player, only_powered=False, skip_sync_childs=False
        ):
            if member.synced_to:
                continue
            player_powered = member.powered
            if not player_powered:
                continue
            group_player.current_item_id = member.current_item_id
            group_player.elapsed_time = member.elapsed_time
            group_player.elapsed_time_last_updated = member.elapsed_time_last_updated
            group_player.state = member.state
            break

    async def on_child_power(self, player_id: str, child_player_id: str, new_power: bool) -> None:
        """
        Call when a power command was executed on one of the child player of a Player/Sync group.

        This is used to handle special actions such as (re)syncing.
        """
        group_player = self.mass.players.get(player_id)
        child_player = self.mass.players.get(child_player_id)

        if not group_player.powered:
            # guard, this should be caught in the player controller but just in case...
            return

        powered_childs = [x for x in self._get_active_members(group_player, True, False)]
        if not new_power and child_player in powered_childs:
            powered_childs.remove(child_player)

        # if the last player of a group turned off, turn off the group
        if len(powered_childs) == 0:
            self.logger.debug(
                "Group %s has no more powered members, turning off group player", player_id
            )
            self.mass.create_task(self.cmd_power(player_id, False))
            return False

        group_playing = group_player.state == PlayerState.PLAYING
        # if a child player turned ON while the group player is already playing
        # we need to resync/resume
        if new_power and group_playing:
            if sync_leader := next(
                (x for x in child_player.can_sync_with if x in self.prev_sync_leaders[player_id]),
                None,
            ):
                # prevent resume when player platform supports sync
                # and one of its players is already playing
                self.logger.debug(
                    "Groupplayer %s forced resync due to groupmember change", player_id
                )
                self.mass.create_task(
                    self.mass.players.cmd_sync(child_player.player_id, sync_leader)
                )
            else:
                self.logger.debug(
                    "Groupplayer %s forced resume due to groupmember change", player_id
                )
                self.mass.create_task(self.mass.player_queues.resume(group_player.player_id))
        elif (
            not new_power
            and group_playing
            and child_player.player_id in self.prev_sync_leaders[player_id]
        ):
            # a sync master player turned OFF while the group player
            # should still be playing - we need to resync/resume
            self.logger.debug("Groupplayer %s forced resume due to groupmember change", player_id)
            self.mass.create_task(self.mass.player_queues.resume, group_player.player_id)

    def _get_active_members(
        self,
        group_player: Player,
        only_powered: bool = False,
        skip_sync_childs: bool = True,
    ) -> Iterator[Player]:
        """Get all (child) players attached to this grouped player."""
        for child_id in group_player.group_childs:
            child_player = self.mass.players.get(child_id, False)
            if not child_player or not child_player.available:
                continue
            # work out power state
            if not (not only_powered or child_player.powered):
                continue
            if child_player.synced_to and skip_sync_childs:
                continue
            if child_player.powered and (
                child_player.type == PlayerType.SYNC_GROUP or child_player.provider == self.domain
            ):
                # handle group within a group, unpack members
                for sub_child_id in child_player.group_childs:
                    sub_child_player = self.mass.players.get(sub_child_id, False)
                    if not sub_child_player or not sub_child_player.available:
                        continue
                    if sub_child_player.synced_to and skip_sync_childs:
                        continue
                    yield sub_child_player
                continue
            yield child_player

    async def _sync_players(self, player_id: str) -> None:
        """Sync all (possible) players."""
        sync_leaders = set()
        group_player = self.mass.players.get(player_id)
        for member in self._get_active_members(group_player, only_powered=True):
            if member.synced_to is not None:
                continue
            if not member.can_sync_with:
                continue
            # check if we can join this player to an already chosen sync leader
            if existing_leader := next(
                (x for x in member.can_sync_with if x in sync_leaders), None
            ):
                await self.mass.players.cmd_sync(member.player_id, existing_leader)
                # set optimistic state to prevent race condition in play media
                member.synced_to = existing_leader
                continue
            # pick this member as new sync leader
            sync_leaders.add(member.player_id)
        self.prev_sync_leaders[player_id] = tuple(sync_leaders)
