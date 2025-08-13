"""Resonate Player implementation."""

from __future__ import annotations

from typing import TYPE_CHECKING, override

from music_assistant_models.constants import PLAYER_CONTROL_NONE
from music_assistant_models.enums import PlaybackState, PlayerType
from music_assistant_models.player import DeviceInfo

from music_assistant.models.player import Player, PlayerMedia

if TYPE_CHECKING:
    from .provider import ResonateProvider


class ResonatePlayer(Player):
    """A resonate audio player in Music Assistant."""

    def __init__(self, provider: ResonateProvider, player_id: str) -> None:
        """Initialize the Player."""
        super().__init__(provider, player_id)
        # init some static variables
        self._attr_name = f"Demo Player {player_id}"
        self._attr_type = PlayerType.PLAYER
        self._attr_supported_features = set()
        self._attr_power_control = PLAYER_CONTROL_NONE
        self._attr_device_info = DeviceInfo()
        self._set_attributes()

    @override
    async def play(self) -> None:
        """Play command."""
        logger = self.provider.logger.getChild(self.player_id)
        logger.info("Received PLAY command on player %s", self.display_name)
        self._attr_playback_state = PlaybackState.PLAYING
        self.update_state()

    @override
    async def stop(self) -> None:
        """Stop command."""
        logger = self.provider.logger.getChild(self.player_id)
        logger.info("Received STOP command on player %s", self.display_name)
        self._attr_playback_state = PlaybackState.IDLE
        self.update_state()

    @override
    async def play_media(self, media: PlayerMedia) -> None:
        """Play media command."""
        logger = self.provider.logger.getChild(self.player_id)
        logger.info(
            "Received PLAY_MEDIA command on player %s with uri %s", self.display_name, media.uri
        )
        self._attr_current_media = media
        self._attr_playback_state = PlaybackState.PLAYING
        self.update_state()

    @override
    async def on_unload(self) -> None:
        """Handle logic when the player is unloaded from the Player controller."""
        # OPTIONAL
        # this method is optional and should be implemented if you need to handle
        # any logic when the player is unloaded from the Player controller.
        # This is called when the player is removed from the Player controller.
        self.logger.info("Player %s unloaded", self.name)

    def _set_attributes(self) -> None:
        """Update/set (dynamic) properties."""
        self._attr_powered = True
        self._attr_volume_muted = False
        self._attr_volume_level = 50
