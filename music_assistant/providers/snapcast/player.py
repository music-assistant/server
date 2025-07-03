"""Snapcast Player implementation."""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import PlayerFeature, PlayerType

from music_assistant.constants import (
    CONF_ENTRY_FLOW_MODE_ENFORCED,
    CONF_ENTRY_OUTPUT_CODEC_HIDDEN,
    create_sample_rates_config_entry,
)
from music_assistant.models.player import DeviceInfo, Player, PlayerMedia

if TYPE_CHECKING:
    from snapcast.control.client import Snapclient

    from .provider import SnapcastPlayerProvider, SnapCastStreamType


class SnapcastPlayer(Player):
    """Snapcast Player implementation."""

    def __init__(
        self,
        provider: SnapcastPlayerProvider,
        player_id: str,
        snap_client: Snapclient,
    ) -> None:
        """Initialize the Snapcast Player."""
        super().__init__(provider, player_id)
        self.snap_client = snap_client

        # Set player attributes
        self._attr_type = PlayerType.PLAYER
        self._attr_supported_features = {
            PlayerFeature.SET_MEMBERS,
            PlayerFeature.VOLUME_SET,
            PlayerFeature.VOLUME_MUTE,
            PlayerFeature.PLAY_ANNOUNCEMENT,
        }
        self._attr_name = snap_client.friendly_name
        self._attr_available = snap_client.connected
        self._attr_device_info = DeviceInfo(
            model=snap_client._client.get("host").get("os"),
            ip_address=snap_client._client.get("host").get("ip"),
            manufacturer=snap_client._client.get("host").get("arch"),
        )
        self._attr_can_group_with = {provider.instance_id}

    async def setup(self) -> None:
        """Set up the player."""
        await self.mass.players.register_or_update(self)

    async def get_config_entries(self) -> list[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the player."""
        return [
            *await super().get_config_entries(),
            CONF_ENTRY_FLOW_MODE_ENFORCED,
            CONF_ENTRY_OUTPUT_CODEC_HIDDEN,
            create_sample_rates_config_entry(
                supported_sample_rates=[48000],
                supported_bit_depths=[16],
                hidden=True,
            ),
        ]

    async def volume_set(self, volume_level: int) -> None:
        """Handle VOLUME_SET command on the player."""
        await self.snap_client.set_volume(volume_level)

    async def volume_mute(self, muted: bool) -> None:
        """Handle VOLUME MUTE command on the player."""
        await self.snap_client.set_muted(muted)

    async def play_media(self, media: PlayerMedia) -> None:
        """Handle PLAY MEDIA on the player."""
        if self.synced_to:
            msg = "A synced player cannot receive play commands directly"
            raise RuntimeError(msg)

        # Stop any existing stream tasks first
        if stream_task := self.provider._stream_tasks.pop(self.player_id, None):
            if not stream_task.done():
                stream_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await stream_task

        # Get stream or create new one
        stream_name = self.provider._get_stream_name(self.player_id, SnapCastStreamType.MUSIC)
        stream = await self.provider._get_or_create_stream(
            stream_name, media.queue_id or self.player_id
        )

        # If no announcement is playing we activate the stream now, otherwise it
        # will be activated by play_announcement when the announcement is over.
        if not self.announcement_in_progress:
            snap_group = self.provider._get_snapgroup(self.player_id)
            await snap_group.set_stream(stream.identifier)

        self._attr_current_media = media
        self._attr_active_source = media.queue_id
        self.update_state()

    async def play_announcement(
        self, announcement: PlayerMedia, volume_level: int | None = None
    ) -> None:
        """Handle PLAY ANNOUNCEMENT on the player."""
        # Implementation for announcements would go here
        # This is complex and involves stream management

    async def set_members(
        self,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        """Handle SET_MEMBERS command on the player."""
        # Get the snapcast group
        snap_group = self.provider._get_snapgroup(self.player_id)

        if player_ids_to_add:
            for player_id in player_ids_to_add:
                snap_client = self.provider.snapcast.clients.get(player_id)
                if snap_client:
                    await snap_group.add_client(snap_client)

        if player_ids_to_remove:
            for player_id in player_ids_to_remove:
                snap_client = self.provider.snapcast.clients.get(player_id)
                if snap_client:
                    await snap_group.remove_client(snap_client)

    async def poll(self) -> None:
        """Poll player for state updates."""
        # Snapcast is event-driven, no polling needed

    def update_from_snap_client(self) -> None:
        """Update player attributes from snapcast client."""
        self._attr_available = self.snap_client.connected
        self._attr_name = self.snap_client.friendly_name

        # Update volume
        if hasattr(self.snap_client, "volume"):
            self._attr_volume_level = self.snap_client.volume
        if hasattr(self.snap_client, "muted"):
            self._attr_volume_muted = self.snap_client.muted

        # Update group information
        if hasattr(self.snap_client, "group"):
            group = self.snap_client.group
            if group and hasattr(group, "clients"):
                self._attr_group_members = [
                    c.identifier for c in group.clients if c.identifier != self.player_id
                ]

        self.update_state()
