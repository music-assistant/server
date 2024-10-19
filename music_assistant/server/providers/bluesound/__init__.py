"""Bluesound Player Provider for BluOS players to work with Music Assistant."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, TypedDict

from pyblu import Player as BluosPlayer
from pyblu import Status, SyncStatus
from zeroconf import ServiceStateChange

from music_assistant.common.models.config_entries import (
    CONF_ENTRY_CROSSFADE,
    CONF_ENTRY_ENABLE_ICY_METADATA,
    CONF_ENTRY_ENFORCE_MP3,
    CONF_ENTRY_FLOW_MODE_ENFORCED,
    CONF_ENTRY_HTTP_PROFILE_FORCED_2,
    ConfigEntry,
    ConfigValueType,
)
from music_assistant.common.models.enums import (
    PlayerFeature,
    PlayerState,
    PlayerType,
    ProviderFeature,
)
from music_assistant.common.models.errors import PlayerCommandFailed
from music_assistant.common.models.player import DeviceInfo, Player, PlayerMedia
from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.server.helpers.util import (
    get_port_from_zeroconf,
    get_primary_ip_address_from_zeroconf,
)
from music_assistant.server.models.player_provider import PlayerProvider

if TYPE_CHECKING:
    from zeroconf.asyncio import AsyncServiceInfo

    from music_assistant.common.models.config_entries import ProviderConfig
    from music_assistant.common.models.provider import ProviderManifest
    from music_assistant.server import MusicAssistant
    from music_assistant.server.models import ProviderInstanceType


PLAYER_FEATURES_BASE = {
    PlayerFeature.SYNC,
    PlayerFeature.VOLUME_MUTE,
    PlayerFeature.PAUSE,
}

PLAYBACK_STATE_MAP = {
    "play": PlayerState.PLAYING,
    "stream": PlayerState.PLAYING,
    "stop": PlayerState.IDLE,
    "pause": PlayerState.PAUSED,
    "connecting": PlayerState.IDLE,
}

PLAYBACK_STATE_POLL_MAP = {
    "play": PlayerState.PLAYING,
    "stream": PlayerState.PLAYING,
    "stop": PlayerState.IDLE,
    "pause": PlayerState.PAUSED,
    "connecting": "CONNECTING",
}

SOURCE_LINE_IN = "line_in"
SOURCE_AIRPLAY = "airplay"
SOURCE_SPOTIFY = "spotify"
SOURCE_UNKNOWN = "unknown"
SOURCE_RADIO = "radio"
POLL_STATE_STATIC = "static"
POLL_STATE_DYNAMIC = "dynamic"


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize BluOS instance with given configuration."""
    return BluesoundPlayerProvider(mass, manifest, config)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """Set up legacy BluOS devices."""
    # ruff: noqa: ARG001
    return ()


class BluesoundDiscoveryInfo(TypedDict):
    """Template for MDNS discovery info."""

    _objectType: str
    ip_address: str
    port: str
    mac: str
    model: str
    zs: bool


