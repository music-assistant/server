"""Squeezelite Player implementation."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, cast

from aioslimproto.models import ButtonCode, RemoteCode, SlimEvent
from aioslimproto.models import EventType as SlimEventType
from aioslimproto.models import Preset as SlimPreset
from aioslimproto.models import VisualisationType as SlimVisualisationType
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import (
    ConfigEntryType,
    IdentifierType,
    MediaType,
    PlaybackState,
    PlayerFeature,
    ProtocolRole,
    RepeatMode,
)
from music_assistant_models.errors import MusicAssistantError

from music_assistant.constants import (
    CONF_ENTRY_PREFER_WAV_FOR_LIVE_SOURCES_DEFAULT_ENABLED,
    CONF_ENTRY_SYNC_ADJUST,
    VERBOSE_LOG_LEVEL,
)
from music_assistant.helpers.audio import get_mime_type
from music_assistant.models.player import DeviceInfo, PlayerMedia
from music_assistant.models.player_protocol import PlayerProtocol
from music_assistant.models.player_state import POSITION_JUMP_THRESHOLD
from music_assistant.providers.sendspin.bridge_manager import evaluate_bridge_of

from .constants import (
    CONF_ENTRY_DISPLAY,
    CONF_ENTRY_VISUALIZATION,
    CONF_STORED_POWER,
    CONF_STORED_VOLUME,
    REPEATMODE_BY_LMS_VALUE,
    REPEATMODE_MAP,
    STATE_MAP,
)

if TYPE_CHECKING:
    from aioslimproto.client import SlimClient
    from aioslimproto.models import MediaMetadata
    from music_assistant_models.player_queue import PlayerQueue

    from .provider import SqueezelitePlayerProvider
    from .sendspin_bridge import SendspinSqueezeliteBridge


# Device models of the LMS bridge tools that expose AirPlay/Chromecast/UPnP devices as
# squeezelite players.
BRIDGE_TOOL_MODELS = ("raopbridge", "castbridge", "upnpbridge")

PROTOCOL_ONLY_MODELS = (
    # Device models where slimproto is only a secondary protocol on a device with
    # its own (native) identity: WiiM/LinkPlay devices (ModelName=WiiM Player) and
    # the LMS bridge tools that expose AirPlay/Chromecast/UPnP devices as
    # squeezelite players. These attach as an audio protocol to the player of
    # the device.
    "wiim",
    *BRIDGE_TOOL_MODELS,
)


def is_protocol_only_device(device_model: str) -> bool:
    """Return True if the device uses squeezelite as a secondary protocol only."""
    device_model_lower = device_model.lower()
    return any(model.lower() in device_model_lower for model in PROTOCOL_ONLY_MODELS)


def is_bridge_tool(device_model: str) -> bool:
    """Return True if the device is an LMS bridge tool for a device of another protocol."""
    device_model_lower = device_model.lower()
    return any(model in device_model_lower for model in BRIDGE_TOOL_MODELS)


class SqueezelitePlayer(PlayerProtocol):
    """Squeezelite Player implementation."""

    # the Sendspin bridge of this player while it has one, set by that bridge
    sendspin_bridge: SendspinSqueezeliteBridge | None = None
    # set once the saved power state is restored: the state a player connects with is not saved
    _power_tracking: bool = False

    def __init__(
        self,
        provider: SqueezelitePlayerProvider,
        protocol_id: str,
        client: SlimClient,
    ) -> None:
        """Initialize the Squeezelite Player."""
        super().__init__(provider, protocol_id)
        self.client = client
        self._provider: SqueezelitePlayerProvider = provider
        # Set static player attributes
        self._attr_supported_features = {
            PlayerFeature.PLAY_MEDIA,
            PlayerFeature.MULTI_DEVICE_DSP,
            PlayerFeature.VOLUME_SET,
            PlayerFeature.PAUSE,
            PlayerFeature.ENQUEUE,
            PlayerFeature.GAPLESS_PLAYBACK,
        }
        # Audio protocols are powered on with the stream and expose no power
        # control; full players get native power support (slimproto power can e.g.
        # drive a GPIO/script wired to an amplifier).
        if is_protocol_only_device(client.device_model):
            self._attr_role = ProtocolRole.AUDIO
        else:
            self._attr_supported_features.add(PlayerFeature.POWER)
        max_sr = int(self.client.max_sample_rate)
        self._attr_supported_sample_rates = [
            (sr, bd)
            for sr in (44100, 48000, 88200, 96000, 176400, 192000)
            if sr <= max_sr
            for bd in (16, 24)
        ]

    async def on_config_updated(self) -> None:
        """Handle logic when the PlayerConfig is first loaded or updated."""
        # set presets and display
        await self._set_preset_items()
        await self._set_display()
        # not awaited: the attach of this protocol does not wait for its Sendspin bridge
        self.mass.create_task(evaluate_bridge_of(self.mass, self.protocol_id))

    def on_player_media_updated(self) -> None:
        """Handle callback when the current media of the player is updated."""
        if (bridge := self.sendspin_bridge) is not None:
            # the stream of the bridge plays on from track to track
            bridge.update_now_playing()
        if queue := self._active_queue():
            self._report_repeat_shuffle(queue)

    def on_queue_updated(self, queue_id: str) -> None:
        """
        Report repeat and shuffle to the client when the queue the player plays changes.

        :param queue_id: The id of the queue that changed.
        """
        if (queue := self._active_queue()) and queue.queue_id == queue_id:
            self._report_repeat_shuffle(queue)

    async def on_remove(self) -> None:
        """Handle logic when the protocol is removed for good."""
        await self._provider.bridge_manager.remove_bridge(self.protocol_id, permanent=True)

    async def setup(self) -> None:
        """Set up the player and register it."""
        protocol_id = self.client.player_id
        self.logger.info("Player %s connected", self.client.name or protocol_id)
        # update all dynamic attributes
        self.update_attributes()
        # its volume was restored on connect, by the provider's _get_initial_volume
        await self.client.stop()
        await self.mass.players.register(self)
        # carries a volume from the legacy cache over before that entry expires; only after the
        # attach, which creates the record of a new protocol
        self._store_volume()
        await self._restore_power()

    async def get_config_entries(self) -> list[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the player."""
        base_entries = await super().get_config_entries()
        # create preset entries (for players that support it)
        presets = []
        async for playlist in self.mass.music.playlists.iter_library_items(True):
            presets.append(ConfigValueOption(playlist.uri, title=playlist.name))
        async for radio in self.mass.music.radio.iter_library_items(True):
            presets.append(ConfigValueOption(radio.uri, title=radio.name))
        preset_count = 10
        preset_entries = [
            ConfigEntry(
                key=f"preset_{index}",
                type=ConfigEntryType.STRING,
                options=presets,
                translation_key="preset",
                translation_params=[str(index)],
                category="presets",
                required=False,
            )
            for index in range(1, preset_count + 1)
        ]
        return [
            *base_entries,
            *preset_entries,
            CONF_ENTRY_SYNC_ADJUST,
            CONF_ENTRY_DISPLAY,
            CONF_ENTRY_VISUALIZATION,
            CONF_ENTRY_PREFER_WAV_FOR_LIVE_SOURCES_DEFAULT_ENABLED,
        ]

    async def volume_set(self, volume_level: int) -> None:
        """Handle VOLUME_SET command on the player."""
        await self.client.volume_set(volume_level)

    async def volume_mute(self, muted: bool) -> None:
        """Handle VOLUME MUTE command on the player."""
        await self.client.mute(muted)

    async def power(self, powered: bool) -> None:
        """Handle POWER command on the player."""
        await self.client.power(powered)

    async def stop(self) -> None:
        """Handle STOP command on the player."""
        if (bridge := self.sendspin_bridge) is not None:
            # a stream of the Sendspin bridge ends with the stop below
            bridge.stop_streaming()
        # like LMS, a stop leaves the output on: a WiiM answers a power off with a pause
        # request a few seconds later, which pauses whatever was started in between
        await self.client.stop()
        self.update_state()

    async def play(self) -> None:
        """Handle PLAY command on the player."""
        await self.client.play()

    async def pause(self) -> None:
        """Handle PAUSE command on the player."""
        await self.client.pause()

    async def play_media(self, media: PlayerMedia) -> None:
        """Handle PLAY MEDIA on the player."""
        # don't report the old stream's position until the new stream's first heartbeat
        self._attr_elapsed_time = 0
        self._attr_elapsed_time_last_updated = time.time()

        # a Sendspin bridge that plays on the player gives it back: the new stream
        # replaces the stream of that bridge
        if (bridge := self.sendspin_bridge) is not None:
            bridge.stop_streaming()

        # Clear next media item during announcements to prevent playing the
        # next enqueued track after it finishes.
        if media.media_type == MediaType.ANNOUNCEMENT:
            self.client._next_media = None

        stream_url = await self.provider.mass.streams.resolve_stream_url(self.protocol_id, media)
        await self._handle_play_url_for_slimplayer(
            url=stream_url,
            media=media,
            send_flush=True,
            auto_play=False,
        )
        self._sync_elapsed_after_flush()

    async def enqueue_next_media(self, media: PlayerMedia) -> None:
        """Handle enqueuing next media item."""
        stream_url = await self.provider.mass.streams.resolve_stream_url(self.protocol_id, media)
        await self._handle_play_url_for_slimplayer(
            url=stream_url,
            media=media,
            enqueue=True,
            send_flush=False,
            auto_play=True,
        )

    def release_native_playback(self) -> None:
        """
        End what this protocol plays itself, for a stream that takes the player over.

        The protocol reports no position of its own anymore. The stream on the player
        itself is left to the one taking over.
        """
        self._attr_elapsed_time = 0
        self._attr_elapsed_time_last_updated = time.time()

    def handle_slim_event(self, event: SlimEvent) -> None:
        """Handle player event from slimproto server."""
        if event.type == SlimEventType.PLAYER_BUFFER_READY:
            # the Sendspin bridge starts its own stream, at the instant of its timeline
            if not self._plays_for_bridge:
                self.mass.create_task(self.client.unpause_at(self.client.jiffies))
            return

        if event.type == SlimEventType.PLAYER_HEARTBEAT:
            # the position of the stream of the Sendspin bridge is not one of this protocol
            if not self._plays_for_bridge:
                self._handle_player_heartbeat()
            return

        if event.type == SlimEventType.PLAYER_BTN_EVENT:
            self.mass.create_task(self._handle_player_button_event(event))
            return

        if event.type == SlimEventType.PLAYER_CLI_EVENT:
            self.mass.create_task(self._handle_player_cli_event(event))
            return

        # all other: update attributes and update state
        self.update_attributes()
        self.update_state()
        self._store_volume()
        self._report_delivered_format()

    def update_attributes(self) -> None:
        """Update player attributes from slim player."""
        # Update player state from slim player
        self._attr_available = self.client.connected
        self._attr_name = self.client.name
        was_powered = self._attr_powered
        self._attr_powered = self.client.powered
        if self._power_tracking and self.client.connected and self._attr_powered != was_powered:
            self.mass.config.set_protocol_value(
                self.protocol_id, CONF_STORED_POWER, self._attr_powered
            )
        old_state = self._attr_playback_state
        self._attr_playback_state = STATE_MAP[self.client.state]
        self._attr_volume_level = self.client.volume_level
        self._attr_volume_muted = self.client.muted
        self._attr_device_info = DeviceInfo(
            model=self.client.device_model,
            manufacturer=self.client.device_type,
        )
        self._attr_device_info.add_identifier(IdentifierType.IP_ADDRESS, self.client.device_address)
        # player_id is the MAC address in slimproto
        self._attr_device_info.add_identifier(IdentifierType.MAC_ADDRESS, self.client.player_id)
        # several squeezelite instances on one host share its IP (and possibly its MAC), and
        # each tells its own slimproto id: that id keeps the instances apart
        self._attr_device_info.add_identifier(IdentifierType.UUID, self.client.player_id)
        if (
            old_state != PlaybackState.PLAYING
            and self._attr_playback_state == PlaybackState.PLAYING
        ):
            # Invalidate elapsed time interpolation to avoid jumps when resuming from pause/stop
            # We need this because some players (e.g. WiiM) keep sending increasing elapsed time
            self._attr_elapsed_time_last_updated = time.time()
        # Update current media if available
        if self.client.current_media and (metadata := self.client.current_media.metadata):
            self._attr_current_media = PlayerMedia(
                uri=metadata.get("item_id"),
                title=metadata.get("title"),
                album=metadata.get("album"),
                artist=metadata.get("artist"),
                image_url=metadata.get("image_url"),
                duration=metadata.get("duration"),
                source_id=cast("str | None", metadata.get("source_id")),
                queue_item_id=cast("str | None", metadata.get("queue_item_id")),
            )
        else:
            self._attr_current_media = None
        if self._plays_for_bridge:
            # the stream of the Sendspin bridge is no media of this protocol
            self._attr_current_media = None

    async def _handle_play_url_for_slimplayer(
        self,
        url: str,
        media: PlayerMedia,
        enqueue: bool = False,
        send_flush: bool = True,
        auto_play: bool = False,
    ) -> None:
        """Handle playback of an url on the slimproto player."""
        mime_type = get_mime_type(url.rsplit(".", maxsplit=1)[-1].split("?", maxsplit=1)[0])
        # MediaMetadata has no source and queue item ids; they ride along so
        # current_media can be restored from the client
        metadata = cast(
            "MediaMetadata",
            {
                "item_id": media.uri,
                "title": media.title,
                "album": media.album,
                "artist": media.artist,
                "image_url": media.image_url,
                # the whole track, to match the position in the track that is reported
                "duration": media.duration or media.stream_duration,
                "source_id": media.source_id,
                "queue_item_id": media.queue_item_id,
            },
        )
        queue = self.mass.player_queues.get(media.source_id) if media.source_id else None
        # live streams and announcements start on a smaller buffer (KB, tenths of a second) to cut
        # latency; a short announcement would never fill the default 2 second output buffer
        low_latency_stream = media.media_type in (
            MediaType.AUDIO_SOURCE,
            MediaType.RADIO,
            MediaType.ANNOUNCEMENT,
        )
        stream_threshold, output_threshold = (64, 1) if low_latency_stream else (200, 20)
        await self.client.play_url(
            url=url,
            mime_type=mime_type,
            metadata=metadata,
            enqueue=enqueue,
            send_flush=send_flush,
            # if autoplay=False playback will not start automatically
            # instead 'buffer ready' will be called when the buffer is full
            autostart=auto_play,
            stream_threshold=stream_threshold,
            output_threshold=output_threshold,
        )
        # if queue is set to single track repeat,
        # immediately set this track as the next
        # this prevents race conditions with super short audio clips (on single repeat)
        # https://github.com/music-assistant/hass-music-assistant/issues/2059
        if queue and queue.repeat_mode == RepeatMode.ONE:
            self.mass.call_later(
                0.2,
                self.client.play_url(
                    url=url,
                    mime_type=mime_type,
                    metadata=metadata,
                    enqueue=True,
                    send_flush=False,
                    autostart=True,
                    stream_threshold=stream_threshold,
                    output_threshold=output_threshold,
                ),
            )

    def _handle_player_heartbeat(self) -> None:
        """Process SlimClient elapsed_time update."""
        if self._attr_playback_state != PlaybackState.PLAYING:
            # ignore server heartbeats when not playing
            # Some players keep sending heartbeat with increasing elapsed time
            # even when paused (e.g. WiiM)
            return
        self._attr_elapsed_time = self.client.elapsed_seconds
        self._attr_elapsed_time_last_updated = time.time()
        # only involve the state machine when the reported position diverged (e.g. buffering/seek)
        published_position = self.player.corrected_elapsed_time if self.player is not None else None
        if (
            published_position is None
            or abs(published_position - self.client.elapsed_seconds) > POSITION_JUMP_THRESHOLD
        ):
            self.update_state()

    async def _handle_player_cli_event(self, event: SlimEvent) -> None:
        """Process CLI Event."""
        if not event.data:
            return
        # event data is str, not dict
        # TODO: fix this in the aioslimproto lib
        event_data = cast("str", event.data)
        if not (queue := self._active_queue()):
            return
        if event_data.startswith("button preset_") and event_data.endswith(".single"):
            preset_id = event_data.split("preset_")[1].split(".")[0]
            preset_index = int(preset_id) - 1
            if len(self.client.presets) >= preset_index + 1:
                preset = self.client.presets[preset_index]
                await self.mass.player_queues.play_media(queue.queue_id, preset.uri)
        elif event_data == "button repeat" or event_data.startswith("playlist repeat"):
            # a client such as a WiiM sends the mode it wants as its LMS number; a button,
            # or a request without a number, cycles through the modes
            lms_value = event_data.removeprefix("playlist repeat ")
            if lms_value in REPEATMODE_BY_LMS_VALUE:
                repeat_mode = REPEATMODE_BY_LMS_VALUE[lms_value]
            elif queue.repeat_mode == RepeatMode.OFF:
                repeat_mode = RepeatMode.ONE
            elif queue.repeat_mode == RepeatMode.ONE:
                repeat_mode = RepeatMode.ALL
            else:
                repeat_mode = RepeatMode.OFF
            await self.mass.player_queues.set_repeat(queue.queue_id, repeat_mode)
            # publish the requested mode: on a delegated queue the queue snapshot only
            # updates once the session's options echo lands
            self.client.extra_data["playlist repeat"] = REPEATMODE_MAP[repeat_mode]
            self.client.signal_update()
        elif event_data == "button shuffle" or event_data.startswith("playlist shuffle"):
            lms_value = event_data.removeprefix("playlist shuffle ")
            # LMS shuffles by track (1) or by album (2); without a number it toggles
            if lms_value in ("0", "1", "2"):
                shuffle_enabled = lms_value != "0"
            else:
                shuffle_enabled = not queue.shuffle_enabled
            await self.mass.player_queues.set_shuffle(queue.queue_id, shuffle_enabled)
            self.client.extra_data["playlist shuffle"] = int(shuffle_enabled)
            self.client.signal_update()
        elif event_data in ("button jump_fwd", "button fwd"):
            await self.mass.player_queues.next(queue.queue_id)
        elif event_data in ("button jump_rew", "button rew"):
            await self.mass.player_queues.previous(queue.queue_id)
        elif event_data.startswith("time "):
            # seek request
            _, param = event_data.split(" ", 1)
            if param.isnumeric():
                await self.mass.player_queues.seek(queue.queue_id, int(param))
        self.logger.log(VERBOSE_LOG_LEVEL, "CLI Event: %s", event_data)

    async def _handle_player_button_event(self, event: SlimEvent) -> None:
        """
        Handle a button or remote key of the device that aioslimproto passed on.

        aioslimproto passes play and pause on when the device can not act on them itself,
        such as on a stopped device: play starts playback on the player and pause toggles
        it, which reaches the group that owns its playback. Other buttons are ignored.

        :param event: The button event of the device.
        """
        match event.data:
            case {"type": "butn", "button": ButtonCode.PLAY} | {
                "type": "ir",
                "code": RemoteCode.PLAY,
            }:
                command = self.mass.players.cmd_play
            case {"type": "butn", "button": ButtonCode.PAUSE} | {
                "type": "ir",
                "code": RemoteCode.PAUSE,
            }:
                command = self.mass.players.cmd_play_pause
            case _:
                return
        if self.player is None:
            return
        bridge = self.sendspin_bridge
        player_id = (bridge.transport_command_target() if bridge else None) or self.player.player_id
        try:
            await command(player_id)
        except MusicAssistantError as err:
            self.logger.warning(
                "Failed to handle the play or pause button of %s: %s", self.display_name, err
            )

    def _active_queue(self) -> PlayerQueue | None:
        """Return the queue the player plays and its buttons act on."""
        if self.player is None:
            return None
        # while the player plays for its Sendspin bridge, that is the queue of the player
        # that leads its Sendspin group
        bridge = self.sendspin_bridge
        player_id = (bridge.transport_command_target() if bridge else None) or self.player.player_id
        return self.mass.player_queues.get_active_queue(player_id)

    def _report_repeat_shuffle(self, queue: PlayerQueue) -> None:
        """Report repeat and shuffle of a queue in the status the client polls or is pushed."""
        repeat = REPEATMODE_MAP[queue.repeat_mode]
        shuffle = int(queue.shuffle_enabled)
        extra_data = self.client.extra_data
        if (
            extra_data.get("playlist repeat") == repeat
            and extra_data.get("playlist shuffle") == shuffle
        ):
            return
        extra_data["playlist repeat"] = repeat
        extra_data["playlist shuffle"] = shuffle
        self.client.signal_update()

    @property
    def _plays_for_bridge(self) -> bool:
        """Return whether the stream on the player is that of its Sendspin bridge."""
        return (bridge := self.sendspin_bridge) is not None and bridge.owns_stream

    async def _set_preset_items(self) -> None:
        """Set the presets for a player."""
        preset_items: list[SlimPreset] = []
        for preset_index in range(1, 11):
            if preset_conf := self.mass.config.get_protocol_value(
                self.protocol_id, f"preset_{preset_index}"
            ):
                try:
                    preset_uri = cast("str", preset_conf)
                    media_item = await self.mass.music.get_item_by_uri(preset_uri)
                    preset_items.append(
                        SlimPreset(
                            uri=media_item.uri or preset_uri,
                            text=media_item.name,
                            icon=(
                                self.mass.metadata.get_image_url(media_item.image)
                                if media_item.image
                                else ""
                            ),
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
        self.client.presets = preset_items

    async def _set_display(self) -> None:
        """Set the display config for a player."""
        display_enabled = self.mass.config.get_protocol_value(
            self.protocol_id,
            CONF_ENTRY_DISPLAY.key,
            CONF_ENTRY_DISPLAY.default_value,
        )
        visualization = self.mass.config.get_protocol_value(
            self.protocol_id,
            CONF_ENTRY_VISUALIZATION.key,
            CONF_ENTRY_VISUALIZATION.default_value,
        )
        await self.client.configure_display(
            visualisation=SlimVisualisationType(visualization), disabled=not display_enabled
        )

    def _sync_elapsed_after_flush(self) -> None:
        """Drop an old-stream heartbeat that landed before the flush."""
        self._attr_elapsed_time = self.client.elapsed_seconds
        self._attr_elapsed_time_last_updated = time.time()

    async def _restore_power(self) -> None:
        """Power the player on again, unless it was switched off before it disconnected."""
        # straight to the device: Music Assistant's power command would also apply auto play
        if PlayerFeature.POWER not in self.supported_features:
            return
        if self.mass.config.get_protocol_value(self.protocol_id, CONF_STORED_POWER, True):
            await self.client.power(True)
        self._power_tracking = True

    def _store_volume(self) -> None:
        """Keep the current volume in the protocol config so it survives a reconnect."""
        volume = self.client.volume_level
        if volume != self.mass.config.get_protocol_value(self.protocol_id, CONF_STORED_VOLUME):
            self.mass.config.set_protocol_value(self.protocol_id, CONF_STORED_VOLUME, volume)

    def _report_delivered_format(self) -> None:
        """Add the format Music Assistant streams to the player to the metadata of its stream."""
        # the Sendspin bridge reports the format of its own stream
        if self._plays_for_bridge or (media := self.client.current_media) is None:
            return
        metadata = media.metadata
        # once, as the stream starts: the update goes to the stream requested last, which
        # later on can be a newer one
        if metadata.get("samplerate"):
            return
        source_id = cast("str | None", metadata.get("source_id"))
        queue_item_id = cast("str | None", metadata.get("queue_item_id"))
        if not source_id or not queue_item_id:
            return
        # the stream controller records the format once the player fetched the stream
        queue_item = self.mass.player_queues.get_item(source_id, queue_item_id)
        streamdetails = queue_item.streamdetails if queue_item else None
        if streamdetails is None or (chain := streamdetails.audio_processing) is None:
            return
        player_id = self.player.player_id if self.player else self.protocol_id
        output_format = next(
            (output.output_format for output in chain.outputs if player_id in output.player_ids),
            None,
        )
        if output_format is None:
            return
        quality = {"samplerate": str(output_format.sample_rate)}
        if output_format.content_type.is_lossless():
            quality["samplesize"] = str(output_format.bit_depth)
        # the update replaces all metadata of the stream
        self.mass.create_task(
            self.client.update_metadata(cast("MediaMetadata", {**metadata, **quality}))
        )
