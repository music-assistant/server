"""Bluesound Player implementation."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueType
from music_assistant_models.enums import PlaybackState, PlayerFeature, PlayerType
from music_assistant_models.errors import PlayerCommandFailed
from pyblu import Player as BluosPlayer
from pyblu import Status, SyncStatus
from pyblu.entities import Input, PairedPlayer, Preset
from pyblu.errors import PlayerUnexpectedResponseError, PlayerUnreachableError

from music_assistant.constants import (
    CONF_ENTRY_ENABLE_ICY_METADATA,
    CONF_ENTRY_FLOW_MODE_ENFORCED,
    CONF_ENTRY_HTTP_PROFILE_DEFAULT_3,
    CONF_ENTRY_OUTPUT_CODEC,
    create_sample_rates_config_entry,
)
from music_assistant.models.player import DeviceInfo, Player, PlayerMedia, PlayerSource
from music_assistant.providers.bluesound.const import (
    IDLE_POLL_INTERVAL,
    PLAYBACK_POLL_INTERVAL,
    PLAYBACK_STATE_MAP,
    PLAYBACK_STATE_POLL_MAP,
    PLAYER_FEATURES_BASE,
    PLAYER_SOURCE_MAP,
    POLL_STATE_DYNAMIC,
    POLL_STATE_STATIC,
)

if TYPE_CHECKING:
    from .provider import BluesoundDiscoveryInfo, BluesoundPlayerProvider


class BluesoundPlayer(Player):
    """Holds the details of the (discovered) BluOS player."""

    def __init__(
        self,
        provider: BluesoundPlayerProvider,
        player_id: str,
        discovery_info: BluesoundDiscoveryInfo,
        name: str,
        ip_address: str,
        port: int,
    ) -> None:
        """Initialize the BluOS Player."""
        super().__init__(provider, player_id)
        self.port = port
        self.discovery_info = discovery_info
        self.ip_address = ip_address
        self.connected: bool = True
        self.client = BluosPlayer(self.ip_address, self.port, self.mass.http_session)
        self.sync_status = SyncStatus
        self.status = Status
        self.poll_state = POLL_STATE_STATIC
        self.dynamic_poll_count: int = 0
        self._listen_task: asyncio.Task | None = None
        # Set base player attributes
        self._attr_type = PlayerType.PLAYER
        self._attr_supported_features = PLAYER_FEATURES_BASE.copy()
        self._attr_name = name
        self._attr_device_info = DeviceInfo(
            model=discovery_info.get("model", "BluOS Device"),
            manufacturer="BluOS",
            ip_address=ip_address,
        )
        self._attr_available = True
        self._attr_source_list = []
        self._attr_needs_poll = True
        self._attr_poll_interval = IDLE_POLL_INTERVAL
        self._attr_can_group_with = {provider.instance_id}

    async def setup(self) -> None:
        """Set up the player."""
        # Add volume support if available
        await self.update_attributes()
        if self.discovery_info.get("zs"):
            self._attr_supported_features.add(PlayerFeature.VOLUME_SET)
        await self.mass.players.register_or_update(self)

    async def get_config_entries(
        self,
        action: str | None = None,
        values: dict[str, ConfigValueType] | None = None,
    ) -> list[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the player."""
        return [
            *await super().get_config_entries(action=action, values=values),
            CONF_ENTRY_HTTP_PROFILE_DEFAULT_3,
            create_sample_rates_config_entry(
                max_sample_rate=192000,
                safe_max_sample_rate=192000,
                max_bit_depth=24,
                safe_max_bit_depth=24,
            ),
            CONF_ENTRY_OUTPUT_CODEC,
            CONF_ENTRY_FLOW_MODE_ENFORCED,
            ConfigEntry.from_dict(
                {**CONF_ENTRY_ENABLE_ICY_METADATA.to_dict(), "default_value": "full"}
            ),
        ]

    async def disconnect(self) -> None:
        """Disconnect the BluOS client and cleanup."""
        if self._listen_task and not self._listen_task.done():
            self._listen_task.cancel()
        if self.client:
            await self.client.close()
        self.connected = False
        self.logger.debug("Disconnected from player API")

    async def stop(self) -> None:
        """Send STOP command to BluOS player."""
        play_state = await self.client.stop(timeout=1)
        if play_state == "stop":
            self._set_polling_dynamic()
        self._attr_playback_state = PlaybackState.IDLE
        self._attr_current_media = None
        self.update_state()

    async def play(self) -> None:
        """Send PLAY command to BluOS player."""
        play_state = await self.client.play(timeout=1)
        if play_state == "stream":
            self._set_polling_dynamic()
        self._attr_playback_state = PlaybackState.PLAYING
        self.update_state()

    async def pause(self) -> None:
        """Send PAUSE command to BluOS player."""
        play_state = await self.client.pause(timeout=1)
        if play_state == "pause":
            self._set_polling_dynamic()
        self.logger.debug("Set BluOS state to %s", play_state)
        self._attr_playback_state = PlaybackState.PAUSED
        self.update_state()

    async def volume_set(self, volume_level: int) -> None:
        """Send VOLUME_SET command to BluOS player."""
        await self.client.volume(level=volume_level, timeout=1)
        self.logger.debug("Set BluOS speaker volume to %s", volume_level)
        self._attr_volume_level = volume_level
        self.update_state()

    async def volume_mute(self, muted: bool) -> None:
        """Send VOLUME MUTE command to BluOS player."""
        await self.client.volume(mute=muted)
        self._attr_volume_muted = muted
        self.update_state()

    async def next_track(self):
        """Send NEXT TRACK command to BluOS player."""
        await self.client.skip()
        self._set_polling_dynamic()
        self.update_state()

    async def previous_track(self):
        """Send PREVIOUS TRACK command to BluOS player."""
        await self.client.back()
        self._set_polling_dynamic()
        self.update_state()

    async def seek(self, position) -> None:
        """Send PLAY command to BluOS player."""
        play_state = await self.client.play(seek=position, timeout=1)
        if play_state in ("stream", "play"):
            self._set_polling_dynamic()
        self._attr_elapsed_time = position
        self._attr_elapsed_time_last_updated = time.time()
        self._attr_playback_state = PlaybackState.PLAYING
        self.update_state()

    async def play_media(self, media: PlayerMedia) -> None:
        """Handle PLAY MEDIA for BluOS player using the provided URL."""
        self.logger.debug("Play_media called")
        self.logger.debug(media)
        play_state = await self.client.play_url(media.uri, timeout=1)

        # Enable dynamic polling
        if play_state == "stream":
            self._set_polling_dynamic()
            self._attr_playback_state = PlaybackState.PLAYING

        self.logger.debug("Set BluOS state to %s", play_state)

        # Optionally, handle the playback_state or additional logic here
        if play_state in ("PlayerUnexpectedResponseError", "PlayerUnreachableError"):
            raise PlayerCommandFailed("Failed to start playback.")

        # Optimistically update state
        self._attr_current_media = media
        self._attr_elapsed_time = 0
        self._attr_elapsed_time_last_updated = time.time()
        self.update_state()

    async def set_members(
        self,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        """Handle GROUP command for BluOS player."""
        if not player_ids_to_add and not player_ids_to_remove:
            # nothing to do
            return

        def player_id_to_paired_player(player_id: str) -> PairedPlayer:
            client = self.mass.players.get(player_id, raise_unavailable=True)
            return PairedPlayer(client.ip_address, client.port)

        if player_ids_to_remove:
            for player_id in player_ids_to_remove:
                paired_player = player_id_to_paired_player(player_id)
                try:
                    self.sync_status = await self.client.remove_follower(
                        paired_player.ip, paired_player.port, timeout=3
                    )
                except (PlayerUnexpectedResponseError, PlayerUnreachableError) as err:
                    self.logger.debug(f"Could not remove players: {err!s}")
                    continue
                removed_player = self.mass.players.get(player_id)
                if removed_player:
                    removed_player._set_polling_dynamic()
                    removed_player._attr_current_media = None
                    removed_player.update_state()

        if player_ids_to_add:
            for player_id in player_ids_to_add:
                paired_player = player_id_to_paired_player(player_id)
                try:
                    await self.client.add_follower(paired_player.ip, paired_player.port, timeout=5)
                except (PlayerUnexpectedResponseError, PlayerUnreachableError) as err:
                    self.logger.debug(f"Could not add player {paired_player}: {err!s}")
                    continue
                self._attr_group_members.append(player_id)
                added_player = self.mass.players.get(player_id)
                if added_player:
                    added_player._set_polling_dynamic()
                    added_player.update_state()

        self._set_polling_dynamic()
        self.update_state()

    async def ungroup(self) -> None:
        """Handle UNGROUP command for BluOS player."""
        leader = self.client.leader
        leader_player_id = self.client.provider.player_map((leader.ip, leader.port))
        await self.mass.player.get(leader_player_id).set_members(None, [self.player_id])

    async def poll(self) -> None:
        """Poll player for state updates."""
        await self.update_attributes()

    def _resolve_source(self) -> None:
        """Check  PLAYER_SOURCE_MAP for known sources, otherwise create a new source."""

        def resolve_analog_digital_source(source_name) -> PlayerMedia:
            """Resolve Analog/Digital Source here, avoid duplicate entries in PLAYER_SOURCE_MAP."""
            return PlayerSource(
                id=source_name,
                name=source_name,
                passive=True,
                can_play_pause=False,
                can_next_previous=False,
                can_seek=False,
            )

        self.logger.debug(self.status)
        mass_active = self.mass.streams.base_url
        if self.status.stream_url and mass_active in self.status.stream_url:
            self._attr_active_source = self.player_id
        elif player_source := PLAYER_SOURCE_MAP.get(self.status.input_id):
            self._attr_active_source = self.status.input_id
            self._attr_source_list.append(player_source)
        elif player_source := PLAYER_SOURCE_MAP.get(self.status.service):
            self._attr_active_source = self.status.service
            self._attr_source_list.append(player_source)
        elif player_source := PLAYER_SOURCE_MAP.get(self.status.name):
            self._attr_active_source = self.status.name
            self._attr_source_list.append(player_source)
        elif (name := self.status.name) and ("Analog Input" in name or "Digital Input" in name):
            player_source = resolve_analog_digital_source(name)
            self._attr_active_source = name
            self._attr_source_list.append(player_source)
        else:
            self._attr_active_source = self.status.input_id
            self.logger.debug("Appending new PlayerSource")
            self._attr_source_list.append(
                PlayerSource(
                    id=self.status.input_id,
                    name=self.status.input_id,
                    passive=True,
                    can_play_pause=True,
                    can_seek=self.status.can_seek,
                    can_next_previous=True,
                )
            )

    def _resolve_media(self) -> None:
        """Resolve currently playing media dependent on available status attributes."""
        image = self.status.image
        if image:
            image_url = image if image.startswith("http") else self.client.base_url + image
        else:
            image_url = None

        self._attr_current_media = PlayerMedia(
            uri=self.status.stream_url if self.status.stream_url else self.status.name,
            title=self.status.name,
            artist=self.status.artist,
            album=self.status.album,
            image_url=image_url,
            duration=self.status.total_seconds if self.status.total_seconds else None,
        )

    async def update_attributes(self) -> None:
        """Update the BluOS player attributes."""
        self.logger.debug(f"updating {self.player_id} attributes")
        if self.dynamic_poll_count > 0:
            self.dynamic_poll_count -= 1

        try:
            self.status = await self.client.status()
            self._attr_available = True
        except (PlayerUnreachableError, PlayerUnexpectedResponseError) as err:
            self.logger.debug(f"Player {self.name} status check failed: {err}")
            self._attr_available = False
            self._attr_poll_interval = IDLE_POLL_INTERVAL
            self.update_state()
            return

        if (
            self.poll_state == POLL_STATE_DYNAMIC and self.dynamic_poll_count <= 0
        ) or self._attr_playback_state == PLAYBACK_STATE_POLL_MAP[self.status.state]:
            self.logger.debug(f"Changing bluos poll state from {self.poll_state} to static")
            self.poll_state = POLL_STATE_STATIC

        self._attr_playback_state = PLAYBACK_STATE_MAP[self.status.state]

        # Update polling interval
        if self.poll_state != POLL_STATE_DYNAMIC:
            if self._attr_playback_state == PlaybackState.PLAYING:
                self.logger.debug("Setting playback poll interval")
                self._attr_poll_interval = PLAYBACK_POLL_INTERVAL
            else:
                self.logger.debug("Setting idle poll interval")
                self._attr_poll_interval = IDLE_POLL_INTERVAL

        self.sync_status = await self.client.sync_status()
        self._attr_source_list = await self._get_bluesound_sources()

        self._attr_name = self.sync_status.name

        # Update timing
        self._attr_elapsed_time = self.status.seconds
        self._attr_elapsed_time_last_updated = time.time()

        if self.sync_status.volume == -1:
            # -1 is fixed volume
            self._attr_volume_level = 100
        else:
            self._attr_volume_level = self.sync_status.volume
        self._attr_volume_muted = self.status.mute

        if not self.sync_status.leader:
            # Player not grouped or player is group leader
            if self.sync_status.followers:
                self._attr_group_members = [
                    self.provider.player_map[f.ip, f.port]
                    for f in self.sync_status.followers
                    if (f.ip, f.port) in self.provider.player_map
                ]
            else:
                self._attr_group_members.clear()

            self._resolve_source()
            self._resolve_media()
        else:
            # Player has group leader
            self._attr_group_members.clear()
            leader = self.sync_status.leader
            leader_player_id = self.provider.player_map.get((leader.ip, leader.port), None)
            self._attr_active_source = leader_player_id

        self.update_state()

    async def select_source(self, source: str) -> None:
        """
        Handle SELECT SOURCE command on the player.

        Will only be called if the PlayerFeature.SELECT_SOURCE is supported.

        :param source: The source(id) to select, as defined in the source_list.
        """
        source_type, source_id = source.split("-", 1)
        if source_type == "preset":
            await self.client.load_preset(preset_id=source_id)
        elif source_type == "input":
            await self.client.play_url(source_id)
        self._set_polling_dynamic()
        self.update_state()

    async def _get_bluesound_sources(self, timeout: float | None = None) -> None:
        """Resolve Bluesound presets and inputs to MA PlayerSource.

        :param timeout: The timeout for getting inputs and presets.
        """

        def _preset_to_ma_source(preset: Preset):
            return PlayerSource(
                id=f"preset-{preset.id}",
                name=f"Preset {preset.id:02d}: {preset.name}",
                passive=False,
                can_play_pause=True,
                can_seek=False,
                can_next_previous=True,
            )

        def _input_to_ma_source(bluos_input: Input):
            return PlayerSource(
                id=f"input-{bluos_input.url}",
                name=f"Input: {bluos_input.text}",
                passive=False,
                can_play_pause=False,
                can_seek=False,
                can_next_previous=False,
            )

        presets = await self.client.presets(timeout=timeout)
        inputs = await self.client.inputs(timeout=timeout)
        inputs_as_sources = [_input_to_ma_source(bluos_input) for bluos_input in inputs]
        return [_preset_to_ma_source(preset) for preset in presets] + inputs_as_sources

    def _set_polling_dynamic(self, poll_count: int = 6, poll_interval: float = 0.5):
        self.poll_state = POLL_STATE_DYNAMIC
        self.dynamic_poll_count = poll_count
        self._attr_poll_interval = poll_interval

    @property
    def synced_to(self) -> str | None:
        """
        Return the id of the player this player is synced to (sync leader).

        If this player is not synced to another player (or is the sync leader itself),
        this should return None.
        If it is part of a (permanent) group, this should also return None.
        """
        if self.sync_status.leader:
            leader = self.sync_status.leader
            return self.provider.player_map.get((leader.ip, leader.port), None)
        return None
