"""
Sonos Player provider for Music Assistant for speakers running the S2 firmware.

Based on the aiosonos library, which leverages the new websockets API of the Sonos S2 firmware.
https://github.com/music-assistant/aiosonos

SonosPlayer: Holds the details of the (discovered) Sonosplayer.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

import shortuuid
from aiohttp.client_exceptions import ClientConnectorError
from aiosonos.api.models import ContainerType, MusicService, SonosCapability
from aiosonos.api.models import PlayBackState as SonosPlayBackState
from aiosonos.client import SonosLocalApiClient
from aiosonos.const import EventType as SonosEventType
from aiosonos.const import SonosEvent
from aiosonos.exceptions import ConnectionFailed, FailedCommand

from music_assistant.common.models.enums import (
    EventType,
    PlayerFeature,
    PlayerState,
    PlayerType,
    RepeatMode,
)
from music_assistant.common.models.event import MassEvent
from music_assistant.common.models.player import DeviceInfo, Player, PlayerMedia
from music_assistant.constants import CONF_CROSSFADE

from .const import (
    CONF_AIRPLAY_MODE,
    PLAYBACK_STATE_MAP,
    PLAYER_FEATURES_BASE,
    SOURCE_AIRPLAY,
    SOURCE_LINE_IN,
    SOURCE_RADIO,
    SOURCE_SPOTIFY,
)

if TYPE_CHECKING:
    from aiosonos.api.models import DiscoveryInfo as SonosDiscoveryInfo

    from .provider import SonosPlayerProvider


class SonosPlayer:
    """Holds the details of the (discovered) Sonosplayer."""

    def __init__(
        self,
        prov: SonosPlayerProvider,
        player_id: str,
        discovery_info: SonosDiscoveryInfo,
        ip_address: str,
    ) -> None:
        """Initialize the SonosPlayer."""
        self.prov = prov
        self.mass = prov.mass
        self.player_id = player_id
        self.discovery_info = discovery_info
        self.ip_address = ip_address
        self.logger = prov.logger.getChild(player_id)
        self.connected: bool = False
        self.client = SonosLocalApiClient(self.ip_address, self.mass.http_session)
        self.mass_player: Player | None = None
        self._listen_task: asyncio.Task | None = None
        # Sonos speakers can optionally have airplay (most S2 speakers do)
        # and this airplay player can also be a player within MA.
        # We can do some smart stuff if we link them together where possible.
        # The player we can just guess from the sonos player id (mac address).
        self.airplay_player_id = f"ap{self.player_id[7:-5].lower()}"
        self.queue_version: str = shortuuid.random(8)
        self._on_cleanup_callbacks: list[Callable[[], None]] = []

    def get_linked_airplay_player(
        self, enabled_only: bool = True, active_only: bool = False
    ) -> Player | None:
        """Return the linked airplay player if available/enabled."""
        if enabled_only and not self.mass.config.get_raw_player_config_value(
            self.player_id, CONF_AIRPLAY_MODE
        ):
            return None
        if not (airplay_player := self.mass.players.get(self.airplay_player_id)):
            return None
        if not airplay_player.available:
            return None
        if active_only and not airplay_player.powered:
            return None
        return airplay_player

    async def setup(self) -> None:
        """Handle setup of the player."""
        # connect the player first so we can fail early
        await self._connect(False)

        # collect supported features
        supported_features = set(PLAYER_FEATURES_BASE)
        if SonosCapability.AUDIO_CLIP in self.discovery_info["device"]["capabilities"]:
            supported_features.add(PlayerFeature.PLAY_ANNOUNCEMENT)
        if not self.client.player.has_fixed_volume:
            supported_features.add(PlayerFeature.VOLUME_SET)

        # instantiate the MA player
        self.mass_player = mass_player = Player(
            player_id=self.player_id,
            provider=self.prov.instance_id,
            type=PlayerType.PLAYER,
            name=self.discovery_info["device"]["name"]
            or self.discovery_info["device"]["modelDisplayName"],
            available=True,
            # treat as powered at start if the player is playing/paused
            powered=self.client.player.group.playback_state
            in (
                SonosPlayBackState.PLAYBACK_STATE_PLAYING,
                SonosPlayBackState.PLAYBACK_STATE_BUFFERING,
                SonosPlayBackState.PLAYBACK_STATE_PAUSED,
            ),
            device_info=DeviceInfo(
                model=self.discovery_info["device"]["modelDisplayName"],
                manufacturer=self.prov.manifest.name,
                address=self.ip_address,
            ),
            supported_features=tuple(supported_features),
        )
        self.update_attributes()
        await self.mass.players.register_or_update(mass_player)

        # register callback for state changed
        self._on_cleanup_callbacks.append(
            self.client.subscribe(
                self._on_player_event,
                (
                    SonosEventType.GROUP_UPDATED,
                    SonosEventType.PLAYER_UPDATED,
                ),
            )
        )
        # register callback for airplay player state changes
        self._on_cleanup_callbacks.append(
            self.mass.subscribe(
                self._on_airplay_player_event,
                (EventType.PLAYER_UPDATED, EventType.PLAYER_ADDED),
                self.airplay_player_id,
            )
        )
        # register callback for playerqueue state changes
        self._on_cleanup_callbacks.append(
            self.mass.subscribe(
                self._on_mass_queue_items_event,
                EventType.QUEUE_ITEMS_UPDATED,
                self.player_id,
            )
        )

    async def unload(self) -> None:
        """Unload the player (disconnect + cleanup)."""
        await self._disconnect()
        self.mass.players.remove(self.player_id, False)
        for callback in self._on_cleanup_callbacks:
            callback()

    def reconnect(self, delay: float = 1) -> None:
        """Reconnect the player."""
        # use a task_id to prevent multiple reconnects
        task_id = f"sonos_reconnect_{self.player_id}"
        self.mass.call_later(delay, self._connect, delay, task_id=task_id)

    async def cmd_stop(self) -> None:
        """Send STOP command to given player."""
        if self.client.player.is_passive:
            self.logger.debug("Ignore STOP command: Player is synced to another player.")
            return
        if (
            airplay := self.get_linked_airplay_player(True, True)
        ) and airplay.state != PlayerState.IDLE:
            # linked airplay player is active, redirect the command
            self.logger.debug("Redirecting STOP command to linked airplay player.")
            await self.mass.players.cmd_stop(airplay.player_id)
            return
        try:
            await self.client.player.group.stop()
        except FailedCommand as err:
            if "ERROR_PLAYBACK_NO_CONTENT" not in str(err):
                raise

    async def cmd_play(self) -> None:
        """Send PLAY command to given player."""
        if self.client.player.is_passive:
            self.logger.debug("Ignore STOP command: Player is synced to another player.")
            return
        if (
            airplay := self.get_linked_airplay_player(True, True)
        ) and airplay.state != PlayerState.IDLE:
            # linked airplay player is active, redirect the command
            self.logger.debug("Redirecting PLAY command to linked airplay player.")
            await self.mass.players.cmd_play(airplay.player_id)
            return
        await self.client.player.group.play()

    async def cmd_pause(self) -> None:
        """Send PAUSE command to given player."""
        if self.client.player.is_passive:
            self.logger.debug("Ignore STOP command: Player is synced to another player.")
            return
        if (
            airplay := self.get_linked_airplay_player(True, True)
        ) and airplay.state != PlayerState.IDLE:
            # linked airplay player is active, redirect the command
            self.logger.debug("Redirecting PAUSE command to linked airplay player.")
            await self.mass.players.cmd_pause(airplay.player_id)
            return
        await self.client.player.group.pause()

    async def cmd_volume_set(self, volume_level: int) -> None:
        """Send VOLUME_SET command to given player."""
        await self.client.player.set_volume(volume_level)
        # sync volume level with airplay player
        if airplay := self.get_linked_airplay_player(False):
            if airplay.state not in (PlayerState.PLAYING, PlayerState.PAUSED):
                airplay.volume_level = volume_level

    async def cmd_volume_mute(self, muted: bool) -> None:
        """Send VOLUME MUTE command to given player."""
        await self.client.player.set_volume(muted=muted)

    def update_attributes(self) -> None:  # noqa: PLR0915
        """Update the player attributes."""
        if not self.mass_player:
            return
        self.mass_player.available = self.connected
        if not self.connected:
            return
        if self.client.player.has_fixed_volume:
            self.mass_player.volume_level = 100
        else:
            self.mass_player.volume_level = self.client.player.volume_level or 0
        self.mass_player.volume_muted = self.client.player.volume_muted

        group_parent = None
        if self.client.player.is_coordinator:
            # player is group coordinator
            active_group = self.client.player.group
            self.mass_player.group_childs = (
                set(self.client.player.group_members)
                if len(self.client.player.group_members) > 1
                else set()
            )
            self.mass_player.synced_to = None
        else:
            # player is group child (synced to another player)
            group_parent = self.prov.sonos_players.get(self.client.player.group.coordinator_id)
            if not group_parent or not group_parent.client or not group_parent.client.player:
                # handle race condition where the group parent is not yet discovered
                return
            active_group = group_parent.client.player.group
            self.mass_player.group_childs = set()
            self.mass_player.synced_to = active_group.coordinator_id
            self.mass_player.active_source = active_group.coordinator_id

        if airplay := self.get_linked_airplay_player(True):
            # linked airplay player is active, update media from there
            self.mass_player.state = airplay.state
            self.mass_player.powered = airplay.powered
            self.mass_player.active_source = airplay.active_source
            self.mass_player.elapsed_time = airplay.elapsed_time
            self.mass_player.elapsed_time_last_updated = airplay.elapsed_time_last_updated
            # mark 'next_previous' feature as unsupported when airplay mode is active
            if PlayerFeature.NEXT_PREVIOUS in self.mass_player.supported_features:
                self.mass_player.supported_features = (
                    x
                    for x in self.mass_player.supported_features
                    if x != PlayerFeature.NEXT_PREVIOUS
                )
            return
        # ensure 'next_previous' feature is supported when airplay mode is not active
        if PlayerFeature.NEXT_PREVIOUS not in self.mass_player.supported_features:
            self.mass_player.supported_features = (
                *self.mass_player.supported_features,
                PlayerFeature.NEXT_PREVIOUS,
            )

        # map playback state
        self.mass_player.state = PLAYBACK_STATE_MAP[active_group.playback_state]
        self.mass_player.elapsed_time = active_group.position

        # figure out the active source based on the container
        container_type = active_group.container_type
        active_service = active_group.active_service
        container = active_group.playback_metadata.get("container")
        if container_type == ContainerType.LINEIN:
            self.mass_player.active_source = SOURCE_LINE_IN
        elif container_type == ContainerType.AIRPLAY:
            # check if the MA airplay player is active
            airplay_player = self.mass.players.get(self.airplay_player_id)
            if airplay_player and airplay_player.state in (
                PlayerState.PLAYING,
                PlayerState.PAUSED,
            ):
                self.mass_player.active_source = airplay_player.active_source
            else:
                self.mass_player.active_source = SOURCE_AIRPLAY
        elif container_type == ContainerType.STATION:
            self.mass_player.active_source = SOURCE_RADIO
        elif active_service == MusicService.SPOTIFY:
            self.mass_player.active_source = SOURCE_SPOTIFY
        elif active_service == MusicService.MUSIC_ASSISTANT:
            if object_id := container.get("id", {}).get("objectId"):
                self.mass_player.active_source = object_id.split(":")[-1]
        else:
            # its playing some service we did not yet map
            self.mass_player.active_source = active_service

        # sonos has this weirdness that it maps idle to paused
        # which is annoying to figure out if we want to resume or let
        # MA back in control again. So for now, we just map it to idle here.
        if (
            self.mass_player.state == PlayerState.PAUSED
            and active_service != MusicService.MUSIC_ASSISTANT
        ):
            self.mass_player.state = PlayerState.IDLE

        # parse current media
        self.mass_player.elapsed_time = self.client.player.group.position
        self.mass_player.elapsed_time_last_updated = time.time()
        current_media = None
        if (current_item := active_group.playback_metadata.get("currentItem")) and (
            (track := current_item.get("track")) and track.get("name")
        ):
            track_images = track.get("images", [])
            track_image_url = track_images[0].get("url") if track_images else None
            track_duration_millis = track.get("durationMillis")
            current_media = PlayerMedia(
                uri=track.get("id", {}).get("objectId") or track.get("mediaUrl"),
                title=track["name"],
                artist=track.get("artist", {}).get("name"),
                album=track.get("album", {}).get("name"),
                duration=track_duration_millis / 1000 if track_duration_millis else None,
                image_url=track_image_url,
            )
            if active_service == MusicService.MUSIC_ASSISTANT:
                current_media.queue_id = self.mass_player.active_source
                current_media.queue_item_id = current_item["id"]
        # radio stream info
        if container and container.get("name") and active_group.playback_metadata.get("streamInfo"):
            images = container.get("images", [])
            image_url = images[0].get("url") if images else None
            current_media = PlayerMedia(
                uri=container.get("id", {}).get("objectId"),
                title=active_group.playback_metadata["streamInfo"],
                album=container["name"],
                image_url=image_url,
            )
        # generic info from container (also when MA is playing!)
        if container and container.get("name") and container.get("id"):
            if not current_media:
                current_media = PlayerMedia(container["id"]["objectId"])
            if not current_media.image_url:
                images = container.get("images", [])
                current_media.image_url = images[0].get("url") if images else None
            if not current_media.title:
                current_media.title = container["name"]
            if not current_media.uri:
                current_media.uri = container["id"]["objectId"]

        self.mass_player.current_media = current_media

    async def _connect(self, retry_on_fail: int = 0) -> None:
        """Connect to the Sonos player."""
        if self._listen_task and not self._listen_task.done():
            self.logger.debug("Already connected to Sonos player: %s", self.player_id)
            return
        try:
            await self.client.connect()
        except (ConnectionFailed, ClientConnectorError) as err:
            self.logger.warning("Failed to connect to Sonos player: %s", err)
            self.mass_player.available = False
            self.mass.players.update(self.player_id)
            if not retry_on_fail:
                raise
            self.reconnect(min(retry_on_fail + 30, 3600))
            return
        self.connected = True
        self.logger.debug("Connected to player API")
        init_ready = asyncio.Event()

        async def _listener() -> None:
            try:
                await self.client.start_listening(init_ready)
            except Exception as err:
                if not isinstance(err, ConnectionFailed | asyncio.CancelledError):
                    self.logger.exception("Error in Sonos player listener: %s", err)
            finally:
                self.logger.info("Disconnected from player API")
                if self.connected:
                    # we didn't explicitly disconnect, try to reconnect
                    # this should simply try to reconnect once and if that fails
                    # we rely on mdns to pick it up again later
                    await self._disconnect()
                    self.mass_player.available = False
                    self.mass.players.update(self.player_id)
                    self.reconnect(5)

        self._listen_task = asyncio.create_task(_listener())
        await init_ready.wait()

    async def _disconnect(self) -> None:
        """Disconnect the client and cleanup."""
        self.connected = False
        if self._listen_task and not self._listen_task.done():
            self._listen_task.cancel()
        if self.client:
            await self.client.disconnect()
        self.logger.debug("Disconnected from player API")

    def _on_player_event(self, event: SonosEvent) -> None:
        """Handle incoming event from player."""
        self.update_attributes()
        self.mass.players.update(self.player_id)

    def _on_airplay_player_event(self, event: MassEvent) -> None:
        """Handle incoming event from linked airplay player."""
        if not self.mass.config.get_raw_player_config_value(self.player_id, CONF_AIRPLAY_MODE):
            return
        if event.object_id != self.airplay_player_id:
            return
        self.update_attributes()
        self.mass.players.update(self.player_id)

    async def _on_mass_queue_items_event(self, event: MassEvent) -> None:
        """Handle incoming event from linked MA playerqueue."""
        # If the queue items changed and we have an active sonos queue,
        # we need to inform the sonos queue to refresh the items.
        if self.mass_player.active_source != event.object_id:
            return
        if not self.connected:
            return
        queue = self.mass.player_queues.get(event.object_id)
        if not queue or queue.state not in (PlayerState.PLAYING, PlayerState.PAUSED):
            return
        if session_id := self.client.player.group.active_session_id:
            await self.client.api.playback_session.refresh_cloud_queue(session_id)

    async def _on_mass_queue_event(self, event: MassEvent) -> None:
        """Handle incoming event from linked MA playerqueue."""
        if self.mass_player.active_source != event.object_id:
            return
        if not self.connected:
            return
        # sync crossfade and repeat modes
        queue = self.mass.player_queues.get(event.object_id)
        if not queue or queue.state not in (PlayerState.PLAYING, PlayerState.PAUSED):
            return
        crossfade = await self.mass.config.get_player_config_value(queue.queue_id, CONF_CROSSFADE)
        repeat_single_enabled = queue.repeat_mode == RepeatMode.ONE
        repeat_all_enabled = queue.repeat_mode == RepeatMode.ALL
        play_modes = self.client.player.group.play_modes
        if (
            play_modes.crossfade != crossfade
            or play_modes.repeat != repeat_all_enabled
            or play_modes.repeat_one != repeat_single_enabled
        ):
            await self.client.player.group.set_play_modes(
                crossfade=crossfade, repeat=repeat_all_enabled, repeat_one=repeat_single_enabled
            )
