"""Model/base for a Metadata Provider implementation."""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Iterable

import shortuuid

from music_assistant.common.models.config_entries import (
    CONF_ENTRY_ANNOUNCE_VOLUME,
    CONF_ENTRY_ANNOUNCE_VOLUME_MAX,
    CONF_ENTRY_ANNOUNCE_VOLUME_MIN,
    CONF_ENTRY_ANNOUNCE_VOLUME_STRATEGY,
    CONF_ENTRY_AUTO_PLAY,
    CONF_ENTRY_FLOW_MODE,
    CONF_ENTRY_HIDE_PLAYER,
    CONF_ENTRY_PLAYER_ICON,
    CONF_ENTRY_PLAYER_ICON_GROUP,
    CONF_ENTRY_SAMPLE_RATES,
    CONF_ENTRY_TTS_PRE_ANNOUNCE,
    CONF_ENTRY_VOLUME_NORMALIZATION,
    CONF_ENTRY_VOLUME_NORMALIZATION_TARGET,
    ConfigEntry,
    ConfigValueOption,
    PlayerConfig,
)
from music_assistant.common.models.enums import (
    ConfigEntryType,
    PlayerFeature,
    PlayerState,
    PlayerType,
    ProviderFeature,
)
from music_assistant.common.models.player import DeviceInfo, Player, PlayerMedia
from music_assistant.constants import CONF_GROUP_MEMBERS, CONF_GROUP_PLAYERS, SYNCGROUP_PREFIX

from .provider import Provider

# ruff: noqa: ARG001, ARG002


