"""
Universal Group Player provider.

This is more like a "virtual" player provider,
allowing the user to create player groups from all players known in the system.
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from music_assistant.common.models.config_entries import (
    CONF_ENTRY_EQ_BASS,
    CONF_ENTRY_EQ_MID,
    CONF_ENTRY_EQ_TREBLE,
    CONF_ENTRY_FLOW_MODE,
    CONF_ENTRY_HIDE_GROUP_MEMBERS,
    CONF_ENTRY_OUTPUT_CHANNELS,
    CONF_ENTRY_OUTPUT_CODEC,
    ConfigEntry,
    ConfigValueOption,
    ConfigValueType,
)
from music_assistant.common.models.enums import (
    ConfigEntryType,
    PlayerFeature,
    PlayerState,
    PlayerType,
)
from music_assistant.common.models.player import DeviceInfo, Player
from music_assistant.common.models.queue_item import QueueItem
from music_assistant.constants import CONF_GROUPED_POWER_ON
from music_assistant.server.models.player_provider import PlayerProvider

if TYPE_CHECKING:
    from music_assistant.common.models.config_entries import ProviderConfig
    from music_assistant.common.models.provider import ProviderManifest
    from music_assistant.server import MusicAssistant
    from music_assistant.server.controllers.streams import MultiClientStreamJob
    from music_assistant.server.models import ProviderInstanceType


CONF_GROUP_MEMBERS = "group_members"
CONF_MUTE_CHILDS = "mute_childs"

CONF_ENTRY_OUTPUT_CHANNELS_FORCED_STEREO = ConfigEntry.from_dict(
    {
        **CONF_ENTRY_OUTPUT_CHANNELS.to_dict(),
        "hidden": True,
        "default_value": "stereo",
        "value": "stereo",
    }
)
CONF_ENTRY_FORCED_FLOW_MODE = ConfigEntry.from_dict(
    {**CONF_ENTRY_FLOW_MODE.to_dict(), "default_value": True, "value": True, "hidden": True}
)
CONF_ENTRY_EQ_BASS_HIDDEN = ConfigEntry.from_dict({**CONF_ENTRY_EQ_BASS.to_dict(), "hidden": True})
CONF_ENTRY_EQ_MID_HIDDEN = ConfigEntry.from_dict({**CONF_ENTRY_EQ_MID.to_dict(), "hidden": True})
CONF_ENTRY_EQ_TREBLE_HIDDEN = ConfigEntry.from_dict(
    {**CONF_ENTRY_EQ_TREBLE.to_dict(), "hidden": True}
)
CONF_ENTRY_OUTPUT_CODEC_HIDDEN = ConfigEntry.from_dict(
    {**CONF_ENTRY_OUTPUT_CODEC.to_dict(), "hidden": True}
)
CONF_ENTRY_GROUPED_POWER_ON = ConfigEntry(
    key=CONF_GROUPED_POWER_ON,
    type=ConfigEntryType.BOOLEAN,
    default_value=False,
    label="Forced Power ON of all group members",
    description="Power ON all child players when the group player is powered on "
    "(or playback started). \n"
    "If this setting is disabled, playback will only start on players that "
    "are already powered ON at the time of playback start.\n"
    "When turning OFF the group player, all group members are turned off, "
    "regardless of this setting.",
    advanced=False,
)
# ruff: noqa: ARG002


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    prov = UniversalGroupProvider(mass, manifest, config)
    await prov.handle_setup()
    return prov


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    instance_id: id of an existing provider instance (None if new instance setup).
    action: [optional] action key called from config entries UI.
    values: the (intermediate) raw values for config entries sent with the action.
    """
    # ruff: noqa: ARG001
    # dynamically extend the amount of entries when needed
    if values.get("ugp_15"):
        player_count = 20
    elif values.get("ugp_10"):
        player_count = 15
    elif values.get("ugp_5"):
        player_count = 10
    else:
        player_count = 5
    player_entries = tuple(
        ConfigEntry(
            key=f"ugp_{index}",
            type=ConfigEntryType.STRING,
            label=f"Group player {index}: Group members",
            default_value=[],
            options=tuple(
                ConfigValueOption(x.display_name, x.player_id)
                for x in mass.players.all(True, True, False)
                if x.player_id != f"ugp_{index}"
            ),
            description="Select all players you want to be part of this group",
            multi_value=True,
            required=False,
        )
        for index in range(1, player_count + 1)
    )
    return player_entries


