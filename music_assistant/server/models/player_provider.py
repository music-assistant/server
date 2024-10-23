"""Model/base for a Metadata Provider implementation."""

from __future__ import annotations

from abc import abstractmethod

from zeroconf import ServiceStateChange
from zeroconf.asyncio import AsyncServiceInfo

from music_assistant.common.models.config_entries import (
    BASE_PLAYER_CONFIG_ENTRIES,
    CONF_ENTRY_ANNOUNCE_VOLUME,
    CONF_ENTRY_ANNOUNCE_VOLUME_MAX,
    CONF_ENTRY_ANNOUNCE_VOLUME_MIN,
    CONF_ENTRY_ANNOUNCE_VOLUME_STRATEGY,
    ConfigEntry,
    PlayerConfig,
)
from music_assistant.common.models.errors import UnsupportedFeaturedException
from music_assistant.common.models.player import Player, PlayerMedia

from .provider import Provider

# ruff: noqa: ARG001, ARG002


class PlayerProvider(Provider):
    """Base representation of a Player Provider (controller).

    Player Provider implementations should inherit from this base model.
    """

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        await self.discover_players()

    async def get_player_config_entries(self, player_id: str) -> tuple[ConfigEntry, ...]:
        """Return all (provider/player specific) Config Entries for the given player (if any)."""
        return (
            *BASE_PLAYER_CONFIG_ENTRIES,
            # add default entries for announce feature
            CONF_ENTRY_ANNOUNCE_VOLUME_STRATEGY,
            CONF_ENTRY_ANNOUNCE_VOLUME,
            CONF_ENTRY_ANNOUNCE_VOLUME_MIN,
            CONF_ENTRY_ANNOUNCE_VOLUME_MAX,
        )

    async def on_player_config_change(self, config: PlayerConfig, changed_keys: set[str]) -> None:
        """Call (by config manager) when the configuration of a player changes."""
        # default implementation: feel free to override
        if (
            "enabled" in changed_keys
            and config.enabled
            and not self.mass.players.get(config.player_id)
        ):
            # if a player gets enabled, trigger discovery
            task_id = f"discover_players_{self.instance_id}"
            self.mass.call_later(5, self.discover_players, task_id=task_id)
        else:
            await self.poll_player(config.player_id)

    @abstractmethod
    async def cmd_stop(self, player_id: str) -> None:
        """Send STOP command to given player.

        - player_id: player_id of the player to handle the command.
        """

    async def cmd_play(self, player_id: str) -> None:
        """Send PLAY (unpause) command to given player.

        - player_id: player_id of the player to handle the command.
        """
        # will only be called for players with Pause feature set.
        raise NotImplementedError

    async def cmd_pause(self, player_id: str) -> None:
        """Send PAUSE command to given player.

        - player_id: player_id of the player to handle the command.
        """
        # will only be called for players with Pause feature set.
        raise NotImplementedError

    @abstractmethod
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

        Called when player reports it started buffering a queue item
        and when the queue items updated.

        A PlayerProvider implementation is in itself responsible for handling this
        so that the queue items keep playing until its empty or the player stopped.

        This will NOT be called if the end of the queue is reached (and repeat disabled).
        This will NOT be called if the player is using flow mode to playback the queue.
        """
        # will only be called for players with ENQUEUE feature set.
        raise NotImplementedError

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
        """Handle SEEK command for given player.

        - player_id: player_id of the player to handle the command.
        - position: position in seconds to seek to in the current playing item.
        """
        # will only be called for players with Seek feature set.
        raise NotImplementedError

    async def cmd_next(self, player_id: str) -> None:
        """Handle NEXT TRACK command for given player."""
        # will only be called for players with 'next_previous' feature set.
        raise NotImplementedError

    async def cmd_previous(self, player_id: str) -> None:
        """Handle PREVIOUS TRACK command for given player."""
        # will only be called for players with 'next_previous' feature set.
        raise NotImplementedError

    async def cmd_sync(self, player_id: str, target_player: str) -> None:
        """Handle SYNC command for given player.

        Join/add the given player(id) to the given (master) player/sync group.

            - player_id: player_id of the player to handle the command.
            - target_player: player_id of the sync leader.
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

    async def remove_player(self, player_id: str) -> None:
        """Remove a player."""
        # will only be called for players with REMOVE_PLAYER feature set.
        raise NotImplementedError

    async def discover_players(self) -> None:
        """Discover players for this provider."""
        # This will be called (once) when the player provider is loaded into MA.
        # Default implementation is mdns discovery, which will also automatically
        # discovery players during runtime. If a provider overrides this method and
        # doesn't use mdns, it is responsible for periodically searching for new players.
        if not self.available:
            return
        for mdns_type in self.manifest.mdns_discovery or []:
            for mdns_name in set(self.mass.aiozc.zeroconf.cache.cache):
                if mdns_type not in mdns_name or mdns_type == mdns_name:
                    continue
                info = AsyncServiceInfo(mdns_type, mdns_name)
                if await info.async_request(self.mass.aiozc.zeroconf, 3000):
                    await self.on_mdns_service_state_change(
                        mdns_name, ServiceStateChange.Added, info
                    )

    async def set_members(self, player_id: str, members: list[str]) -> None:
        """Set members for a groupplayer."""
        # will only be called for (group)players with SET_MEMBERS feature set.
        raise UnsupportedFeaturedException

    # DO NOT OVERRIDE BELOW

    @property
    def players(self) -> list[Player]:
        """Return all players belonging to this provider."""
        return [
            player
            for player in self.mass.players
            if player.provider in (self.instance_id, self.domain)
        ]