class PlayerProvider(Provider):
    """Base representation of a Player Provider (controller).

    Player Provider implementations should inherit from this base model.
    """

    async def get_player_config_entries(self, player_id: str) -> tuple[ConfigEntry, ...]:
        """Return all (provider/player specific) Config Entries for the given player (if any)."""
        entries = (
            CONF_ENTRY_PLAYER_ICON,
            CONF_ENTRY_FLOW_MODE,
            CONF_ENTRY_VOLUME_NORMALIZATION,
            CONF_ENTRY_AUTO_PLAY,
            CONF_ENTRY_VOLUME_NORMALIZATION_TARGET,
            CONF_ENTRY_HIDE_PLAYER,
            CONF_ENTRY_TTS_PRE_ANNOUNCE,
            CONF_ENTRY_SAMPLE_RATES,
        )
        if player_id.startswith(SYNCGROUP_PREFIX):
            # add default entries for syncgroups
            entries = (
                *entries,
                ConfigEntry(
                    key=CONF_GROUP_MEMBERS,
                    type=ConfigEntryType.STRING,
                    label="Group members",
                    default_value=[],
                    options=tuple(
                        ConfigValueOption(x.display_name, x.player_id)
                        for x in self.mass.players.all(True, False)
                        if x.player_id != player_id and x.provider == self.instance_id
                    ),
                    description="Select all players you want to be part of this group",
                    multi_value=True,
                    required=True,
                ),
                CONF_ENTRY_PLAYER_ICON_GROUP,
            )
        if not player_id.startswith(SYNCGROUP_PREFIX):
            # add default entries for announce feature
            entries = (
                *entries,
                CONF_ENTRY_ANNOUNCE_VOLUME_STRATEGY,
                CONF_ENTRY_ANNOUNCE_VOLUME,
                CONF_ENTRY_ANNOUNCE_VOLUME_MIN,
                CONF_ENTRY_ANNOUNCE_VOLUME_MAX,
            )
        return entries

    def on_player_config_changed(self, config: PlayerConfig, changed_keys: set[str]) -> None:
        """Call (by config manager) when the configuration of a player changes."""
        if f"values/{CONF_GROUP_MEMBERS}" in changed_keys:
            player = self.mass.players.get(config.player_id)
            player.group_childs = config.get_value(CONF_GROUP_MEMBERS)
            self.mass.players.update(config.player_id)

    def on_player_config_removed(self, player_id: str) -> None:
        """Call (by config manager) when the configuration of a player is removed."""
        # ensure that any group players get removed
        group_players = self.mass.config.get_raw_provider_config_value(
            self.instance_id, CONF_GROUP_PLAYERS, {}
        )
        if player_id in group_players:
            del group_players[player_id]
            self.mass.config.set_raw_provider_config_value(
                self.instance_id, CONF_GROUP_PLAYERS, group_players
            )

    @abstractmethod
    async def cmd_stop(self, player_id: str) -> None:
        """Send STOP command to given player.

        - player_id: player_id of the player to handle the command.
        """

    @abstractmethod
    async def cmd_play(self, player_id: str) -> None:
        """Send PLAY (unpause) command to given player.

        - player_id: player_id of the player to handle the command.
        """

    async def cmd_pause(self, player_id: str) -> None:
        """Send PAUSE command to given player.

        - player_id: player_id of the player to handle the command.
        """
        # will only be called for players with Pause feature set.
        raise NotImplementedError

    async def play_media(
        self,
        player_id: str,
        media: PlayerMedia,
    ) -> None:
        """Handle PLAY MEDIA on given player.

        This is called by the Players controller to start playing a mediaitem on the given player.
        The provider's own implementation should work out how to handle this request.

            - player_id: player_id of the player to handle the command.
            - media: Details of the item that needs to be played on the player.
        """
        raise NotImplementedError

    async def enqueue_next_media(self, player_id: str, media: PlayerMedia) -> None:
        """
        Handle enqueuing of the next (queue) item on the player.

        Only called if the player supports PlayerFeature.ENQUE_NEXT.
        Called about 1 second after a new track started playing.
        Called about 15 seconds before the end of the current track.

        A PlayerProvider implementation is in itself responsible for handling this
        so that the queue items keep playing until its empty or the player stopped.

        This will NOT be called if the end of the queue is reached (and repeat disabled).
        This will NOT be called if the player is using flow mode to playback the queue.
        """

    async def play_announcement(
        self, player_id: str, announcement: PlayerMedia, volume_level: int | None = None
    ) -> None:
        """Handle (provider native) playback of an announcement on given player."""
        # will only be called for players with PLAY_ANNOUNCEMENT feature set.
        raise NotImplementedError

    async def cmd_power(self, player_id: str, powered: bool) -> None:
        """Send POWER command to given player.

        - player_id: player_id of the player to handle the command.
        - powered: bool if player should be powered on or off.
        """
        # will only be called for players with Power feature set.
        raise NotImplementedError

    async def cmd_volume_set(self, player_id: str, volume_level: int) -> None:
        """Send VOLUME_SET command to given player.

        - player_id: player_id of the player to handle the command.
        - volume_level: volume level (0..100) to set on the player.
        """
        # will only be called for players with Volume feature set.
        raise NotImplementedError

    async def cmd_volume_mute(self, player_id: str, muted: bool) -> None:
        """Send VOLUME MUTE command to given player.

        - player_id: player_id of the player to handle the command.
        - muted: bool if player should be muted.
        """
        # will only be called for players with Mute feature set.
        raise NotImplementedError

    async def cmd_seek(self, player_id: str, position: int) -> None:
        """Handle SEEK command for given queue.

        - player_id: player_id of the player to handle the command.
        - position: position in seconds to seek to in the current playing item.
        """
        # will only be called for players with Seek feature set.
        raise NotImplementedError

    async def cmd_sync(self, player_id: str, target_player: str) -> None:
        """Handle SYNC command for given player.

        Join/add the given player(id) to the given (master) player/sync group.

            - player_id: player_id of the player to handle the command.
            - target_player: player_id of the syncgroup master or group player.
        """
        # will only be called for players with SYNC feature set.
        raise NotImplementedError

    async def cmd_unsync(self, player_id: str) -> None:
        """Handle UNSYNC command for given player.

        Remove the given player from any syncgroups it currently is synced to.

            - player_id: player_id of the player to handle the command.
        """
        # will only be called for players with SYNC feature set.
        raise NotImplementedError

    async def cmd_sync_many(self, target_player: str, child_player_ids: list[str]) -> None:
        """Create temporary sync group by joining given players to target player."""
        for child_id in child_player_ids:
            # default implementation, simply call the cmd_sync for all child players
            await self.cmd_sync(child_id, target_player)

    async def cmd_unsync_many(self, player_ids: str) -> None:
        """Handle UNSYNC command for all the given players.

        Remove the given player from any syncgroups it currently is synced to.

            - player_id: player_id of the player to handle the command.
        """
        for player_id in player_ids:
            # default implementation, simply call the cmd_sync for all player_ids
            await self.cmd_unsync(player_id)

    async def create_group(self, name: str, members: list[str]) -> Player:
        """Create new PlayerGroup on this provider.

        Create a new SyncGroup (or PlayerGroup) with given name and members.

            - name: Name for the new group to create.
            - members: A list of player_id's that should be part of this group.
        """
        # should only be called for providers with PLAYER_GROUP_CREATE feature set.
        if ProviderFeature.PLAYER_GROUP_CREATE not in self.supported_features:
            raise NotImplementedError
        # default implementation: create syncgroup
        new_group_id = f"{SYNCGROUP_PREFIX}{shortuuid.random(8).lower()}"
        # cleanup list, filter groups (should be handled by frontend, but just in case)
        members = [
            x.player_id
            for x in self.players
            if x.player_id in members
            if not x.player_id.startswith(SYNCGROUP_PREFIX)
            if x.provider == self.instance_id and PlayerFeature.SYNC in x.supported_features
        ]
        # create default config with the user chosen name
        self.mass.config.create_default_player_config(
            new_group_id,
            self.instance_id,
            name=name,
            enabled=True,
            values={CONF_GROUP_MEMBERS: members},
        )
        return self.register_syncgroup(group_player_id=new_group_id, name=name, members=members)

    async def poll_player(self, player_id: str) -> None:
        """Poll player for state updates.

        This is called by the Player Manager;
        if 'needs_poll' is set to True in the player object.
        """

    def on_child_power(self, player_id: str, child_player_id: str, new_power: bool) -> None:
        """
        Call when a power command was executed on one of the child players of a Sync group.

        This is used to handle special actions such as (re)syncing.
        """
        group_player = self.mass.players.get(player_id)
        child_player = self.mass.players.get(child_player_id)

        if not group_player.powered:
            # guard, this should be caught in the player controller but just in case...
            return

        powered_childs = list(self.mass.players.iter_group_members(group_player, True))
        if not new_power and child_player in powered_childs:
            powered_childs.remove(child_player)
        if new_power and child_player not in powered_childs:
            powered_childs.append(child_player)

        # if the last player of a group turned off, turn off the group
        if len(powered_childs) == 0:
            self.logger.debug(
                "Group %s has no more powered members, turning off group player",
                group_player.display_name,
            )
            self.mass.create_task(self.mass.players.cmd_power(player_id, False))
            return

        # the below actions are only suitable for syncgroups
        if ProviderFeature.SYNC_PLAYERS not in self.supported_features:
            return

        group_playing = group_player.state == PlayerState.PLAYING
        is_sync_leader = (
            len(child_player.group_childs) > 0
            and child_player.active_source == group_player.player_id
        )
        if group_playing and not new_power and is_sync_leader:
            # the current sync leader player turned OFF while the group player
            # should still be playing - we need to select a new sync leader and resume
            self.logger.warning(
                "Syncleader %s turned off while syncgroup is playing, "
                "a forced resume for syngroup %s will be attempted in 5 seconds...",
                child_player.display_name,
                group_player.display_name,
            )

            async def full_resync() -> None:
                await self.mass.players.sync_syncgroup(group_player.player_id)
                await self.mass.player_queues.resume(group_player.player_id)

            self.mass.call_later(5, full_resync, task_id=f"forced_resync_{player_id}")
            return
        elif new_power:
            # if a child player turned ON while the group is already active, we need to resync
            sync_leader = self.mass.players.get_sync_leader(group_player)
            if sync_leader.player_id != child_player_id:
                self.mass.create_task(
                    self.cmd_sync(child_player_id, sync_leader.player_id),
                )

    def register_syncgroup(self, group_player_id: str, name: str, members: Iterable[str]) -> Player:
        """Register a (virtual/fake) syncgroup player."""
        # extract player features from first/random player
        for member in members:
            if first_player := self.mass.players.get(member):
                break
        else:
            # edge case: no child player is (yet) available; postpone register
            return None
        player = Player(
            player_id=group_player_id,
            provider=self.instance_id,
            type=PlayerType.SYNC_GROUP,
            name=name,
            available=True,
            powered=False,
            device_info=DeviceInfo(model="SyncGroup", manufacturer=self.name),
            supported_features=first_player.supported_features,
            group_childs=set(members),
            active_source=group_player_id,
        )
        self.mass.players.register_or_update(player)
        return player

    # DO NOT OVERRIDE BELOW

    @property
    def players(self) -> list[Player]:
        """Return all players belonging to this provider."""
        # pylint: disable=no-member
        return [
            player
            for player in self.mass.players
            if player.provider in (self.instance_id, self.domain)
        ]