class BluesoundPlayer:
    """Holds the details of the (discovered) BluOS player."""

    def __init__(
        self,
        prov: BluesoundPlayerProvider,
        player_id: str,
        discovery_info: BluesoundDiscoveryInfo,
        ip_address: str,
        port: int,
    ) -> None:
        """Initialize the BluOS Player."""
        self.port = port
        self.prov = prov
        self.mass = prov.mass
        self.player_id = player_id
        self.discovery_info = discovery_info
        self.ip_address = ip_address
        self.logger = prov.logger.getChild(player_id)
        self.connected: bool = True
        self.client = BluosPlayer(self.ip_address, self.port, self.mass.http_session)
        self.sync_status = SyncStatus
        self.status = Status
        self.poll_state = POLL_STATE_STATIC
        self.dynamic_poll_count: int = 0
        self.mass_player: Player | None = None
        self._listen_task: asyncio.Task | None = None

    async def disconnect(self) -> None:
        """Disconnect the BluOS client and cleanup."""
        if self._listen_task and not self._listen_task.done():
            self._listen_task.cancel()
        if self.client:
            await self.client.close()
        self.connected = False
        self.logger.debug("Disconnected from player API")

    async def update_attributes(self) -> None:
        """Update the BluOS player attributes."""
        self.logger.debug("updating %s attributes", self.player_id)
        if self.dynamic_poll_count > 0:
            self.dynamic_poll_count -= 1

        self.sync_status = await self.client.sync_status()
        self.status = await self.client.status()

        # Update timing
        self.mass_player.elapsed_time = self.status.seconds
        self.mass_player.elapsed_time_last_updated = time.time()

        if not self.mass_player:
            return
        if self.sync_status.volume == -1:
            self.mass_player.volume_level = 100
        else:
            self.mass_player.volume_level = self.sync_status.volume
        self.mass_player.volume_muted = self.status.mute

        self.logger.log(
            VERBOSE_LOG_LEVEL,
            "Speaker state: %s vs reported state: %s",
            PLAYBACK_STATE_POLL_MAP[self.status.state],
            self.mass_player.state,
        )

        if (
            self.poll_state == POLL_STATE_DYNAMIC and self.dynamic_poll_count <= 0
        ) or self.mass_player.state == PLAYBACK_STATE_POLL_MAP[self.status.state]:
            self.logger.debug("Changing bluos poll state from %s to static", self.poll_state)
            self.poll_state = POLL_STATE_STATIC
            self.mass_player.poll_interval = 30
            self.mass.players.update(self.player_id)

        if self.status.state == "stream":
            mass_active = self.mass.streams.base_url
        elif self.status.state == "stream" and self.status.input_id == "input0":
            self.mass_player.active_source = SOURCE_LINE_IN
        elif self.status.state == "stream" and self.status.input_id == "Airplay":
            self.mass_player.active_source = SOURCE_AIRPLAY
        elif self.status.state == "stream" and self.status.input_id == "Spotify":
            self.mass_player.active_source = SOURCE_SPOTIFY
        elif self.status.state == "stream" and self.status.input_id == "RadioParadise":
            self.mass_player.active_source = SOURCE_RADIO
        elif self.status.state == "stream" and (mass_active not in self.status.stream_url):
            self.mass_player.active_source = SOURCE_UNKNOWN

        # TODO check pair status

        # TODO fix pairing

        if self.sync_status.master is None:
            if self.sync_status.slaves:
                self.mass_player.group_childs = (
                    self.sync_status.slaves if len(self.sync_status.slaves) > 1 else set()
                )
                self.mass_player.synced_to = None

            if self.status.state == "stream":
                self.mass_player.current_media = PlayerMedia(
                    uri=self.status.stream_url,
                    title=self.status.name,
                    artist=self.status.artist,
                    album=self.status.album,
                    image_url=self.status.image,
                )
            else:
                self.mass_player.current_media = None

        else:
            self.mass_player.group_childs = set()
            self.mass_player.synced_to = self.sync_status.master
            self.mass_player.active_source = self.sync_status.master

        self.mass_player.state = PLAYBACK_STATE_MAP[self.status.state]
        self.mass.players.update(self.player_id)


