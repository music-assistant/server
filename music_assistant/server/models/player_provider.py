"""Model/base for a Metadata Provider implementation."""
from __future__ import annotations

from abc import abstractmethod
from collections.abc import Iterable
from typing import TYPE_CHECKING

import shortuuid

from music_assistant.common.models.config_entries import (
    CONF_ENTRY_AUTO_PLAY,
    CONF_ENTRY_GROUPED_POWER_ON,
    CONF_ENTRY_VOLUME_NORMALIZATION,
    CONF_ENTRY_VOLUME_NORMALIZATION_TARGET,
    ConfigEntry,
    ConfigValueOption,
    PlayerConfig,
)
from music_assistant.common.models.enums import ConfigEntryType, PlayerFeature, PlayerType
from music_assistant.common.models.player import DeviceInfo, Player
from music_assistant.constants import CONF_GROUP_MEMBERS, CONF_GROUP_PLAYERS

from .provider import Provider

if TYPE_CHECKING:
    from music_assistant.common.models.queue_item import QueueItem


# ruff: noqa: ARG001, ARG002


class PlayerProvider(Provider):
    """Base representation of a Player Provider (controller).

    Player Provider implementations should inherit from this base model.
    """

    async def get_player_config_entries(self, player_id: str) -> tuple[ConfigEntry, ...]:
        """Return all (provider/player specific) Config Entries for the given player (if any)."""
        entries = (
            CONF_ENTRY_VOLUME_NORMALIZATION,
            CONF_ENTRY_AUTO_PLAY,
            CONF_ENTRY_VOLUME_NORMALIZATION_TARGET,
        )
        if not player_id.startswith("syncgroup_"):
            return entries
        # add default entries for syncgroups
        return entries + (
            ConfigEntry(
                key=CONF_GROUP_MEMBERS,
                type=ConfigEntryType.STRING,
                label="Group members",
                default_value=[],
                options=tuple(
                    ConfigValueOption(x.display_name, x.player_id)
                    for x in self.mass.players.all(True, True, False)
                    if x.player_id != player_id and x.provider == self.instance_id
                ),
                description="Select all players you want to be part of this group",
                multi_value=True,
                required=True,
            ),
            CONF_ENTRY_GROUPED_POWER_ON,
        )

    def on_player_config_changed(self, config: PlayerConfig, changed_keys: set[str]) -> None:
        """Call (by config manager) when the configuration of a player changes."""

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
        # default implementation (for a player without enqueue_next feature):
        # simply start playback of the next track.
        # player providers need to override this behavior if/when needed
        await self.play_media(
            player_id=player_id, queue_item=queue_item, seek_position=0, fade_in=False
        )

    async def cmd_power(self, player_id: str, powered: bool) -> None:
        """Send POWER command to given player.

        - player_id: player_id of the player to handle the command.
        - powered: bool if player should be powered on or off.
        """
        # will only be called for players with Power feature set.

    async def cmd_volume_set(self, player_id: str, volume_level: int) -> None:
        """Send VOLUME_SET command to given player.

        - player_id: player_id of the player to handle the command.
        - volume_level: volume level (0..100) to set on the player.
        """
        # will only be called for players with Volume feature set.

    async def cmd_volume_mute(self, player_id: str, muted: bool) -> None:
        """Send VOLUME MUTE command to given player.

        - player_id: player_id of the player to handle the command.
        - muted: bool if player should be muted.
        """
        # will only be called for players with Mute feature set.

    async def cmd_seek(self, player_id: str, position: int) -> None:
        """Handle SEEK command for given queue.

        - player_id: player_id of the player to handle the command.
        - position: position in seconds to seek to in the current playing item.
        """
        # will only be called for players with Seek feature set.

    async def cmd_sync(self, player_id: str, target_player: str) -> None:
        """Handle SYNC command for given player.

        Join/add the given player(id) to the given (master) player/sync group.

            - player_id: player_id of the player to handle the command.
            - target_player: player_id of the syncgroup master or group player.
        """
        # will only be called for players with SYNC feature set.

    async def cmd_unsync(self, player_id: str) -> None:
        """Handle UNSYNC command for given player.

        Remove the given player from any syncgroups it currently is synced to.

            - player_id: player_id of the player to handle the command.
        """
        # will only be called for players with SYNC feature set.

    async def create_group(self, name: str, members: list[str]) -> Player:
        """Create new PlayerGroup on this provider.

        Create a new SyncGroup (or PlayerGroup) with given name and members.

            - name: Name for the new group to create.
            - members: A list of player_id's that should be part of this group.
        """
        # will only be called for players with PLAYER_GROUP_CREATE feature set.
        # default implementation: create "fake" player and store in config.
        # should work for all players that support the sync feature.
        # may be overridden with provider specific implementation
        # if the provider supports this natively.
        new_group_id = f"syncgroup_{shortuuid.random(8)}"
        # create default config with the user chosen name
        self.mass.config.create_default_player_config(
            new_group_id,
            self.instance_id,
            name=name,
            enabled=True,
            values={CONF_GROUP_MEMBERS: members},
        )
        player = self._register_group_player(new_group_id, members=members)
        return player

    async def poll_player(self, player_id: str) -> None:
        """Poll player for state updates.

        This is called by the Player Manager;
        - every 360 seconds if the player if not powered
        - every 30 seconds if the player is powered
        - every 10 seconds if the player is playing

        Use this method to request any info that is not automatically updated and/or
        to detect if the player is still alive.
        If this method raises the PlayerUnavailable exception,
        the player is marked as unavailable until
        the next successful poll or event where it becomes available again.
        If the player does not need any polling, simply do not override this method.
        """

    async def on_child_power(self, player_id: str, child_player: Player, new_power: bool) -> None:
        """
        Call when a power command was executed on one of the child players.

        This is used to handle special actions such as muting as power or (re)syncing.
        """

    async def register_group_players(self) -> None:
        """Register all (virtual/fake) group players in the Player controller."""
        player_configs = await self.mass.config.get_player_configs(self.instance_id)
        for player_config in player_configs:
            if not player_config.player_id.startswith("syncgroup_"):
                continue
            members = player_config.get_value("group_members")
            self._register_group_player(
                player_config.player_id, player_config.name or player_config.default_name, members
            )

    def _register_group_player(
        self, group_player_id: str, name: str, members: Iterable[str]
    ) -> Player:
        """Register a (virtual/fake) group player in the Player controller."""
        # extract player features from first/random player
        if first_player := next(
            (self.mass.players.get(x) for x in members if x in self.players), None
        ):
            supported_features = tuple(
                x
                for x in first_player.supported_features
                if x not in (PlayerFeature.POWER, PlayerFeature.SYNC, PlayerFeature.VOLUME_MUTE)
            )
        else:
            # edge case: no child player is (yet) available; use safe default feature set
            supported_features = (PlayerFeature.PAUSE, PlayerFeature.VOLUME_SET)
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

    # DO NOT OVERRIDE BELOW

    @property
    def players(self) -> list[Player]:
        """Return all players belonging to this provider."""
        # pylint: disable=no-member
        return [player for player in self.mass.players if player.provider == self.domain]
