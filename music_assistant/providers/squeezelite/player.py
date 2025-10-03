"""Squeezelite Player implementation."""

from __future__ import annotations

import asyncio
import statistics
import time
from collections import deque
from collections.abc import Iterator
from typing import TYPE_CHECKING, cast

from aioslimproto.models import EventType as SlimEventType
from aioslimproto.models import PlayerState as SlimPlayerState
from aioslimproto.models import Preset as SlimPreset
from aioslimproto.models import SlimEvent
from aioslimproto.models import VisualisationType as SlimVisualisationType
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    MediaType,
    PlaybackState,
    PlayerFeature,
    PlayerType,
    RepeatMode,
)
from music_assistant_models.errors import MusicAssistantError
from music_assistant_models.media_items import AudioFormat

from music_assistant.constants import (
    CONF_ENTRY_DEPRECATED_EQ_BASS,
    CONF_ENTRY_DEPRECATED_EQ_MID,
    CONF_ENTRY_DEPRECATED_EQ_TREBLE,
    CONF_ENTRY_HTTP_PROFILE_FORCED_2,
    CONF_ENTRY_OUTPUT_CODEC,
    CONF_ENTRY_SYNC_ADJUST,
    DEFAULT_PCM_FORMAT,
    create_sample_rates_config_entry,
)
from music_assistant.helpers.ffmpeg import get_ffmpeg_stream
from music_assistant.helpers.util import TaskManager
from music_assistant.models.player import DeviceInfo, Player, PlayerMedia

from .constants import (
    CONF_ENTRY_DISPLAY,
    CONF_ENTRY_VISUALIZATION,
    DEFAULT_PLAYER_VOLUME,
    DEVIATION_JUMP_IGNORE,
    MAX_SKIP_AHEAD_MS,
    MIN_DEVIATION_ADJUST,
    MIN_REQ_PLAYPOINTS,
    REPEATMODE_MAP,
    STATE_MAP,
    SyncPlayPoint,
)
from .multi_client_stream import MultiClientStream

if TYPE_CHECKING:
    from aioslimproto.client import SlimClient

    from music_assistant.providers.universal_group import UniversalGroupPlayer

    from .provider import SqueezelitePlayerProvider


CACHE_CATEGORY_PREV_STATE = 0  # category for caching previous player state


