"""Squeezelite Player implementation."""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

from aioslimproto.client import PlayerState as SlimPlayerState
from aioslimproto.client import SlimClient
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption, PlayerConfig
from music_assistant_models.enums import ConfigEntryType, PlaybackState, PlayerFeature, PlayerType
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
from music_assistant.helpers.util import TaskManager
from music_assistant.models.player import DeviceInfo, Player, PlayerMedia

from .constants import (
    CONF_DISPLAY,
    CONF_VISUALIZATION,
    DEFAULT_VISUALIZATION,
    SlimVisualisationType,
)

if TYPE_CHECKING:
    from aioslimproto.models import EventType as SlimEventType

    from .provider import SqueezelitePlayerProvider


STATE_MAP = {
    SlimPlayerState.BUFFERING: PlaybackState.PLAYING,
    SlimPlayerState.BUFFER_READY: PlaybackState.PLAYING,
    SlimPlayerState.PAUSED: PlaybackState.PAUSED,
    SlimPlayerState.PLAYING: PlaybackState.PLAYING,
    SlimPlayerState.STOPPED: PlaybackState.IDLE,
}

CONF_ENTRY_DISPLAY = ConfigEntry(
    key=CONF_DISPLAY,
    type=ConfigEntryType.BOOLEAN,
    default_value=False,
    required=False,
    label="Enable display support",
    description="Enable/disable native display support on squeezebox or squeezelite32 hardware.",
    category="advanced",
)
CONF_ENTRY_VISUALIZATION = ConfigEntry(
    key=CONF_VISUALIZATION,
    type=ConfigEntryType.STRING,
    default_value=DEFAULT_VISUALIZATION,
    options=[
        ConfigValueOption(title=x.name.replace("_", " ").title(), value=x.value)
        for x in SlimVisualisationType
    ],
    required=False,
    label="Visualization type",
    description="The type of visualization to show on the display "
    "during playback if the device supports this.",
    category="advanced",
    depends_on=CONF_DISPLAY,
)


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
        self._attr_can_group_with = {provider.instance_id}

    async def setup(self) -> None:
        """Set up the player."""
        await self.mass.players.register_or_update(self)

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
        if powered:
            await self.client.power_on()
        else:
            await self.client.power_off()

    async def volume_set(self, volume_level: int) -> None:
        """Handle VOLUME_SET command on the player."""
        await self.client.volume_set(volume_level)

    async def volume_mute(self, muted: bool) -> None:
        """Handle VOLUME MUTE command on the player."""
        await self.client.volume_mute(muted)

    async def stop(self) -> None:
        """Handle STOP command on the player."""
        async with TaskManager(self.mass) as tg:
            for client in self.provider._get_sync_clients(self.player_id):
                tg.create_task(client.stop())

    async def play(self) -> None:
        """Handle PLAY command on the player."""
        async with TaskManager(self.mass) as tg:
            for client in self.provider._get_sync_clients(self.player_id):
                tg.create_task(client.play())

    async def pause(self) -> None:
        """Handle PAUSE command on the player."""
        async with TaskManager(self.mass) as tg:
            for client in self.provider._get_sync_clients(self.player_id):
                tg.create_task(client.pause())

    async def play_media(self, media: PlayerMedia) -> None:
        """Handle PLAY MEDIA on the player."""
        if self.synced_to:
            msg = "A synced player cannot receive play commands directly"
            raise RuntimeError(msg)

        if not self.group_members:
            # Simple, single-player playback
            await self._handle_play_url(
                self.client,
                url=media.uri,
                media=media,
                send_flush=True,
                auto_play=False,
            )
            return

        # This is a syncgroup, we need to handle this with a multi client stream
        master_audio_format = AudioFormat(
            content_type=DEFAULT_PCM_FORMAT.content_type,
            sample_rate=48000,  # Default for squeezelite
            bit_depth=16,
            channels=2,
        )

        # Start multi-client stream for sync group
        await self._handle_multi_client_stream(media, master_audio_format)

    async def enqueue_next_media(self, media: PlayerMedia) -> None:
        """Handle enqueuing next media item."""
        if self.synced_to:
            msg = "A synced player cannot receive enqueue commands directly"
            raise RuntimeError(msg)

        # Handle enqueue for single player or sync group
        if not self.group_members:
            await self._handle_play_url(
                self.client,
                url=media.uri,
                media=media,
                send_flush=False,
                auto_play=True,
            )
        else:
            # Handle multi-client enqueue
            await self._handle_multi_client_enqueue(media)

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
        client: SlimClient,
        url: str,
        media: PlayerMedia,
        send_flush: bool = True,
        auto_play: bool = True,
    ) -> None:
        """Handle playing a URL on a client."""
        if send_flush:
            await client.flush()

        # Send play command with metadata
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

        await client.play_url(url, metadata=metadata, auto_play=auto_play)

    def _get_sync_clients(self) -> Iterator[SlimClient]:
        """Get all sync clients for a player."""
        yield self.client
        for member_id in self.group_members:
            yield self.provider.slimproto.get_player(member_id)

    async def _handle_multi_client_stream(
        self, media: PlayerMedia, master_audio_format: AudioFormat
    ) -> None:
        """Handle multi-client stream for sync groups."""
        # This would need implementation of the multi-client streaming logic
        # For now, simplified implementation
        sync_clients = list(self.provider._get_sync_clients(self.player_id))

        # Play on all sync clients
        async with TaskManager(self.mass) as tg:
            for slimclient in sync_clients:
                tg.create_task(
                    self._handle_play_url(
                        slimclient,
                        media.uri,
                        media,
                        send_flush=True,
                        auto_play=False,
                    )
                )

    async def _handle_multi_client_enqueue(self, media: PlayerMedia) -> None:
        """Handle multi-client enqueue for sync groups."""
        sync_clients = list(self.provider._get_sync_clients(self.player_id))

        # Enqueue on all sync clients
        async with TaskManager(self.mass) as tg:
            for slimclient in sync_clients:
                tg.create_task(
                    self._handle_play_url(
                        slimclient,
                        media.uri,
                        media,
                        send_flush=False,
                        auto_play=True,
                    )
                )
