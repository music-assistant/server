"""Wiim Player implementation."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, cast

import pywiim
from music_assistant_models.enums import PlaybackState, PlayerFeature, PlayerType
from music_assistant_models.player import DeviceInfo, PlayerSource
from pywiim.upnp.eventer import UpnpEventer

from music_assistant.models.player import Player, PlayerMedia

if TYPE_CHECKING:
    from pywiim.upnp.client import UpnpClient

    from .provider import WiimProvider


class WiimPlayer(Player):
    """Wiim Player in Music Assistant."""

    def __init__(
        self,
        provider: WiimProvider,
        player_id: str,
        name: str,
        client: pywiim.WiiMClient,
        upnp_client: UpnpClient,
    ) -> None:
        """Initialize the Player."""
        super().__init__(provider, player_id)

        # init some static variables
        self._attr_name = name
        self._attr_type = PlayerType.PLAYER
        self._attr_supported_features = {
            PlayerFeature.VOLUME_SET,
            PlayerFeature.VOLUME_MUTE,
            PlayerFeature.PAUSE,
            PlayerFeature.SET_MEMBERS,
            PlayerFeature.NEXT_PREVIOUS,
            PlayerFeature.SEEK,
            PlayerFeature.SELECT_SOURCE,
            PlayerFeature.PLAY_ANNOUNCEMENT,
        }
        self._attr_can_group_with = {provider.instance_id}
        self.wiim_client = client
        self.wiim_upnp_client = upnp_client
        self.wiim_player = pywiim.Player(
            client,
            upnp_client=upnp_client,
            on_state_changed=self.update_ma_state,
        )
        self.current_uri: str | None = None

    async def setup(self) -> None:
        """Handle logic when the player is set up in the Player controller."""
        # Create UpnpEventer with same UPnP client (for real-time events)
        self.wiim_eventer = UpnpEventer(
            self.wiim_upnp_client,  # Share same UPnP client
            self.wiim_player,  # Player implements apply_diff() for state updates
            self.player_id,
            state_updated_callback=self.update_ma_state,
        )

        # Start UPnP event subscriptions
        await self.wiim_eventer.start()

        await self.wiim_player.refresh()

        self._attr_device_info = DeviceInfo(
            model=self.wiim_player.model if self.wiim_player.model else "",
            software_version=self.wiim_player.firmware if self.wiim_player.firmware else "",
        )

        for source in self.wiim_player.source_catalog:
            self._attr_source_list.append(
                PlayerSource(
                    id=source.get("id", ""),
                    name=source.get("name", ""),
                    passive=not source.get("selectable", False),
                    can_play_pause=source.get("supports_pause", False),
                    can_seek=source.get("supports_seek", False),
                    can_next_previous=source.get("supports_next_track", False)
                    and source.get("supports_previous_track", False),
                )
            )

    @property
    def needs_poll(self) -> bool:
        """Return if the player needs to be polled for state updates."""
        return True

    @property
    def poll_interval(self) -> int:
        """Return the interval in seconds to poll the player for state updates."""
        return 5

    async def poll(self) -> None:
        """Poll player for state updates."""
        await self.wiim_player.refresh()

    async def select_source(self, source: str) -> None:
        """Handle SELECT SOURCE command on the player."""
        await self.wiim_player.set_source(source)

    async def volume_set(self, volume_level: int) -> None:
        """Handle VOLUME_SET command on the player."""
        await self.wiim_player.set_volume(volume_level / 100.0)

    async def volume_mute(self, muted: bool) -> None:
        """Handle VOLUME MUTE command on the player."""
        await self.wiim_player.set_mute(muted)

    async def next_track(self) -> None:
        """Next command."""
        await self.wiim_player.next_track()

    async def previous_track(self) -> None:
        """Previous command."""
        await self.wiim_player.previous_track()

    async def seek(self, position: int) -> None:
        """SEEK command on the player."""
        await self.wiim_player.seek(position)

    async def play(self) -> None:
        """Play command."""
        await self.wiim_player.resume()

    async def stop(self) -> None:
        """Stop command."""
        await self.wiim_player.stop()

    async def pause(self) -> None:
        """Pause command."""
        await self.wiim_player.pause()

    async def play_media(self, media: PlayerMedia) -> None:
        """Play media command."""
        await self.wiim_player.play_url(media.uri)
        self.current_uri = media.uri

    async def play_announcement(
        self, announcement: PlayerMedia, volume_level: int | None = None
    ) -> None:
        """Handle (native) playback of an announcement on the player."""
        await self.wiim_player.play_notification(announcement.uri)

    async def on_unload(self) -> None:
        """Handle logic when the player is unloaded from the Player controller."""
        await self.wiim_upnp_client.close()
        await self.wiim_client.close()

    async def set_members(
        self,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        """Handle SET_MEMBERS command on the player."""
        if player_ids_to_add:
            for i in player_ids_to_add:
                child_player = cast("WiimPlayer", self.mass.players.get(i))
                await child_player.wiim_player.join_group(self.wiim_player)

        if player_ids_to_remove:
            for i in player_ids_to_remove:
                child_player = cast("WiimPlayer", self.mass.players.get(i))
                await child_player.wiim_player.leave_group()

    def update_ma_state(self) -> None:
        """Update MA state from SDK's cache/HTTP poll attributes."""
        logger = self.logger
        logger.debug("Device %s: Updating MA state from SDK cache/HTTP poll", self._attr_name)

        self._attr_available = self.wiim_player.available

        self._attr_volume_level = (
            int(self.wiim_player.volume_level * 100)
            if self.wiim_player.volume_level is not None
            else None
        )
        self._attr_volume_muted = self.wiim_player.is_muted

        self._attr_playback_state = PlaybackState(self.wiim_player.state)

        self._attr_elapsed_time = self.wiim_player.media_position
        self._attr_elapsed_time_last_updated = time.time()

        if self.wiim_player.is_master:
            self._attr_group_members = (
                [i.uuid for i in self.wiim_player.group.slaves if i.uuid is not None]
                if self.wiim_player.group is not None
                else []
            )
        else:
            self._attr_group_members.clear()

        if not self.wiim_player.is_slave:
            if self.current_uri and self.current_uri == self.wiim_player.media_content_id:
                self._attr_active_source = self.player_id
            else:
                self._attr_active_source = (
                    self.wiim_player.source if self.wiim_player.source else ""
                )
                self.set_current_media(
                    uri=self.wiim_player.media_content_id or "",
                    title=self.wiim_player.media_title,
                    artist=self.wiim_player.media_artist,
                    album=self.wiim_player.media_album,
                    image_url=self.wiim_player.media_image_url,
                    duration=self.wiim_player.media_duration,
                )

        self.update_state()
