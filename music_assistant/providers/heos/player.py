"""HEOS Player implementation."""

from __future__ import annotations

from copy import copy
from typing import TYPE_CHECKING

from music_assistant_models.enums import PlayerFeature, PlayerType
from music_assistant_models.player import DeviceInfo
from pyheos import Heos, const

from music_assistant.constants import (
    CONF_ENTRY_FLOW_MODE_ENFORCED,
    create_sample_rates_config_entry,
)
from music_assistant.models.player import Player, PlayerMedia
from music_assistant.providers.heos.helpers import media_uri_from_now_playing_media

from .constants import (
    HEOS_MEDIA_TYPE_TO_MEDIA_TYPE,
    HEOS_PLAY_STATE_TO_PLAYBACK_STATE,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry, ConfigValueType
    from pyheos import HeosPlayer as pyheosPlayer

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

        self.logger.info("[%s] Loaded config: %s", self._device.name, self.config)

    async def setup(self) -> None:
        """Set up the player."""
        self.set_dynamic_attributes()

        await self.mass.players.register_or_update(self)

        self.logger.debug("[%s] Player currently enabled: %s", self._device.name, self.enabled)
        if self.enabled:
            self._on_unload_callbacks.append(
                self._device.add_on_player_event(self._player_event_received)
            )

            await self.build_group_list()

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
        self._attr_playback_state = HEOS_PLAY_STATE_TO_PLAYBACK_STATE[self._device.state]

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
            self.logger.debug("[%s] Now playing change: %s", self._device.name, now_playing)

            self._attr_active_source = str(now_playing.source_id)
            self._attr_current_media = PlayerMedia(
                uri=now_playing.media_id or media_uri_from_now_playing_media(now_playing),
                media_type=HEOS_MEDIA_TYPE_TO_MEDIA_TYPE[now_playing.type],
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
            # Update group to only include player's own ID, effectively removing the group
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
            *await super().get_config_entries(action=action, values=values),
            create_sample_rates_config_entry(
                max_sample_rate=192000,
                safe_max_sample_rate=192000,
                max_bit_depth=24,
                safe_max_bit_depth=24,
            ),
            CONF_ENTRY_FLOW_MODE_ENFORCED,
        ]
