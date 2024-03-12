"""Base/builtin provider with support for players using slimproto."""

from __future__ import annotations

import asyncio
import logging
import statistics
import time
from collections import deque
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING

from aioslimproto.client import PlayerState as SlimPlayerState
from aioslimproto.client import SlimClient
from aioslimproto.client import TransitionType as SlimTransition
from aioslimproto.models import EventType as SlimEventType
from aioslimproto.models import Preset as SlimPreset
from aioslimproto.models import VisualisationType as SlimVisualisationType
from aioslimproto.server import SlimServer

from music_assistant.common.models.config_entries import (
    CONF_ENTRY_CROSSFADE,
    CONF_ENTRY_CROSSFADE_DURATION,
    CONF_ENTRY_ENFORCE_MP3,
    CONF_ENTRY_EQ_BASS,
    CONF_ENTRY_EQ_MID,
    CONF_ENTRY_EQ_TREBLE,
    CONF_ENTRY_OUTPUT_CHANNELS,
    CONF_ENTRY_SYNC_ADJUST,
    ConfigEntry,
    ConfigValueOption,
    ConfigValueType,
    PlayerConfig,
)
from music_assistant.common.models.enums import (
    ConfigEntryType,
    ContentType,
    PlayerFeature,
    PlayerState,
    PlayerType,
    ProviderFeature,
    RepeatMode,
)
from music_assistant.common.models.errors import MusicAssistantError, SetupFailedError
from music_assistant.common.models.player import DeviceInfo, Player
from music_assistant.constants import (
    CONF_CROSSFADE,
    CONF_CROSSFADE_DURATION,
    CONF_ENFORCE_MP3,
    CONF_PORT,
    CONF_SYNC_ADJUST,
    MASS_LOGO_ONLINE,
)
from music_assistant.server.models.player_provider import PlayerProvider

if TYPE_CHECKING:
    from aioslimproto.models import SlimEvent

    from music_assistant.common.models.config_entries import ProviderConfig
    from music_assistant.common.models.provider import ProviderManifest
    from music_assistant.common.models.queue_item import QueueItem
    from music_assistant.server import MusicAssistant
    from music_assistant.server.controllers.streams import MultiClientStreamJob
    from music_assistant.server.models import ProviderInstanceType


CACHE_KEY_PREV_STATE = "slimproto_prev_state"


STATE_MAP = {
    SlimPlayerState.BUFFERING: PlayerState.PLAYING,
    SlimPlayerState.BUFFER_READY: PlayerState.PLAYING,
    SlimPlayerState.PAUSED: PlayerState.PAUSED,
    SlimPlayerState.PLAYING: PlayerState.PLAYING,
    SlimPlayerState.STOPPED: PlayerState.IDLE,
}
REPEATMODE_MAP = {RepeatMode.OFF: 0, RepeatMode.ONE: 1, RepeatMode.ALL: 2}

# sync constants
MIN_DEVIATION_ADJUST = 6  # 6 milliseconds
MIN_REQ_PLAYPOINTS = 8  # we need at least 8 measurements
MAX_SKIP_AHEAD_MS = 1500  # 1.5 seconds


@dataclass
class SyncPlayPoint:
    """Simple structure to describe a Sync Playpoint."""

    timestamp: float
    sync_job_id: str
    diff: int


CONF_CLI_TELNET_PORT = "cli_telnet_port"
CONF_CLI_JSON_PORT = "cli_json_port"
CONF_DISCOVERY = "discovery"
CONF_DISPLAY = "display"
CONF_VISUALIZATION = "visualization"

DEFAULT_PLAYER_VOLUME = 20
DEFAULT_SLIMPROTO_PORT = 3483
DEFAULT_VISUALIZATION = SlimVisualisationType.SPECTRUM_ANALYZER.value


