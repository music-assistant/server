from collections.abc import Callable
from typing import TYPE_CHECKING
import asyncio
from music_assistant_models.enums import EventType, PlaybackState, PlayerFeature, PlayerType
from music_assistant_models.player import DeviceInfo, PlayerMedia
from music_assistant.models.player import Player
from music_assistant.providers.sonos.const import CONF_AIRPLAY_MODE, PLAYER_SOURCE_MAP, SOURCE_AIRPLAY, SOURCE_LINE_IN, SOURCE_TV
from music_assistant.providers.sonos.provider import SonosPlayerProvider
from aiosonos.api.models import ContainerType, MusicService, SonosCapability
from aiosonos.client import SonosLocalApiClient
from aiosonos.const import EventType as SonosEventType
from aiosonos.const import SonosEvent
from aiosonos.exceptions import ConnectionFailed, FailedCommand

if TYPE_CHECKING:
    from aiosonos.api.models import DiscoveryInfo as SonosDiscoveryInfo
    from music_assistant_models.event import MassEvent

    from .provider import SonosPlayerProvider

SUPPORTED_FEATURES = {
    PlayerFeature.VOLUME_SET,
    PlayerFeature.VOLUME_MUTE,
    PlayerFeature.PAUSE,
    PlayerFeature.NEXT_PREVIOUS,
    PlayerFeature.SEEK,
    # PlayerFeature.PLAY_ANNOUNCEMENT,
    PlayerFeature.SELECT_SOURCE,
    PlayerFeature.NEXT_PREVIOUS,
    PlayerFeature.SELECT_SOURCE
}
class SonosPlayer(Player):
    
    def __init__(
        self,
        prov: SonosPlayerProvider,
        player_id: str,
        discovery_info: SonosDiscoveryInfo,
        ip_address: str,
    ) -> None:
        """Initialize the SonosPlayer."""
        # self.prov = prov
        # self.mass = prov.mass
        # self.player_id = player_id
        super().__init__(prov, player_id)
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
        self._attr_supported_features

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
            and airplay_player.state in (PlaybackState.PLAYING, PlaybackState.PAUSED)
        )

    async def setup(self) -> None:
        """Handle setup of the player."""
        # connect the player first so we can fail early
        await self._connect(False)

        # collect supported features
        if SonosCapability.AUDIO_CLIP in self.discovery_info["device"]["capabilities"]:
            self._attr_supported_features.add(PlayerFeature.PLAY_ANNOUNCEMENT)
        if not self.client.player.has_fixed_volume:
            self._attr_supported_features.add(PlayerFeature.VOLUME_SET)
            self._attr_supported_features.add(PlayerFeature.VOLUME_MUTE)
        if not self.get_linked_airplay_player(False):
            self._attr_supported_features.add(PlayerFeature.NEXT_PREVIOUS)

        self._attr_name = self.discovery_info["device"]["name"] or self.discovery_info["device"]["modelDisplayName"]
        self._attr_device_info = DeviceInfo(
                model=self.discovery_info["device"]["modelDisplayName"],
                manufacturer=self._provider.manifest.name,
                ip_address=self.ip_address,
            ),
        self._attr_can_group_with = {self._provider.instance_id}

        if SonosCapability.LINE_IN in self.discovery_info["device"]["capabilities"]:
            self._attr_source_list.append(PLAYER_SOURCE_MAP[SOURCE_LINE_IN])
        if SonosCapability.HT_PLAYBACK in self.discovery_info["device"]["capabilities"]:
            self._attr_source_list.append(PLAYER_SOURCE_MAP[SOURCE_TV])
        if SonosCapability.AIRPLAY in self.discovery_info["device"]["capabilities"]:
            self._attr_source_list.append(PLAYER_SOURCE_MAP[SOURCE_AIRPLAY])

        self.update_attributes()
        await self.mass.players.register_or_update(self)

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

    def get_linked_airplay_player(self, enabled_only: bool = True) -> Player | None:
        """Return the linked airplay player if available/enabled."""
        if enabled_only and not self.airplay_mode_enabled:
            return None
        if not (airplay_player := self.mass.players.get(self.airplay_player_id)):
            return None
        if not airplay_player.available:
            return None
        return airplay_player

    async def volume_set(self, volume_level: int) -> None:
        """
        Handle VOLUME_SET command on the player.

        Will only be called if the PlayerFeature.VOLUME_SET is supported.

        :param volume_level: volume level (0..100) to set on the player.
        """
        await self.client.player.set_volume(volume_level)
        # sync volume level with airplay player
        if airplay := self.get_linked_airplay_player(False):
            if airplay.state not in (PlaybackState.PLAYING, PlaybackState.PAUSED):
                airplay.volume_level = volume_level

    async def volume_mute(self, muted: bool) -> None:
        """
        Handle VOLUME MUTE command on the player.

        Will only be called if the PlayerFeature.VOLUME_MUTE is supported.

        :param muted: bool if player should be muted.
        """
        await self.client.player.set_volume(muted=muted)

    async def play(self) -> None:
        """Handle PLAY command on the player."""
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

    async def stop(self) -> None:
        """Handle STOP command on the player."""
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

    async def pause(self) -> None:
        """
        Handle PAUSE command on the player.

        Will only be called if the player reports PlayerFeature.PAUSE is supported.
        """
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

    async def next_track(self) -> None:
        """
        Handle NEXT_TRACK command on the player.

        Will only be called if the player reports PlayerFeature.NEXT_PREVIOUS
        is supported and the player is not currently playing a MA queue.
        """
        await self.client.player.group.skip_to_next_track()

    async def previous_track(self) -> None:
        """
        Handle PREVIOUS_TRACK command on the player.

        Will only be called if the player reports PlayerFeature.NEXT_PREVIOUS
        is supported and the player is not currently playing a MA queue.
        """
        await self.client.player.group.skip_to_previous_track()

    async def seek(self, position: int) -> None:
        """
        Handle SEEK command on the player.

        Seek to a specific position in the current track.
        Will only be called if the player reports PlayerFeature.SEEK is
        supported and the player is NOT currently playing a MA queue.

        :param position: The position to seek to, in seconds.
        """
        if self.client.player.is_passive:
            self.logger.debug("Ignore STOP command: Player is synced to another player.")
            return
        await self.client.player.group.seek(position)

    async def play_media(
        self,
        media: PlayerMedia,
    ) -> None:
        """
        Handle PLAY MEDIA command on given player.

        This is called by the Player controller to start playing Media on the player,
        which can be a MA queue item/stream or a native source.
        The provider's own implementation should work out how to handle this request.

        :param media: Details of the item that needs to be played on the player.
        """
        raise NotImplementedError("play_media needs to be implemented")
    
    async def select_source(self, source: str) -> None:
        """
        Handle SELECT SOURCE command on the player.

        Will only be called if the PlayerFeature.SELECT_SOURCE is supported.

        :param source: The source(id) to select, as defined in the source_list.
        """
        if source == SOURCE_LINE_IN:
            await self.client.player.group.load_line_in(play_on_completion=True)
        elif source == SOURCE_TV:
            await self.client.player.load_home_theater_playback()
        else:
            # unsupported source - try to clear the queue/player
            await self.cmd_stop()