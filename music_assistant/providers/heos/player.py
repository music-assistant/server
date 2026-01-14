"""Demo Player implementation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import PlaybackState, PlayerFeature, PlayerType
from music_assistant_models.player import DeviceInfo
from pyheos import PlayState

from music_assistant.models.player import Player, PlayerMedia

if TYPE_CHECKING:
    from pyheos import HeosPlayer as HeosSourcePlayer

    from .provider import HeosPlayerProvider

PLAY_STATE_TO_PLAY_BACK_STATE: dict[PlayState | None, PlaybackState] = {
    PlayState.PLAY: PlaybackState.PLAYING,
    PlayState.PAUSE: PlaybackState.PAUSED,
    PlayState.STOP: PlaybackState.IDLE,
    PlayState.UNKNOWN: PlaybackState.UNKNOWN,
    None: PlaybackState.UNKNOWN,
}


class HeosPlayer(Player):
    """HeosPLayer in Music Assistant."""

    def __init__(self, provider: HeosPlayerProvider, client: HeosSourcePlayer) -> None:
        """Initialize the Player."""
        super().__init__(provider, str(client.player_id))

        self.client: HeosSourcePlayer = client

        # init some static variables
        self._attr_name = client.name
        self._attr_type = PlayerType.PLAYER
        self._attr_supported_features = {
            PlayerFeature.VOLUME_SET,
            PlayerFeature.VOLUME_MUTE,
            PlayerFeature.PAUSE,
            PlayerFeature.NEXT_PREVIOUS,
        }
        self._attr_device_info = DeviceInfo(
            model=client.model,
            software_version=client.version,
            ip_address=client.ip_address,
        )
        self._attr_available = self.client.available

    async def setup(self) -> None:
        """Set up the player."""
        if self.client.available:
            self.client.add_on_player_event(self._player_update)

            await self.mass.players.register_or_update(self)

    async def _player_update(self, event: str) -> None:
        self._attr_playback_state = PLAY_STATE_TO_PLAY_BACK_STATE[self.client.state]
        self._attr_volume_muted = self.client.is_muted
        self._attr_volume_level = self.client.volume

        self.update_state()

    async def volume_set(self, volume_level: int) -> None:
        """Handle VOLUME_SET command on the player."""
        await self.client.set_volume(volume_level)

    async def volume_mute(self, muted: bool) -> None:
        """Handle VOLUME MUTE command on the player."""
        if muted:
            await self.client.mute()
        else:
            await self.client.unmute()

    async def play(self) -> None:
        """Handle PLAY command on the player."""
        await self.client.play()

    async def stop(self) -> None:
        """Handle STOP command on the player."""
        await self.client.stop()

    async def pause(self) -> None:
        """Handle PAUSE command on the player."""
        await self.client.pause()

    async def next_track(self) -> None:
        """Handle NEXT_TRACK command on the player."""
        await self.client.play_next()

    async def previous_track(self) -> None:
        """Handle PREVIOUS_TRACK command on the player."""
        await self.client.play_previous()

    async def play_media(self, media: PlayerMedia) -> None:
        """Handle PLAY MEDIA command on given player."""
        await self.client.play_url(media.uri)

        self._attr_current_media = media

        self.update_state()
