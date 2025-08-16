"""Bluesound Player implementation."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, cast

from music_assistant_models.enums import PlaybackState, PlayerFeature, PlayerType
from music_assistant_models.errors import PlayerCommandFailed
from pyblu import Player as BluosPlayer
from pyblu import Status, SyncStatus

from music_assistant.constants import (
    CONF_ENTRY_ENABLE_ICY_METADATA,
    CONF_ENTRY_FLOW_MODE_ENFORCED,
    CONF_ENTRY_HTTP_PROFILE_FORCED_2,
    CONF_ENTRY_OUTPUT_CODEC,
    VERBOSE_LOG_LEVEL,
)
from music_assistant.models.player import DeviceInfo, Player, PlayerMedia

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry

    from .provider import BluesoundDiscoveryInfo, BluesoundPlayerProvider


PLAYER_FEATURES_BASE = {
    PlayerFeature.SET_MEMBERS,
    PlayerFeature.VOLUME_MUTE,
    PlayerFeature.PAUSE,
}

PLAYBACK_STATE_MAP = {
    "play": PlaybackState.PLAYING,
    "stream": PlaybackState.PLAYING,
    "stop": PlaybackState.IDLE,
    "pause": PlaybackState.PAUSED,
    "connecting": PlaybackState.IDLE,
}

PLAYBACK_STATE_POLL_MAP = {
    "play": PlaybackState.PLAYING,
    "stream": PlaybackState.PLAYING,
    "stop": PlaybackState.IDLE,
    "pause": PlaybackState.PAUSED,
    "connecting": "CONNECTING",
}

SOURCE_LINE_IN = "line_in"
SOURCE_AIRPLAY = "airplay"
SOURCE_SPOTIFY = "spotify"
SOURCE_UNKNOWN = "unknown"
SOURCE_RADIO = "radio"
POLL_STATE_STATIC = "static"
POLL_STATE_DYNAMIC = "dynamic"


class BluesoundPlayer(Player):
    """Holds the details of the (discovered) BluOS player."""

    def __init__(
        self,
        provider: BluesoundPlayerProvider,
        player_id: str,
        discovery_info: BluesoundDiscoveryInfo,
        name: str,
        ip_address: str,
        port: int,
    ) -> None:
        """Initialize the BluOS Player."""
        super().__init__(provider, player_id)
        self.port = port
        self.discovery_info = discovery_info
        self.ip_address = ip_address
        self.connected: bool = True
        self.client = BluosPlayer(self.ip_address, self.port, self.mass.http_session)
        self.sync_status = SyncStatus
        self.status = Status
        self.poll_state = POLL_STATE_STATIC
        self.dynamic_poll_count: int = 0
        self._listen_task: asyncio.Task | None = None
        # Set base player attributes
        self._attr_type = PlayerType.PLAYER
        self._attr_supported_features = PLAYER_FEATURES_BASE.copy()
        self._attr_name = name
        self._attr_device_info = DeviceInfo(
            model=discovery_info.get("model", "BluOS Device"),
            manufacturer="BluOS",
            ip_address=ip_address,
        )
        self._attr_available = True
        self._attr_needs_poll = True
        self._attr_poll_interval = 30
        self._attr_can_group_with = {provider.lookup_key}

    async def setup(self) -> None:
        """Set up the player."""
        # Add volume support if available
        if self.discovery_info.get("zs"):
            self._attr_supported_features.add(PlayerFeature.VOLUME_SET)
        await self.mass.players.register_or_update(self)

    async def get_config_entries(self) -> list[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the player."""
        return [
            *await super().get_config_entries(),
            CONF_ENTRY_HTTP_PROFILE_FORCED_2,
            CONF_ENTRY_OUTPUT_CODEC,
            CONF_ENTRY_FLOW_MODE_ENFORCED,
            CONF_ENTRY_ENABLE_ICY_METADATA,
        ]

    async def disconnect(self) -> None:
        """Disconnect the BluOS client and cleanup."""
        if self._listen_task and not self._listen_task.done():
            self._listen_task.cancel()
        if self.client:
            await self.client.close()
        self.connected = False
        self.logger.debug("Disconnected from player API")

    async def stop(self) -> None:
        """Send STOP command to BluOS player."""
        play_state = await self.client.stop(timeout=1)
        if play_state == "stop":
            self.poll_state = POLL_STATE_DYNAMIC
            self.dynamic_poll_count = 6
            self._attr_poll_interval = 0.5
        self._attr_playback_state = PlaybackState.IDLE
        self.update_state()

    async def play(self) -> None:
        """Send PLAY command to BluOS player."""
        play_state = await self.client.play(timeout=1)
        if play_state == "stream":
            self.poll_state = POLL_STATE_DYNAMIC
            self.dynamic_poll_count = 6
            self._attr_poll_interval = 0.5
        self._attr_playback_state = PlaybackState.PLAYING
        self.update_state()

    async def pause(self) -> None:
        """Send PAUSE command to BluOS player."""
        play_state = await self.client.pause(timeout=1)
        if play_state == "pause":
            self.poll_state = POLL_STATE_DYNAMIC
            self.dynamic_poll_count = 6
            self._attr_poll_interval = 0.5
        self.logger.debug("Set BluOS state to %s", play_state)
        self._attr_playback_state = PlaybackState.PAUSED
        self.update_state()

    async def volume_set(self, volume_level: int) -> None:
        """Send VOLUME_SET command to BluOS player."""
        await self.client.volume(level=volume_level, timeout=1)
        self.logger.debug("Set BluOS speaker volume to %s", volume_level)
        self._attr_volume_level = volume_level
        self.update_state()

    async def volume_mute(self, muted: bool) -> None:
        """Send VOLUME MUTE command to BluOS player."""
        await self.client.volume(mute=muted)
        self._attr_volume_muted = muted
        self.update_state()

    async def play_media(self, media: PlayerMedia) -> None:
        """Handle PLAY MEDIA for BluOS player using the provided URL."""
        self.logger.debug("Play_media called")
        play_state = await self.client.play_url(media.uri, timeout=1)

        # Enable dynamic polling
        if play_state == "stream":
            self.poll_state = POLL_STATE_DYNAMIC
            self.dynamic_poll_count = 6
            self._attr_poll_interval = 0.5
            self._attr_playback_state = PlaybackState.PLAYING

        self.logger.debug("Set BluOS state to %s", play_state)

        # Optionally, handle the playback_state or additional logic here
        if play_state in ("PlayerUnexpectedResponseError", "PlayerUnreachableError"):
            raise PlayerCommandFailed("Failed to start playback.")

        self.update_state()

    async def set_members(
        self,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        """Handle GROUP command for BluOS player."""
        # TODO: Implement grouping logic

    async def ungroup(self) -> None:
        """Handle UNGROUP command for BluOS player."""
        await self.client.player.leave_group()

    async def poll(self) -> None:
        """Poll player for state updates."""
        await self.update_attributes()

    async def update_attributes(self) -> None:
        """Update the BluOS player attributes."""
        self.logger.debug("updating %s attributes", self.player_id)
        if self.dynamic_poll_count > 0:
            self.dynamic_poll_count -= 1

        self.sync_status = await self.client.sync_status()
        self.status = await self.client.status()

        # Update timing
        self._attr_elapsed_time = self.status.seconds
        self._attr_elapsed_time_last_updated = time.time()

        if self.sync_status.volume == -1:
            self._attr_volume_level = 100
        else:
            self._attr_volume_level = self.sync_status.volume
        self._attr_volume_muted = self.status.mute

        self.logger.log(
            VERBOSE_LOG_LEVEL,
            "Speaker state: %s vs reported state: %s",
            PLAYBACK_STATE_POLL_MAP[self.status.state],
            self._attr_playback_state,
        )

        if (
            self.poll_state == POLL_STATE_DYNAMIC and self.dynamic_poll_count <= 0
        ) or self._attr_playback_state == PLAYBACK_STATE_POLL_MAP[self.status.state]:
            self.logger.debug("Changing bluos poll state from %s to static", self.poll_state)
            self.poll_state = POLL_STATE_STATIC
            self._attr_poll_interval = 30

        if self.status.state == "stream":
            mass_active = self.mass.streams.base_url
        elif self.status.state == "stream" and self.status.input_id == "input0":
            self._attr_active_source = SOURCE_LINE_IN
        elif self.status.state == "stream" and self.status.input_id == "Airplay":
            self._attr_active_source = SOURCE_AIRPLAY
        elif self.status.state == "stream" and self.status.input_id == "Spotify":
            self._attr_active_source = SOURCE_SPOTIFY
        elif self.status.state == "stream" and self.status.input_id == "RadioParadise":
            self._attr_active_source = SOURCE_RADIO
        elif self.status.state == "stream" and (mass_active not in self.status.stream_url):
            self._attr_active_source = SOURCE_UNKNOWN

        # TODO check pair status

        # TODO fix pairing

        # Create a lookup map of (ip, port) -> player_id for all known players.
        player_map = {
            (player.ip_address, player.port): player.player_id
            for player in cast("list[BluesoundPlayer]", self.provider.players)
        }

        if self.sync_status.leader is None:
            if self.sync_status.followers:
                if len(self.sync_status.followers) > 1:
                    self._attr_group_members = [
                        player_map[f.ip, f.port]
                        for f in self.sync_status.followers
                        if (f.ip, f.port) in player_map
                    ]
                else:
                    self._attr_group_members.clear()

            if self.status.state == "stream":
                self._attr_current_media = PlayerMedia(
                    uri=self.status.stream_url,
                    title=self.status.name,
                    artist=self.status.artist,
                    album=self.status.album,
                    image_url=self.status.image,
                )
            else:
                self._attr_current_media = None

        else:
            self._attr_group_members.clear()
            leader = self.sync_status.leader
            self._attr_active_source = player_map[leader.ip, leader.port]

        self._attr_playback_state = PLAYBACK_STATE_MAP[self.status.state]
        self.update_state()

    @property
    def synced_to(self) -> str | None:
        """
        Return the id of the player this player is synced to (sync leader).

        If this player is not synced to another player (or is the sync leader itself),
        this should return None.
        """
        if self.sync_status.leader:
            return self.sync_status.leader
        return None
