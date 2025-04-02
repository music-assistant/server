"""Built-in HTTP-based Player Provider for Music Assistant.

This provider creates a standards HTTP audio streaming endpoint that can be utilized
by the MA web interface, accessed directly as a URL, consumed by Home Assistant media
browser, or integrated with other plugins without requiring third-party protocols.

Usage requires registering a player through the 'builtin_player/register' API command.
The registered player must regularly update its state via 'builtin_player/update_state'
to maintain the connection. Players can be manually disconnected with 'builtin_player/unregister'
when no longer needed.

Communication with the player occurs via events. The provider sends commands (play media url, pause,
stop, volume changes, etc.) through the BUILTIN_PLAYER event type. Client implementations must
listen for these events and respond accordingly to control playback and handle media changes.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from time import time
from typing import TYPE_CHECKING, cast

import shortuuid
from aiohttp import web
from music_assistant_models.builtin_player import BuiltinPlayerEvent, BuiltinPlayerState
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.constants import PLAYER_CONTROL_NATIVE
from music_assistant_models.enums import (
    BuiltinPlayerEventType,
    ConfigEntryType,
    ContentType,
    EventType,
    PlayerFeature,
    PlayerState,
    PlayerType,
    ProviderFeature,
)
from music_assistant_models.errors import PlayerUnavailableError
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.player import DeviceInfo, Player, PlayerMedia

from music_assistant.constants import (
    CONF_ENTRY_FLOW_MODE_ENFORCED,
    CONF_ENTRY_HTTP_PROFILE_HIDDEN,
    CONF_ENTRY_OUTPUT_CODEC_HIDDEN,
    CONF_MUTE_CONTROL,
    CONF_POWER_CONTROL,
    CONF_VOLUME_CONTROL,
    DEFAULT_PCM_FORMAT,
    DEFAULT_STREAM_HEADERS,
    create_sample_rates_config_entry,
)
from music_assistant.helpers.audio import get_player_filter_params
from music_assistant.helpers.ffmpeg import get_ffmpeg_stream
from music_assistant.mass import MusicAssistant
from music_assistant.models import ProviderInstanceType
from music_assistant.models.player_provider import PlayerProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest


# If the player does not send an update within this time, it will be considered offline
DURATION_UNTIL_TIMEOUT = 90  # 30 second extra headroom
POLL_INTERVAL = 30


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return BuiltinPlayerProvider(mass, manifest, config)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    instance_id: id of an existing provider instance (None if new instance setup).
    action: [optional] action key called from config entries UI.
    values: the (intermediate) raw values for config entries sent with the action.
    """
    # ruff: noqa: ARG001
    return ()


@dataclass
class PlayerInstance:
    """Dataclass for a connected instance."""

    unregister_cbs: list[Callable[[], None]]
    last_update: float


