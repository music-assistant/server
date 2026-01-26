"""HEOS Player implementation."""

from __future__ import annotations

from copy import copy
from typing import TYPE_CHECKING, cast

from music_assistant_models.enums import MediaType, PlaybackState, PlayerFeature, PlayerType
from music_assistant_models.errors import SetupFailedError
from music_assistant_models.player import DeviceInfo, PlayerSource
from pyheos import Heos, const

from music_assistant.constants import create_sample_rates_config_entry
from music_assistant.models.player import Player, PlayerMedia
from music_assistant.providers.heos.helpers import media_uri_from_now_playing_media

from .constants import HEOS_MEDIA_TYPE_TO_MEDIA_TYPE, HEOS_PLAY_STATE_TO_PLAYBACK_STATE

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry, ConfigValueType
    from pyheos import HeosPlayer as PyHeosPlayer

    from .provider import HeosPlayerProvider


PLAYER_FEATURES = {
    PlayerFeature.VOLUME_SET,
    PlayerFeature.VOLUME_MUTE,
    PlayerFeature.PAUSE,
    PlayerFeature.NEXT_PREVIOUS,
    PlayerFeature.SELECT_SOURCE,
    PlayerFeature.SET_MEMBERS,
}


class HeosPlayer(Player):
    """HeosPlayer in Music Assistant."""

    _heos: Heos
    _device: PyHeosPlayer

    @property
    def requires_flow_mode(self) -> bool:
        """Return if the player requires flow mode."""
        return True

    def __init__(self, provider: HeosPlayerProvider, device: PyHeosPlayer) -> None:
        """Initialize the Player."""
        super().__init__(provider, str(device.player_id))

        self._device: PyHeosPlayer = device

        if self._device.heos is None:
            raise SetupFailedError("HEOS device has no controller assigned")

        # Keep internal reference so we don't need to check None on each call
        self._heos = self._device.heos

    async def setup(self) -> None:
        """Set up the player."""
        self.set_static_attributes()
        self.set_dynamic_attributes()

        await self.mass.players.register_or_update(self)

        if self.enabled:
            self._on_unload_callbacks.append(
                self._device.add_on_player_event(self._player_event_received)
            )

            await self.build_group_list()
            await self.build_source_list()

    def set_static_attributes(self) -> None:
        """Set all player static attributes."""
        # Extract manufacturer and model from device model string, if available
        model_parts = self._device.model.split(maxsplit=1)
        manufacturer = model_parts[0] if len(model_parts) == 2 else "HEOS"
        model = model_parts[1] if len(model_parts) == 2 else self._device.model

        self._attr_type = PlayerType.PLAYER
        self._attr_supported_features = PLAYER_FEATURES
        _device_info = DeviceInfo(
            model=model,
            software_version=self._device.version,
            manufacturer=manufacturer,
        )
        _device_info.ip_address = self._device.ip_address
        self._attr_device_info = _device_info
        self._attr_can_group_with = {self.provider.instance_id}
        self._attr_available = self._device.available
        self._attr_name = self._device.name

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

    async def build_source_list(self) -> None:
        """Build source list based on music source list, combined with player specific inputs."""
        prov = cast("HeosPlayerProvider", self.provider)
        self._attr_source_list = prov.music_source_list[:]  # copy so we can modify

        for input_source in prov.input_source_list:
            # Only add input sources that belong to this player
            if str(input_source.source_id) != self.player_id or input_source.media_id is None:
                continue

            self._attr_source_list.append(
                PlayerSource(
                    id=input_source.media_id,
                    name=input_source.name,
                    can_play_pause=True,
                )
            )

        self.update_state()

    async def _player_event_received(self, event: str) -> None:
        """Handle player device events."""
        self.logger.debug("[%s] Event received: %s", self._device.name, event)

        match event:
            case const.EVENT_PLAYER_STATE_CHANGED:
                self._update_player_state()

            case const.EVENT_PLAYER_NOW_PLAYING_CHANGED:
                self._update_player_current_media()
                self._update_player_playing_progress()

            case const.EVENT_PLAYER_NOW_PLAYING_PROGRESS:
                self._update_player_playing_progress()

            case const.EVENT_PLAYER_VOLUME_CHANGED:
                self._update_player_volume()

            case _:
                # Update everything on other events
                self.set_dynamic_attributes()

        self.update_state()

    def _update_player_volume(self) -> None:
        """Update volume properties."""
        self._attr_volume_level = self._device.volume
        self._attr_volume_muted = self._device.is_muted

    def _update_player_state(self) -> None:
        """Update playback state."""
        self._attr_playback_state = HEOS_PLAY_STATE_TO_PLAYBACK_STATE.get(
            self._device.state, PlaybackState.UNKNOWN
        )

    def _update_player_current_media(self) -> None:
        """Update current media properties."""
        now_playing = self._device.now_playing_media

        # Only update if we're not playing from our queue
        # HEOS does not make a distinction on source ID when playing from a DLNA server, USB stick,
        # generic URL (like MA), or other local source.
        # We can only know we're playing from MA if we started this session.
        if (now_playing.source_id != const.MUSIC_SOURCE_LOCAL_MUSIC) or (
            self._attr_active_source != self.player_id
        ):
            self.logger.debug(
                "[%s] Now playing changed externally: %s", self._device.name, now_playing
            )

            if now_playing.source_id == const.MUSIC_SOURCE_AUX_INPUT:
                self._attr_active_source = str(now_playing.media_id)
            else:
                self._attr_active_source = str(now_playing.source_id)

            self._attr_current_media = PlayerMedia(
                uri=now_playing.media_id or media_uri_from_now_playing_media(now_playing),
                media_type=HEOS_MEDIA_TYPE_TO_MEDIA_TYPE.get(
                    now_playing.type,
                    MediaType.UNKNOWN,
                ),
                title=now_playing.song,
                artist=now_playing.artist,
                album=now_playing.album,
                image_url=now_playing.image_url,
                duration=now_playing.duration,
                source_id=str(now_playing.source_id),
                elapsed_time=now_playing.current_position,
                elapsed_time_last_updated=(
                    now_playing.current_position_updated.timestamp()
                    if now_playing.current_position_updated
                    else None
                ),
                # TODO: We can use custom_data to set the IDs
            )

    def _update_player_playing_progress(self) -> None:
        """Update current media progress properties."""
        now_playing = self._device.now_playing_media

        self._attr_elapsed_time = (
            now_playing.current_position / 1000 if now_playing.current_position else None
        )
        self._attr_elapsed_time_last_updated = (
            now_playing.current_position_updated.timestamp()
            if now_playing.current_position_updated
            else None
        )

    def set_dynamic_attributes(self) -> None:
        """Update all player dynamic attributes."""
        self._update_player_volume()
        self._update_player_state()
        self._update_player_current_media()
        self._update_player_playing_progress()

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
        self._attr_active_source = self.player_id

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
            await self._heos.remove_group(self._device.player_id)
        else:
            await self._heos.set_group([int(player) for player in members])
        # group_members will be updated when group_changed event is handled

    async def get_config_entries(
        self,
        action: str | None = None,
        values: dict[str, ConfigValueType] | None = None,
    ) -> list[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the player."""
        return [
            # Gen 1 devices, like HEOS Link, only support up to 48kHz/16bit
            create_sample_rates_config_entry(
                max_sample_rate=192000,
                safe_max_sample_rate=48000,
                max_bit_depth=24,
                safe_max_bit_depth=16,
            ),
        ]
