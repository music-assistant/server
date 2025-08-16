"""Squeezelite Player implementation."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterator
from typing import TYPE_CHECKING

from aioslimproto.client import SlimClient
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption, PlayerConfig
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    MediaType,
    PlayerFeature,
    PlayerType,
    RepeatMode,
)
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
    CACHE_KEY_PREV_STATE,
    CONF_ENTRY_DISPLAY,
    CONF_ENTRY_VISUALIZATION,
    REPEATMODE_MAP,
    STATE_MAP,
    SyncPlayPoint,
)
from .multi_client_stream import MultiClientStream

if TYPE_CHECKING:
    from aioslimproto.models import EventType as SlimEventType

    from music_assistant.providers.universal_group import UniversalGroupPlayer

    from .provider import SqueezelitePlayerProvider


class SqueezelitePlayer(Player):
    """Squeezelite Player implementation."""

    _attr_type = PlayerType.PLAYER
    _multi_client_stream: MultiClientStream | None = None
    _sync_playpoints: deque[SyncPlayPoint] | None = None
    _do_not_resync_before: float = 0.0

    def __init__(
        self,
        provider: SqueezelitePlayerProvider,
        player_id: str,
        client: SlimClient,
    ) -> None:
        """Initialize the Squeezelite Player."""
        super().__init__(provider, player_id)
        self.client = client
        self.provider: SqueezelitePlayerProvider = provider

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
        self._attr_name = client.name
        self._attr_available = True
        self._attr_powered = client.powered
        self._attr_device_info = DeviceInfo(
            model=client.device_model,
            ip_address=client.device_address,
            manufacturer=client.device_type,
        )
        self._attr_can_group_with = {provider.lookup_key}

    async def get_config_entries(self) -> list[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the player."""
        base_entries = await super().get_config_entries()
        max_sample_rate = int(self.client.max_sample_rate)
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
                category="presets",
                required=False,
            )
            for index in range(1, preset_count + 1)
        )
        return (
            base_entries
            + preset_entries
            + (
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
            )
        )

    async def handle_slim_event(self, event: SlimEventType) -> None:
        """Handle player update from slimproto server."""
        # Update player state from slim player
        self._attr_available = True
        self._attr_name = self.client.name
        self._attr_powered = self.client.powered
        self._attr_playback_state = STATE_MAP[self.client.state]
        self._attr_volume_level = self.client.volume_level
        self._attr_volume_muted = self.client.muted
        self._attr_active_source = self.player_id

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

        self.update_state()

    async def power(self, powered: bool) -> None:
        """Handle POWER command on the player."""
        await self.client.power(powered)
        # store last state in cache
        await self.mass.cache.set(
            self.player_id, (powered, self.client.volume_level), base_key=CACHE_KEY_PREV_STATE
        )

    async def volume_set(self, volume_level: int) -> None:
        """Handle VOLUME_SET command on the player."""
        await self.client.volume_set(volume_level)
        # store last state in cache
        await self.mass.cache.set(
            self.player_id, (self.client.powered, volume_level), base_key=CACHE_KEY_PREV_STATE
        )

    async def volume_mute(self, muted: bool) -> None:
        """Handle VOLUME MUTE command on the player."""
        await self.client.mute(muted)

    async def stop(self) -> None:
        """Handle STOP command on the player."""
        async with TaskManager(self.mass) as tg:
            for client in self._get_sync_clients():
                tg.create_task(client.stop())

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
            await self._handle_play_url(
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
                media.custom_data["url"],
                output_format=master_audio_format,
                use_pre_announce=media.custom_data["use_pre_announce"],
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
        elif media.queue_id.startswith("ugp_"):
            # special case: UGP stream
            ugp_player: UniversalGroupPlayer = self.mass.players.get(media.queue_id)
            ugp_stream = ugp_player.stream
            # Filter is later applied in MultiClientStream
            audio_source = ugp_stream.get_stream(master_audio_format, filter_params=None)
        elif media.queue_id and media.queue_item_id:
            # regular queue stream request
            audio_source = self.mass.streams.get_queue_flow_stream(
                queue=self.mass.player_queues.get(media.queue_id),
                start_queue_item=self.mass.player_queues.get_item(
                    media.queue_id, media.queue_item_id
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
        self._multi_client_stream = stream = MultiClientStream(
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
                    self._handle_play_url(
                        slimplayer,
                        url=url,
                        media=media,
                        send_flush=True,
                        auto_play=False,
                    )
                )

    async def enqueue_next_media(self, media: PlayerMedia) -> None:
        """Handle enqueuing next media item."""
        await self._handle_play_url(
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

        raop_session = self.raop_stream.session if self.raop_stream else None
        # handle removals first
        if player_ids_to_remove:
            if self.player_id in player_ids_to_remove:
                # dissolve the entire sync group
                if self.raop_stream and self.raop_stream.running:
                    # stop the stream session if it is running
                    await self.raop_stream.session.stop()
                self._attr_group_members = []
                self.update_state()
                return

            for child_player in self._get_sync_clients():
                if child_player.player_id in player_ids_to_remove:
                    if raop_session:
                        await raop_session.remove_client(child_player)
                    self._attr_group_members.remove(child_player.player_id)

        # handle additions
        for player_id in player_ids_to_add or []:
            if player_id == self.player_id or player_id in self.group_members:
                # nothing to do: player is already part of the group
                continue
            child_player: SqueezelitePlayer | None = self.mass.players.get(player_id)
            if not child_player:
                # should not happen, but guard against it
                continue
            if child_player.synced_to and child_player.synced_to != self.player_id:
                raise RuntimeError("Player is already synced to another player")

            # ensure the child does not have an existing stream session active
            if child_player := self.mass.players.get(player_id):
                if (
                    child_player.raop_stream
                    and child_player.raop_stream.running
                    and child_player.raop_stream.session != raop_session
                ):
                    await child_player.raop_stream.session.remove_client(child_player)

            # add new child to the existing raop session (if any)
            self._attr_group_members.append(player_id)
            if raop_session:
                await raop_session.add_client(child_player)

        # always update the state after modifying group members
        self.update_state()

    def set_config(self, config: PlayerConfig) -> None:
        """Set/update the player config."""
        super().set_config(config)
        self.mass.create_task(self._set_preset_items())
        self.mass.create_task(self._set_display())

    async def _handle_play_url(
        self,
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
            "queue_id": media.queue_id,
            "queue_item_id": media.queue_item_id,
        }
        if queue := self.mass.player_queues.get(media.queue_id):
            self.extra_data["playlist repeat"] = REPEATMODE_MAP[queue.repeat_mode]
            self.extra_data["playlist shuffle"] = int(queue.shuffle_enabled)
        await self.client.play_url(
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
                self.client.play_url(
                    url=url,
                    mime_type=f"audio/{url.split('.')[-1].split('?')[0]}",
                    metadata=metadata,
                    enqueue=True,
                    send_flush=False,
                    autostart=True,
                ),
            )

    def _get_sync_clients(self) -> Iterator[SlimClient]:
        """Get all sync clients for a player."""
        yield self.client
        for member_id in self.group_members:
            yield self.provider.slimproto.get_player(member_id)
