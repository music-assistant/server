"""Alexa Devices player implementation."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import aiohttp
from aioamazondevices.exceptions import CannotConnect, CannotRetrieveData
from aioamazondevices.structures import AmazonMediaControls
from music_assistant_models.enums import IdentifierType, PlaybackState, PlayerFeature
from music_assistant_models.errors import ActionUnavailable, PlayerCommandFailed
from music_assistant_models.player import DeviceInfo

from music_assistant.models.player import Player

from .constants import ALEXA_LANGUAGE_COMMANDS, CONF_ALEXA_LANGUAGE, DEFAULT_LANGUAGE

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from aioamazondevices.structures import AmazonDevice, AmazonMediaState, AmazonVolumeState
    from music_assistant_models.player import PlayerMedia

    from .provider import AlexaDevicesProvider

# Alexa media player_state values mapped to MA playback states
PLAYBACK_STATE_MAP: dict[str, PlaybackState] = {
    "PLAYING": PlaybackState.PLAYING,
    "PAUSED": PlaybackState.PAUSED,
}


class AlexaDevicesPlayer(Player):
    """An Echo device that plays Music Assistant streams through the companion skill."""

    _attr_needs_poll = False

    def __init__(self, provider: AlexaDevicesProvider, device: AmazonDevice) -> None:
        """
        Initialize the player.

        :param provider: The owning provider.
        :param device: The aioamazondevices device this player controls.
        """
        super().__init__(provider, device.serial_number)
        self.device = device
        self._alexa = provider
        self._attr_name = device.account_name
        self._attr_supported_features = {
            PlayerFeature.PLAY_MEDIA,
            PlayerFeature.VOLUME_SET,
            PlayerFeature.PAUSE,
        }
        self._attr_available = device.online
        self._attr_device_info = DeviceInfo(
            model=device.model or device.device_type,
            manufacturer=device.manufacturer or "Amazon",
            software_version=device.software_version,
            model_id=device.device_type,
            identifiers={IdentifierType.SERIAL_NUMBER: device.serial_number},
        )
        # stream url of the last play_media, reused by metadata updates
        self._last_stream_url: str | None = None
        self._last_meta_checksum: str | None = None
        # set once the skill shows it keeps its player screen open across commands
        self._skill_keeps_screen_open = False
        # requests to the skill go out one at a time, so it gets them in command order
        self._skill_request_lock = asyncio.Lock()

    @property
    def requires_flow_mode(self) -> bool:
        """Return if the player requires flow mode."""
        return True

    async def stop(self) -> None:
        """Handle STOP command on the player."""
        await self._alexa_call(
            self._alexa.api.send_media_command(self.device, AmazonMediaControls.Stop)
        )
        self._attr_current_media = None
        self._attr_playback_state = PlaybackState.IDLE
        self.update_state()

    async def play(self) -> None:
        """Handle PLAY command on the player."""
        if not await self._send_to_open_screen("resume"):
            await self._run_intent("AMAZON.ResumeIntent", "resume")
        self._attr_playback_state = PlaybackState.PLAYING
        self.update_state()

    async def pause(self) -> None:
        """Handle PAUSE command on the player."""
        if not await self._send_to_open_screen("pause"):
            await self._run_intent("AMAZON.PauseIntent", "pause")
        self._attr_playback_state = PlaybackState.PAUSED
        self.update_state()

    async def volume_set(self, volume_level: int) -> None:
        """Handle VOLUME_SET command on the player."""
        await self._alexa_call(self._alexa.api.set_device_volume(self.device, volume_level))
        self._attr_volume_level = volume_level
        self.update_state()

    async def play_media(self, media: PlayerMedia) -> None:
        """Handle PLAY MEDIA on the player."""
        stream_url = await self.mass.streams.resolve_stream_url(self.player_id, media)
        payload = {
            "streamUrl": stream_url,
            "title": media.title,
            "artist": media.artist,
            "album": media.album,
            "imageUrl": media.image_url,
            "playerId": self.player_id,
            "canSkipSpeech": True,
        }
        async with self._skill_request_lock:
            answer = await self._alexa.companion.push_url(payload)
        # only a skill that keeps its screen open answers with pageLive (true or false)
        if "pageLive" in answer:
            self._skill_keeps_screen_open = True
        self._last_stream_url = stream_url

        if not answer.get("pageLive"):
            language = str(self._alexa.config.get_value(CONF_ALEXA_LANGUAGE) or DEFAULT_LANGUAGE)
            command = ALEXA_LANGUAGE_COMMANDS.get(
                language, ALEXA_LANGUAGE_COMMANDS[DEFAULT_LANGUAGE]
            )
            await self._alexa_call(self._alexa.api.call_alexa_text_command(self.device, command))

        self._attr_elapsed_time = 0
        self._attr_elapsed_time_last_updated = time.time()
        self._attr_playback_state = PlaybackState.PLAYING
        self._attr_current_media = media
        self.update_state()

    def on_player_media_updated(self) -> None:
        """Push updated metadata (e.g. next track in the flow stream) to the skill."""
        if self._last_stream_url is None or (media := self.state.current_media) is None:
            return
        payload = {
            "streamUrl": self._last_stream_url,
            "title": media.title,
            "artist": media.artist,
            "album": media.album,
            "imageUrl": media.image_url,
        }
        checksum = "-".join(str(value) for value in payload.values())
        if checksum == self._last_meta_checksum:
            return

        async def _upload_metadata() -> None:
            await self._alexa.companion.push_url(payload)
            self._last_meta_checksum = checksum

        self.mass.create_task(
            _upload_metadata(), task_name=f"alexa_devices_upload_metadata_{self.player_id}"
        )

    def update_device(self, device: AmazonDevice | None) -> None:
        """
        Apply refreshed device data (online state, name).

        :param device: The refreshed aioamazondevices device, None if Amazon no longer
            lists it.
        """
        if device is None:
            self._attr_available = False
        else:
            self.device = device
            self._attr_available = device.online
            self._attr_name = device.account_name
        self.update_state()

    def update_volume(self, volume_state: AmazonVolumeState) -> None:
        """
        Apply a volume state pushed by Amazon.

        :param volume_state: The device volume state.
        """
        if volume_state.volume is not None:
            self._attr_volume_level = volume_state.volume
        if volume_state.is_muted is not None:
            self._attr_volume_muted = volume_state.is_muted
        self.update_state()

    def update_media(self, media_state: AmazonMediaState) -> None:
        """
        Apply a media state pushed by Amazon.

        :param media_state: The device media state.
        """
        self._attr_playback_state = PLAYBACK_STATE_MAP.get(
            media_state.player_state or "", PlaybackState.IDLE
        )
        if media_state.media_position is not None:
            self._attr_elapsed_time = media_state.media_position
            self._attr_elapsed_time_last_updated = (
                media_state.media_position_updated_at.timestamp()
                if media_state.media_position_updated_at
                else time.time()
            )
        self.update_state()

    async def _run_intent(self, intent_name: str, default: str) -> None:
        """Ask the device by voice command to run a companion skill intent."""
        command = await self._alexa.companion.get_intent_utterance(intent_name, default)
        await self._alexa_call(self._alexa.api.call_alexa_text_command(self.device, command))

    async def _send_to_open_screen(self, command: str) -> bool:
        """Send pause/resume to the skill's open screen; return True if the screen handled it."""
        # waits for a new track still being sent, so the skill gets the track first
        async with self._skill_request_lock:
            # an older skill has no /ma/control: only try once it showed it keeps its screen open
            if not self._skill_keeps_screen_open:
                return False
            try:
                answer = await self._alexa.companion.control(self.player_id, command)
            except ActionUnavailable, aiohttp.ClientError, TimeoutError:
                # don't try again until the skill confirms with its next pageLive answer
                self._skill_keeps_screen_open = False
                return False
        # pageLive: true means the skill took the command over
        return bool(answer.get("pageLive"))

    async def _alexa_call(self, call: Awaitable[None]) -> None:
        """Await an Amazon API call, raising library errors as a failed player command."""
        try:
            await call
        except (CannotConnect, CannotRetrieveData) as err:
            msg = f"Alexa command failed for {self.display_name}: {err}"
            raise PlayerCommandFailed(msg) from err
