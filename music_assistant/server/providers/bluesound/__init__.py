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
    CONF_ENTRY_CROSSFADE_FLOW_MODE_REQUIRED,
    CONF_ENTRY_ENABLE_ICY_METADATA,
    CONF_ENTRY_ENFORCE_MP3,
    CONF_ENTRY_FLOW_MODE_DEFAULT_ENABLED,
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
    PlayerFeature.ENQUEUE_NEXT,
    PlayerFeature.PAUSE,
}

PLAYBACK_STATE_MAP = {
    "play": PlayerState.PLAYING,
    "stream": PlayerState.PLAYING,
    "stop": PlayerState.IDLE,
    "pause": PlayerState.PAUSED,
    "connecting": PlayerState.IDLE,
}

SOURCE_LINE_IN = "line_in"
SOURCE_AIRPLAY = "airplay"
SOURCE_SPOTIFY = "spotify"
SOURCE_UNKNOWN = "unknown"
SOURCE_RADIO = "radio"


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
        self.logger.debug("Update attributes")

        self.sync_status = await self.client.sync_status()
        self.status = await self.client.status()

        # Update accurate timing
        self.mass_player.elapsed_time = self.status.seconds
        self.mass_player.elapsed_time_last_updated = time.time()

        if not self.mass_player:
            return
        if self.sync_status.volume == -1:
            self.mass_player.volume_level = 100
        else:
            self.mass_player.volume_level = self.sync_status.volume
        self.mass_player.volume_muted = self.status.mute

        if self.status.state == "stream":
            mass_active = self.mass.streams.base_url
            # self.logger.debug(mass_active)
            # self.logger.debug(self.status.stream_url)
        if self.status.state == "stream" and self.status.input_id == "input0":
            self.mass_player.active_source = SOURCE_LINE_IN
        elif self.status.state == "stream" and self.status.input_id == "Airplay":
            self.mass_player.active_source = SOURCE_AIRPLAY
        elif self.status.state == "stream" and self.status.input_id == "Spotify":
            self.mass_player.active_source = SOURCE_SPOTIFY
        elif self.status.state == "stream" and self.status.input_id == "RadioParadise":
            self.mass_player.active_source = SOURCE_RADIO
        elif self.status.state == "stream" and (mass_active not in self.status.stream_url):
            self.logger.debug("mass_active")
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

            # TODO fix sync and multiple players

            # elif container.get("name") and container.get("id"):
            #     images = self.status.image
            #     image_url = self.status.image
            #     self.mass_player.current_media = PlayerMedia(
            #         uri=self.status.stream_url,
            #         title=self.status.name,
            #         image_url=self.status.image,
            #     )
            else:
                self.mass_player.current_media = None

        else:
            self.mass_player.group_childs = set()
            self.mass_player.synced_to = self.sync_status.master
            self.mass_player.active_source = self.sync_status.master

        # self.mass_player.state = PLAYBACK_STATE_MAP[active_group.playback_state]

        # self.logger.debug(self.status.seconds)
        self.mass_player.state = PLAYBACK_STATE_MAP[self.status.state]
        self.mass_player.can_sync_with = (
            tuple(x for x in self.prov.bluos_players if x != self.player_id),
        )

        self.mass.players.update(self.player_id)


