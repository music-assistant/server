"""State synchronization handler."""

from __future__ import annotations

import asyncio
import time
from functools import partial
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import PlaybackState
from music_assistant_models.errors import PlayerCommandFailed
from music_assistant_models.player import DeviceInfo
from pywam.lib.api_call import ApiCall
from pywam.lib.exceptions import PywamError
from pywam.speaker import Speaker

from music_assistant.providers.samsung_wam.consts import MANUFACTURER_NAME
from music_assistant.providers.samsung_wam.features.base import (
    WamPlayerFeatureBase,
    handle_pywam_errors,
    retry_command,
)

from .consts import HEALTH_CHECK_TIMEOUT
from .mapper import StateSyncMapper

if TYPE_CHECKING:
    from .models import WamSpeakerAttributes


def get_speaker_status() -> ApiCall:
    """(UIC) Get speaker status. Used for health checks."""
    return ApiCall(api_type="UIC", method="GetSpeakerStatus", expected_response="SpeakerStatus")


def get_current_play_time() -> ApiCall:
    """(UIC) Get current play time."""
    return ApiCall(api_type="UIC", method="GetCurrentPlayTime", expected_response="MusicPlayTime")


class StateSyncHandler(WamPlayerFeatureBase):
    """Encapsulates polling, state updates, and connection recovery."""

    def apply_initial_state(self, attrs: WamSpeakerAttributes) -> None:
        """Apply the initial state snapshot to the player during setup.

        :param attrs: The initial speaker attributes.
        """
        self.player._attr_device_info = DeviceInfo(
            model=attrs.model or "Unknown",
            manufacturer=MANUFACTURER_NAME,
            software_version=attrs.software_version,
            identifiers=self.player._attr_device_info.identifiers,
        )
        self.suppress_speaker_status_events(self.speaker)
        self._subscribe_speaker_events()
        self.player._attr_available = True

    async def poll(self) -> None:
        """Poll the player for state updates and handle connection recovery."""
        try:
            async with self.player.connection_lock:
                if not self.player.connected:
                    self.logger.debug("Poller found disconnected speaker. Attempting reconnect.")
                    self._mark_player_unavailable()
                    await self._reconnect_speaker()

            await self.check_status()

            if self.player.playback_state == PlaybackState.PLAYING:
                await self.update_play_time()

            if not self.player.available:
                self.logger.info("Player %s is back online.", self.player.log_name)
                self.player._attr_available = True

            self.player.update_state()

        except (ConnectionError, PywamError, TimeoutError, PlayerCommandFailed) as err:
            self.logger.debug(
                "Poll failed for %s (%s). Dropping connection.",
                self.player.log_name,
                err.__class__.__name__,
            )
            await self.disconnect_speaker()
            self._mark_player_unavailable()

    def _mark_player_unavailable(self) -> None:
        """Mark the player as unavailable and reset playback state."""
        if not self.player.available:
            return
        self.player._attr_available = False
        self.player._attr_playback_state = PlaybackState.IDLE
        self.player.stream_active = False
        self.player.update_state()

    @retry_command()
    @handle_pywam_errors
    async def check_status(self) -> None:
        """Verify device connectivity with a direct API call."""
        if not self.player.connected:
            raise ConnectionError("The underlying pywam client is not connected.")
        async with asyncio.timeout(HEALTH_CHECK_TIMEOUT):
            await self.speaker.client.request(get_speaker_status())

    @retry_command()
    @handle_pywam_errors
    async def update_play_time(self) -> None:
        """Fetch the current play time and synchronize."""
        if not self.player.connected:
            return

        async with asyncio.timeout(HEALTH_CHECK_TIMEOUT):
            response = await self.speaker.client.request(get_current_play_time())

            if not response or not hasattr(response, "data") or "playtime" not in response.data:
                return

            try:
                playtime = float(response.data["playtime"])
                self.player._attr_elapsed_time = playtime
                self.player._attr_elapsed_time_last_updated = time.time()
            except (ValueError, TypeError):
                self.logger.debug("Failed to parse playtime from response: %s", response.data)

    def on_speaker_event(self, event: Any = None) -> None:
        """Handle a state update event broadcast by the speaker.

        :param event: The payload data emitted from the speaker.
        """
        # Opportunistically parse playtime from broadcast events (e.g. PausePlaybackEvent)
        if (
            event
            and hasattr(event, "data")
            and isinstance(event.data, dict)
            and "playtime" in event.data
        ):
            try:
                playtime = float(event.data["playtime"])
                self.player._attr_elapsed_time = playtime
                self.player._attr_elapsed_time_last_updated = time.time()
            except (ValueError, TypeError):
                pass

        self.refresh_state(notify_provider=True)

    def refresh_state(self, notify_provider: bool = False) -> None:
        """Force a re-application of current state to the player object.

        :param notify_provider: Trigger an event signal if true.
        """
        if not self.player.connected:
            return

        speaker_attrs = StateSyncMapper.create_speaker_attributes(self.speaker)
        group_children = self.player.prov.groups.states.get(self.player.player_id, set())

        queue_id = None
        if queue := self.mass.player_queues.get(self.player.player_id):
            queue_id = queue.queue_id

        StateSyncMapper.apply_attributes_to_player(
            player=self.player,
            speaker_attrs=speaker_attrs,
            group_children=group_children,
            stream_active=self.player.stream_active,
            queue_id=queue_id,
        )

        self.player.update_state()

        if notify_provider:
            self.player.signal_state_update_event()
            self.player.prov.groups.on_player_state_changed(self.player)

    async def ensure_speaker_connected(self) -> None:
        """Ensure the speaker is connected, reconnecting if necessary."""
        async with self.player.connection_lock:
            if not self.player.connected:
                await self._reconnect_speaker()

    @staticmethod
    def suppress_speaker_status_events(speaker: Speaker) -> None:
        """Patch out the SpeakerStatus event handler to suppress log spam.

        :param speaker: The Speaker instance to patch.
        """

        def _handle_speaker_status_event(_event: Any) -> bool:
            return False

        speaker.events.event_SpeakerStatus = _handle_speaker_status_event

    async def _reconnect_speaker(self) -> None:
        """Swap the internal speaker object and re-establish the connection."""
        await self.disconnect_speaker()

        new_speaker = Speaker(self.player.ip_address)
        await new_speaker.connect()

        try:
            await new_speaker.update()

            attrs = StateSyncMapper.create_speaker_attributes(new_speaker)
            if not attrs.mac or attrs.mac != self.player.player_id:
                raise ConnectionError("Reconnected to wrong MAC or failed to get MAC.")

            self.suppress_speaker_status_events(new_speaker)
            self.player.speaker = new_speaker
            self._subscribe_speaker_events()

        except Exception:
            await new_speaker.disconnect()
            raise

    async def unload(self) -> None:
        """Handle cleanup when the player is being permanently unloaded."""
        self.player.prov.groups.unregister_player(self.player)
        await self.disconnect_speaker()

    async def disconnect_speaker(self) -> None:
        """Safely disconnect the underlying pywam speaker."""
        if self.speaker and self.player.connected:
            await self.speaker.disconnect()

    def _subscribe_speaker_events(self) -> None:
        """Register the event subscriber for the current speaker instance."""
        if self.speaker:
            self.speaker.events.register_subscriber(partial(self.on_speaker_event), info_level=0)