class BuiltinPlayerProvider(PlayerProvider):
    """Builtin Player Provider for playing to the Music Assistant Web Interface."""

    _unregister_cbs: list[Callable[[], None]] = []
    instances: dict[str, PlayerInstance] = {}

    def __init__(
        self, mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
    ) -> None:
        """Initialize the provider."""
        super().__init__(mass, manifest, config)
        self._unregister_cbs = [
            self.mass.register_api_command("builtin_player/register", self.register_player),
            self.mass.register_api_command("builtin_player/unregister", self.unregister_player),
            self.mass.register_api_command("builtin_player/update_state", self.update_player_state),
        ]

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return the features supported by this Provider."""
        return {ProviderFeature.REMOVE_PLAYER}

    async def unload(self, is_removed: bool = False) -> None:
        """
        Handle unload/close of the provider.

        Called when provider is deregistered (e.g. MA exiting or config reloading).
        is_removed will be set to True when the provider is removed from the configuration.
        """
        for unload_cb in self._unregister_cbs:
            unload_cb()
        for instance in self.instances.values():
            for unregister_cb in instance.unregister_cbs:
                unregister_cb()

    async def get_player_config_entries(self, player_id: str) -> tuple[ConfigEntry, ...]:
        """Return all (provider/player specific) Config Entries for the given player (if any)."""
        return (
            *await super().get_player_config_entries(player_id),
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
            # These options don't do anything here
            CONF_ENTRY_OUTPUT_CODEC_HIDDEN,
            CONF_ENTRY_HTTP_PROFILE_HIDDEN,
            create_sample_rates_config_entry(max_sample_rate=48000, max_bit_depth=16),
        )

    async def cmd_stop(self, player_id: str) -> None:
        """Send STOP command to given player."""
        self.mass.signal_event(
            EventType.BUILTIN_PLAYER,
            player_id,
            BuiltinPlayerEvent(type=BuiltinPlayerEventType.STOP),
        )

    async def cmd_play(self, player_id: str) -> None:
        """Send PLAY command to given player."""
        self.mass.signal_event(
            EventType.BUILTIN_PLAYER,
            player_id,
            BuiltinPlayerEvent(type=BuiltinPlayerEventType.PLAY),
        )

    async def cmd_pause(self, player_id: str) -> None:
        """Send PAUSE command to given player."""
        self.mass.signal_event(
            EventType.BUILTIN_PLAYER,
            player_id,
            BuiltinPlayerEvent(type=BuiltinPlayerEventType.PAUSE),
        )

    async def cmd_volume_set(self, player_id: str, volume_level: int) -> None:
        """Send VOLUME_SET command to given player."""
        self.mass.signal_event(
            EventType.BUILTIN_PLAYER,
            player_id,
            BuiltinPlayerEvent(type=BuiltinPlayerEventType.SET_VOLUME, volume=volume_level),
        )

    async def cmd_volume_mute(self, player_id: str, muted: bool) -> None:
        """Send VOLUME MUTE command to given player."""
        self.mass.signal_event(
            EventType.BUILTIN_PLAYER,
            player_id,
            BuiltinPlayerEvent(
                type=BuiltinPlayerEventType.MUTE if muted else BuiltinPlayerEventType.UNMUTE
            ),
        )

    async def play_media(
        self,
        player_id: str,
        media: PlayerMedia,
    ) -> None:
        """Handle PLAY MEDIA on given player."""
        url = f"builtin_player/flow/{player_id}.mp3"
        player = cast("Player", self.mass.players.get(player_id, raise_unavailable=True))
        player.current_media = media

        self.mass.signal_event(
            EventType.BUILTIN_PLAYER,
            player_id,
            BuiltinPlayerEvent(type=BuiltinPlayerEventType.PLAY_MEDIA, media_url=url),
        )

    async def cmd_power(self, player_id: str, powered: bool) -> None:
        """Send POWER command to given player.

        - player_id: player_id of the player to handle the command.
        - powered: bool if player should be powered on or off.
        """
        self.mass.signal_event(
            EventType.BUILTIN_PLAYER,
            player_id,
            BuiltinPlayerEvent(
                type=BuiltinPlayerEventType.POWER_ON
                if powered
                else BuiltinPlayerEventType.POWER_OFF
            ),
        )
        if (not powered) and (player := self.mass.players.get(player_id)):
            player.powered = False

    async def poll_player(self, player_id: str) -> None:
        """Poll player for state updates.

        This is called by the Player Manager;
        if 'needs_poll' is set to True in the player object.
        """
        if instance := self.instances.get(player_id, None):
            last_updated = time() - instance.last_update
            if last_updated > DURATION_UNTIL_TIMEOUT:
                self.mass.signal_event(
                    EventType.BUILTIN_PLAYER,
                    player_id,
                    BuiltinPlayerEvent(type=BuiltinPlayerEventType.TIMEOUT),
                )
                raise PlayerUnavailableError("Connection to player timed out")

    async def remove_player(self, player_id: str) -> None:
        """Remove a player."""
        self.mass.signal_event(
            EventType.BUILTIN_PLAYER,
            player_id,
            BuiltinPlayerEvent(type=BuiltinPlayerEventType.TIMEOUT),
        )
        await self.unregister_player(player_id)

    async def register_player(self, player_name: str, player_id: str | None) -> Player:
        """Register a player.

        Every player must first be registered through this `builtin_player/register` API command
        before any playback can occur.
        Since players queues can time out, this command either will create a new player queue,
        or restore it from the last session.

        - player_name: Human readable name of the player, will only be used in case this call
                       creates a new queue.
        - player_id: the id of the builtin player, set to None on new sessions. The returned player
                     will have a new random player_id
        """
        if player_id is None:
            player_id = f"ma_{shortuuid.random(10).lower()}"

        already_registered = player_id in self.instances

        player_features = {
            PlayerFeature.VOLUME_SET,
            PlayerFeature.VOLUME_MUTE,
            PlayerFeature.PAUSE,
            PlayerFeature.POWER,
        }

        if not already_registered:
            self.instances[player_id] = PlayerInstance(
                unregister_cbs=[
                    self.mass.webserver.register_dynamic_route(
                        f"/builtin_player/flow/{player_id}.mp3", self._serve_audio_stream
                    ),
                ],
                last_update=time(),
            )

        player = self.mass.players.get(player_id)

        if player is None:
            player = Player(
                player_id=player_id,
                provider=self.instance_id,
                type=PlayerType.PLAYER,
                name=player_name,
                available=True,
                power_control=PLAYER_CONTROL_NATIVE,
                powered=False,
                device_info=DeviceInfo(),
                supported_features=player_features,
                needs_poll=True,
                poll_interval=POLL_INTERVAL,
                hidden_by_default=True,
                expose_to_ha_by_default=False,
                state=PlayerState.IDLE,
            )
        else:
            player.state = PlayerState.IDLE
            player.name = player_name
            player.available = True
            player.powered = False

        await self.mass.players.register_or_update(player)
        return player

    async def unregister_player(self, player_id: str) -> None:
        """Manually unregister a player with `builtin_player/unregister`."""
        instance = self.instances.pop(player_id, None)
        if instance is None:
            return
        for cb in instance.unregister_cbs:
            cb()
        if player := self.mass.players.get(player_id):
            player.available = False
            player.state = PlayerState.IDLE
            player.powered = False
            self.mass.players.update(player.player_id)

    async def update_player_state(self, player_id: str, state: BuiltinPlayerState) -> bool:
        """Update current state of a player.

        A player must periodically update the state of through this `builtin_player/update_state`
        API command.

        Returns False in case the player already timed out or simply doesn't exist.
        In that case, register the player first with `builtin_player/register`.
        """
        if not (player := self.mass.players.get(player_id)):
            return False

        if player_id not in self.instances:
            return False
        instance = self.instances[player_id]
        instance.last_update = time()

        if not player.powered and state.powered:
            # The player was powered off, so this state message is already out of date
            # Skip, it.
            return True

        player.elapsed_time_last_updated = time()
        player.elapsed_time = float(state.position)
        player.volume_muted = state.muted
        player.volume_level = state.volume
        if not state.powered:
            player.powered = False
            player.state = PlayerState.IDLE
        elif state.playing:
            player.powered = True
            player.state = PlayerState.PLAYING
        elif state.paused:
            player.powered = True
            player.state = PlayerState.PAUSED
        else:
            player.powered = True
            player.state = PlayerState.IDLE

        self.mass.players.update(player_id)
        return True

    async def _serve_audio_stream(self, request: web.Request) -> web.StreamResponse:
        """Serve the flow stream audio to a player."""
        player_id = request.path.rsplit(".")[0].rsplit("/")[-1]
        format_str = request.path.rsplit(".")[-1]
        # bitrate = request.query.get("bitrate")
        queue = self.mass.player_queues.get(player_id)

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

        media = player.current_media
        if queue is None or media is None:
            raise web.HTTPNotFound(reason="No active queue or media found!")

        if media.queue_id is None:
            raise web.HTTPError  # TODO: better error

        queue_item = self.mass.player_queues.get_item(media.queue_id, media.queue_item_id)

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
            content_type=DEFAULT_PCM_FORMAT.content_type,
            bit_depth=DEFAULT_PCM_FORMAT.bit_depth,
            channels=DEFAULT_PCM_FORMAT.channels,
        )

        async for chunk in get_ffmpeg_stream(
            audio_input=self.mass.streams.get_queue_flow_stream(
                queue=queue,
                start_queue_item=queue_item,
                pcm_format=pcm_format,
            ),
            input_format=pcm_format,
            output_format=stream_format,
            filter_params=get_player_filter_params(self.mass, player_id, pcm_format, stream_format),
        ):
            try:
                await resp.write(chunk)
            except (ConnectionError, ConnectionResetError):
                break

        return resp