class BluesoundPlayerProvider(PlayerProvider):
    """Bluos compatible player provider, providing support for bluesound speakers."""

    bluos_players: dict[str, BluesoundPlayer]


    @property
    def supported_features(self) -> tuple[ProviderFeature, ...]:
        """Return the features supported by this Provider."""
        # MANDATORY
        # you should return a tuple of provider-level features
        # here that your player provider supports or an empty tuple if none.
        # for example 'ProviderFeature.SYNC_PLAYERS' if you can sync players.
        return (ProviderFeature.SYNC_PLAYERS,)

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self.bluos_players: dict[str, BluosPlayer] = {}

    async def on_mdns_service_state_change(
        self, name: str, state_change: ServiceStateChange, info: AsyncServiceInfo | None
    ) -> None:
        """Handle MDNS service state callback for BluOS."""
        name = name.split(".", 1)[0]
        self.player_id = info.decoded_properties["mac"]  # this is just an example!
        # handle removed player

        if state_change == ServiceStateChange.Removed:
            # check if the player manager has an existing entry for this player
            if mass_player := self.mass.players.get(self.player_id):
                # the player has become unavailable
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
                    mass_player.available = True
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
        # self.logger.debug(name)
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
            # set the supported features for this player only with
            # the ones the player actually supports
            supported_features=(
                # PlayerFeature.POWER,  # if the player can be turned on/off
                PlayerFeature.VOLUME_SET,
                PlayerFeature.VOLUME_MUTE,
                PlayerFeature.PLAY_ANNOUNCEMENT,  # see play_announcement method
                PlayerFeature.ENQUEUE_NEXT,  # see play_media/enqueue_next_media methods
                PlayerFeature.PAUSE,
                # PlayerFeature.SEEK,
            ),
            needs_poll=True,
            poll_interval=30,
        )
        self.mass.players.register(mass_player)

        # TODO sync
        # sync_status_result = await self.client.sync_status()
        # status_result = await self.client.status()

        await bluos_player.update_attributes()
        self.mass.players.update(self.player_id)

    async def get_player_config_entries(
        self,
        player_id: str,
    ) -> tuple[ConfigEntry, ...]:
        """Return Config Entries for the given player."""
        base_entries = await super().get_player_config_entries(self.player_id)
        if not self.bluos_players.get(self.player_id):
            # TODO fix player entries
            # if not (bluos_player := self.bluos_players.get(self.player_id)):
            #     # most probably a syncgroup
            return (*base_entries, CONF_ENTRY_CROSSFADE)
        return (
            *base_entries,
            CONF_ENTRY_HTTP_PROFILE_FORCED_2,
            CONF_ENTRY_CROSSFADE,
            CONF_ENTRY_CROSSFADE_FLOW_MODE_REQUIRED,
            CONF_ENTRY_ENFORCE_MP3,
            CONF_ENTRY_FLOW_MODE_DEFAULT_ENABLED,
            CONF_ENTRY_ENABLE_ICY_METADATA,
        )

    async def cmd_stop(self, player_id: str) -> None:
        """Send STOP command to BluOS player."""
        if bluos_player := self.bluos_players[player_id]:
            await bluos_player.client.stop()
            mass_player = self.mass.players.get(player_id)
            # Optimistic state, reduces interface lag
            mass_player.state = PLAYBACK_STATE_MAP["stop"]
            await bluos_player.update_attributes()
            # self.mass.players.update(player_id)

    async def cmd_play(self, player_id: str) -> None:
        """Send PLAY command to BluOS player."""
        if bluos_player := self.bluos_players[player_id]:
            await bluos_player.client.play()
            # Optimistic state, reduces interface lag
            mass_player = self.mass.players.get(player_id)
            mass_player.state = PLAYBACK_STATE_MAP["play"]
            await bluos_player.update_attributes()

    async def cmd_pause(self, player_id: str) -> None:
        """Send PAUSE command to BluOS player."""
        if bluos_player := self.bluos_players[player_id]:
            await bluos_player.client.pause()
            # Optimistic state, reduces interface lag
            mass_player = self.mass.players.get(player_id)
            mass_player.state = PLAYBACK_STATE_MAP["pause"]
            # self.mass.players.update(player_id)
            await bluos_player.update_attributes()

    async def cmd_volume_set(self, player_id: str, volume_level: int) -> None:
        """Send VOLUME_SET command to BluOS player."""
        # self.logger.debug(volume_level)
        if bluos_player := self.bluos_players[player_id]:
            await bluos_player.client.volume(level=volume_level)
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

    async def play_media(
        self, player_id: str, media: PlayerMedia, timeout: float | None = None
    ) -> None:
        """Handle PLAY MEDIA for BluOS player using the provided URL."""
        mass_player = self.mass.players.get(player_id)
        if bluos_player := self.bluos_players[player_id]:
            await bluos_player.client.play_url(media.uri, timeout=timeout)
            # Update media info then optimistically override playback state and source
            await bluos_player.update_attributes()
            mass_player.state = PLAYBACK_STATE_MAP["play"]
            mass_player.active_source = None
            self.mass.players.update(player_id)

        mass_player = self.mass.players.get(player_id)

        # Optionally, handle the playback_state or additional logic here
        if mass_player.state != "playing":
            raise PlayerCommandFailed("Failed to start playback.")

    async def play_announcement(
        self, player_id: str, announcement: PlayerMedia, volume_level: int | None = None
    ) -> None:
        """Send announcement to player."""
        if bluos_player := self.bluos_players[player_id]:
            await bluos_player.client.Input(announcement.uri, volume_level)

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
