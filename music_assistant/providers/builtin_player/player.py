"""Player model implementation for the Built-in Player."""

from __future__ import annotations

from collections.abc import Callable
from time import time

from aiohttp import web
from music_assistant_models.builtin_player import BuiltinPlayerEvent, BuiltinPlayerState
from music_assistant_models.config_entries import ConfigEntry, ConfigValueType
from music_assistant_models.constants import PLAYER_CONTROL_NATIVE
from music_assistant_models.enums import (
    BuiltinPlayerEventType,
    ConfigEntryType,
    ContentType,
    EventType,
    PlaybackState,
    PlayerFeature,
    PlayerType,
)
from music_assistant_models.media_items import AudioFormat

from music_assistant.constants import (
    CONF_ENTRY_FLOW_MODE_ENFORCED,
    CONF_ENTRY_HTTP_PROFILE_HIDDEN,
    CONF_ENTRY_OUTPUT_CODEC_HIDDEN,
    CONF_MUTE_CONTROL,
    CONF_POWER_CONTROL,
    CONF_VOLUME_CONTROL,
    DEFAULT_STREAM_HEADERS,
    INTERNAL_PCM_FORMAT,
    create_sample_rates_config_entry,
)
from music_assistant.helpers.audio import get_player_filter_params
from music_assistant.helpers.ffmpeg import get_ffmpeg_stream
from music_assistant.models.player import DeviceInfo, Player, PlayerMedia
from music_assistant.models.player_provider import PlayerProvider

# If the player does not send an update within this time, it will be considered offline
DURATION_UNTIL_TIMEOUT = 120  # 60 second extra headroom
POLL_INTERVAL = 30


