"""Demo Player implementation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import PlaybackState, PlayerFeature, PlayerType
from music_assistant_models.player import DeviceInfo, PlayerSource
from pyheos import Heos, PlayState

from music_assistant.models.player import Player, PlayerMedia

if TYPE_CHECKING:
    from pyheos import HeosPlayer as pyheosPlayer

    from .provider import HeosPlayerProvider

PLAY_STATE_TO_PLAY_BACK_STATE: dict[PlayState | None, PlaybackState] = {
    PlayState.PLAY: PlaybackState.PLAYING,
    PlayState.PAUSE: PlaybackState.PAUSED,
    PlayState.STOP: PlaybackState.IDLE,
    PlayState.UNKNOWN: PlaybackState.UNKNOWN,
    None: PlaybackState.UNKNOWN,
}

PLAYER_FEATURES = {
    PlayerFeature.VOLUME_SET,
    PlayerFeature.VOLUME_MUTE,
    PlayerFeature.PAUSE,
    PlayerFeature.NEXT_PREVIOUS,
    PlayerFeature.SELECT_SOURCE,
}


class HeosPlayer(Player):
    """HeosPLayer in Music Assistant."""

    _heos: Heos

    def __init__(self, provider: HeosPlayerProvider, client: pyheosPlayer) -> None:
        """Initialize the Player."""
        super().__init__(provider, str(client.player_id))

        self.device: pyheosPlayer = client

        # Keep internal reference so we don't need to check None on each call
        assert self.device.heos
        self._heos = self.device.heos

        # Set player attributes
        self._attr_type = PlayerType.PLAYER
        self._attr_supported_features = PLAYER_FEATURES
        self._attr_device_info = DeviceInfo(
            model=client.model,
            software_version=client.version,
            ip_address=client.ip_address,
            manufacturer="Denon",  # TODO: Grab this from API, technically can be others as well
        )
        self._attr_available = self.device.available
        self._attr_name = client.name

        self._attr_can_group_with = {self.provider.instance_id}

    async def setup(self) -> None:
        """Set up the player."""
        self.device.add_on_player_event(self._player_event_received)

        await self.mass.players.register_or_update(self)

        await self._build_source_list()

    async def _build_source_list(self) -> None:
        """Build source list based on from controller."""
        music_sources = await self._heos.get_music_sources()

        for source_id, source in music_sources.items():
            self._attr_source_list.append(
                PlayerSource(
                    id=str(source_id),
                    name=source.name,
                    can_play_pause=source_id == 1024,
                    can_next_previous=source_id == 1024,
                )
            )

    async def _player_event_received(self, event: str) -> None:
        """Handle player device events."""
        await self.set_dynamic_attributes()

    async def set_dynamic_attributes(self) -> None:
        """Update Player attributes."""
        self._attr_playback_state = PLAY_STATE_TO_PLAY_BACK_STATE[self.device.state]
        self._attr_volume_muted = self.device.is_muted
        self._attr_volume_level = self.device.volume

        if self.device.now_playing_media.current_position is not None:
            self._attr_elapsed_time = self.device.now_playing_media.current_position / 1000
        else:
            self._attr_elapsed_time = None

        if self.device.now_playing_media.current_position_updated is not None:
            self._attr_elapsed_time_last_updated = (
                self.device.now_playing_media.current_position_updated.timestamp()
            )
        else:
            self._attr_elapsed_time_last_updated = None

        self.logger.debug(self.device.now_playing_media)

        self._attr_active_source = str(self.device.now_playing_media.source_id)

        self.update_state()

    async def volume_set(self, volume_level: int) -> None:
        """Handle VOLUME_SET command on the player."""
        await self.device.set_volume(volume_level)

    async def volume_mute(self, muted: bool) -> None:
        """Handle VOLUME MUTE command on the player."""
        if muted:
            await self.device.mute()
        else:
            await self.device.unmute()

    async def play(self) -> None:
        """Handle PLAY command on the player."""
        await self.device.play()

    async def stop(self) -> None:
        """Handle STOP command on the player."""
        await self.device.stop()

    async def pause(self) -> None:
        """Handle PAUSE command on the player."""
        await self.device.pause()

    async def next_track(self) -> None:
        """Handle NEXT_TRACK command on the player."""
        await self.device.play_next()

    async def previous_track(self) -> None:
        """Handle PREVIOUS_TRACK command on the player."""
        await self.device.play_previous()

    async def play_media(self, media: PlayerMedia) -> None:
        """Handle PLAY MEDIA command on given player."""
        await self.device.play_url(media.uri)

        self._attr_current_media = media

        self.update_state()