CONF_ENTRY_DISPLAY = ConfigEntry(
    key=CONF_DISPLAY,
    type=ConfigEntryType.BOOLEAN,
    default_value=True,
    required=False,
    label="Enable display support",
    description="Enable/disable native display support on " "squeezebox or squeezelite32 hardware.",
    advanced=True,
)
CONF_ENTRY_VISUALIZATION = ConfigEntry(
    key=CONF_VISUALIZATION,
    type=ConfigEntryType.STRING,
    default_value=DEFAULT_VISUALIZATION,
    options=tuple(
        ConfigValueOption(title=x.name.replace("_", " ").title(), value=x.value)
        for x in SlimVisualisationType
    ),
    required=False,
    label="Visualization type",
    description="The type of visualization to show on the display "
    "during playback if the device supports this.",
    advanced=True,
    depends_on=CONF_DISPLAY,
)


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    prov = SlimprotoProvider(mass, manifest, config)
    await prov.handle_async_init()
    return prov


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
    return (
        ConfigEntry(
            key=CONF_CLI_TELNET_PORT,
            type=ConfigEntryType.INTEGER,
            default_value=9090,
            label="Classic Squeezebox CLI Port",
            description="Some slimproto based players require the presence of the telnet CLI "
            " to request more information. \n\n"
            "By default this CLI is hosted on port 9090 but some players also accept "
            "a different port. Set to 0 to disable this functionality.\n\n"
            "Commands allowed on this interface are very limited and just enough to satisfy "
            "player compatibility, so security risks are minimized to practically zero."
            "You may safely disable this option if you have no players that rely on this feature "
            "or you dont care about the additional metadata.",
            advanced=True,
        ),
        ConfigEntry(
            key=CONF_CLI_JSON_PORT,
            type=ConfigEntryType.INTEGER,
            default_value=9000,
            label="JSON-RPC CLI/API Port",
            description="Some slimproto based players require the presence of the JSON-RPC "
            "API from LMS to request more information. For example to fetch the album cover "
            "and other metadata. \n\n"
            "This JSON-RPC API is compatible with Logitech Media Server but not all commands "
            "are implemented. Just enough to satisfy player compatibility. \n\n"
            "By default this JSON CLI is hosted on port 9000 but most players also accept "
            "it on a different port. Set to 0 to disable this functionality.\n\n"
            "You may safely disable this option if you have no players that rely on this feature "
            "or you dont care about the additional metadata.",
            advanced=True,
        ),
        ConfigEntry(
            key=CONF_DISCOVERY,
            type=ConfigEntryType.BOOLEAN,
            default_value=True,
            label="Enable Discovery server",
            description="Broadcast discovery packets for slimproto clients to automatically "
            "discover and connect to this server. \n\n"
            "You may want to disable this feature if you are running multiple slimproto servers "
            "on your network and/or you don't want clients to auto connect to this server.",
            advanced=True,
        ),
        ConfigEntry(
            key=CONF_PORT,
            type=ConfigEntryType.INTEGER,
            default_value=DEFAULT_SLIMPROTO_PORT,
            label="Slimproto port",
            description="The TCP/UDP port to run the slimproto sockets server. "
            "The default is 3483 and using a different port is not supported by "
            "hardware squeezebox players. Only adjust this port if you want to "
            "use other slimproto based servers side by side with (squeezelite) software players.",
            advanced=True,
        ),
    )


