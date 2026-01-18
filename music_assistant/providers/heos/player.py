"""Demo Player implementation."""

from __future__ import annotations

from copy import copy
from typing import TYPE_CHECKING

from music_assistant_models.enums import PlaybackState, PlayerFeature, PlayerType
from music_assistant_models.player import DeviceInfo
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
    PlayerFeature.SET_MEMBERS,
}


class HeosPlayer(Player):
    """HeosPLayer in Music Assistant."""

    _heos: Heos
    _device: pyheosPlayer

    def __init__(self, provider: HeosPlayerProvider, client: pyheosPlayer) -> None:
        """Initialize the Player."""
        super().__init__(provider, str(client.player_id))

        self._device: pyheosPlayer = client

        # Keep internal reference so we don't need to check None on each call
        assert self._device.heos
        self._heos = self._device.heos

        self.logger.debug("Setting up player based on %s", client)

        # Set player attributes
        self._attr_type = PlayerType.PLAYER
        self._attr_supported_features = PLAYER_FEATURES
        self._attr_device_info = DeviceInfo(
            model=client.model,
            software_version=client.version,
            ip_address=client.ip_address,
            manufacturer="Denon",  # TODO: Grab this from API, technically can be others as well
        )
        self._attr_can_group_with = {provider.instance_id}
        self._attr_source_list = provider.source_list
        self._attr_available = self._device.available
        self._attr_name = client.name

    async def setup(self) -> None:
        """Set up the player."""
        self._on_unload_callbacks.append(
            self._device.add_on_player_event(self._player_event_received)
        )

        await self.mass.players.register_or_update(self)

        await self.build_group_list()

        await self.set_dynamic_attributes()

    async def build_group_list(self) -> None:
        """Build group list based on group info from controller."""
        # Group IDs are the player ID of the leader
        if self._device.group_id is not None and str(self._device.group_id) == self.player_id:
            group_info = await self._heos.get_group_info(self._device.group_id)
            self._attr_group_members = [
                str(group_info.lead_player_id),
                *(str(member) for member in group_info.member_player_ids),
            ]
        else:
            self._attr_group_members.clear()

        self.update_state()

    async def _player_event_received(self, event: str) -> None:
        """Handle player device events."""
        await self.set_dynamic_attributes()

    async def set_dynamic_attributes(self) -> None:
        """Update Player attributes."""
        self._attr_playback_state = PLAY_STATE_TO_PLAY_BACK_STATE[self._device.state]
        self._attr_volume_level = self._device.volume
        self._attr_volume_muted = self._device.is_muted

        if self._device.now_playing_media.current_position is not None:
            self._attr_elapsed_time = self._device.now_playing_media.current_position / 1000
        else:
            self._attr_elapsed_time = None

        if self._device.now_playing_media.current_position_updated is not None:
            self._attr_elapsed_time_last_updated = (
                self._device.now_playing_media.current_position_updated.timestamp()
            )
        else:
            self._attr_elapsed_time_last_updated = None

        self._attr_active_source = str(self._device.now_playing_media.source_id)

        self.update_state()

    async def volume_set(self, volume_level: int) -> None:
        """Handle VOLUME_SET command on the player."""
        await self._device.set_volume(volume_level)

    async def volume_mute(self, muted: bool) -> None:
        """Handle VOLUME MUTE command on the player."""
        if muted:
            await self._device.mute()
        else:
            await self._device.unmute()

    async def play(self) -> None:
        """Handle PLAY command on the player."""
        await self._device.play()

    async def stop(self) -> None:
        """Handle STOP command on the player."""
        await self._device.stop()

    async def pause(self) -> None:
        """Handle PAUSE command on the player."""
        await self._device.pause()

    async def next_track(self) -> None:
        """Handle NEXT_TRACK command on the player."""
        await self._device.play_next()

    async def previous_track(self) -> None:
        """Handle PREVIOUS_TRACK command on the player."""
        await self._device.play_previous()

    async def play_media(self, media: PlayerMedia) -> None:
        """Handle PLAY MEDIA command on given player."""
        await self._device.play_url(media.uri)

        self._attr_current_media = media

        self.update_state()

    async def set_members(
        self,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        """Handle SET MEMBERS command on player."""
        if player_ids_to_add is None and player_ids_to_remove is None:
            return

        members: list[str] = copy(self._attr_group_members)

        #  Make sure we are always in the group
        if self.player_id not in members:
            members = [self.player_id, *members]

        for added_player_id in player_ids_to_add or []:
            members.append(added_player_id)

        for removed_player_id in player_ids_to_remove or []:
            members.remove(removed_player_id)

        if len(members) <= 1:
            # Update group to only include player's own ID, effectively removing the group
            await self._heos.remove_group(self._device.player_id)
        else:
            await self._heos.set_group([int(player) for player in members])
        # group_members will be updated when group_changed event is handled
