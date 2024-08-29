"""Model/base for a Metadata Provider implementation."""

from __future__ import annotations

from abc import abstractmethod

from music_assistant.common.models.config_entries import (
    BASE_PLAYER_CONFIG_ENTRIES,
    CONF_ENTRY_ANNOUNCE_VOLUME,
    CONF_ENTRY_ANNOUNCE_VOLUME_MAX,
    CONF_ENTRY_ANNOUNCE_VOLUME_MIN,
    CONF_ENTRY_ANNOUNCE_VOLUME_STRATEGY,
    CONF_ENTRY_PLAYER_ICON_GROUP,
    ConfigEntry,
    ConfigValueOption,
    PlayerConfig,
)
from music_assistant.common.models.enums import ConfigEntryType, PlayerState
from music_assistant.common.models.player import Player, PlayerMedia
from music_assistant.constants import (
    CONF_GROUP_MEMBERS,
    CONF_PREVENT_SYNC_LEADER_OFF,
    CONF_SYNC_LEADER,
    CONF_SYNCGROUP_DEFAULT_ON,
    SYNCGROUP_PREFIX,
)

from .provider import Provider

# ruff: noqa: ARG001, ARG002


class PlayerProvider(Provider):
    """Base representation of a Player Provider (controller).

    Player Provider implementations should inherit from this base model.
    """

    async def get_player_config_entries(self, player_id: str) -> tuple[ConfigEntry, ...]:
        """Return all (provider/player specific) Config Entries for the given player (if any)."""
        if player_id.startswith(SYNCGROUP_PREFIX):
            # default entries for syncgroups
            return (
                *BASE_PLAYER_CONFIG_ENTRIES,
                CONF_ENTRY_PLAYER_ICON_GROUP,
                ConfigEntry(
                    key=CONF_GROUP_MEMBERS,
                    type=ConfigEntryType.STRING,
                    label="Group members",
                    default_value=[],
                    options=tuple(
                        ConfigValueOption(x.display_name, x.player_id)
                        for x in self.mass.players.all(True, False)
                        if x.player_id != player_id
                        and x.provider == self.instance_id
                        and not x.player_id.startswith(SYNCGROUP_PREFIX)
                    ),
                    description="Select all players you want to be part of this group",
                    multi_value=True,
                    required=True,
                ),
                ConfigEntry(
                    key=CONF_SYNC_LEADER,
                    type=ConfigEntryType.STRING,
                    label="Preferred sync leader",
                    default_value="auto",
                    options=(
                        *tuple(
                            ConfigValueOption(x.display_name, x.player_id)
                            for x in self.mass.players.all(True, False)
                            if x.player_id
                            in self.mass.config.get_raw_player_config_value(
                                player_id, CONF_GROUP_MEMBERS, []
                            )
                        ),
                        ConfigValueOption("Select automatically", "auto"),
                    ),
                    description="By default Music Assistant will automatically assign a "
                    "(random) player as sync leader, meaning the other players in the sync group "
                    "will be synced to that player. If you want to force a specific player to be "
                    "the sync leader, select it here.",
                    required=True,
                ),
                ConfigEntry(
                    key=CONF_PREVENT_SYNC_LEADER_OFF,
                    type=ConfigEntryType.BOOLEAN,
                    label="Prevent sync leader power off",
                    default_value=False,
                    description="With this setting enabled, Music Assistant will disallow powering "
                    "off the sync leader player if other players are still "
                    "active in the sync group. This is useful if you want to prevent "
                    "a short drop in the music while the music is transferred to another player.",
                    required=True,
                ),
                ConfigEntry(
                    key=CONF_SYNCGROUP_DEFAULT_ON,
                    type=ConfigEntryType.STRING,
                    label="Default power ON behavior",
                    default_value="powered_only",
                    options=(
                        ConfigValueOption("Always power ON all child devices", "always_all"),
                        ConfigValueOption("Always power ON sync leader", "always_leader"),
                        ConfigValueOption("Start with powered players", "powered_only"),
                        ConfigValueOption("Ignore", "ignore"),
                    ),
                    description="What should happen if you power ON a sync group "
                    "(or you start playback to it), while no (or not all) players "
                    "are powered ON ?\n\nShould Music Assistant power ON all players, or only the "
                    "sync leader, or should it ignore the command if no players are powered ON ?",
                    required=False,
                ),
            )

        return (
            *BASE_PLAYER_CONFIG_ENTRIES,
            # add default entries for announce feature
            CONF_ENTRY_ANNOUNCE_VOLUME_STRATEGY,
            CONF_ENTRY_ANNOUNCE_VOLUME,
            CONF_ENTRY_ANNOUNCE_VOLUME_MIN,
            CONF_ENTRY_ANNOUNCE_VOLUME_MAX,
        )

    def on_player_config_changed(self, config: PlayerConfig, changed_keys: set[str]) -> None:
        """Call (by config manager) when the configuration of a player changes."""

    def on_player_config_removed(self, player_id: str) -> None:
        """Call (by config manager) when the configuration of a player is removed."""

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

    async def poll_player(self, player_id: str) -> None:
        """Poll player for state updates.

        This is called by the Player Manager;
        if 'needs_poll' is set to True in the player object.
        """

    def on_group_child_power(
        self, group_player: Player, child_player: Player, new_power: bool, group_state: PlayerState
    ) -> None:
        """
        Call when a power command was executed on one of the child players of a PlayerGroup.

        This is used to handle special actions such as (re)syncing.
        The group state is sent with the state BEFORE the power command was executed.
        """

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
