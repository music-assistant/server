"""Bluesound Player Provider for BluOS players to work with Music Assistant."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, TypedDict

from music_assistant_models.enums import PlayerFeature, PlayerState, PlayerType, ProviderFeature
from music_assistant_models.errors import PlayerCommandFailed
from music_assistant_models.player import DeviceInfo, Player, PlayerMedia
from pyblu import Player as BluosPlayer
from pyblu import Status, SyncStatus
from zeroconf import ServiceStateChange

from music_assistant.constants import (
    CONF_ENTRY_ENABLE_ICY_METADATA,
    CONF_ENTRY_FLOW_MODE_ENFORCED,
    CONF_ENTRY_HTTP_PROFILE_FORCED_2,
    CONF_ENTRY_OUTPUT_CODEC,
)
from music_assistant.helpers.util import (
    get_port_from_zeroconf,
    get_primary_ip_address_from_zeroconf,
)
from music_assistant.models.player_provider import PlayerProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry, ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest
    from zeroconf.asyncio import AsyncServiceInfo

    from music_assistant import MusicAssistant
    from music_assistant.models import ProviderInstanceType


PLAYER_FEATURES_BASE = {
    PlayerFeature.SET_MEMBERS,
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

SOURCE_UNKNOWN = "unknown"
POLL_STATE_STATIC = "static"
POLL_STATE_DYNAMIC = "dynamic"


SOURCE_MAP = {
    "input0": "line_in",
    "Airplay": "airplay",
    "Spotify": "spotify",
    "RadioParadise": "radio",
}


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

        if not self.mass_player:
            return

        self.sync_status = await self.client.sync_status()
        self.logger.debug(self.sync_status)
        self.status = await self.client.status()

        # Update timing
        self.mass_player.elapsed_time = self.status.seconds
        self.mass_player.elapsed_time_last_updated = time.time()

        # Check volume
        volume = 100 if self.sync_status.volume == -1 else self.sync_status.volume
        self.mass_player.volume_level = volume

        # Check if mute is toggled
        self.mass_player.volume_muted = self.status.mute

        if (
            self.poll_state == POLL_STATE_DYNAMIC and self.dynamic_poll_count <= 0
        ) or self.mass_player.state == PLAYBACK_STATE_POLL_MAP[self.status.state]:
            self.logger.debug("Changing bluos poll state from %s to static", self.poll_state)
            self.poll_state = POLL_STATE_STATIC
            self.mass_player.poll_interval = 30
            self.mass.players.update(self.player_id)

        if self.status.state == "stream":
            source = SOURCE_MAP.get(self.status.input_id)
            if (
                not source
                and self.mass.streams.base_url not in self.status.stream_url
                and self.sync_status.leader
            ):
                source = SOURCE_UNKNOWN
            self.mass_player.active_source = source
        if self.sync_status.leader is None:
            if self.sync_status.followers:
                self.logger.debug("followers %s", self.sync_status.followers)
                self.followers = self.sync_status.followers
                if len(self.sync_status.followers) >= 1:
                    child_player_ids = [
                        player.player_id
                        for follower in self.followers
                        if (player := self.mass.players.get_by_ip(follower.ip)) is not None
                    ]
                    self.mass_player.group_childs.set(child_player_ids)
                    self.logger.debug("Children %s", self.mass_player.group_childs)
                else:
                    self.mass_player.group_childs.clear()
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
        elif self.sync_status.leader is not None:
            # self.mass_player.group_childs.clear()
            self.statusleader = self.sync_status.leader
            statusleadermass = self.mass.players.get_by_ip(self.statusleader.ip)
            self.logger.debug("synced to %s", statusleadermass.player_id)
            self.logger.debug(self.mass_player)
            self.leader = self.mass.players.get_by_ip(self.sync_status.leader.ip).player_id

            self.mass_player.active_source = self.mass.players.get_by_ip(
                self.statusleader.ip
            ).player_id

        self.mass_player.state = PLAYBACK_STATE_MAP[self.status.state]
        self.mass.players.update(self.player_id)


class BluesoundPlayerProvider(PlayerProvider):
    """Bluos compatible player provider, providing support for bluesound speakers."""

    bluos_players: dict[str, BluesoundPlayer]

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return the features supported by this Provider."""
        return {ProviderFeature.SYNC_PLAYERS}

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self.bluos_players: dict[str, BluesoundPlayer] = {}

    async def on_mdns_service_state_change(
        self, name: str, state_change: ServiceStateChange, info: AsyncServiceInfo | None
    ) -> None:
        """Handle MDNS service state callback for BluOS."""
        name = name.split(".", 1)[0]
        if info:
            self.player_id = info.decoded_properties["mac"]
        # Handle removed player

        if state_change == ServiceStateChange.Removed:
            # Check if the player manager has an existing entry for this player
            if mass_player := self.mass.players.get(self.player_id):
                # The player has become unavailable
                self.logger.debug("Player offline: %s", mass_player.display_name)
                # mass_player.available = False
                self.mass.players.update(self.player_id)
            return

        if bluos_player := self.bluos_players.get(self.player_id):
            if mass_player := self.mass.players.get(self.player_id):
                cur_address = get_primary_ip_address_from_zeroconf(info)
                cur_port = get_port_from_zeroconf(info)
                if cur_address and cur_address != mass_player.device_info.ip_address:
                    self.logger.debug(
                        "Address updated to %s for player %s", cur_address, mass_player.display_name
                    )
                    bluos_player.ip_address = cur_address
                    bluos_player.port = cur_port
                    mass_player.device_info = DeviceInfo(
                        model=mass_player.device_info.model,
                        manufacturer=mass_player.device_info.manufacturer,
                        ip_address=str(cur_address),
                    )
                if not mass_player.available:
                    self.logger.debug("Player back online: %s", mass_player.display_name)
                    # not sure
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
            device_info=DeviceInfo(
                model="BluOS speaker",
                manufacturer="Bluesound",
                ip_address=cur_address,
            ),
            # Set the supported features for this player
            supported_features={
                PlayerFeature.VOLUME_SET,
                PlayerFeature.VOLUME_MUTE,
                PlayerFeature.PAUSE,
                PlayerFeature.SET_MEMBERS,
            },
            needs_poll=True,
            poll_interval=30,
            can_group_with={self.instance_id},
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
            return (*base_entries,)
        return (
            *base_entries,
            CONF_ENTRY_HTTP_PROFILE_FORCED_2,
            CONF_ENTRY_OUTPUT_CODEC,
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

    # TODO fix sync & ungroup

    async def cmd_group(self, player_id: str, target_player: str) -> None:
        """Handle GROUP command for BluOS player."""
        if bluos_player := self.bluos_players[player_id]:
            await bluos_player.client.add_follower(
                self.bluos_players[target_player].ip_address,
                self.bluos_players[target_player].port,
            )
            # mass_player.synced_to = target_player
            # mass_target_player.group_childs.append(player_id)
            # self.mass.players.update(player_id)
            # self.mass.players.update(target_player)
            await bluos_player.update_attributes()

    async def cmd_ungroup(self, player_id: str) -> None:
        """Handle UNGROUP command for BluOS player."""
        self.logger.debug("Ungrouping player")
        if bluos_player := self.bluos_players[player_id]:
            play_state = await bluos_player.client.play(timeout=1)
            if play_state == "stream":
                bluos_player.poll_state = POLL_STATE_DYNAMIC
                bluos_player.dynamic_poll_count = 6
                bluos_player.mass_player.poll_interval = 0.5
            # Optimistic state, reduces interface lag
        # mass_player = self.mass.players.get(player_id)
        # if not mass_player or not mass_player.synced_to:
        #     self.logger.warning("Cannot ungroup: player %s is not part of a group", player_id)
        #     return

        # leader_id = mass_player.synced_to
        # bluos_follower = self.bluos_players.get(player_id)

        # self.logger.debug("Ungrouping player %s from leader %s", player_id, leader_id)
        # if bluos_leader := self.bluos_players[leader_id]:
        #     await bluos_leader.client.remove_follower(
        #         bluos_follower.ip_address, bluos_follower.port
        #     )
        #     await bluos_leader.update_attributes()