class SlimprotoProvider(PlayerProvider):
    """Base/builtin provider for players using the SLIM protocol (aka slimproto)."""

    slimproto: SlimServer
    _sync_playpoints: dict[str, deque[SyncPlayPoint]]
    _do_not_resync_before: dict[str, float]

    @property
    def supported_features(self) -> tuple[ProviderFeature, ...]:
        """Return the features supported by this Provider."""
        return (ProviderFeature.SYNC_PLAYERS,)

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self._sync_playpoints = {}
        self._do_not_resync_before = {}
        self._resync_handle: asyncio.TimerHandle | None = None
        control_port = self.config.get_value(CONF_PORT)
        telnet_port = self.config.get_value(CONF_CLI_TELNET_PORT)
        json_port = self.config.get_value(CONF_CLI_JSON_PORT)
        logging.getLogger("aioslimproto").setLevel(self.logger.level)
        self.slimproto = SlimServer(
            cli_port=telnet_port or None,
            cli_port_json=json_port or None,
            ip_address=self.mass.streams.publish_ip,
            name="Music Assistant",
            control_port=control_port,
        )
        # start slimproto socket server
        try:
            await self.slimproto.start()
        except OSError as err:
            msg = f"Unable to start the Slimproto server - is port {control_port} already taken ?"
            raise SetupFailedError(msg) from err

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        self.slimproto.subscribe(self._client_callback)

    async def unload(self) -> None:
        """Handle close/cleanup of the provider."""
        await self.slimproto.stop()

    async def get_player_config_entries(self, player_id: str) -> tuple[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the given player (if any)."""
        base_entries = await super().get_player_config_entries(player_id)

        # create preset entries (for players that support it)
        preset_entries = ()
        presets = []
        async for playlist in self.mass.music.playlists.iter_library_items(True):
            presets.append(ConfigValueOption(playlist.name, playlist.uri))
        async for radio in self.mass.music.radio.iter_library_items(True):
            presets.append(ConfigValueOption(radio.name, radio.uri))
        preset_count = 10
        preset_entries = tuple(
            ConfigEntry(
                key=f"preset_{index}",
                type=ConfigEntryType.STRING,
                options=presets,
                label=f"Preset {index}",
                description="Assign a playable item to the player's preset. "
                "Only supported on real squeezebox hardware or jive(lite) based emulators.",
                advanced=False,
                required=False,
            )
            for index in range(1, preset_count + 1)
        )

        return (
            base_entries
            + preset_entries
            + (
                CONF_ENTRY_CROSSFADE,
                CONF_ENTRY_EQ_BASS,
                CONF_ENTRY_EQ_MID,
                CONF_ENTRY_EQ_TREBLE,
                CONF_ENTRY_OUTPUT_CHANNELS,
                CONF_ENTRY_CROSSFADE_DURATION,
                CONF_ENTRY_ENFORCE_MP3,
                CONF_ENTRY_SYNC_ADJUST,
                CONF_ENTRY_DISPLAY,
                CONF_ENTRY_VISUALIZATION,
            )
        )

    def on_player_config_changed(self, config: PlayerConfig, changed_keys: set[str]) -> None:
        """Call (by config manager) when the configuration of a player changes."""
        super().on_player_config_changed(config, changed_keys)

        if slimplayer := self.slimproto.get_player(config.player_id):
            self.mass.create_task(self._set_preset_items(slimplayer))
            self.mass.create_task(self._set_display(slimplayer))

    async def cmd_stop(self, player_id: str) -> None:
        """Send STOP command to given player."""
        # forward command to player and any connected sync members
        async with asyncio.TaskGroup() as tg:
            for slimplayer in self._get_sync_clients(player_id):
                tg.create_task(slimplayer.stop())

    async def cmd_play(self, player_id: str) -> None:
        """Send PLAY command to given player."""
        # forward command to player and any connected sync members
        async with asyncio.TaskGroup() as tg:
            for slimplayer in self._get_sync_clients(player_id):
                tg.create_task(slimplayer.play())

    async def play_media(
        self,
        player_id: str,
        queue_item: QueueItem,
        seek_position: int,
        fade_in: bool,
    ) -> None:
        """Handle PLAY MEDIA on given player.

        This is called by the Queue controller to start playing a queue item on the given player.
        The provider's own implementation should work out how to handle this request.

            - player_id: player_id of the player to handle the command.
            - queue_item: The QueueItem that needs to be played on the player.
            - seek_position: Optional seek to this position.
            - fade_in: Optionally fade in the item at playback start.
        """
        # fix race condition where resync and play media are called at more or less the same time
        if self._resync_handle:
            self._resync_handle.cancel()
            self._resync_handle = None
        player = self.mass.players.get(player_id)
        if player.synced_to:
            msg = "A synced player cannot receive play commands directly"
            raise RuntimeError(msg)
        enforce_mp3 = await self.mass.config.get_player_config_value(player_id, CONF_ENFORCE_MP3)
        if player.group_childs:
            # player has sync members, we need to start a multi slimplayer stream job
            stream_job = await self.mass.streams.create_multi_client_stream_job(
                queue_id=queue_item.queue_id,
                start_queue_item=queue_item,
                seek_position=int(seek_position),
                fade_in=fade_in,
            )
            # forward command to player and any connected sync members
            sync_clients = self._get_sync_clients(player_id)
            async with asyncio.TaskGroup() as tg:
                for slimplayer in sync_clients:
                    tg.create_task(
                        self._handle_play_url(
                            slimplayer,
                            url=stream_job.resolve_stream_url(
                                slimplayer.player_id,
                                output_codec=ContentType.MP3 if enforce_mp3 else ContentType.FLAC,
                            ),
                            queue_item=None,
                            send_flush=True,
                            auto_play=False,
                        )
                    )
        else:
            # regular, single player playback
            slimplayer = self.slimproto.get_player(player_id)
            if not slimplayer:
                return
            url = await self.mass.streams.resolve_stream_url(
                queue_item=queue_item,
                # for now just hardcode flac as we assume that every (modern)
                # slimproto based player can handle that just fine
                output_codec=ContentType.MP3 if enforce_mp3 else ContentType.FLAC,
                seek_position=seek_position,
                fade_in=fade_in,
                flow_mode=False,
            )
            await self._handle_play_url(
                slimplayer,
                url=url,
                queue_item=queue_item,
                send_flush=True,
                auto_play=True,
            )

    async def play_stream(self, player_id: str, stream_job: MultiClientStreamJob) -> None:
        """Handle PLAY STREAM on given player.

        This is a special feature from the Universal Group provider.
        """
        enforce_mp3 = await self.mass.config.get_player_config_value(player_id, CONF_ENFORCE_MP3)
        # fix race condition where resync and play media are called at more or less the same time
        if self._resync_handle:
            self._resync_handle.cancel()
            self._resync_handle = None
        # forward command to player and any connected sync members
        sync_clients = self._get_sync_clients(player_id)
        async with asyncio.TaskGroup() as tg:
            for slimplayer in sync_clients:
                tg.create_task(
                    self._handle_play_url(
                        slimplayer,
                        url=stream_job.resolve_stream_url(
                            slimplayer.player_id,
                            output_codec=ContentType.MP3 if enforce_mp3 else ContentType.FLAC,
                        ),
                        queue_item=None,
                        send_flush=True,
                        auto_play=False,
                    )
                )

    async def enqueue_next_queue_item(self, player_id: str, queue_item: QueueItem) -> None:
        """Handle enqueuing of the next queue item on the player."""
        if not (slimplayer := self.slimproto.get_player(player_id)):
            return
        enforce_mp3 = await self.mass.config.get_player_config_value(player_id, CONF_ENFORCE_MP3)
        url = await self.mass.streams.resolve_stream_url(
            queue_item=queue_item,
            output_codec=ContentType.MP3 if enforce_mp3 else ContentType.FLAC,
            flow_mode=False,
        )
        await self._handle_play_url(
            slimplayer,
            url=url,
            queue_item=queue_item,
            enqueue=True,
            send_flush=False,
            auto_play=True,
        )

    async def _handle_play_url(
        self,
        slimplayer: SlimClient,
        url: str,
        queue_item: QueueItem | None,
        enqueue: bool = False,
        send_flush: bool = True,
        auto_play: bool = False,
    ) -> None:
        """Handle playback of an url on slimproto player(s)."""
        player_id = slimplayer.player_id
        if crossfade := await self.mass.config.get_player_config_value(player_id, CONF_CROSSFADE):
            transition_duration = await self.mass.config.get_player_config_value(
                player_id, CONF_CROSSFADE_DURATION
            )
        else:
            transition_duration = 0

        if queue_item and queue_item.media_item:
            album = getattr(queue_item.media_item, "album", None)
            metadata = {
                "item_id": queue_item.queue_item_id,
                "title": queue_item.media_item.name,
                "album": album.name if album else "",
                "artist": getattr(queue_item.media_item, "artist_str", "Music Assistant"),
                "image_url": self.mass.metadata.get_image_url(
                    queue_item.image,
                    size=512,
                    prefer_proxy=True,
                )
                if queue_item.image
                else MASS_LOGO_ONLINE,
                "duration": queue_item.duration,
            }
        elif queue_item:
            metadata = {
                "item_id": queue_item.queue_item_id,
                "title": queue_item.name,
                "artist": "Music Assistant",
                "image_url": self.mass.metadata.get_image_url(
                    queue_item.image,
                    size=512,
                    prefer_proxy=True,
                )
                if queue_item.image
                else MASS_LOGO_ONLINE,
                "duration": queue_item.duration,
            }
        else:
            metadata = {
                "item_id": "flow",
                "title": "Music Assistant",
                "image_url": MASS_LOGO_ONLINE,
            }
        queue = self.mass.player_queues.get(queue_item.queue_id if queue_item else player_id)
        slimplayer.extra_data["playlist repeat"] = REPEATMODE_MAP[queue.repeat_mode]
        slimplayer.extra_data["playlist shuffle"] = int(queue.shuffle_enabled)
        # slimplayer.extra_data["can_seek"] = 1 if queue_item else 0
        await slimplayer.play_url(
            url=url,
            mime_type=f"audio/{url.split('.')[-1].split('?')[0]}",
            metadata=metadata,
            enqueue=enqueue,
            send_flush=send_flush,
            transition=SlimTransition.CROSSFADE if crossfade else SlimTransition.NONE,
            transition_duration=transition_duration,
            # if autoplay=False playback will not start automatically
            # instead 'buffer ready' will be called when the buffer is full
            # to coordinate a start of multiple synced players
            autostart=auto_play,
        )

    async def cmd_pause(self, player_id: str) -> None:
        """Send PAUSE command to given player."""
        # forward command to player and any connected sync members
        async with asyncio.TaskGroup() as tg:
            for slimplayer in self._get_sync_clients(player_id):
                tg.create_task(slimplayer.pause())

    async def cmd_power(self, player_id: str, powered: bool) -> None:
        """Send POWER command to given player."""
        if slimplayer := self.slimproto.get_player(player_id):
            await slimplayer.power(powered)
            # store last state in cache
            await self.mass.cache.set(
                f"{CACHE_KEY_PREV_STATE}.{player_id}", (powered, slimplayer.volume_level)
            )

    async def cmd_volume_set(self, player_id: str, volume_level: int) -> None:
        """Send VOLUME_SET command to given player."""
        if slimplayer := self.slimproto.get_player(player_id):
            await slimplayer.volume_set(volume_level)
            # store last state in cache
            await self.mass.cache.set(
                f"{CACHE_KEY_PREV_STATE}.{player_id}", (slimplayer.powered, volume_level)
            )

    async def cmd_volume_mute(self, player_id: str, muted: bool) -> None:
        """Send VOLUME MUTE command to given player."""
        if slimplayer := self.slimproto.get_player(player_id):
            await slimplayer.mute(muted)

    async def cmd_sync(self, player_id: str, target_player: str) -> None:
        """Handle SYNC command for given player."""
        child_player = self.mass.players.get(player_id)
        assert child_player  # guard
        parent_player = self.mass.players.get(target_player)
        assert parent_player  # guard
        if parent_player.synced_to:
            raise RuntimeError("Player is already synced")
        if child_player.synced_to and child_player.synced_to != target_player:
            raise RuntimeError("Player is already synced to another player")
        # always make sure that the parent player is part of the sync group
        parent_player.group_childs.add(parent_player.player_id)
        parent_player.group_childs.add(child_player.player_id)
        child_player.synced_to = parent_player.player_id
        # check if we should (re)start or join a stream session
        active_queue = self.mass.player_queues.get_active_queue(parent_player.player_id)
        if active_queue.state == PlayerState.PLAYING:
            # playback needs to be restarted to form a new multi slimplayer stream session
            def resync() -> None:
                self._resync_handle = None
                self.mass.create_task(
                    self.mass.player_queues.resume(active_queue.queue_id, fade_in=False)
                )

            # this could potentially be called by multiple players at the exact same time
            # so we debounce the resync a bit here with a timer
            if self._resync_handle:
                self._resync_handle.cancel()
            self._resync_handle = self.mass.loop.call_later(0.5, resync)
        else:
            # make sure that the player manager gets an update
            self.mass.players.update(child_player.player_id, skip_forward=True)
            self.mass.players.update(parent_player.player_id, skip_forward=True)

    async def cmd_unsync(self, player_id: str) -> None:
        """Handle UNSYNC command for given player."""
        child_player = self.mass.players.get(player_id)
        parent_player = self.mass.players.get(child_player.synced_to)
        # make sure to send stop to the player
        await self.cmd_stop(child_player.player_id)
        child_player.synced_to = None
        with suppress(KeyError):
            parent_player.group_childs.remove(child_player.player_id)
        if parent_player.group_childs == {parent_player.player_id}:
            # last child vanished; the sync group is dissolved
            parent_player.group_childs.remove(parent_player.player_id)
        self.mass.players.update(child_player.player_id)
        self.mass.players.update(parent_player.player_id)

    def _client_callback(
        self,
        event: SlimEvent,
    ) -> None:
        if self.mass.closing:
            return

        if event.type == SlimEventType.PLAYER_DISCONNECTED:
            if mass_player := self.mass.players.get(event.player_id):
                mass_player.available = False
                self.mass.players.update(mass_player.player_id)
            return

        if not (slimplayer := self.slimproto.get_player(event.player_id)):
            return

        if event.type == SlimEventType.PLAYER_CONNECTED:
            self.mass.create_task(self._handle_connected(slimplayer))
            return

        if event.type == SlimEventType.PLAYER_BUFFER_READY:
            self.mass.create_task(self._handle_buffer_ready(slimplayer))
            return

        if event.type == SlimEventType.PLAYER_HEARTBEAT:
            self._handle_player_heartbeat(slimplayer)
            return

        if event.type in (SlimEventType.PLAYER_BTN_EVENT, SlimEventType.PLAYER_CLI_EVENT):
            self.mass.create_task(self._handle_player_cli_event(slimplayer, event))
            return

        # forward player update to MA player controller
        self.mass.create_task(self._handle_player_update(slimplayer))

    async def _handle_player_update(self, slimplayer: SlimClient) -> None:
        """Process SlimClient update/add to Player controller."""
        player_id = slimplayer.player_id
        player = self.mass.players.get(player_id, raise_unavailable=False)
        if not player:
            # player does not yet exist, create it
            player = Player(
                player_id=player_id,
                provider=self.instance_id,
                type=PlayerType.PLAYER,
                name=slimplayer.name,
                available=True,
                powered=slimplayer.powered,
                device_info=DeviceInfo(
                    model=slimplayer.device_model,
                    address=slimplayer.device_address,
                    manufacturer=slimplayer.device_type,
                ),
                supported_features=(
                    PlayerFeature.POWER,
                    PlayerFeature.SYNC,
                    PlayerFeature.VOLUME_SET,
                    PlayerFeature.PAUSE,
                    PlayerFeature.VOLUME_MUTE,
                    PlayerFeature.ENQUEUE_NEXT,
                ),
                max_sample_rate=int(slimplayer.max_sample_rate),
                supports_24bit=int(slimplayer.max_sample_rate) > 44100,
                can_sync_with=tuple(
                    x.player_id for x in self.slimproto.players if x.player_id != player_id
                ),
            )
            self.mass.players.register_or_update(player)

        # update player state on player events
        player.available = True
        player.current_item_id = (
            slimplayer.current_media.metadata.get("item_id")
            if slimplayer.current_media and slimplayer.current_media.metadata
            else slimplayer.current_url
        )
        player.active_source = player.player_id
        player.name = slimplayer.name
        player.powered = slimplayer.powered
        player.state = STATE_MAP[slimplayer.state]
        player.volume_level = slimplayer.volume_level
        player.volume_muted = slimplayer.muted
        self.mass.players.update(player_id)

    def _handle_player_heartbeat(self, slimplayer: SlimClient) -> None:
        """Process SlimClient elapsed_time update."""
        if slimplayer.state == SlimPlayerState.STOPPED:
            # ignore server heartbeats when stopped
            return

        # elapsed time change on the player will be auto picked up
        # by the player manager.
        player = self.mass.players.get(slimplayer.player_id)
        player.elapsed_time = slimplayer.elapsed_seconds
        player.elapsed_time_last_updated = time.time()

        # handle sync
        if player.synced_to:
            self._handle_client_sync(slimplayer)

    async def _handle_player_cli_event(self, slimplayer: SlimClient, event: SlimEvent) -> None:
        """Process CLI Event."""
        if not event.data:
            return
        queue = self.mass.player_queues.get_active_queue(slimplayer.player_id)
        if event.data.startswith("button preset_") and event.data.endswith(".single"):
            preset_id = event.data.split("preset_")[1].split(".")[0]
            preset_index = int(preset_id) - 1
            if len(slimplayer.presets) >= preset_index + 1:
                preset = slimplayer.presets[preset_index]
                await self.mass.player_queues.play_media(queue.queue_id, preset.uri)
        elif event.data == "button repeat":
            if queue.repeat_mode == RepeatMode.OFF:
                repeat_mode = RepeatMode.ONE
            elif queue.repeat_mode == RepeatMode.ONE:
                repeat_mode = RepeatMode.ALL
            else:
                repeat_mode = RepeatMode.OFF
            self.mass.player_queues.set_repeat(queue.queue_id, repeat_mode)
            slimplayer.extra_data["playlist repeat"] = REPEATMODE_MAP[queue.repeat_mode]
            slimplayer.signal_update()
        elif event.data == "button shuffle":
            self.mass.player_queues.set_shuffle(queue.queue_id, not queue.shuffle_enabled)
            slimplayer.extra_data["playlist shuffle"] = int(queue.shuffle_enabled)
            slimplayer.signal_update()
        elif event.data == "button jump_fwd":
            await self.mass.player_queues.next(queue.queue_id)
        elif event.data == "button jump_rew":
            await self.mass.player_queues.previous(queue.queue_id)
        elif event.data.startswith("time "):
            # seek request
            _, param = event.data.split(" ", 1)
            if param.isnumeric():
                await self.mass.player_queues.seek(queue.queue_id, int(param))
        self.logger.debug("CLI Event: %s", event.data)

    def _handle_client_sync(self, slimplayer: SlimClient) -> None:
        """Synchronize audio of a sync slimplayer."""
        player = self.mass.players.get(slimplayer.player_id)
        sync_master_id = player.synced_to
        if not sync_master_id:
            # we only correct sync members, not the sync master itself
            return
        if not (sync_master := self.slimproto.get_player(sync_master_id)):
            return  # just here as a guard as bad things can happen

        if sync_master.state != SlimPlayerState.PLAYING:
            return
        if slimplayer.state != SlimPlayerState.PLAYING:
            return

        if backoff_time := self._do_not_resync_before.get(slimplayer.player_id):
            # player has set a timestamp we should backoff from syncing it
            if time.time() < backoff_time:
                return

        # we collect a few playpoints of the player to determine
        # average lag/drift so we can adjust accordingly
        sync_playpoints = self._sync_playpoints.setdefault(
            slimplayer.player_id, deque(maxlen=MIN_REQ_PLAYPOINTS)
        )

        active_queue = self.mass.player_queues.get_active_queue(slimplayer.player_id)
        stream_job = self.mass.streams.multi_client_jobs.get(active_queue.queue_id)
        if not stream_job:
            # should not happen, but just in case
            return

        last_playpoint = sync_playpoints[-1] if sync_playpoints else None
        if last_playpoint and (time.time() - last_playpoint.timestamp) > 10:
            # last playpoint is too old, invalidate
            sync_playpoints.clear()
        if last_playpoint and last_playpoint.sync_job_id != stream_job.job_id:
            # streamjob has changed, invalidate
            sync_playpoints.clear()

        diff = int(
            self._get_corrected_elapsed_milliseconds(sync_master)
            - self._get_corrected_elapsed_milliseconds(slimplayer)
        )

        # we can now append the current playpoint to our list
        sync_playpoints.append(SyncPlayPoint(time.time(), stream_job.job_id, diff))

        if len(sync_playpoints) < MIN_REQ_PLAYPOINTS:
            return

        # get the average diff
        avg_diff = statistics.fmean(x.diff for x in sync_playpoints)
        delta = int(abs(avg_diff))

        if delta < MIN_DEVIATION_ADJUST:
            return

        # resync the player by skipping ahead or pause for x amount of (milli)seconds
        sync_playpoints.clear()
        self._do_not_resync_before[slimplayer.player_id] = time.time() + (delta / 1000) + 2
        if avg_diff > MAX_SKIP_AHEAD_MS:
            # player lagging behind more than MAX_SKIP_AHEAD_MS,
            # we need to correct the sync_master
            self.logger.warning(
                "%s is lagging behind more than %s milliseconds!",
                player.display_name,
                MAX_SKIP_AHEAD_MS,
            )
            self.mass.create_task(sync_master.pause_for(delta))
        elif avg_diff > 0:
            # handle player lagging behind, fix with skip_ahead
            self.logger.debug("%s resync: skipAhead %sms", player.display_name, delta)
            self.mass.create_task(slimplayer.skip_over(delta))
        else:
            # handle player is drifting too far ahead, use pause_for to adjust
            self.logger.debug("%s resync: pauseFor %sms", player.display_name, delta)
            self.mass.create_task(slimplayer.pause_for(delta))

    async def _handle_buffer_ready(self, slimplayer: SlimClient) -> None:
        """Handle buffer ready event, player has buffered a (new) track.

        Only used when autoplay=0 for coordinated start of synced players.
        """
        player = self.mass.players.get(slimplayer.player_id)
        if player.synced_to:
            # unpause of sync child is handled by sync master
            return
        if not player.group_childs:
            # not a sync group, continue
            await slimplayer.play()
            return
        count = 0
        while count < 40:
            childs_total = 0
            childs_ready = 0
            for sync_child in self._get_sync_clients(player.player_id):
                childs_total += 1
                if sync_child.state == SlimPlayerState.BUFFER_READY:
                    childs_ready += 1
            if childs_total == childs_ready:
                break
            await asyncio.sleep(0.1)
        # all child's ready (or timeout) - start play
        async with asyncio.TaskGroup() as tg:
            for _client in self._get_sync_clients(player.player_id):
                timestamp = _client.jiffies + 500
                sync_delay = self.mass.config.get_raw_player_config_value(
                    _client.player_id, CONF_SYNC_ADJUST, 0
                )
                timestamp -= sync_delay
                self._do_not_resync_before[_client.player_id] = time.time() + 1
                tg.create_task(_client.unpause_at(int(timestamp)))

    async def _handle_connected(self, slimplayer: SlimClient) -> None:
        """Handle a slimplayer connected event."""
        player_id = slimplayer.player_id
        self.logger.info("Player %s connected", slimplayer.name or player_id)
        # set presets and display
        await self._set_preset_items(slimplayer)
        await self._set_display(slimplayer)
        # update all attributes
        await self._handle_player_update(slimplayer)
        # update existing players so they can update their `can_sync_with` field
        for _player in self.players:
            _player.can_sync_with = tuple(
                x.player_id for x in self.slimproto.players if x.player_id != _player.player_id
            )
            self.mass.players.update(_player.player_id)
        # restore volume and power state
        if last_state := await self.mass.cache.get(f"{CACHE_KEY_PREV_STATE}.{player_id}"):
            init_power = last_state[0]
            init_volume = last_state[1]
        else:
            init_volume = DEFAULT_PLAYER_VOLUME
            init_power = False
        await slimplayer.power(init_power)
        await slimplayer.volume_set(init_volume)

    def _get_sync_clients(self, player_id: str) -> Iterator[SlimClient]:
        """Get all sync clients for a player."""
        player = self.mass.players.get(player_id)
        # we need to return the player itself too
        group_child_ids = {player_id}
        group_child_ids.update(player.group_childs)
        for child_id in group_child_ids:
            if slimplayer := self.slimproto.get_player(child_id):
                yield slimplayer

    def _get_corrected_elapsed_milliseconds(self, slimplayer: SlimClient) -> int:
        """Return corrected elapsed milliseconds."""
        sync_delay = self.mass.config.get_raw_player_config_value(
            slimplayer.player_id, CONF_SYNC_ADJUST, 0
        )
        return slimplayer.elapsed_milliseconds - sync_delay

    async def _set_preset_items(self, slimplayer: SlimClient) -> None:
        """Set the presets for a player."""
        preset_items: list[SlimPreset] = []
        for preset_index in range(1, 11):
            if preset_conf := self.mass.config.get_raw_player_config_value(
                slimplayer.player_id, f"preset_{preset_index}"
            ):
                try:
                    media_item = await self.mass.music.get_item_by_uri(preset_conf)
                    preset_items.append(
                        SlimPreset(
                            uri=media_item.uri,
                            text=media_item.name,
                            icon=self.mass.metadata.get_image_url(media_item.image),
                        )
                    )
                except MusicAssistantError:
                    # non-existing media item or some other edge case
                    preset_items.append(
                        SlimPreset(
                            uri=f"preset_{preset_index}",
                            text=f"ERROR <preset {preset_index}>",
                            icon="",
                        )
                    )
            else:
                break
        slimplayer.presets = preset_items

    async def _set_display(self, slimplayer: SlimClient) -> None:
        """Set the display config for a player."""
        display_enabled = self.mass.config.get_raw_player_config_value(
            slimplayer.player_id, CONF_DISPLAY, True
        )
        visualization = self.mass.config.get_raw_player_config_value(
            slimplayer.player_id, CONF_VISUALIZATION, DEFAULT_VISUALIZATION
        )
        await slimplayer.configure_display(
            visualisation=SlimVisualisationType(visualization), disabled=not display_enabled
        )