class BluesoundPlayerProvider(PlayerProvider):
    """Bluos compatible player provider, providing support for bluesound speakers."""

    bluos_players: dict[str, BluesoundPlayer]

    @property
    def supported_features(self) -> tuple[ProviderFeature, ...]:
        """Return the features supported by this Provider."""
        return (ProviderFeature.SYNC_PLAYERS,)

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self.bluos_players: dict[str, BluesoundPlayer] = {}

    async def on_mdns_service_state_change(
        self, name: str, state_change: ServiceStateChange, info: AsyncServiceInfo | None
    ) -> None:
        """Handle MDNS service state callback for BluOS."""
        name = name.split(".", 1)[0]
        self.player_id = info.decoded_properties["mac"]
        # Handle removed player

        if state_change == ServiceStateChange.Removed:
            # Check if the player manager has an existing entry for this player
            if mass_player := self.mass.players.get(self.player_id):
                # The player has become unavailable
                self.logger.debug("Player offline: %s", mass_player.display_name)
                mass_player.available = False
                self.mass.players.update(self.player_id)
            return

        if bluos_player := self.bluos_players.get(self.player_id):
            if mass_player := self.mass.players.get(self.player_id):
                cur_address = get_primary_ip_address_from_zeroconf(info)
                cur_port = get_port_from_zeroconf(info)
                if cur_address and cur_address != mass_player.device_info.address:
                    self.logger.debug(
                        "Address updated to %s for player %s", cur_address, mass_player.display_name
                    )
                    bluos_player.ip_address = cur_address
                    bluos_player.port = cur_port
                    mass_player.device_info = DeviceInfo(
                        model=mass_player.device_info.model,
                        manufacturer=mass_player.device_info.manufacturer,
                        address=str(cur_address),
                    )
                if not mass_player.available:
                    self.logger.debug("Player back online: %s", mass_player.display_name)
                    bluos_player.client.sync()
                bluos_player.discovery_info = info
                self.mass.players.update(self.player_id)
                return
            # handle new player
        cur_address = get_primary_ip_address_from_zeroconf(info)
        cur_port = get_port_from_zeroconf(info)
        self.logger.debug("Discovered device %s on %s", name, cur_address)

        self.bluos_players[self.player_id] = bluos_player = BluesoundPlayer(
            self, self.player_id, discovery_info=info, ip_address=cur_address, port=cur_port
        )

        bluos_player.mass_player = mass_player = Player(
            player_id=self.player_id,
            provider=self.instance_id,
            type=PlayerType.PLAYER,
            name=name,
            available=True,
            powered=True,
            device_info=DeviceInfo(
                model="BluOS speaker",
                manufacturer="Bluesound",
                address=cur_address,
            ),
            # Set the supported features for this player
            supported_features=(
                PlayerFeature.VOLUME_SET,
                PlayerFeature.VOLUME_MUTE,
                PlayerFeature.PAUSE,
            ),
            needs_poll=True,
            poll_interval=30,
        )
        await self.mass.players.register(mass_player)

        # TODO sync
        await bluos_player.update_attributes()
        self.mass.players.update(self.player_id)

    async def get_player_config_entries(
        self,
        player_id: str,
    ) -> tuple[ConfigEntry, ...]:
        """Return Config Entries for the given player."""
        base_entries = await super().get_player_config_entries(self.player_id)
        if not self.bluos_players.get(player_id):
            # TODO fix player entries
            return (*base_entries, CONF_ENTRY_CROSSFADE)
        return (
            *base_entries,
            CONF_ENTRY_HTTP_PROFILE_FORCED_2,
            CONF_ENTRY_CROSSFADE,
            CONF_ENTRY_ENFORCE_MP3,
            CONF_ENTRY_FLOW_MODE_ENFORCED,
            CONF_ENTRY_ENABLE_ICY_METADATA,
        )

    async def cmd_stop(self, player_id: str) -> None:
        """Send STOP command to BluOS player."""
        if bluos_player := self.bluos_players[player_id]:
            play_state = await bluos_player.client.stop(timeout=1)
            if play_state == "stop":
                bluos_player.poll_state = POLL_STATE_DYNAMIC
                bluos_player.dynamic_poll_count = 6
                bluos_player.mass_player.poll_interval = 0.5
            # Update media info then optimistically override playback state and source

    async def cmd_play(self, player_id: str) -> None:
        """Send PLAY command to BluOS player."""
        if bluos_player := self.bluos_players[player_id]:
            play_state = await bluos_player.client.play(timeout=1)
            if play_state == "stream":
                bluos_player.poll_state = POLL_STATE_DYNAMIC
                bluos_player.dynamic_poll_count = 6
                bluos_player.mass_player.poll_interval = 0.5
            # Optimistic state, reduces interface lag

    async def cmd_pause(self, player_id: str) -> None:
        """Send PAUSE command to BluOS player."""
        if bluos_player := self.bluos_players[player_id]:
            play_state = await bluos_player.client.pause(timeout=1)
            if play_state == "pause":
                bluos_player.poll_state = POLL_STATE_DYNAMIC
                bluos_player.dynamic_poll_count = 6
                bluos_player.mass_player.poll_interval = 0.5
            self.logger.debug("Set BluOS state to %s", play_state)
            # Optimistic state, reduces interface lag

    async def cmd_volume_set(self, player_id: str, volume_level: int) -> None:
        """Send VOLUME_SET command to BluOS player."""
        if bluos_player := self.bluos_players[player_id]:
            await bluos_player.client.volume(level=volume_level, timeout=1)
            self.logger.debug("Set BluOS speaker volume to %s", volume_level)
            mass_player = self.mass.players.get(player_id)
            # Optimistic state, reduces interface lag
            mass_player.volume_level = volume_level
            await bluos_player.update_attributes()

    async def cmd_volume_mute(self, player_id: str, muted: bool) -> None:
        """Send VOLUME MUTE command to BluOS player."""
        if bluos_player := self.bluos_players[player_id]:
            await bluos_player.client.volume(mute=muted)
            # Optimistic state, reduces interface lag
            mass_player = self.mass.players.get(player_id)
            mass_player.volume_mute = muted
            await bluos_player.update_attributes()

    async def play_media(self, player_id: str, media: PlayerMedia) -> None:
        """Handle PLAY MEDIA for BluOS player using the provided URL."""
        self.logger.debug("Play_media called")
        if bluos_player := self.bluos_players[player_id]:
            self.mass.players.update(player_id)
            play_state = await bluos_player.client.play_url(media.uri, timeout=1)
            # Enable dynamic polling
            if play_state == "stream":
                bluos_player.poll_state = POLL_STATE_DYNAMIC
                bluos_player.dynamic_poll_count = 6
                bluos_player.mass_player.poll_interval = 0.5
            self.logger.debug("Set BluOS state to %s", play_state)
            await bluos_player.update_attributes()

        # Optionally, handle the playback_state or additional logic here
        if play_state in ("PlayerUnexpectedResponseError", "PlayerUnreachableError"):
            raise PlayerCommandFailed("Failed to start playback.")

    async def poll_player(self, player_id: str) -> None:
        """Poll player for state updates."""
        if bluos_player := self.bluos_players[player_id]:
            await bluos_player.update_attributes()

    # TODO fix sync & unsync

    async def cmd_sync(self, player_id: str, target_player: str) -> None:
        """Handle SYNC command for BluOS player."""

    async def cmd_unsync(self, player_id: str) -> None:
        """Handle UNSYNC command for BluOS player."""
        if bluos_player := self.bluos_players[player_id]:
            await bluos_player.client.player.leave_group()