class UniversalGroupProvider(PlayerProvider):
    """Base/builtin provider for universally grouping players."""

    prev_sync_leaders: dict[str, tuple[str]] | None = None

    async def handle_setup(self) -> None:
        """Handle async initialization of the provider."""
        self.prev_sync_leaders = {}
        self.muted_clients = set()

        for index in range(1, 100):
            conf_key = f"ugp_{index}"
            try:
                player_conf = self.config.get_value(conf_key)
            except KeyError:
                break
            if player_conf == []:
                # cleanup player config if player config is removed/reset
                self.mass.players.remove(conf_key)
                continue
            elif not player_conf:
                continue

            player = Player(
                player_id=conf_key,
                provider=self.domain,
                type=PlayerType.GROUP,
                name=f"{self.name}: {index}",
                available=True,
                powered=False,
                device_info=DeviceInfo(model=self.manifest.name, manufacturer="Music Assistant"),
                # TODO: derive playerfeatures from (all) underlying child players?
                supported_features=(
                    PlayerFeature.POWER,
                    PlayerFeature.PAUSE,
                    PlayerFeature.VOLUME_SET,
                    PlayerFeature.VOLUME_MUTE,
                    PlayerFeature.SET_MEMBERS,
                ),
                active_source=conf_key,
                group_childs=player_conf,
            )
            player.extra_data["optimistic_state"] = PlayerState.IDLE
            self.prev_sync_leaders[conf_key] = None
            self.mass.players.register_or_update(player)

    async def get_player_config_entries(self, player_id: str) -> tuple[ConfigEntry]:  # noqa: ARG002
        """Return all (provider/player specific) Config Entries for the given player (if any)."""
        return (
            CONF_ENTRY_HIDE_GROUP_MEMBERS,
            CONF_ENTRY_GROUPED_POWER_ON,
            ConfigEntry(
                key=CONF_MUTE_CHILDS,
                type=ConfigEntryType.STRING,
                label="Use muting for power commands",
                multi_value=True,
                options=(
                    ConfigValueOption(x.display_name, x.player_id)
                    for x in self._get_active_members(player_id, False, False)
                ),
                default_value=[],
                description="To prevent a restart of the stream, when a child player "
                "turns on while the group is already playing, you can enable a workaround "
                "where Music Assistant uses muting to control the group players. \n\n"
                "This means that while the group player is playing, power actions to these "
                "child players will be treated as (un)mute commands to prevent the small "
                "interruption of music when the stream is restarted.",
            ),
            CONF_ENTRY_OUTPUT_CHANNELS_FORCED_STEREO,
            CONF_ENTRY_FORCED_FLOW_MODE,
            # group player outputs to individual members so
            # these settings make no sense, hide them
            CONF_ENTRY_EQ_BASS_HIDDEN,
            CONF_ENTRY_EQ_MID_HIDDEN,
            CONF_ENTRY_EQ_TREBLE_HIDDEN,
            CONF_ENTRY_OUTPUT_CODEC_HIDDEN,
        )

    async def cmd_stop(self, player_id: str) -> None:
        """Send STOP command to given player."""
        group_player = self.mass.players.get(player_id)
        group_player.extra_data["optimistic_state"] = PlayerState.IDLE
        # forward command to player and any connected sync child's
        async with asyncio.TaskGroup() as tg:
            for member in self._get_active_members(
                player_id, only_powered=True, skip_sync_childs=True
            ):
                if member.state == PlayerState.IDLE:
                    continue
                tg.create_task(self.mass.players.cmd_stop(member.player_id))

    async def cmd_play(self, player_id: str) -> None:
        """Send PLAY command to given player."""
        group_player = self.mass.players.get(player_id)
        group_player.extra_data["optimistic_state"] = PlayerState.PLAYING
        async with asyncio.TaskGroup() as tg:
            for member in self._get_active_members(
                player_id, only_powered=False, skip_sync_childs=True
            ):
                tg.create_task(self.mass.players.cmd_play(member.player_id))

    async def cmd_play_url(
        self,
        player_id: str,
        url: str,
        queue_item: QueueItem | None,
    ) -> None:
        """Send PLAY URL command to given player.

        This is called when the Queue wants the player to start playing a specific url.
        If an item from the Queue is being played, the QueueItem will be provided with
        all metadata present.

            - player_id: player_id of the player to handle the command.
            - url: the url that the player should start playing.
            - queue_item: the QueueItem that is related to the URL (None when playing direct url).
        """
        # send stop first
        await self.cmd_stop(player_id)
        # power ON
        await self.cmd_power(player_id, True)
        group_player = self.mass.players.get(player_id)

        active_members = self._get_active_members(
            player_id, only_powered=True, skip_sync_childs=True
        )
        if len(active_members) == 0:
            self.logger.warning(
                "Play media requested for player %s but no member players are powered, "
                "the request will be ignored",
                group_player.display_name,
            )
            return

        group_player.extra_data["optimistic_state"] = PlayerState.PLAYING

        # forward command to all (powered) group child's
        async with asyncio.TaskGroup() as tg:
            for member in active_members:
                player_prov = self.mass.players.get_player_provider(member.player_id)
                tg.create_task(
                    player_prov.cmd_play_url(member.player_id, url=url, queue_item=queue_item)
                )

    async def cmd_handle_stream_job(self, player_id: str, stream_job: MultiClientStreamJob) -> None:
        """Handle StreamJob play command on given player."""
        #  send stop first
        await self.cmd_stop(player_id)
        # power ON
        await self.cmd_power(player_id, True)
        group_player = self.mass.players.get(player_id)

        active_members = self._get_active_members(
            player_id, only_powered=True, skip_sync_childs=True
        )
        if len(active_members) == 0:
            self.logger.warning(
                "Play media requested for player %s but no member players are powered, "
                "the request will be ignored",
                group_player.display_name,
            )
            return

        group_player.extra_data["optimistic_state"] = PlayerState.PLAYING

        # forward command to all (powered) group child's
        async with asyncio.TaskGroup() as tg:
            for member in active_members:
                player_prov = self.mass.players.get_player_provider(member.player_id)
                # we forward the stream_job to child to allow for nested groups etc
                tg.create_task(
                    player_prov.cmd_handle_stream_job(member.player_id, stream_job=stream_job)
                )

    async def cmd_pause(self, player_id: str) -> None:
        """Send PAUSE command to given player."""
        group_player = self.mass.players.get(player_id)
        group_player.extra_data["optimistic_state"] = PlayerState.PAUSED
        async with asyncio.TaskGroup() as tg:
            for member in self._get_active_members(
                player_id, only_powered=True, skip_sync_childs=True
            ):
                tg.create_task(self.mass.players.cmd_pause(member.player_id))

    async def cmd_power(self, player_id: str, powered: bool) -> None:
        """Send POWER command to given player."""
        group_power_on = await self.mass.config.get_player_config_value(
            player_id, CONF_GROUPED_POWER_ON
        )
        mute_childs = self.mass.config.get_raw_player_config_value(player_id, CONF_MUTE_CHILDS, [])
        group_player = self.mass.players.get(player_id)

        async def set_child_power(child_player: Player) -> None:
            # do not turn on the player if not explicitly requested
            # so either the group player turns off OR
            # it turns ON and we have the group_power_on config option enabled
            if not (not powered or group_power_on):
                return
            # make sure to disable the mute as power workaround,
            # otherwise the player keeps on playing "invisible"
            if not powered and child_player.player_id in mute_childs:
                child_player.mute_as_power = False
                if child_player.volume_muted:
                    await self.mass.players.cmd_volume_mute(child_player.player_id, False)
            # send actual power command to child player
            await self.mass.players.cmd_power(child_player.player_id, powered)

            # set optimistic state on child player to prevent race conditions in other actions
            child_player.powered = powered

        # turn on/off child players if needed
        async with asyncio.TaskGroup() as tg:
            for member in self._get_active_members(
                player_id, only_powered=False, skip_sync_childs=False
            ):
                tg.create_task(set_child_power(member))

        # (re)set mute_as_power feature for group members
        for child_player_id in mute_childs:
            if child_player := self.mass.players.get(child_player_id):
                child_player.mute_as_power = powered

        group_player.powered = powered
        if not powered:
            group_player.extra_data["optimistic_state"] = PlayerState.IDLE
        self.mass.players.update(player_id)
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

    def update_attributes(self, player_id: str) -> None:
        """Update player attributes."""
        group_player = self.mass.players.get(player_id)
        if not group_player.powered:
            group_player.state = PlayerState.IDLE
            return

        all_members = self._get_active_members(
            player_id, only_powered=False, skip_sync_childs=False
        )
        group_player.group_childs = list(x.player_id for x in all_members)
        # read the state from the first active group member
        for member in all_members:
            if member.synced_to:
                continue
            if member.mute_as_power:
                player_powered = member.powered and not member.volume_muted
            else:
                player_powered = member.powered
            if not player_powered:
                continue
            group_player.current_url = member.current_url
            group_player.elapsed_time = member.elapsed_time
            group_player.elapsed_time_last_updated = member.elapsed_time_last_updated
            group_player.state = member.state
            break

    async def on_child_power(self, player_id: str, child_player: Player, new_power: bool) -> None:
        """
        Call when a power command was executed on one of the child players.

        This is used to handle special actions such as muting as power or (re)syncing.
        """
        group_player = self.mass.players.get(player_id)

        if not group_player.powered:
            # guard, this should be caught in the player controller but just in case...
            return

        powered_childs = self._get_active_members(player_id, True, False)
        if not new_power and child_player in powered_childs:
            powered_childs.remove(child_player)

        # if the last player of a group turned off, turn off the group
        if len(powered_childs) == 0:
            self.mass.create_task(self.cmd_power, player_id, False)
            return False

        # if a child player turned ON while the group player is already playing
        # we need to resync/resume
        if (
            group_player.powered
            and new_power
            and group_player.extra_data["optimistic_state"] == PlayerState.PLAYING
            and child_player.state != PlayerState.PLAYING
        ):
            if group_player.state == PlayerState.PLAYING and (
                sync_leader := next(
                    (
                        x
                        for x in child_player.can_sync_with
                        if x in self.prev_sync_leaders[player_id]
                    ),
                    None,
                )
            ):
                # prevent resume when player platform supports sync
                # and one of its players is already playing
                self.mass.create_task(
                    self.mass.players.cmd_sync, child_player.player_id, sync_leader
                )
            else:
                # send atcive source because the group may be within another group
                self.mass.create_task(self.mass.player_queues.resume, group_player.active_source)
        elif (
            not new_power
            and group_player.extra_data["optimistic_state"] == PlayerState.PLAYING
            and child_player.player_id in self.prev_sync_leaders[player_id]
            and not child_player.mute_as_power
        ):
            # a sync master player turned OFF while the group player
            # should still be playing - we need to resync/resume
            # send atcive source because the group may be within another group
            self.mass.create_task(self.mass.player_queues.resume, group_player.active_source)

    def _get_active_members(
        self,
        player_id: str,
        only_powered: bool = False,
        skip_sync_childs: bool = True,
    ) -> list[Player]:
        """Get (child) players attached to a grouped player."""
        child_players: list[Player] = []
        conf_members: list[str] = self.config.get_value(player_id)
        ignore_ids = set()
        group_player = self.mass.players.get(player_id)
        parent_source = group_player.active_source
        for child_id in conf_members:
            if child_player := self.mass.players.get(child_id, False):
                # work out power state
                if child_player.mute_as_power:
                    player_powered = child_player.powered and not child_player.volume_muted
                else:
                    player_powered = child_player.powered
                if not (not only_powered or player_powered):
                    continue
                if child_player.synced_to and skip_sync_childs:
                    continue
                allowed_sources = [child_player.player_id, player_id, parent_source] + conf_members
                if child_player.active_source not in allowed_sources:
                    # edge case: the child player has another group already active!
                    continue
                if child_player.synced_to and child_player.synced_to not in allowed_sources:
                    # edge case: the child player is already synced to another player
                    continue
                child_players.append(child_player)
                # handle edge case where a group is in the group and both the group
                # and (one of its) child's are added to this universal group.
                if child_player.type == PlayerType.GROUP:
                    ignore_ids.update(
                        x for x in child_player.group_childs if x != child_player.player_id
                    )
        return [x for x in child_players if x.player_id not in ignore_ids]

    async def _sync_players(self, player_id: str) -> None:
        """Sync all (possible) players."""
        sync_leaders = set()
        # TODO: sort members on sync master priority attribute ?
        for member in self._get_active_members(player_id, only_powered=True):
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
