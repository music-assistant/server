"""AirPlay Player implementation."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, cast

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    MediaType,
    PlaybackState,
    PlayerFeature,
    PlayerType,
)
from music_assistant_models.media_items import AudioFormat

from music_assistant.constants import (
    CONF_ENTRY_DEPRECATED_EQ_BASS,
    CONF_ENTRY_DEPRECATED_EQ_MID,
    CONF_ENTRY_DEPRECATED_EQ_TREBLE,
    CONF_ENTRY_FLOW_MODE_ENFORCED,
    CONF_ENTRY_OUTPUT_CODEC_HIDDEN,
    CONF_ENTRY_SYNC_ADJUST,
    create_sample_rates_config_entry,
)
from music_assistant.helpers.ffmpeg import get_ffmpeg_stream
from music_assistant.models.player import DeviceInfo, Player, PlayerMedia
from music_assistant.providers.universal_group.constants import UGP_PREFIX

from .constants import (
    AIRPLAY_FLOW_PCM_FORMAT,
    AIRPLAY_PCM_FORMAT,
    CACHE_KEY_PREV_VOLUME,
    CONF_ALAC_ENCODE,
    CONF_ENCRYPTION,
    CONF_IGNORE_VOLUME,
    CONF_PASSWORD,
    CONF_READ_AHEAD_BUFFER,
    FALLBACK_VOLUME,
)
from .helpers import get_primary_ip_address_from_zeroconf, is_broken_raop_model
from .raop import RaopStreamSession

if TYPE_CHECKING:
    from zeroconf.asyncio import AsyncServiceInfo

    from music_assistant.providers.universal_group import UniversalGroupPlayer

    from .provider import AirPlayProvider
    from .raop import RaopStream


BROKEN_RAOP_WARN = ConfigEntry(
    key="broken_raop",
    type=ConfigEntryType.ALERT,
    default_value=None,
    required=False,
    label="This player is known to have broken AirPlay 1 (RAOP) support. "
    "Playback may fail or simply be silent. There is no workaround for this issue at the moment.",
)


class AirPlayPlayer(Player):
    """AirPlay Player implementation."""

    def __init__(
        self,
        provider: AirPlayProvider,
        player_id: str,
        discovery_info: AsyncServiceInfo,
        address: str,
        display_name: str,
        manufacturer: str,
        model: str,
        initial_volume: int = FALLBACK_VOLUME,
    ) -> None:
        """Initialize AirPlayPlayer."""
        super().__init__(provider, player_id)
        self.discovery_info = discovery_info
        self.address = address
        self.raop_stream: RaopStream | None = None
        self.last_command_sent = 0.0
        self._lock = asyncio.Lock()

        # Set (static) player attributes
        self._attr_type = PlayerType.PLAYER
        self._attr_name = display_name
        self._attr_available = True
        self._attr_device_info = DeviceInfo(
            model=model,
            manufacturer=manufacturer,
            ip_address=address,
        )
        self._attr_supported_features = {
            PlayerFeature.PAUSE,
            PlayerFeature.SET_MEMBERS,
            PlayerFeature.MULTI_DEVICE_DSP,
            PlayerFeature.VOLUME_SET,
        }
        self._attr_volume_level = initial_volume
        self._attr_can_group_with = {provider.lookup_key}
        self._attr_enabled_by_default = not is_broken_raop_model(manufacturer, model)

    async def get_config_entries(self) -> list[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the given player (if any)."""
        base_entries = [
            *await super().get_config_entries(),
            CONF_ENTRY_FLOW_MODE_ENFORCED,
            CONF_ENTRY_DEPRECATED_EQ_BASS,
            CONF_ENTRY_DEPRECATED_EQ_MID,
            CONF_ENTRY_DEPRECATED_EQ_TREBLE,
            CONF_ENTRY_OUTPUT_CODEC_HIDDEN,
            ConfigEntry(
                key=CONF_ENCRYPTION,
                type=ConfigEntryType.BOOLEAN,
                default_value=False,
                label="Enable encryption",
                description="Enable encrypted communication with the player, "
                "some (3rd party) players require this.",
                category="airplay",
            ),
            ConfigEntry(
                key=CONF_ALAC_ENCODE,
                type=ConfigEntryType.BOOLEAN,
                default_value=True,
                label="Enable compression",
                description="Save some network bandwidth by sending the audio as "
                "(lossless) ALAC at the cost of a bit CPU.",
                category="airplay",
            ),
            CONF_ENTRY_SYNC_ADJUST,
            ConfigEntry(
                key=CONF_PASSWORD,
                type=ConfigEntryType.SECURE_STRING,
                default_value=None,
                required=False,
                label="Device password",
                description="Some devices require a password to connect/play.",
                category="airplay",
            ),
            ConfigEntry(
                key=CONF_READ_AHEAD_BUFFER,
                type=ConfigEntryType.INTEGER,
                default_value=1000,
                required=False,
                label="Audio buffer (ms)",
                description="Amount of buffer (in milliseconds), "
                "the player should keep to absorb network throughput jitter. "
                "If you experience audio dropouts, try increasing this value.",
                category="airplay",
                range=(500, 3000),
            ),
            # airplay has fixed sample rate/bit depth so make this config entry static and hidden
            create_sample_rates_config_entry(
                supported_sample_rates=[44100], supported_bit_depths=[16], hidden=True
            ),
            ConfigEntry(
                key=CONF_IGNORE_VOLUME,
                type=ConfigEntryType.BOOLEAN,
                default_value=False,
                label="Ignore volume reports sent by the device itself",
                description=(
                    "The AirPlay protocol allows devices to report their own volume "
                    "level. \n"
                    "For some devices this is not reliable and can cause unexpected "
                    "volume changes. \n"
                    "Enable this option to ignore these reports."
                ),
                category="airplay",
            ),
        ]

        if is_broken_raop_model(self.device_info.manufacturer, self.device_info.model):
            base_entries.insert(-1, BROKEN_RAOP_WARN)

        return base_entries

    async def stop(self) -> None:
        """Send STOP command to player."""
        if self.raop_stream and self.raop_stream.session:
            # forward stop to the entire stream session
            await self.raop_stream.session.stop()

    async def play(self) -> None:
        """Send PLAY (unpause) command to player."""
        async with self._lock:
            if self.raop_stream and self.raop_stream.running:
                await self.raop_stream.send_cli_command("ACTION=PLAY")

    async def pause(self) -> None:
        """Send PAUSE command to player."""
        if self.group_members:
            # pause is not supported while synced, use stop instead
            self.logger.debug("Player is synced, using STOP instead of PAUSE")
            await self.stop()
            return

        async with self._lock:
            if not self.raop_stream or not self.raop_stream.running:
                return
            await self.raop_stream.send_cli_command("ACTION=PAUSE")

    async def play_media(self, media: PlayerMedia) -> None:
        """Handle PLAY MEDIA on given player."""
        if self.synced_to:
            # this should not happen, but guard anyways
            raise RuntimeError("Player is synced")

        # set the active source for the player to the media queue
        # this accounts for syncgroups and linked players (e.g. sonos)
        self._attr_active_source = media.source_id
        self._attr_current_media = media

        # select audio source
        if media.media_type == MediaType.ANNOUNCEMENT:
            # special case: stream announcement
            assert media.custom_data
            input_format = AIRPLAY_PCM_FORMAT
            audio_source = self.mass.streams.get_announcement_stream(
                media.custom_data["url"],
                output_format=AIRPLAY_PCM_FORMAT,
                use_pre_announce=media.custom_data["use_pre_announce"],
            )
        elif media.media_type == MediaType.PLUGIN_SOURCE:
            # special case: plugin source stream
            input_format = AIRPLAY_PCM_FORMAT
            assert media.custom_data
            audio_source = self.mass.streams.get_plugin_source_stream(
                plugin_source_id=media.custom_data["source_id"],
                output_format=AIRPLAY_PCM_FORMAT,
                # need to pass player_id from the PlayerMedia object
                # because this could have been a group
                player_id=media.custom_data["player_id"],
            )
        elif media.source_id and media.source_id.startswith(UGP_PREFIX):
            # special case: UGP stream
            ugp_player = cast("UniversalGroupPlayer", self.mass.players.get(media.source_id))
            ugp_stream = ugp_player.stream
            assert ugp_stream is not None  # for type checker
            input_format = ugp_stream.base_pcm_format
            audio_source = ugp_stream.subscribe_raw()
        elif media.source_id and media.queue_item_id:
            # regular queue (flow) stream request
            input_format = AIRPLAY_FLOW_PCM_FORMAT
            queue = self.mass.player_queues.get(media.source_id)
            assert queue
            start_queue_item = self.mass.player_queues.get_item(
                media.source_id, media.queue_item_id
            )
            assert start_queue_item
            audio_source = self.mass.streams.get_queue_flow_stream(
                queue=queue,
                start_queue_item=start_queue_item,
                pcm_format=input_format,
            )
        else:
            # assume url or some other direct path
            # NOTE: this will fail if its an uri not playable by ffmpeg
            input_format = AIRPLAY_PCM_FORMAT
            audio_source = get_ffmpeg_stream(
                audio_input=media.uri,
                input_format=AudioFormat(content_type=ContentType.try_parse(media.uri)),
                output_format=AIRPLAY_PCM_FORMAT,
            )

        # if an existing stream session is running, we could replace it with the new stream
        if self.raop_stream and self.raop_stream.running:
            # check if we need to replace the stream
            if self.raop_stream.prevent_playback:
                # player is in prevent playback mode, we need to stop the stream
                await self.stop()
            else:
                await self.raop_stream.session.replace_stream(audio_source)
                return

        # setup RaopStreamSession for player (and its sync childs if any)
        sync_clients = self._get_sync_clients()
        provider = cast("AirPlayProvider", self.provider)
        raop_stream_session = RaopStreamSession(provider, sync_clients, input_format, audio_source)
        await raop_stream_session.start()

    async def volume_set(self, volume_level: int) -> None:
        """Send VOLUME_SET command to given player."""
        if self.raop_stream and self.raop_stream.running:
            await self.raop_stream.send_cli_command(f"VOLUME={volume_level}\n")
        self._attr_volume_level = volume_level
        self.update_state()
        # store last state in cache
        await self.mass.cache.set(self.player_id, volume_level, base_key=CACHE_KEY_PREV_VOLUME)

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
            child_player_to_add: AirPlayPlayer | None = cast(
                "AirPlayPlayer | None", self.mass.players.get(player_id)
            )
            if not child_player_to_add:
                # should not happen, but guard against it
                continue
            if child_player_to_add.synced_to and child_player_to_add.synced_to != self.player_id:
                raise RuntimeError("Player is already synced to another player")

            # ensure the child does not have an existing stream session active
            if child_player_to_add := cast(
                "AirPlayPlayer | None", self.mass.players.get(player_id)
            ):
                if (
                    child_player_to_add.raop_stream
                    and child_player_to_add.raop_stream.running
                    and child_player_to_add.raop_stream.session != raop_session
                ):
                    await child_player_to_add.raop_stream.session.remove_client(child_player_to_add)

            # add new child to the existing raop session (if any)
            self._attr_group_members.append(player_id)
            if raop_session:
                await raop_session.add_client(child_player_to_add)

        # always update the state after modifying group members
        self.update_state()

    def update_volume_from_device(self, volume: int) -> None:
        """Update volume from device feedback."""
        ignore_volume_report = (
            self.mass.config.get_raw_player_config_value(self.player_id, CONF_IGNORE_VOLUME, False)
            or self.device_info.manufacturer.lower() == "apple"
        )

        if ignore_volume_report:
            return

        cur_volume = self.volume_level or 0
        if abs(cur_volume - volume) > 3 or (time.time() - self.last_command_sent) > 3:
            self.mass.create_task(self.volume_set(volume))
        else:
            self._attr_volume_level = volume
            self.update_state()

    def set_discovery_info(self, discovery_info: AsyncServiceInfo, display_name: str) -> None:
        """Set/update the discovery info for the player."""
        self._attr_name = display_name
        self.discovery_info = discovery_info
        cur_address = self.address
        new_address = get_primary_ip_address_from_zeroconf(discovery_info)
        assert new_address  # should always be set, but guard against None
        if cur_address != new_address:
            self.logger.debug("Address updated from %s to %s", cur_address, new_address)
            self.address = cur_address
            self._attr_device_info.ip_address = new_address
        self.update_state()

    def set_state_from_raop(
        self, state: PlaybackState | None = None, elapsed_time: float | None = None
    ) -> None:
        """Set the playback state from RAOP."""
        if state is not None:
            self._attr_playback_state = state
        if elapsed_time is not None:
            self._attr_elapsed_time = elapsed_time
            self._attr_elapsed_time_last_updated = time.time()
        self.update_state()

    async def on_unload(self) -> None:
        """Handle logic when the player is unloaded from the Player controller."""
        await super().on_unload()
        if self.raop_stream:
            # stop the stream session if it is running
            if self.raop_stream.running:
                self.mass.create_task(self.raop_stream.session.stop())
            self.raop_stream = None

    def _get_sync_clients(self) -> list[AirPlayPlayer]:
        """Get all sync clients for a player."""
        sync_clients: list[AirPlayPlayer] = []
        # we need to return the player itself too
        group_child_ids = {self.player_id}
        group_child_ids.update(self.group_members)
        for child_id in group_child_ids:
            if client := cast("AirPlayPlayer | None", self.mass.players.get(child_id)):
                sync_clients.append(client)
        return sync_clients