class BuiltinPlayer(Player):
    """Representation of a Builtin Player."""

    last_update: float
    unregister_cbs: list[Callable[[], None]] = []

    def __init__(
        self,
        player_id: str,
        provider: PlayerProvider,
        name: str,
        features: tuple[PlayerFeature, ...],
    ) -> None:
        """Initialize the Builtin Player."""
        super().__init__(provider, player_id)
        self._attr_type = PlayerType.PLAYER
        self._attr_power_control = PLAYER_CONTROL_NATIVE
        self._attr_device_info = DeviceInfo()
        self._attr_supported_features = set(features)
        self._attr_needs_poll = True
        self._attr_poll_interval = POLL_INTERVAL
        self._attr_hidden_by_default = True
        self._attr_expose_to_ha_by_default = False
        self.register(name, False)

    def unregister_routes(self) -> None:
        """Unregister all routes associated with this player."""
        for cb in self.unregister_cbs:
            cb()
        self.unregister_cbs.clear()
        self._attr_available = False
        self._attr_playback_state = PlaybackState.IDLE
        self._attr_powered = False
        self._attr_needs_poll = False
        self.update_state()

    def register(self, player_name: str, update_state: bool = True) -> None:
        """Register the player for playback."""
        if not self.unregister_cbs:
            self.unregister_cbs = [
                self.mass.webserver.register_dynamic_route(
                    f"/builtin_player/flow/{self.player_id}.mp3", self._serve_audio_stream
                ),
            ]

        self._attr_playback_state = PlaybackState.IDLE
        self._attr_name = player_name
        self._attr_available = True
        self._attr_powered = False
        self._attr_needs_poll = True
        self.last_update = time()
        if update_state:
            self.update_state()

    async def get_config_entries(
        self,
        action: str | None = None,
        values: dict[str, ConfigValueType] | None = None,
    ) -> list[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the player."""
        base_entries = await super().get_config_entries(action=action, values=values)
        return [
            *base_entries,
            CONF_ENTRY_FLOW_MODE_ENFORCED,
            # Hide power/volume/mute control options since they are guaranteed to work
            ConfigEntry(
                key=CONF_POWER_CONTROL,
                type=ConfigEntryType.STRING,
                label=CONF_POWER_CONTROL,
                default_value=PLAYER_CONTROL_NATIVE,
                hidden=True,
            ),
            ConfigEntry(
                key=CONF_VOLUME_CONTROL,
                type=ConfigEntryType.STRING,
                label=CONF_VOLUME_CONTROL,
                default_value=PLAYER_CONTROL_NATIVE,
                hidden=True,
            ),
            ConfigEntry(
                key=CONF_MUTE_CONTROL,
                type=ConfigEntryType.STRING,
                label=CONF_MUTE_CONTROL,
                default_value=PLAYER_CONTROL_NATIVE,
                hidden=True,
            ),
            CONF_ENTRY_HTTP_PROFILE_HIDDEN,
            CONF_ENTRY_OUTPUT_CODEC_HIDDEN,
            create_sample_rates_config_entry([48000]),
        ]

    async def stop(self) -> None:
        """Send STOP command to player."""
        self.mass.signal_event(
            EventType.BUILTIN_PLAYER,
            self.player_id,
            BuiltinPlayerEvent(type=BuiltinPlayerEventType.STOP),
        )
        self._attr_active_source = None
        self._attr_current_media = None
        self.update_state()

    async def play(self) -> None:
        """Send PLAY command to player."""
        self.mass.signal_event(
            EventType.BUILTIN_PLAYER,
            self.player_id,
            BuiltinPlayerEvent(type=BuiltinPlayerEventType.PLAY),
        )

    async def pause(self) -> None:
        """Send PAUSE command to player."""
        self.mass.signal_event(
            EventType.BUILTIN_PLAYER,
            self.player_id,
            BuiltinPlayerEvent(type=BuiltinPlayerEventType.PAUSE),
        )

    async def volume_set(self, volume_level: int) -> None:
        """Send VOLUME_SET command to player."""
        self.mass.signal_event(
            EventType.BUILTIN_PLAYER,
            self.player_id,
            BuiltinPlayerEvent(type=BuiltinPlayerEventType.SET_VOLUME, volume=volume_level),
        )

    async def volume_mute(self, muted: bool) -> None:
        """Send VOLUME MUTE command to player."""
        self.mass.signal_event(
            EventType.BUILTIN_PLAYER,
            self.player_id,
            BuiltinPlayerEvent(
                type=BuiltinPlayerEventType.MUTE if muted else BuiltinPlayerEventType.UNMUTE
            ),
        )

    async def play_media(self, media: PlayerMedia) -> None:
        """Handle PLAY MEDIA on player."""
        url = f"builtin_player/flow/{self.player_id}.mp3"
        self._attr_current_media = media
        self._attr_playback_state = PlaybackState.PLAYING
        self._attr_active_source = media.source_id
        self.update_state()
        self.mass.signal_event(
            EventType.BUILTIN_PLAYER,
            self.player_id,
            BuiltinPlayerEvent(type=BuiltinPlayerEventType.PLAY_MEDIA, media_url=url),
        )

    async def power(self, powered: bool) -> None:
        """Send POWER ON command to player."""
        self.mass.signal_event(
            EventType.BUILTIN_PLAYER,
            self.player_id,
            BuiltinPlayerEvent(
                type=BuiltinPlayerEventType.POWER_ON
                if powered
                else BuiltinPlayerEventType.POWER_OFF
            ),
        )
        if not powered:
            self._attr_powered = False
            self.update_state()

    async def poll(self) -> None:
        """
        Poll player for state updates.

        This is called by the Player Manager;
        if the 'needs_poll' property is True.
        """
        last_updated = time() - self.last_update
        if last_updated > DURATION_UNTIL_TIMEOUT:
            self.mass.signal_event(
                EventType.BUILTIN_PLAYER,
                self.player_id,
                BuiltinPlayerEvent(type=BuiltinPlayerEventType.TIMEOUT),
            )
            self.unregister_routes()

    async def on_unload(self) -> None:
        """Handle logic when the player is unloaded from the Player controller."""
        self.unregister_routes()

    async def _serve_audio_stream(self, request: web.Request) -> web.StreamResponse:
        """Serve the flow stream audio to a player."""
        player_id = request.path.rsplit(".")[0].rsplit("/")[-1]
        format_str = request.path.rsplit(".")[-1]
        # bitrate = request.query.get("bitrate")
        queue = self.mass.player_queues.get(player_id)
        self.logger.debug("Serving audio stream to %s", player_id)

        if not (player := self.mass.players.get(player_id)):
            raise web.HTTPNotFound(reason=f"Unknown player: {player_id}")

        headers = {
            **DEFAULT_STREAM_HEADERS,
            "Content-Type": f"audio/{format_str}",
            "Accept-Ranges": "none",
        }

        resp = web.StreamResponse(status=200, reason="OK", headers=headers)
        resp.enable_chunked_encoding()

        await resp.prepare(request)

        # return early if this is not a GET request
        if request.method != "GET":
            return resp

        # Check for a client probe request (from an iPhone/iPad)
        if (range_header := request.headers.get("Range")) and range_header == "bytes=0-1":
            self.logger.debug("Client is probing the stream.")
            # We don't early exit here since playback would otherwise never start
            # on iOS devices with Home Assistant OS installations.

        media = player._current_media
        if queue is None or media is None:
            raise web.HTTPNotFound(reason="No active queue or media found!")

        if media.source_id is None:
            raise web.HTTPError  # TODO: better error

        queue_item = self.mass.player_queues.get_item(media.source_id, media.queue_item_id)

        if queue_item is None:
            raise web.HTTPError  # TODO: better error

        # TODO: set encoding quality using a bitrate parameter,
        # maybe even dynamic with auto/semiauto switching with bad network?
        if format_str == "mp3":
            stream_format = AudioFormat(content_type=ContentType.MP3)
        else:
            stream_format = AudioFormat(content_type=ContentType.FLAC)

        pcm_format = AudioFormat(
            sample_rate=stream_format.sample_rate,
            content_type=INTERNAL_PCM_FORMAT.content_type,
            bit_depth=INTERNAL_PCM_FORMAT.bit_depth,
            channels=INTERNAL_PCM_FORMAT.channels,
        )
        async for chunk in get_ffmpeg_stream(
            audio_input=self.mass.streams.get_queue_flow_stream(
                queue=queue,
                start_queue_item=queue_item,
                pcm_format=pcm_format,
            ),
            input_format=pcm_format,
            output_format=stream_format,
            # Apple ignores "Accept-Ranges=none" on iOS and iPadOS for some reason,
            # so we need to slowly feed the music to avoid the Browser stopping and later
            # restarting the audio stream (from a wrong position!) by keeping the buffer short.
            extra_input_args=["-readrate", "1.05", "-readrate_initial_burst", "8"],
            filter_params=get_player_filter_params(self.mass, player_id, pcm_format, stream_format),
        ):
            try:
                await resp.write(chunk)
            except (ConnectionError, ConnectionResetError):
                break

        return resp

    def update_builtin_player_state(self, state: BuiltinPlayerState) -> None:
        """Update the current state of the player."""
        self._attr_elapsed_time_last_updated = time()
        self.last_update = time()
        self._attr_elapsed_time = float(state.position)
        self._attr_volume_muted = state.muted
        self._attr_volume_level = state.volume
        if not state.powered:
            self._attr_powered = False
            self._attr_playback_state = PlaybackState.IDLE
        elif state.playing:
            self._attr_powered = True
            self._attr_playback_state = PlaybackState.PLAYING
        elif state.paused:
            self._attr_powered = True
            self._attr_playback_state = PlaybackState.PAUSED
        else:
            self._attr_powered = True
            self._attr_playback_state = PlaybackState.IDLE

        self.update_state()
