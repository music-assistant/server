"""Wiim Player implementation."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, cast

import pywiim
from music_assistant_models.enums import PlaybackState, PlayerFeature, PlayerType
from pywiim import WiiMClient
from pywiim.upnp.client import UpnpClient
from pywiim.upnp.eventer import UpnpEventer

from music_assistant.models.player import Player, PlayerMedia

if TYPE_CHECKING:
    from .provider import WiimProvider


class WiimPlayer(Player):
    """Wiim Player in Music Assistant."""

    def __init__(
        self, provider: WiimProvider, player_id: str, name: str, client: WiiMClient
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
            PlayerFeature.SELECT_SOURCE,
            # PlayerFeature.PLAY_ANNOUNCEMENT,
        }
        self._attr_can_group_with = {provider.instance_id}
        self.client = client
        self.wiim_player: pywiim.Player | None = None
        self.current_uri: str | None = None

    async def setup(self, ip_address: str) -> None:
        """Handle logic when the player is set up in the Player controller."""
        # Create UPnP client (required for events and queue management)
        description_url = f"http://{ip_address}:49152/description.xml"
        upnp_client = await UpnpClient.create(ip_address, description_url)

        # Create Player with UPnP client (for queue management + events)
        self.wiim_player = pywiim.Player(
            self.client,
            upnp_client=upnp_client,
            on_state_changed=self._update_ma_state_from_sdk_cache,
        )

        await self.wiim_player.refresh()

        if self.wiim_player.uuid is None:
            raise RuntimeError("Could not get UUID from WiiM player")

        # Create UpnpEventer with same UPnP client (for real-time events)
        eventer = UpnpEventer(
            upnp_client,  # Share same UPnP client
            self.wiim_player,  # Player implements apply_diff() for state updates
            self.wiim_player.uuid,
            state_updated_callback=self.foo,
        )

        # Start UPnP event subscriptions
        await eventer.start()

    def foo(self) -> None:
        """Call the next status update method."""
        self._update_ma_state_from_sdk_cache()

    @property
    def needs_poll(self) -> bool:
        """Return if the player needs to be polled for state updates."""
        return True

    @property
    def poll_interval(self) -> int:
        """Return the interval in seconds to poll the player for state updates."""
        # OPTIONAL
        # used in conjunction with the needs_poll property.
        # this should return the interval in seconds to poll the player for state updates.
        return 5

    async def poll(self) -> None:
        """Poll player for state updates."""
        # OPTIONAL - This is called by the Player Manager if the 'needs_poll' property is True.
        if self.wiim_player is not None:
            await self.wiim_player.refresh()

    async def select_source(self, source: str) -> None:
        """
        Handle SELECT SOURCE command on the player.

        Will only be called if the PlayerFeature.SELECT_SOURCE is supported.

        :param source: The source(id) to select, as defined in the source_list.
        """
        # if source == SOURCE_LINE_IN:
        #     await self.client.player.group.load_line_in(play_on_completion=True)
        # elif source == SOURCE_TV:
        #     await self.client.player.load_home_theater_playback()
        # else:
        #     # unsupported source - try to clear the queue/player
        #     await self.stop()

    async def volume_set(self, volume_level: int) -> None:
        """Handle VOLUME_SET command on the player."""
        if self.wiim_player is not None:
            await self.wiim_player.set_volume(volume_level / 100.0)

    async def volume_mute(self, muted: bool) -> None:
        """Handle VOLUME MUTE command on the player."""
        if self.wiim_player is not None:
            await self.wiim_player.set_mute(muted)

    async def play(self) -> None:
        """Play command."""
        if self.wiim_player is not None:
            await self.wiim_player.resume()

    async def stop(self) -> None:
        """Stop command."""
        if self.wiim_player is not None:
            await self.wiim_player.stop()

    async def pause(self) -> None:
        """Pause command."""
        if self.wiim_player is not None:
            await self.wiim_player.pause()

    async def play_media(self, media: PlayerMedia) -> None:
        """Play media command."""
        if self.wiim_player is not None:
            await self.wiim_player.play_url(media.uri)
            self.current_uri = media.uri

    async def on_unload(self) -> None:
        """Handle logic when the player is unloaded from the Player controller."""
        # OPTIONAL
        # this method is optional and should be implemented if you need to handle
        # any logic when the player is unloaded from the Player controller.
        # This is called when the player is removed from the Player controller.
        self.logger.info("Player %s unloaded", self.name)

    async def set_members(
        self,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        """Handle SET_MEMBERS command on the player."""
        if self.wiim_player is not None:
            if player_ids_to_add:
                for i in player_ids_to_add:
                    child_player = cast("WiimPlayer", self.mass.players.get(i))
                    if child_player is not None and child_player.wiim_player is not None:
                        await child_player.wiim_player.join_group(self.wiim_player)

            if player_ids_to_remove:
                for i in player_ids_to_remove:
                    child_player = cast("WiimPlayer", self.mass.players.get(i))
                    if child_player is not None and child_player.wiim_player is not None:
                        await child_player.wiim_player.leave_group()

    def _update_ma_state_from_sdk_cache(self) -> None:
        """Update MA state from SDK's cache/HTTP poll attributes.

        This is the main method for updating this entity's MA attributes.
        Crucially, it also handles propagating metadata to followers if this is a leader.
        """
        if self.wiim_player is not None:
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

            # for source in self.wiim_player.available_sources:
            #     self._attr_source_list.append(PlayerSource(source.lower(), source))

            if not self.wiim_player.is_slave:
                if self.current_uri and self.current_uri == self.wiim_player.media_content_id:
                    self._attr_active_source = self.player_id
                else:
                    self._attr_active_source = (
                        self.wiim_player.source.lower() if self.wiim_player.source else ""
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
