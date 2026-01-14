"""Demo Player implementation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import PlaybackState, PlayerFeature, PlayerType
from music_assistant_models.player import DeviceInfo

from music_assistant.models.player import Player, PlayerMedia

if TYPE_CHECKING:
    from pyheos import HeosPlayer as HeosSourcePlayer

    from .provider import HeosPlayerProvider


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
            PlayerFeature.POWER,
            PlayerFeature.VOLUME_SET,
            PlayerFeature.VOLUME_MUTE,
        }
        self._attr_device_info = DeviceInfo(
            model=client.model,
            software_version=client.version,
            ip_address=client.ip_address,
        )
        self._attr_available = True

    async def setup(self) -> None:
        """Set up the player."""
        if self.client.available:
            await self.mass.players.register_or_update(self)

    async def stop(self) -> None:
        """Handle STOP command on the player."""
        await self.client.stop()

    async def play_media(self, media: PlayerMedia) -> None:
        """Handle PLAY MEDIA command on given player."""
        await self.client.play_url(media.uri)

        self._attr_current_media = media
        self._attr_playback_state = PlaybackState.PLAYING

        self.update_state()