class SqueezelitePlayer(Player):
    """Squeezelite Player implementation."""

    _attr_type = PlayerType.PLAYER

    def __init__(
        self,
        provider: SqueezelitePlayerProvider,
        player_id: str,
        client: SlimClient,
    ) -> None:
        """Initialize the Squeezelite Player."""
        super().__init__(provider, player_id)
        self.client = client
        self._provider: SqueezelitePlayerProvider = provider
        # Set static player attributes
        self._attr_supported_features = {
            PlayerFeature.POWER,
            PlayerFeature.SET_MEMBERS,
            PlayerFeature.MULTI_DEVICE_DSP,
            PlayerFeature.VOLUME_SET,
            PlayerFeature.PAUSE,
            PlayerFeature.VOLUME_MUTE,
            PlayerFeature.ENQUEUE,
            PlayerFeature.GAPLESS_PLAYBACK,
        }
        self._attr_can_group_with = {provider.lookup_key}
        self.multi_client_stream: MultiClientStream | None = None
        self._sync_playpoints: deque[SyncPlayPoint] = deque(maxlen=MIN_REQ_PLAYPOINTS)
        self._do_not_resync_before: float = 0.0

    async def on_config_updated(self) -> None:
        """Handle logic when the player is registered or the config was updated."""
        # set presets and display
        await self._set_preset_items()
        await self._set_display()

    async def setup(self) -> None:
        """Set up the player."""
        player_id = self.client.player_id
        self.logger.info("Player %s connected", self.client.name or player_id)
        # update all dynamic attributes
        self.update_attributes()
        # restore volume and power state
        if last_state := await self.mass.cache.get(
            key=player_id, provider=self.provider.instance_id, category=CACHE_CATEGORY_PREV_STATE
        ):
            init_power = last_state[0]
            init_volume = last_state[1]
        else:
            init_volume = DEFAULT_PLAYER_VOLUME
            init_power = False
        await self.client.power(init_power)
        await self.client.stop()
        await self.client.volume_set(init_volume)
        await self.mass.players.register_or_update(self)

    async def get_config_entries(self) -> list[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the player."""
        base_entries = await super().get_config_entries()
        max_sample_rate = int(self.client.max_sample_rate)
        # create preset entries (for players that support it)
        presets = []
        async for playlist in self.mass.music.playlists.iter_library_items(True):
            presets.append(ConfigValueOption(playlist.name, playlist.uri))
        async for radio in self.mass.music.radio.iter_library_items(True):
            presets.append(ConfigValueOption(radio.name, radio.uri))
        preset_count = 10
        preset_entries = [
            ConfigEntry(
                key=f"preset_{index}",
                type=ConfigEntryType.STRING,
                options=presets,
                label=f"Preset {index}",
                description="Assign a playable item to the player's preset. "
                "Only supported on real squeezebox hardware or jive(lite) based emulators.",
                category="presets",
                required=False,
            )
            for index in range(1, preset_count + 1)
        ]
        return [
            *base_entries,
            *preset_entries,
            CONF_ENTRY_DEPRECATED_EQ_BASS,
            CONF_ENTRY_DEPRECATED_EQ_MID,
            CONF_ENTRY_DEPRECATED_EQ_TREBLE,
            CONF_ENTRY_OUTPUT_CODEC,
            CONF_ENTRY_SYNC_ADJUST,
            CONF_ENTRY_DISPLAY,
            CONF_ENTRY_VISUALIZATION,
            CONF_ENTRY_HTTP_PROFILE_FORCED_2,
            create_sample_rates_config_entry(
                max_sample_rate=max_sample_rate, max_bit_depth=24, safe_max_bit_depth=24
            ),
        ]

    async def power(self, powered: bool) -> None:
        """Handle POWER command on the player."""
        await self.client.power(powered)
        # store last state in cache
        await self.mass.cache.set(
            key=self.player_id,
            data=(powered, self.client.volume_level),
            provider=self.provider.instance_id,
            category=CACHE_CATEGORY_PREV_STATE,
        )

    async def volume_set(self, volume_level: int) -> None:
        """Handle VOLUME_SET command on the player."""
        await self.client.volume_set(volume_level)
        # store last state in cache
        await self.mass.cache.set(
            key=self.player_id,
            data=(self.client.powered, volume_level),
            provider=self.provider.instance_id,
            category=CACHE_CATEGORY_PREV_STATE,
        )

    async def volume_mute(self, muted: bool) -> None:
        """Handle VOLUME MUTE command on the player."""
        await self.client.mute(muted)

    async def stop(self) -> None:
        """Handle STOP command on the player."""
        async with TaskManager(self.mass) as tg:
            for client in self._get_sync_clients():
                tg.create_task(client.stop())
        self._attr_active_source = None
        self.update_state()

    async def play(self) -> None:
        """Handle PLAY command on the player."""
        async with TaskManager(self.mass) as tg:
            for client in self._get_sync_clients():
                tg.create_task(client.play())

    async def pause(self) -> None:
        """Handle PAUSE command on the player."""
        async with TaskManager(self.mass) as tg:
            for client in self._get_sync_clients():
                tg.create_task(client.pause())

    async def play_media(self, media: PlayerMedia) -> None:
        """Handle PLAY MEDIA on the player."""
        if self.synced_to:
            msg = "A synced player cannot receive play commands directly"
            raise RuntimeError(msg)

        if not self.group_members:
            # Simple, single-player playback
            await self._handle_play_url_for_slimplayer(
                self.client,
                url=media.uri,
                media=media,
                send_flush=True,
                auto_play=False,
            )
            return

        # this is a syncgroup, we need to handle this with a multi client stream
        master_audio_format = AudioFormat(
            content_type=DEFAULT_PCM_FORMAT.content_type,
            sample_rate=DEFAULT_PCM_FORMAT.sample_rate,
            bit_depth=DEFAULT_PCM_FORMAT.bit_depth,
        )
        if media.media_type == MediaType.ANNOUNCEMENT:
            # special case: stream announcement
            audio_source = self.mass.streams.get_announcement_stream(
                media.custom_data["announcement_url"],
                output_format=master_audio_format,
                pre_announce=media.custom_data["pre_announce"],
                pre_announce_url=media.custom_data["pre_announce_url"],
            )
        elif media.media_type == MediaType.PLUGIN_SOURCE:
            # special case: plugin source stream
            audio_source = self.mass.streams.get_plugin_source_stream(
                plugin_source_id=media.custom_data["source_id"],
                output_format=master_audio_format,
                # need to pass player_id from the PlayerMedia object
                # because this could have been a group
                player_id=media.custom_data["player_id"],
            )
        elif media.source_id.startswith("ugp_"):
            # special case: UGP stream
            ugp_player: UniversalGroupPlayer = self.mass.players.get(media.source_id)
            ugp_stream = ugp_player.stream
            # Filter is later applied in MultiClientStream
            audio_source = ugp_stream.get_stream(master_audio_format, filter_params=None)
        elif media.source_id and media.queue_item_id:
            # regular queue stream request
            audio_source = self.mass.streams.get_queue_flow_stream(
                queue=self.mass.player_queues.get(media.source_id),
                start_queue_item=self.mass.player_queues.get_item(
                    media.source_id, media.queue_item_id
                ),
                pcm_format=master_audio_format,
            )
        else:
            # assume url or some other direct path
            # NOTE: this will fail if its an uri not playable by ffmpeg
            audio_source = get_ffmpeg_stream(
                audio_input=media.uri,
                input_format=AudioFormat(ContentType.try_parse(media.uri)),
                output_format=master_audio_format,
            )
        # start the stream task
        self.multi_client_stream = stream = MultiClientStream(
            audio_source=audio_source, audio_format=master_audio_format
        )
        base_url = (
            f"{self.mass.streams.base_url}/slimproto/multi?player_id={self.player_id}&fmt=flac"
        )

        # forward to downstream play_media commands
        async with TaskManager(self.mass) as tg:
            for slimplayer in self._get_sync_clients():
                url = f"{base_url}&child_player_id={slimplayer.player_id}"
                stream.expected_clients += 1
                tg.create_task(
                    self._handle_play_url_for_slimplayer(
                        slimplayer,
                        url=url,
                        media=media,
                        send_flush=True,
                        auto_play=False,
                    )
                )

    async def enqueue_next_media(self, media: PlayerMedia) -> None:
        """Handle enqueuing next media item."""
        await self._handle_play_url_for_slimplayer(
            self.client,
            url=media.uri,
            media=media,
            enqueue=True,
            send_flush=False,
            auto_play=True,
        )

    async def set_members(
        self,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        """Handle SET_MEMBERS command on the player."""
        if self.synced_to:
            # this should not happen, but guard anyways
            raise RuntimeError("Player is synced, cannot set members")
        if not player_ids_to_add and not player_ids_to_remove:
            # nothing to do
            return

        # handle removals first
        if player_ids_to_remove:
            for sync_client in self._get_sync_clients():
                if sync_client.player_id in player_ids_to_remove:
                    if sync_client.player_id in self._attr_group_members:
                        # remove child from the group
                        self._attr_group_members.remove(sync_client.player_id)
                        if sync_client.state != SlimPlayerState.STOPPED:
                            # stop the player if it is playing
                            await sync_client.stop()

        # handle additions
        players_added = False
        for player_id in player_ids_to_add or []:
            if player_id == self.player_id or player_id in self.group_members:
                # nothing to do: player is already part of the group
                continue
            child_player: SqueezelitePlayer | None = self.mass.players.get(player_id)
            if not child_player:
                # should not happen, but guard against it
                continue
            if child_player.state != SlimPlayerState.STOPPED:
                # stop the player if it is already playing something else
                await child_player.stop()
            self._attr_group_members.append(player_id)
            players_added = True

        # always update the state after modifying group members
        self.update_state()

        if players_added and self.current_media and self.playback_state == PlaybackState.PLAYING:
            # restart stream session if it was already playing
            # for now, we dont support late joining into an existing stream
            self.mass.create_task(self.mass.players.cmd_resume(self.player_id))

    def handle_slim_event(self, event: SlimEvent) -> None:
        """Handle player event from slimproto server."""
        if event.type == SlimEventType.PLAYER_BUFFER_READY:
            self.mass.create_task(self._handle_buffer_ready())
            return

        if event.type == SlimEventType.PLAYER_HEARTBEAT:
            self._handle_player_heartbeat()
            return

        if event.type in (SlimEventType.PLAYER_BTN_EVENT, SlimEventType.PLAYER_CLI_EVENT):
            self.mass.create_task(self._handle_player_cli_event(event))
            return

        # all other: update attributes and update state
        self.update_attributes()
        self.update_state()

    def update_attributes(self) -> None:
        """Update player attributes from slim player."""
        # Update player state from slim player
        self._attr_available = self.client.connected
        self._attr_name = self.client.name
        self._attr_powered = self.client.powered
        self._attr_playback_state = STATE_MAP[self.client.state]
        self._attr_volume_level = self.client.volume_level
        self._attr_volume_muted = self.client.muted
        self._attr_active_source = self.player_id
        self._attr_device_info = DeviceInfo(
            model=self.client.device_model,
            ip_address=self.client.device_address,
            manufacturer=self.client.device_type,
        )
        self._attr_elapsed_time = self.client.elapsed_seconds
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
                queue_id=metadata.get("queue_id"),
                queue_item_id=metadata.get("queue_item_id"),
            )
        else:
            self._attr_current_media = None

    async def _handle_play_url_for_slimplayer(
        self,
        slimplayer: SlimClient,
        url: str,
        media: PlayerMedia,
        enqueue: bool = False,
        send_flush: bool = True,
        auto_play: bool = False,
    ) -> None:
        """Handle playback of an url on slimproto player(s)."""
        metadata = {
            "item_id": media.uri,
            "title": media.title,
            "album": media.album,
            "artist": media.artist,
            "image_url": media.image_url,
            "duration": media.duration,
            "queue_id": media.source_id,
            "queue_item_id": media.queue_item_id,
        }
        if queue := self.mass.player_queues.get(media.source_id):
            self.extra_data["playlist repeat"] = REPEATMODE_MAP[queue.repeat_mode]
            self.extra_data["playlist shuffle"] = int(queue.shuffle_enabled)
        await slimplayer.play_url(
            url=url,
            mime_type=f"audio/{url.split('.')[-1].split('?')[0]}",
            metadata=metadata,
            enqueue=enqueue,
            send_flush=send_flush,
            # if autoplay=False playback will not start automatically
            # instead 'buffer ready' will be called when the buffer is full
            # to coordinate a start of multiple synced players
            autostart=auto_play,
        )
        # if queue is set to single track repeat,
        # immediately set this track as the next
        # this prevents race conditions with super short audio clips (on single repeat)
        # https://github.com/music-assistant/hass-music-assistant/issues/2059
        if queue and queue.repeat_mode == RepeatMode.ONE:
            self.mass.call_later(
                0.2,
                slimplayer.play_url(
                    url=url,
                    mime_type=f"audio/{url.split('.')[-1].split('?')[0]}",
                    metadata=metadata,
                    enqueue=True,
                    send_flush=False,
                    autostart=True,
                ),
            )

    def _handle_player_heartbeat(self) -> None:
        """Process SlimClient elapsed_time update."""
        if self.client.state == SlimPlayerState.STOPPED:
            # ignore server heartbeats when stopped
            return
        # elapsed time change on the player will be auto picked up
        # by the player manager.
        self._attr_elapsed_time = self.client.elapsed_seconds
        self._attr_elapsed_time_last_updated = time.time()

        # handle sync
        if self.synced_to:
            self._handle_sync()

    async def _handle_buffer_ready(self) -> None:
        """
        Handle buffer ready event, player has buffered a (new) track.

        Only used when autoplay=0 for coordinated start of synced players.
        """
        if self.synced_to:
            # unpause of sync child is handled by sync master
            return
        if not self.group_members:
            # not a sync group, continue
            await self.client.unpause_at(self.client.jiffies)
            return
        count = 0
        while count < 40:
            childs_total = 0
            childs_ready = 0
            await asyncio.sleep(0.2)
            for sync_child in self._get_sync_clients():
                childs_total += 1
                if sync_child.state == SlimPlayerState.BUFFER_READY:
                    childs_ready += 1
            if childs_total == childs_ready:
                break
            count += 1

        # all child's ready (or timeout) - start play
        async with TaskManager(self.mass) as tg:
            for sync_client in self._get_sync_clients():
                # NOTE: Officially you should do an unpause_at based on the player timestamp
                # but I did not have any good results with that.
                # Instead just start playback on all players and let the sync logic work out
                # the delays etc.
                tg.create_task(sync_client.pause_for(200))

    async def _handle_player_cli_event(self, event: SlimEvent) -> None:
        """Process CLI Event."""
        if not event.data:
            return
        # event data is str, not dict
        # TODO: fix this in the aioslimproto lib
        event_data = cast("str", event.data)
        queue = self.mass.player_queues.get_active_queue(self.player_id)
        if event_data.startswith("button preset_") and event_data.endswith(".single"):
            preset_id = event_data.split("preset_")[1].split(".")[0]
            preset_index = int(preset_id) - 1
            if len(self.client.presets) >= preset_index + 1:
                preset = self.client.presets[preset_index]
                await self.mass.player_queues.play_media(queue.queue_id, preset.uri)
        elif event_data == "button repeat":
            if queue.repeat_mode == RepeatMode.OFF:
                repeat_mode = RepeatMode.ONE
            elif queue.repeat_mode == RepeatMode.ONE:
                repeat_mode = RepeatMode.ALL
            else:
                repeat_mode = RepeatMode.OFF
            self.mass.player_queues.set_repeat(queue.queue_id, repeat_mode)
            self.client.extra_data["playlist repeat"] = REPEATMODE_MAP[queue.repeat_mode]
            self.client.signal_update()
        elif event.data == "button shuffle":
            self.mass.player_queues.set_shuffle(queue.queue_id, not queue.shuffle_enabled)
            self.client.extra_data["playlist shuffle"] = int(queue.shuffle_enabled)
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
        self.logger.debug("CLI Event: %s", event_data)

    def _handle_sync(self) -> None:
        """Synchronize audio of a sync slimplayer."""
        sync_master_id = self.synced_to
        if not sync_master_id:
            # we only correct sync members, not the sync master itself
            return
        if not (sync_master := self.provider.slimproto.get_player(sync_master_id)):
            return  # just here as a guard as bad things can happen

        if sync_master.state != SlimPlayerState.PLAYING:
            return
        if self.client.state != SlimPlayerState.PLAYING:
            return

        # we collect a few playpoints of the player to determine
        # average lag/drift so we can adjust accordingly
        sync_playpoints = self._sync_playpoints

        now = time.time()
        if now < self._do_not_resync_before:
            return

        last_playpoint = sync_playpoints[-1] if sync_playpoints else None
        if last_playpoint and (now - last_playpoint.timestamp) > 10:
            # last playpoint is too old, invalidate
            sync_playpoints.clear()
        if last_playpoint and last_playpoint.sync_master != sync_master.player_id:
            # this should not happen, but just in case
            sync_playpoints.clear()

        diff = int(
            self.provider.get_corrected_elapsed_milliseconds(sync_master)
            - self.provider.get_corrected_elapsed_milliseconds(self.client)
        )

        sync_playpoints.append(SyncPlayPoint(now, sync_master.player_id, diff))

        # ignore unexpected spikes
        if (
            sync_playpoints
            and abs(statistics.fmean(abs(x.diff) for x in sync_playpoints) - abs(diff))
            > DEVIATION_JUMP_IGNORE
        ):
            return

        min_req_playpoints = 2 if sync_master.elapsed_seconds < 2 else MIN_REQ_PLAYPOINTS
        if len(sync_playpoints) < min_req_playpoints:
            return

        # get the average diff
        avg_diff = statistics.fmean(x.diff for x in sync_playpoints)
        delta = int(abs(avg_diff))

        if delta < MIN_DEVIATION_ADJUST:
            return

        # resync the player by skipping ahead or pause for x amount of (milli)seconds
        sync_playpoints.clear()
        self._do_not_resync_before = now + 5
        if avg_diff > MAX_SKIP_AHEAD_MS:
            # player lagging behind more than MAX_SKIP_AHEAD_MS,
            # we need to correct the sync_master
            self.logger.debug("%s resync: pauseFor %sms", sync_master.name, delta)
            self.mass.create_task(sync_master.pause_for(delta))
        elif avg_diff > 0:
            # handle player lagging behind, fix with skip_ahead
            self.logger.debug("%s resync: skipAhead %sms", self.display_name, delta)
            self.mass.create_task(self.client.skip_over(delta))
        else:
            # handle player is drifting too far ahead, use pause_for to adjust
            self.logger.debug("%s resync: pauseFor %sms", self.display_name, delta)
            self.mass.create_task(self.client.pause_for(delta))

    async def _set_preset_items(self) -> None:
        """Set the presets for a player."""
        preset_items: list[SlimPreset] = []
        for preset_index in range(1, 11):
            if preset_conf := self.mass.config.get_raw_player_config_value(
                self.player_id, f"preset_{preset_index}"
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
        self.client.presets = preset_items

    async def _set_display(self) -> None:
        """Set the display config for a player."""
        display_enabled = self.mass.config.get_raw_player_config_value(
            self.player_id,
            CONF_ENTRY_DISPLAY.key,
            CONF_ENTRY_DISPLAY.default_value,
        )
        visualization = self.mass.config.get_raw_player_config_value(
            self.player_id,
            CONF_ENTRY_VISUALIZATION.key,
            CONF_ENTRY_VISUALIZATION.default_value,
        )
        await self.client.configure_display(
            visualisation=SlimVisualisationType(visualization), disabled=not display_enabled
        )

    def _get_sync_clients(self) -> Iterator[SlimClient]:
        """Get all sync clients for a player."""
        yield self.client
        for member_id in self.group_members:
            if slimplayer := self.provider.slimproto.get_player(member_id):
                yield slimplayer
