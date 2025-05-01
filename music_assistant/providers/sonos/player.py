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

from aiohttp.client_exceptions import ClientConnectorError
from aiosonos.api.models import ContainerType, MusicService, SonosCapability
from aiosonos.client import SonosLocalApiClient
from aiosonos.const import EventType as SonosEventType
from aiosonos.const import SonosEvent
from aiosonos.exceptions import ConnectionFailed, FailedCommand
from music_assistant_models.enums import (
    EventType,
    PlayerFeature,
    PlayerState,
    PlayerType,
    RepeatMode,
)
from music_assistant_models.player import DeviceInfo, Player, PlayerMedia

from .const import (
    CONF_AIRPLAY_MODE,
    PLAYBACK_STATE_MAP,
    PLAYER_FEATURES_BASE,
    PLAYER_SOURCE_MAP,
    SOURCE_AIRPLAY,
    SOURCE_LINE_IN,
    SOURCE_RADIO,
    SOURCE_SPOTIFY,
    SOURCE_TV,
)

if TYPE_CHECKING:
    from aiosonos.api.models import DiscoveryInfo as SonosDiscoveryInfo
    from music_assistant_models.event import MassEvent

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
        self._on_cleanup_callbacks: list[Callable[[], None]] = []

    @property
    def airplay_mode_enabled(self) -> bool:
        """Return if airplay mode is enabled for the player."""
        return self.mass.config.get_raw_player_config_value(
            self.player_id, CONF_AIRPLAY_MODE, False
        )

    @property
    def airplay_mode_active(self) -> bool:
        """Return if airplay mode is active for the player."""
        return (
            self.airplay_mode_enabled
            and self.client.player.is_coordinator
            and (airplay_player := self.get_linked_airplay_player(False))
            and airplay_player.state in (PlayerState.PLAYING, PlayerState.PAUSED)
        )

    def get_linked_airplay_player(self, enabled_only: bool = True) -> Player | None:
        """Return the linked airplay player if available/enabled."""
        if enabled_only and not self.airplay_mode_enabled:
            return None
        if not (airplay_player := self.mass.players.get(self.airplay_player_id)):
            return None
        if not airplay_player.available:
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
            supported_features.add(PlayerFeature.VOLUME_MUTE)
        if not self.get_linked_airplay_player(False):
            supported_features.add(PlayerFeature.NEXT_PREVIOUS)

        # instantiate the MA player
        self.mass_player = mass_player = Player(
            player_id=self.player_id,
            provider=self.prov.instance_id,
            type=PlayerType.PLAYER,
            name=self.discovery_info["device"]["name"]
            or self.discovery_info["device"]["modelDisplayName"],
            available=True,
            device_info=DeviceInfo(
                model=self.discovery_info["device"]["modelDisplayName"],
                manufacturer=self.prov.manifest.name,
                ip_address=self.ip_address,
            ),
            supported_features=supported_features,
            # NOTE: strictly taken we can have multiple sonos households
            # but for now we assume we only have one
            can_group_with={self.prov.instance_id},
        )
        if SonosCapability.LINE_IN in self.discovery_info["device"]["capabilities"]:
            mass_player.source_list.append(PLAYER_SOURCE_MAP[SOURCE_LINE_IN])
        if SonosCapability.HT_PLAYBACK in self.discovery_info["device"]["capabilities"]:
            mass_player.source_list.append(PLAYER_SOURCE_MAP[SOURCE_TV])
        if SonosCapability.AIRPLAY in self.discovery_info["device"]["capabilities"]:
            mass_player.source_list.append(PLAYER_SOURCE_MAP[SOURCE_AIRPLAY])

        self.update_attributes()
        await self.mass.players.register_or_update(mass_player)

        # register callback for state changed
        self._on_cleanup_callbacks.append(
            self.client.subscribe(
                self.on_player_event,
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
        # note we don't filter on the player_id here because we also need to catch
        # events from group players
        self._on_cleanup_callbacks.append(
            self.mass.subscribe(
                self._on_mass_queue_items_event,
                EventType.QUEUE_ITEMS_UPDATED,
            )
        )
        self._on_cleanup_callbacks.append(
            self.mass.subscribe(
                self._on_mass_queue_event,
                (EventType.QUEUE_UPDATED, EventType.QUEUE_ITEMS_UPDATED),
            )
        )

    async def unload(self, is_removed: bool = False) -> None:
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
        if (airplay := self.get_linked_airplay_player(True)) and self.airplay_mode_active:
            # linked airplay player is active, redirect the command
            self.logger.debug("Redirecting STOP command to linked airplay player.")
            if player_provider := self.mass.get_provider(airplay.provider):
                await player_provider.cmd_stop(airplay.player_id)
            return
        await self.client.player.group.stop()

    async def cmd_play(self) -> None:
        """Send PLAY command to given player."""
        if self.client.player.is_passive:
            self.logger.debug("Ignore STOP command: Player is synced to another player.")
            return
        if airplay := self.get_linked_airplay_player(True):
            # linked airplay player is active, redirect the command
            self.logger.debug("Redirecting PLAY command to linked airplay player.")
            if player_provider := self.mass.get_provider(airplay.provider):
                await player_provider.cmd_play(airplay.player_id)
            return
        await self.client.player.group.play()

    async def cmd_pause(self) -> None:
        """Send PAUSE command to given player."""
        if self.client.player.is_passive:
            self.logger.debug("Ignore STOP command: Player is synced to another player.")
            return
        if (airplay := self.get_linked_airplay_player(True)) and self.airplay_mode_active:
            # linked airplay player is active, redirect the command
            self.logger.debug("Redirecting PAUSE command to linked airplay player.")
            if player_provider := self.mass.get_provider(airplay.provider):
                await player_provider.cmd_pause(airplay.player_id)
            return
        active_source = self.mass_player.active_source
        if self.mass.player_queues.get(active_source):
            # Sonos seems to be bugged when playing our queue tracks and we send pause,
            # it can't resume the current track and simply aborts/skips it
            # so we stop the player instead.
            # https://github.com/music-assistant/support/issues/3758
            # TODO: revisit this later once we implemented support for range requests
            # as I have the feeling the pause issue is related to seek support (=range requests)
            await self.cmd_stop()
            return
        if not self.client.player.group.playback_actions.can_pause:
            await self.cmd_stop()
            return
        await self.client.player.group.pause()

    async def cmd_seek(self, position: int) -> None:
        """Handle SEEK command for given player.

        - position: position in seconds to seek to in the current playing item.
        """
        if self.client.player.is_passive:
            self.logger.debug("Ignore STOP command: Player is synced to another player.")
            return
        await self.client.player.group.seek(position)

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

    async def select_source(self, source: str) -> None:
        """Handle SELECT SOURCE command on given player."""
        if source == SOURCE_LINE_IN:
            await self.client.player.group.load_line_in(play_on_completion=True)
        elif source == SOURCE_TV:
            await self.client.player.load_home_theater_playback()
        else:
            # unsupported source - try to clear the queue/player
            await self.cmd_stop()

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
        airplay_player = self.get_linked_airplay_player(False)
        if self.client.player.is_coordinator:
            # player is group coordinator
            active_group = self.client.player.group
            if len(self.client.player.group_members) > 1:
                self.mass_player.group_childs.set(self.client.player.group_members)
            else:
                self.mass_player.group_childs.clear()
            # append airplay child's to group childs
            if self.airplay_mode_enabled and airplay_player:
                airplay_childs = [
                    x for x in airplay_player.group_childs if x != airplay_player.player_id
                ]
                self.mass_player.group_childs.extend(airplay_childs)
                airplay_prov = self.mass.get_provider(airplay_player.provider)
                self.mass_player.can_group_with.update(
                    x.player_id
                    for x in airplay_prov.players
                    if x.player_id != airplay_player.player_id
                )
            else:
                self.mass_player.can_group_with = {self.prov.instance_id}
            self.mass_player.synced_to = None
        else:
            # player is group child (synced to another player)
            group_parent = self.prov.sonos_players.get(self.client.player.group.coordinator_id)
            if not group_parent or not group_parent.client or not group_parent.client.player:
                # handle race condition where the group parent is not yet discovered
                return
            active_group = group_parent.client.player.group
            self.mass_player.group_childs.clear()
            self.mass_player.synced_to = active_group.coordinator_id
            self.mass_player.active_source = active_group.coordinator_id

        # map playback state
        self.mass_player.state = PLAYBACK_STATE_MAP[active_group.playback_state]
        self.mass_player.elapsed_time = active_group.position

        # figure out the active source based on the container
        container_type = active_group.container_type
        active_service = active_group.active_service
        container = active_group.playback_metadata.get("container")
        if container_type == ContainerType.LINEIN:
            self.mass_player.active_source = SOURCE_LINE_IN
        elif container_type in (ContainerType.HOME_THEATER_HDMI, ContainerType.HOME_THEATER_SPDIF):
            self.mass_player.active_source = SOURCE_TV
        elif container_type == ContainerType.AIRPLAY:
            # check if the MA airplay player is active
            if airplay_player and airplay_player.state in (
                PlayerState.PLAYING,
                PlayerState.PAUSED,
            ):
                self.mass_player.state = airplay_player.state
                self.mass_player.active_source = airplay_player.active_source
                self.mass_player.elapsed_time = airplay_player.elapsed_time
                self.mass_player.elapsed_time_last_updated = (
                    airplay_player.elapsed_time_last_updated
                )
                self.mass_player.current_media = airplay_player.current_media
                # return early as we dont need further info
                return
            else:
                self.mass_player.active_source = SOURCE_AIRPLAY
        elif container_type == ContainerType.STATION:
            self.mass_player.active_source = SOURCE_RADIO
            # add radio to source list if not yet there
            if SOURCE_RADIO not in [x.id for x in self.mass_player.source_list]:
                self.mass_player.source_list.append(PLAYER_SOURCE_MAP[SOURCE_RADIO])
        elif active_service == MusicService.SPOTIFY:
            self.mass_player.active_source = SOURCE_SPOTIFY
            # add spotify to source list if not yet there
            if SOURCE_SPOTIFY not in [x.id for x in self.mass_player.source_list]:
                self.mass_player.source_list.append(PLAYER_SOURCE_MAP[SOURCE_SPOTIFY])
        elif active_service == MusicService.MUSIC_ASSISTANT:
            if self.client.player.is_coordinator:
                self.mass_player.active_source = self.mass_player.player_id
            elif object_id := container.get("id", {}).get("objectId"):
                self.mass_player.active_source = object_id.split(":")[-1]
            else:
                self.mass_player.active_source = None
        # its playing some service we did not yet map
        elif container and container.get("service", {}).get("name"):
            self.mass_player.active_source = container["service"]["name"]
        elif container and container.get("name"):
            self.mass_player.active_source = container["name"]
        elif active_service:
            self.mass_player.active_source = active_service
        elif container_type:
            self.mass_player.active_source = container_type
        else:
            # the player has nothing loaded at all (empty queue and no service active)
            self.mass_player.active_source = None

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
            if not retry_on_fail or not self.mass_player:
                raise
            self.mass_player.available = False
            self.mass.players.update(self.player_id)
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

        self._listen_task = self.mass.create_task(_listener())
        await init_ready.wait()

    async def _disconnect(self) -> None:
        """Disconnect the client and cleanup."""
        self.connected = False
        if self._listen_task and not self._listen_task.done():
            self._listen_task.cancel()
        if self.client:
            await self.client.disconnect()
        self.logger.debug("Disconnected from player API")

    def on_player_event(self, event: SonosEvent | None) -> None:
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
        if not self.client.player.is_coordinator:
            return
        if event.event == EventType.QUEUE_UPDATED:
            # sync crossfade and repeat modes
            await self.sync_play_modes(event.object_id)
        elif event.event == EventType.QUEUE_ITEMS_UPDATED:
            # refresh cloud queue
            if session_id := self.client.player.group.active_session_id:
                await self.client.api.playback_session.refresh_cloud_queue(session_id)

    async def sync_play_modes(self, queue_id: str) -> None:
        """Sync the play modes between MA and Sonos."""
        queue = self.mass.player_queues.get(queue_id)
        if not queue or queue.state not in (PlayerState.PLAYING, PlayerState.PAUSED):
            return
        repeat_single_enabled = queue.repeat_mode == RepeatMode.ONE
        repeat_all_enabled = queue.repeat_mode == RepeatMode.ALL
        play_modes = self.client.player.group.play_modes
        if (
            play_modes.repeat != repeat_all_enabled
            or play_modes.repeat_one != repeat_single_enabled
        ):
            try:
                await self.client.player.group.set_play_modes(
                    repeat=repeat_all_enabled,
                    repeat_one=repeat_single_enabled,
                )
            except FailedCommand as err:
                if "groupCoordinatorChanged" not in str(err):
                    # this may happen at race conditions
                    raise
