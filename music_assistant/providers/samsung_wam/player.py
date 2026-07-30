"""Samsung WAM player."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING

from music_assistant_models.enums import IdentifierType, PlaybackState
from music_assistant_models.player import DeviceInfo, PlayerMedia

from music_assistant.constants import CONF_ENTRY_ENABLE_ICY_METADATA_HIDDEN
from music_assistant.helpers.util import is_valid_mac_address
from music_assistant.models.player import Player

from .consts import (
    CONF_ENTRY_HTTP_PROFILE_WAM,
    MANUFACTURER_NAME,
    PLAYER_FEATURES_BASE,
)
from .features.device_config.handler import DeviceConfigHandler
from .features.grouping.handler import GroupingHandler
from .features.playback.handler import PlaybackHandler
from .features.state_sync.consts import POLL_INTERVAL
from .features.state_sync.handler import StateSyncHandler
from .features.volume.handler import VolumeHandler

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry
    from pywam.speaker import Speaker

    from .provider import SamsungWamProvider


class WamPlayer(Player):
    """Representation of a Samsung WAM speaker."""

    def __init__(
        self,
        prov: SamsungWamProvider,
        ip_address: str,
        udn: str,
        mac: str,
        speaker: Speaker,
    ) -> None:
        """
        Initialize the WamPlayer.

        :param prov: The parent provider instance.
        :param ip_address: The IP address of the speaker.
        :param udn: The Universal Device Name of the speaker.
        :param mac: The MAC address of the speaker.
        :param speaker: The underlying pywam Speaker instance.
        """
        self.prov = prov
        self._ip_address = ip_address
        self._udn = udn
        self.speaker = speaker

        self.stream_active = False
        self._state_update_event = asyncio.Event()
        self.connection_lock = asyncio.Lock()
        self.synced_to_internal: str | None = None

        super().__init__(provider=prov, player_id=mac)

        self.logger = self.prov.logger.getChild(self.player_id)
        self._attr_supported_features = set(PLAYER_FEATURES_BASE)
        self._attr_supported_sample_rates = [
            (sr, bd) for sr in (44100, 48000, 88200, 96000, 176400, 192000) for bd in (16, 24)
        ]
        self._attr_can_group_with = {prov.instance_id}
        self._attr_needs_poll = True
        self._attr_poll_interval = POLL_INTERVAL

        self._attr_device_info = DeviceInfo(model="Unknown Model", manufacturer=MANUFACTURER_NAME)
        if is_valid_mac_address(self.player_id):
            self._attr_device_info.add_identifier(IdentifierType.MAC_ADDRESS, self.player_id)
        self._attr_device_info.add_identifier(IdentifierType.IP_ADDRESS, self.ip_address)

        self.state_sync = StateSyncHandler(self)
        self.playback = PlaybackHandler(self)
        self.grouping = GroupingHandler(self)
        self.volume = VolumeHandler(self)
        self.device_config = DeviceConfigHandler(self)

    # --- Properties ---

    @property
    def requires_flow_mode(self) -> bool:
        """Force flow mode for WAM as native gapless queueing is unsupported."""
        return True

    @property
    def ip_address(self) -> str:
        """Return the IP address of the speaker."""
        return self._ip_address

    @property
    def udn(self) -> str:
        """Return the Universal Device Name (UDN) of the speaker."""
        return self._udn

    @property
    def connected(self) -> bool:
        """Return True if the underlying speaker client is connected."""
        return bool(self.speaker.is_connected)

    @property
    def synced_to(self) -> str | None:
        """Return the ID of the player this player is synced to."""
        return self.synced_to_internal

    @property
    def log_name(self) -> str:
        """Return a friendly name for logging output."""
        return self.display_name or self.ip_address

    # --- Configuration ---

    async def get_config_entries(self) -> list[ConfigEntry]:
        """
        Return player-specific configuration entries.

        :return: A list of ConfigEntry objects.
        """
        return [CONF_ENTRY_HTTP_PROFILE_WAM, CONF_ENTRY_ENABLE_ICY_METADATA_HIDDEN]

    async def on_config_updated(self) -> None:
        """Handle player config updates."""
        if new_name := self.config.name:
            if self.connected and new_name != self._attr_name:
                self.mass.create_task(self.device_config.set_name(new_name))

    # --- Player Controls ---

    async def poll(self) -> None:
        """Poll the player."""
        await self.state_sync.poll()

    async def on_unload(self) -> None:
        """Handle cleanup when player is removed."""
        await super().on_unload()
        await self.state_sync.unload()

    async def play(self) -> None:
        """Resume playback."""
        await self.playback.play()

    async def pause(self) -> None:
        """Pause playback."""
        await self.playback.pause()

    async def stop(self) -> None:
        """Stop playback."""
        await self.playback.stop()

    async def play_media(self, media: PlayerMedia) -> None:
        """
        Play media.

        :param media: The media item to play.
        """
        await self.playback.play_media(media)

    async def play_announcement(
        self, announcement: PlayerMedia, volume_level: int | None = None
    ) -> None:
        """
        Play an announcement on the player.

        :param announcement: The announcement media item to play.
        :param volume_level: The volume level to play the announcement at (0..100).
        """
        prev_state = self.playback_state
        prev_volume = self.volume_level

        if prev_state in (PlaybackState.PLAYING, PlaybackState.PAUSED):
            await self.playback.stop()
            await self.await_state_change(
                lambda: self.playback_state == PlaybackState.IDLE,
                timeout=5.0,
            )

        if volume_level is not None and prev_volume is not None and volume_level != prev_volume:
            await self.volume.set_volume(volume_level)

        await self.playback.play_media(announcement)
        await self.await_state_change(
            lambda: self.playback_state == PlaybackState.PLAYING,
            timeout=10.0,
        )

        # Samsung speakers auto-resume after a URL stream ends rather than going idle,
        # so we stop the stream manually at the expected end of the announcement
        duration = await self.mass.streams.get_announcement_duration(announcement)
        if duration is not None:
            await asyncio.sleep(duration + 1.0)
        else:
            # Unknown duration — fall back to waiting for idle
            await self.await_state_change(
                lambda: self.playback_state == PlaybackState.IDLE,
                timeout=30.0,
            )

        # Explicitly stop to cancel any auto-resume before restoring state
        await self.playback.stop()

        if volume_level is not None and prev_volume is not None and volume_level != prev_volume:
            await self.volume.set_volume(prev_volume)

        if prev_state == PlaybackState.PLAYING:
            await self.mass.player_queues.resume(self.player_id)

    async def select_source(self, source: str) -> None:
        """
        Select source.

        :param source: The source identifier to select.
        """
        await self.playback.select_source(source)

    async def volume_set(self, volume_level: int) -> None:
        """
        Set volume level.

        :param volume_level: The volume level to set.
        """
        await self.volume.set_volume(volume_level)

    async def volume_mute(self, muted: bool) -> None:
        """
        Set mute state.

        :param muted: True to mute, False to unmute.
        """
        await self.volume.set_mute(muted)

    async def set_members(
        self,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        """
        Handle group membership changes.

        :param player_ids_to_add: List of player IDs to add to the group.
        :param player_ids_to_remove: List of player IDs to remove from the group.
        """
        await self.prov.groups.set_members(
            self.player_id,
            player_ids_to_add,
            player_ids_to_remove,
        )

    # --- Helpers ---

    def signal_state_update_event(self) -> None:
        """Signal that state has been updated."""
        self._state_update_event.set()

    async def await_state_change(self, check: Callable[[], bool], timeout: float) -> None:
        """
        Wait for a specific condition in the state.

        :param check: A callable returning a boolean indicating if the condition is met.
        :param timeout: Maximum time in seconds to wait.
        """
        try:
            async with asyncio.timeout(timeout):
                while True:
                    self._state_update_event.clear()
                    if check():
                        return
                    await self._state_update_event.wait()
        except TimeoutError:
            pass
