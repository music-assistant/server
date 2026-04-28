"""State synchronization handler."""

from __future__ import annotations

import asyncio
import contextlib
import time
from functools import partial
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import PlaybackState
from music_assistant_models.errors import PlayerCommandFailed
from music_assistant_models.player import DeviceInfo
from pywam.lib.api_call import ApiCall
from pywam.lib.exceptions import PywamError
from pywam.speaker import Speaker

from music_assistant.constants import ATTR_ANNOUNCEMENT_IN_PROGRESS
from music_assistant.providers.samsung_wam.consts import MANUFACTURER_NAME
from music_assistant.providers.samsung_wam.features.base import (
    WamPlayerFeatureBase,
    handle_pywam_errors,
    retry_command,
)
from music_assistant.providers.samsung_wam.features.playback.models import WamSource

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

    _terminating_wifi_stream: bool = False

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
        self.player._attr_name = attrs.name
        self.suppress_speaker_status_events(self.speaker)
        self.player._attr_available = True

    def subscribe_speaker_events(self) -> None:
        """Register the event subscriber for the current speaker instance."""
        if self.speaker:
            self.speaker.events.register_subscriber(partial(self.on_speaker_event), info_level=0)

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
        # Skip during announcements: the announcement stream's playtime events would
        # corrupt elapsed_time tracking and cause the post-announcement resume to seek incorrectly.
        if (
            event
            and hasattr(event, "data")
            and isinstance(event.data, dict)
            and "playtime" in event.data
            and not self.player.extra_data.get(ATTR_ANNOUNCEMENT_IN_PROGRESS)
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

        # If the speaker has externally switched away from the Wi-Fi stream (e.g. a phone
        # connecting via Bluetooth), clear the stream flag and capture the new source.
        external_source = None
        if (
            self.player.stream_active
            and speaker_attrs.source
            and speaker_attrs.source not in (WamSource.WIFI, "Unknown")
        ):
            self.player.stream_active = False
            external_source = speaker_attrs.source

        queue_id = None
        if queue := self.mass.player_queues.get(self.player.player_id):
            queue_id = queue.queue_id

        StateSyncMapper.apply_attributes_to_player(
            player=self.player,
            speaker_attrs=speaker_attrs,
            group_children=group_children,
            stream_active=self.player.stream_active,
        )

        self.player.update_state()

        # Set the active source after update_state() to immediately cancel the timer
        # it schedules, preventing the UI from reverting to the queue name.
        if external_source:
            self.player.set_active_mass_source(external_source)
            # Stopping the queue alone won't terminate the stream as the speaker keeps
            # its HTTP connection alive in the background.
            if queue_id:
                self.mass.create_task(self.mass.player_queues.stop(queue_id))
            self.mass.create_task(self._terminate_wifi_stream(external_source))

        if notify_provider:
            self.player.signal_state_update_event()
            self.player.prov.groups.on_player_state_changed(self.player)

    async def ensure_speaker_connected(self) -> None:
        """Ensure the speaker is connected, reconnecting if necessary."""
        async with self.player.connection_lock:
            if not self.player.connected:
                await self._reconnect_speaker()

    async def _terminate_wifi_stream(self, target_source: str) -> None:
        """Terminate the MA audio stream after an external source switch.

        :param target_source: The source the speaker should end up on (e.g. 'Bluetooth').
        """
        # The speaker keeps its HTTP connection to the MA stream URL open as a background
        # buffer even when on an external source — this sequence forces it to close.
        if self._terminating_wifi_stream:
            return
        if not self.player.connected or self.player.synced_to:
            return
        if self.mass.players.get_player(self.player.player_id) is not self.player:
            return

        self._terminating_wifi_stream = True
        try:
            try:
                await self.speaker.select_source(str(WamSource.WIFI))
                await self.player.await_state_change(
                    lambda: self.speaker.attribute.source == str(WamSource.WIFI),
                    2.0,
                )
                await self.speaker.cmd_pause()
            except (PywamError, ConnectionError, TimeoutError) as err:
                self.logger.debug(
                    "Stream termination (Wi-Fi phase) failed for %s: %s",
                    self.player.log_name,
                    err,
                )

            try:
                await self.speaker.select_source(target_source)
            except (PywamError, ConnectionError, TimeoutError) as err:
                self.logger.debug(
                    "Stream termination (source restore) failed for %s: %s",
                    self.player.log_name,
                    err,
                )
            finally:
                # Restore the target source to cancel any timer that fired while the
                # speaker was briefly back on Wi-Fi.
                self.player.set_active_mass_source(target_source)
        finally:
            self._terminating_wifi_stream = False

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

            # Guard against the case where this player instance has been replaced in the
            # player controller (e.g. after a provider restart). Continuing would leave
            # an orphaned pywam connection with a live event subscriber.
            if self.mass.players.get_player(self.player.player_id) is not self.player:
                raise ConnectionError("Player instance superseded; aborting reconnect.")

            self.suppress_speaker_status_events(new_speaker)
            self.player.speaker = new_speaker
            self.subscribe_speaker_events()

        except Exception:
            await new_speaker.disconnect()
            raise

    async def unload(self) -> None:
        """Handle cleanup when the player is being permanently unloaded."""
        self.player.prov.groups.unregister_player(self.player)
        await self.disconnect_speaker()

    async def disconnect_speaker(self) -> None:
        """Safely disconnect the underlying pywam speaker."""
        if self.speaker:
            with contextlib.suppress(Exception):
                await self.speaker.disconnect()
