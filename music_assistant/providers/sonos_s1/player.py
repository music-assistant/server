"""
Sonos Player provider for Music Assistant: SonosPlayer object/model.

Note that large parts of this code are copied over from the Home Assistant
integration for Sonos.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from music_assistant_models.enums import PlaybackState, PlayerFeature, PlayerType
from music_assistant_models.errors import PlayerCommandFailed
from soco import SoCoException
from soco.core import (
    MUSIC_SRC_AIRPLAY,
    MUSIC_SRC_LINE_IN,
    MUSIC_SRC_RADIO,
    MUSIC_SRC_SPOTIFY_CONNECT,
    MUSIC_SRC_TV,
    SoCo,
)

from music_assistant.constants import (
    CONF_ENTRY_FLOW_MODE_HIDDEN_DISABLED,
    CONF_ENTRY_HTTP_PROFILE_DEFAULT_1,
    CONF_ENTRY_OUTPUT_CODEC,
    create_sample_rates_config_entry,
)
from music_assistant.helpers.upnp import create_didl_metadata
from music_assistant.models.player import DeviceInfo, Player, PlayerMedia

from .helpers import soco_error

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry
    from soco.events_base import Event as SonosEvent
    from soco.events_base import SubscriptionBase

    from .provider import SonosPlayerProvider

CALLBACK_TYPE = Callable[[], None]
LOGGER = logging.getLogger(__name__)

PLAYER_FEATURES = (
    PlayerFeature.SET_MEMBERS,
    PlayerFeature.VOLUME_MUTE,
    PlayerFeature.VOLUME_SET,
)

SOURCES_MAP = {
    MUSIC_SRC_LINE_IN: "Line-in",
    MUSIC_SRC_TV: "TV",
    MUSIC_SRC_RADIO: "Radio",
    MUSIC_SRC_SPOTIFY_CONNECT: "Spotify",
    MUSIC_SRC_AIRPLAY: "AirPlay",
}

PLAYBACK_STATE_MAP = {
    "PLAYING": PlaybackState.PLAYING,
    "PAUSED_PLAYBACK": PlaybackState.PAUSED,
    "STOPPED": PlaybackState.IDLE,
    "TRANSITIONING": PlaybackState.PLAYING,
}

NEVER_TIME = 0


class SonosSubscriptionsFailed(PlayerCommandFailed):
    """Subscription creation failed."""


class SonosPlayer(Player):
    """Sonos Player implementation for S1 speakers."""

    def __init__(
        self,
        provider: SonosPlayerProvider,
        soco: SoCo,
    ) -> None:
        """Initialize SonosPlayer instance."""
        super().__init__(provider, soco.uid)
        self.soco = soco
        self.household_id: str = soco.household_id
        self.subscriptions: list[SubscriptionBase] = []

        # Set player attributes
        self._attr_type = PlayerType.PLAYER
        self._attr_supported_features = set(PLAYER_FEATURES)
        self._attr_name = soco.player_name
        self._attr_device_info = DeviceInfo(
            model=soco.speaker_info["model_name"],
            manufacturer="Sonos",
            ip_address=soco.ip_address,
        )
        self._attr_available = True
        self._attr_can_group_with = {provider.lookup_key}

        # Cached attributes
        self.crossfade: bool = False
        self.play_mode: str | None = None
        self.playback_status: str | None = None
        self.channel: str | None = None
        self.duration: float | None = None
        self.image_url: str | None = None
        self.source_name: str | None = None
        self.title: str | None = None
        self.uri: str | None = None
        self.position: int | None = None
        self.position_updated_at: datetime.datetime | None = None
        self.loudness: bool = False
        self.bass: int = 0
        self.treble: int = 0

        # Subscriptions and events
        self._subscriptions: list[SubscriptionBase] = []
        self._subscription_lock: asyncio.Lock | None = None
        self._last_activity: float = NEVER_TIME
        self._resub_cooldown_expires_at: float | None = None

        # Grouping
        self.sync_coordinator: SonosPlayer | None = None
        # self.group_members: list[SonosPlayer] = [self]

    async def setup(self) -> None:
        """Set up the player."""
        await self.mass.players.register_or_update(self)

    async def get_config_entries(self) -> list[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the player."""
        return [
            *await super().get_config_entries(),
            CONF_ENTRY_FLOW_MODE_HIDDEN_DISABLED,
            CONF_ENTRY_HTTP_PROFILE_DEFAULT_1,
            CONF_ENTRY_OUTPUT_CODEC,
            create_sample_rates_config_entry(
                supported_sample_rates=[44100, 48000],
                supported_bit_depths=[16],
                hidden=False,
            ),
        ]

    async def stop(self) -> None:
        """Send STOP command to the player."""
        if self.sync_coordinator:
            self.logger.debug(
                "Ignore STOP command for %s: Player is synced to another player.",
                self.player_id,
            )
            return
        await asyncio.to_thread(self.soco.stop)
        self.mass.call_later(2, self.poll_speaker)
        self._attr_active_source = None
        self.update_state()

    async def play(self) -> None:
        """Send PLAY command to the player."""
        if self.sync_coordinator:
            self.logger.debug(
                "Ignore PLAY command for %s: Player is synced to another player.",
                self.player_id,
            )
            return
        await asyncio.to_thread(self.soco.play)
        self._attr_poll_interval = 5
        self.mass.call_later(2, self.poll_speaker)

    async def pause(self) -> None:
        """Send PAUSE command to the player."""
        if self.sync_coordinator:
            self.logger.debug(
                "Ignore PAUSE command for %s: Player is synced to another player.",
                self.player_id,
            )
            return
        if "Pause" not in self.soco.available_actions:
            # pause not possible
            await self.stop()
            return
        await asyncio.to_thread(self.soco.pause)
        self.mass.call_later(2, self.poll_speaker)

    async def volume_set(self, volume_level: int) -> None:
        """Send VOLUME_SET command to the player."""

        def set_volume_level(volume_level: int) -> None:
            self.soco.volume = volume_level

        await asyncio.to_thread(set_volume_level, volume_level)
        self.mass.call_later(2, self.poll_speaker)

    async def volume_mute(self, muted: bool) -> None:
        """Send VOLUME MUTE command to the player."""

        def set_volume_mute(muted: bool) -> None:
            self.soco.mute = muted

        await asyncio.to_thread(set_volume_mute, muted)
        self.mass.call_later(2, self.poll_speaker)

    async def play_media(self, media: PlayerMedia) -> None:
        """Handle PLAY MEDIA on the player."""
        if self.sync_coordinator:
            # this should be already handled by the player manager, but just in case...
            msg = (
                f"Player {self.display_name} can not "
                "accept play_media command, it is synced to another player."
            )
            raise PlayerCommandFailed(msg)

        didl_metadata = create_didl_metadata(media)
        await asyncio.to_thread(self.soco.play_uri, media.uri, meta=didl_metadata)
        self.mass.call_later(2, self.poll_speaker)
        self._attr_poll_interval = 5

    async def enqueue_next_media(self, media: PlayerMedia) -> None:
        """Handle enqueuing next media item."""
        if self.sync_coordinator:
            # this should be already handled by the player manager, but just in case...
            msg = (
                f"Player {self.display_name} can not "
                "accept enqueue command, it is synced to another player."
            )
            raise PlayerCommandFailed(msg)

        didl_metadata = create_didl_metadata(media)

        # Disable crossfade mode if needed
        # crossfading is handled by our streams controller
        if self.crossfade:

            def set_crossfade() -> None:
                try:
                    self.soco.cross_fade = False
                except SoCoException as err:
                    self.logger.debug("Error setting crossfade: %s", err)

            await asyncio.to_thread(set_crossfade)

        def add_to_queue() -> None:
            self.soco.add_uri_to_queue(media.uri, didl_metadata)

        await asyncio.to_thread(add_to_queue)
        self.mass.call_later(2, self.poll_speaker)

    async def set_members(
        self,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        """Handle SET_MEMBERS command on the player."""
        # TODO: Implement Sonos S1 grouping logic
        # This would involve calling SoCo grouping methods

    async def poll(self) -> None:
        """Poll player for state updates."""
        self.poll_speaker()

    def poll_speaker(self) -> None:
        """Poll speaker for state updates."""
        try:
            # Update speaker state from SoCo
            self._update_speaker_state()
        except Exception as err:
            self.logger.debug("Error polling speaker: %s", err)

    def _update_speaker_state(self) -> None:
        """Update speaker state from SoCo."""
        try:
            # Get current transport info
            transport_info = self.soco.get_current_transport_info()
            self.playback_status = transport_info.get("current_transport_state")

            # Update playback state
            if self.playback_status:
                self._attr_playback_state = PLAYBACK_STATE_MAP.get(
                    self.playback_status, PlaybackState.IDLE
                )

            # Get volume info
            self._attr_volume_level = self.soco.volume
            self._attr_volume_muted = self.soco.mute

            # Get current track info
            track_info = self.soco.get_current_track_info()
            if track_info:
                self._attr_current_media = PlayerMedia(
                    uri=track_info.get("uri", ""),
                    title=track_info.get("title"),
                    artist=track_info.get("artist"),
                    album=track_info.get("album"),
                    image_url=track_info.get("album_art"),
                )
                self.position = int(track_info.get("position", "0").split(":")[0]) * 60 + int(
                    track_info.get("position", "0").split(":")[1]
                )

            # Update other attributes
            self._attr_name = self.soco.player_name
            self.crossfade = self.soco.cross_fade

            self.update_state()

        except Exception as err:
            self.logger.debug("Error updating speaker state: %s", err)

    @property
    def is_coordinator(self) -> bool:
        """Return True if this player is the group coordinator."""
        return self.sync_coordinator is None

    def schedule_poll(self, interval: float = 2.0) -> None:
        """Schedule a poll update."""
        self.mass.call_later(interval, self.poll_speaker)

    @soco_error()
    def join(self, target_player: SonosPlayer) -> None:
        """Join this player to another player's group."""
        self.soco.join(target_player.soco)

    @soco_error()
    def unjoin(self) -> None:
        """Remove this player from its group."""
        self.soco.unjoin()

    def speaker_activity(self, event: SonosEvent) -> None:
        """Handle speaker activity from Sonos events."""
        self._last_activity = time.time()
        self.schedule_poll()
