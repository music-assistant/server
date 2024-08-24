"""
Sonos Player provider for Music Assistant: SonosPlayer object/model.

Note that large parts of this code are copied over from the Home Assistant
integration for Sonos.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import logging
import time
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any

from soco import SoCoException
from soco.core import (
    MUSIC_SRC_AIRPLAY,
    MUSIC_SRC_LINE_IN,
    MUSIC_SRC_RADIO,
    MUSIC_SRC_SPOTIFY_CONNECT,
    MUSIC_SRC_TV,
    SoCo,
)
from soco.data_structures import DidlAudioBroadcast, DidlPlaylistContainer

from music_assistant.common.helpers.datetime import utc
from music_assistant.common.models.enums import PlayerFeature, PlayerState
from music_assistant.common.models.errors import PlayerCommandFailed
from music_assistant.common.models.player import DeviceInfo, Player
from music_assistant.constants import VERBOSE_LOG_LEVEL

from .helpers import SonosUpdateError, soco_error

if TYPE_CHECKING:
    from soco.events_base import Event as SonosEvent
    from soco.events_base import SubscriptionBase

    from . import SonosPlayerProvider

CALLBACK_TYPE = Callable[[], None]
LOGGER = logging.getLogger(__name__)

PLAYER_FEATURES = (
    PlayerFeature.SYNC,
    PlayerFeature.VOLUME_MUTE,
    PlayerFeature.VOLUME_SET,
    PlayerFeature.ENQUEUE_NEXT,
)
DURATION_SECONDS = "duration_in_s"
POSITION_SECONDS = "position_in_s"
SUBSCRIPTION_TIMEOUT = 1200
ZGS_SUBSCRIPTION_TIMEOUT = 2
AVAILABILITY_CHECK_INTERVAL = datetime.timedelta(minutes=1)
AVAILABILITY_TIMEOUT = AVAILABILITY_CHECK_INTERVAL.total_seconds() * 4.5
SONOS_STATE_PLAYING = "PLAYING"
SONOS_STATE_TRANSITIONING = "TRANSITIONING"
NEVER_TIME = -1200.0
RESUB_COOLDOWN_SECONDS = 10.0
SUBSCRIPTION_SERVICES = {
    # "alarmClock",
    "avTransport",
    # "contentDirectory",
    "deviceProperties",
    "renderingControl",
    "zoneGroupTopology",
}
SUPPORTED_VANISH_REASONS = ("powered off", "sleeping", "switch to bluetooth", "upgrade")
UNUSED_DEVICE_KEYS = ["SPID", "TargetRoomName"]
LINEIN_SOURCES = (MUSIC_SRC_TV, MUSIC_SRC_LINE_IN)
SOURCE_AIRPLAY = "AirPlay"
SOURCE_LINEIN = "Line-in"
SOURCE_SPOTIFY_CONNECT = "Spotify Connect"
SOURCE_TV = "TV"
SOURCE_MAPPING = {
    MUSIC_SRC_AIRPLAY: SOURCE_AIRPLAY,
    MUSIC_SRC_TV: SOURCE_TV,
    MUSIC_SRC_LINE_IN: SOURCE_LINEIN,
    MUSIC_SRC_SPOTIFY_CONNECT: SOURCE_SPOTIFY_CONNECT,
}


class SonosSubscriptionsFailed(PlayerCommandFailed):
    """Subscription creation failed."""


class SonosPlayer:
    """Wrapper around Sonos/SoCo with some additional attributes."""

    def __init__(
        self,
        sonos_prov: SonosPlayerProvider,
        soco: SoCo,
        mass_player: Player,
    ) -> None:
        """Initialize SonosPlayer instance."""
        self.sonos_prov = sonos_prov
        self.mass = sonos_prov.mass
        self.player_id = soco.uid
        self.soco = soco
        self.logger = sonos_prov.logger
        self.household_id: str = soco.household_id
        self.subscriptions: list[SubscriptionBase] = []
        self.mass_player: Player = mass_player
        self.available: bool = True
        # cached attributes
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
        self.group_members: list[SonosPlayer] = [self]
        self.group_members_ids: list[str] = []
        self._group_members_missing: set[str] = set()

    def __hash__(self) -> int:
        """Return a hash of self."""
        return hash(self.player_id)

    @property
    def zone_name(self) -> str:
        """Return zone name."""
        if self.mass_player:
            return self.mass_player.display_name
        return self.soco.speaker_info["zone_name"]

    @property
    def subscription_address(self) -> str:
        """Return the current subscription callback address."""
        assert len(self._subscriptions) > 0
        addr, port = self._subscriptions[0].event_listener.address
        return ":".join([addr, str(port)])

    @property
    def missing_subscriptions(self) -> set[str]:
        """Return a list of missing service subscriptions."""
        subscribed_services = {sub.service.service_type for sub in self._subscriptions}
        return SUBSCRIPTION_SERVICES - subscribed_services

    @property
    def should_poll(self) -> bool:
        """Return if this player should be polled/pinged."""
        if not self.available:
            return True
        return (time.monotonic() - self._last_activity) > self.mass_player.poll_interval

    def setup(self) -> None:
        """Run initial setup of the speaker (NOT async friendly)."""
        if self.soco.is_coordinator:
            self.crossfade = self.soco.cross_fade
        self.mass_player.volume_level = self.soco.volume
        self.mass_player.volume_muted = self.soco.mute
        self.loudness = self.soco.loudness
        self.bass = self.soco.bass
        self.treble = self.soco.treble
        self.update_groups()
        if not self.sync_coordinator:
            self.poll_media()

        asyncio.run_coroutine_threadsafe(self.subscribe(), self.mass.loop)

    async def offline(self) -> None:
        """Handle removal of speaker when unavailable."""
        if not self.available:
            return

        if self._resub_cooldown_expires_at is None and not self.mass.closing:
            self._resub_cooldown_expires_at = time.monotonic() + RESUB_COOLDOWN_SECONDS
            self.logger.debug("Starting resubscription cooldown for %s", self.zone_name)

        self.available = False
        self.mass_player.available = False
        self.mass.players.update(self.player_id)
        self._share_link_plugin = None

        await self.unsubscribe()

    def log_subscription_result(self, result: Any, event: str, level: int = logging.DEBUG) -> None:
        """Log a message if a subscription action (create/renew/stop) results in an exception."""
        if not isinstance(result, Exception):
            return

        if isinstance(result, asyncio.exceptions.TimeoutError):
            message = "Request timed out"
            exc_info = None
        else:
            message = str(result)
            exc_info = result if not str(result) else None

        self.logger.log(
            level,
            "%s failed for %s: %s",
            event,
            self.zone_name,
            message,
            exc_info=exc_info if self.logger.isEnabledFor(10) else None,
        )

    async def subscribe(self) -> None:
        """Initiate event subscriptions under an async lock."""
        if not self._subscription_lock:
            self._subscription_lock = asyncio.Lock()

        async with self._subscription_lock:
            try:
                # Create event subscriptions.
                subscriptions = [
                    self._subscribe_target(getattr(self.soco, service), self._handle_event)
                    for service in self.missing_subscriptions
                ]
                if not subscriptions:
                    return
                self.logger.log(VERBOSE_LOG_LEVEL, "Creating subscriptions for %s", self.zone_name)
                results = await asyncio.gather(*subscriptions, return_exceptions=True)
                for result in results:
                    self.log_subscription_result(result, "Creating subscription", logging.WARNING)
                if any(isinstance(result, Exception) for result in results):
                    raise SonosSubscriptionsFailed
            except SonosSubscriptionsFailed:
                self.logger.warning("Creating subscriptions failed for %s", self.zone_name)
                assert self._subscription_lock is not None
                async with self._subscription_lock:
                    await self.offline()

    async def unsubscribe(self) -> None:
        """Cancel all subscriptions."""
        if not self._subscriptions:
            return
        self.logger.log(VERBOSE_LOG_LEVEL, "Unsubscribing from events for %s", self.zone_name)
        results = await asyncio.gather(
            *(subscription.unsubscribe() for subscription in self._subscriptions),
            return_exceptions=True,
        )
        for result in results:
            self.log_subscription_result(result, "Unsubscribe")
        self._subscriptions = []

    async def check_poll(self) -> bool:
        """Validate availability of the speaker based on recent activity."""
        if not self.should_poll:
            return False
        self.logger.log(VERBOSE_LOG_LEVEL, "Polling player for availability...")
        try:
            await asyncio.to_thread(self.ping)
            self._speaker_activity("ping")
        except SonosUpdateError:
            if not self.available:
                return False  # already offline
            self.logger.warning(
                "No recent activity and cannot reach %s, marking unavailable",
                self.zone_name,
            )
            await self.offline()
        return True

    def update_ip(self, ip_address: str) -> None:
        """Handle updated IP of a Sonos player (NOT async friendly)."""
        if self.available:
            return
        self.logger.debug(
            "Player IP-address changed from %s to %s", self.soco.ip_address, ip_address
        )
        try:
            self.ping()
        except SonosUpdateError:
            return
        self.soco.ip_address = ip_address
        self.setup()
        self.mass_player.device_info = DeviceInfo(
            model=self.mass_player.device_info.model,
            address=ip_address,
            manufacturer=self.mass_player.device_info.manufacturer,
        )
        self.update_player()

    @soco_error()
    def ping(self) -> None:
        """Test device availability. Failure will raise SonosUpdateError."""
        self.soco.renderingControl.GetVolume([("InstanceID", 0), ("Channel", "Master")], timeout=1)

    async def join(
        self,
        members: list[SonosPlayer],
    ) -> None:
        """Sync given players/speakers with this player."""
        async with self.sonos_prov.topology_condition:
            group: list[SonosPlayer] = await self.mass.create_task(self._join, members)
            await self.wait_for_groups([group])

    async def unjoin(self) -> None:
        """Unjoin player from all/any groups."""
        async with self.sonos_prov.topology_condition:
            await self.mass.create_task(self._unjoin)
            await self.wait_for_groups([[self]])

    def update_player(self, signal_update: bool = True) -> None:
        """Update Sonos Player."""
        self._update_attributes()
        if signal_update:
            # send update to the player manager right away only if we are triggered from an event
            # when we're just updating from a manual poll, the player manager
            # will detect changes to the player object itself
            self.mass.loop.call_soon_threadsafe(self.sonos_prov.mass.players.update, self.player_id)

    async def poll_speaker(self) -> None:
        """Poll the speaker for updates."""

        def _poll():
            """Poll the speaker for updates (NOT async friendly)."""
            self.update_groups()
            self.poll_media()
            self.mass_player.volume_level = self.soco.volume
            self.mass_player.volume_muted = self.soco.mute

        await asyncio.to_thread(_poll)

    @soco_error()
    def poll_media(self) -> None:
        """Poll information about currently playing media."""
        transport_info = self.soco.get_current_transport_info()
        new_status = transport_info["current_transport_state"]

        if new_status == SONOS_STATE_TRANSITIONING:
            return

        update_position = new_status != self.playback_status
        self.playback_status = new_status
        self.play_mode = self.soco.play_mode
        self._set_basic_track_info(update_position=update_position)
        self.update_player()

    async def _subscribe_target(self, target: SubscriptionBase, sub_callback: Callable) -> None:
        """Create a Sonos subscription for given target."""
        subscription = await target.subscribe(
            auto_renew=True, requested_timeout=SUBSCRIPTION_TIMEOUT
        )

        def on_renew_failed(exception: Exception) -> None:
            """Handle a failed subscription renewal callback."""
            self.mass.create_task(self._renew_failed(exception))

        subscription.callback = sub_callback
        subscription.auto_renew_fail = on_renew_failed
        self._subscriptions.append(subscription)

    async def _renew_failed(self, exception: Exception) -> None:
        """Mark the speaker as offline after a subscription renewal failure.

        This is to reset the state to allow a future clean subscription attempt.
        """
        if not self.available:
            return

        self.log_subscription_result(exception, "Subscription renewal", logging.WARNING)
        await self.offline()

    def _handle_event(self, event: SonosEvent) -> None:
        """Handle SonosEvent callback."""
        service_type: str = event.service.service_type
        self._speaker_activity(f"{service_type} subscription")

        if service_type == "DeviceProperties":
            self.update_player()
            return
        if service_type == "AVTransport":
            self._handle_avtransport_event(event)
            return
        if service_type == "RenderingControl":
            self._handle_rendering_control_event(event)
            return
        if service_type == "ZoneGroupTopology":
            self._handle_zone_group_topology_event(event)
            return

    def _handle_avtransport_event(self, event: SonosEvent) -> None:
        """Update information about currently playing media from an event."""
        # NOTE: The new coordinator can be provided in a media update event but
        # before the ZoneGroupState updates. If this happens the playback
        # state will be incorrect and should be ignored. Switching to the
        # new coordinator will use its media. The regrouping process will
        # be completed during the next ZoneGroupState update.
        av_transport_uri = event.variables.get("av_transport_uri", "")
        current_track_uri = event.variables.get("current_track_uri", "")
        if av_transport_uri == current_track_uri and av_transport_uri.startswith("x-rincon:"):
            new_coordinator_uid = av_transport_uri.split(":")[-1]
            if new_coordinator_speaker := self.sonos_prov.sonosplayers.get(new_coordinator_uid):
                self.logger.log(
                    5,
                    "Media update coordinator (%s) received for %s",
                    new_coordinator_speaker.zone_name,
                    self.zone_name,
                )
                self.sync_coordinator = new_coordinator_speaker
            else:
                self.logger.debug(
                    "Media update coordinator (%s) for %s not yet available",
                    new_coordinator_uid,
                    self.zone_name,
                )
            return

        if crossfade := event.variables.get("current_crossfade_mode"):
            self.crossfade = bool(int(crossfade))

        # Missing transport_state indicates a transient error
        if (new_status := event.variables.get("transport_state")) is None:
            return

        # Ignore transitions, we should get the target state soon
        if new_status == SONOS_STATE_TRANSITIONING:
            return

        evars = event.variables
        new_status = evars["transport_state"]
        state_changed = new_status != self.playback_status

        self.play_mode = evars["current_play_mode"]
        self.playback_status = new_status

        track_uri = evars["enqueued_transport_uri"] or evars["current_track_uri"]
        audio_source = self.soco.music_source_from_uri(track_uri)

        self._set_basic_track_info(update_position=state_changed)

        if (ct_md := evars["current_track_meta_data"]) and not self.image_url:
            if album_art_uri := getattr(ct_md, "album_art_uri", None):
                # TODO: handle library mess here
                self.image_url = album_art_uri

        et_uri_md = evars["enqueued_transport_uri_meta_data"]
        if isinstance(et_uri_md, DidlPlaylistContainer):
            self.playlist_name = et_uri_md.title

        if queue_size := evars.get("number_of_tracks", 0):
            self.queue_size = int(queue_size)

        if audio_source == MUSIC_SRC_RADIO:
            if et_uri_md:
                self.channel = et_uri_md.title

            # Extra guards for S1 compatibility
            if ct_md and hasattr(ct_md, "radio_show") and ct_md.radio_show:
                radio_show = ct_md.radio_show.split(",")[0]
                self.channel = " • ".join(filter(None, [self.channel, radio_show]))

            if isinstance(et_uri_md, DidlAudioBroadcast):
                self.title = self.title or self.channel

        self.update_player()

    def _handle_rendering_control_event(self, event: SonosEvent) -> None:
        """Update information about currently volume settings."""
        variables = event.variables

        if "volume" in variables:
            volume = variables["volume"]
            self.mass_player.volume_level = int(volume["Master"])

        if mute := variables.get("mute"):
            self.mass_player.volume_muted = mute["Master"] == "1"

        self.update_player()

    def _handle_zone_group_topology_event(self, event: SonosEvent) -> None:
        """Handle callback for topology change event."""
        if "zone_player_uui_ds_in_group" not in event.variables:
            return
        asyncio.run_coroutine_threadsafe(self.create_update_groups_coro(event), self.mass.loop)

    async def _rebooted(self) -> None:
        """Handle a detected speaker reboot."""
        self.logger.debug("%s rebooted, reconnecting", self.zone_name)
        await self.offline()
        self._speaker_activity("reboot")

    def update_groups(self) -> None:
        """Update group topology when polling."""
        asyncio.run_coroutine_threadsafe(self.create_update_groups_coro(), self.mass.loop)

    def update_group_for_uid(self, uid: str) -> None:
        """Update group topology if uid is missing."""
        if uid not in self._group_members_missing:
            return
        missing_zone = self.sonos_prov.sonosplayers[uid].zone_name
        self.logger.debug("%s was missing, adding to %s group", missing_zone, self.zone_name)
        self.update_groups()

    def create_update_groups_coro(self, event: SonosEvent | None = None) -> Coroutine:
        """Handle callback for topology change event."""

        def _get_soco_group() -> list[str]:
            """Ask SoCo cache for existing topology."""
            coordinator_uid = self.soco.uid
            joined_uids = []
            with contextlib.suppress(OSError, SoCoException):
                if self.soco.group and self.soco.group.coordinator:
                    coordinator_uid = self.soco.group.coordinator.uid
                    joined_uids = [
                        p.uid
                        for p in self.soco.group.members
                        if p.uid != coordinator_uid and p.is_visible
                    ]

            return [coordinator_uid, *joined_uids]

        async def _extract_group(event: SonosEvent | None) -> list[str]:
            """Extract group layout from a topology event."""
            group = event and event.zone_player_uui_ds_in_group
            if group:
                assert isinstance(group, str)
                return group.split(",")
            return await self.mass.create_task(_get_soco_group)

        def _regroup(group: list[str]) -> None:
            """Rebuild internal group layout (async safe)."""
            if group == [self.soco.uid] and self.group_members == [self] and self.group_members_ids:
                # Skip updating existing single speakers in polling mode
                return

            group_members = []
            group_members_ids = []

            for uid in group:
                speaker = self.sonos_prov.sonosplayers.get(uid)
                if speaker:
                    self._group_members_missing.discard(uid)
                    group_members.append(speaker)
                    group_members_ids.append(uid)
                else:
                    self._group_members_missing.add(uid)
                    self.logger.debug(
                        "%s group member unavailable (%s), will try again",
                        self.zone_name,
                        uid,
                    )
                    return

            if self.group_members_ids == group_members_ids:
                # Useful in polling mode for speakers with stereo pairs or surrounds
                # as those "invisible" speakers will bypass the single speaker check
                return

            self.sync_coordinator = None
            self.group_members = group_members
            self.group_members_ids = group_members_ids
            self.mass.players.update(self.player_id)

            for joined_uid in group[1:]:
                joined_speaker: SonosPlayer = self.sonos_prov.sonosplayers.get(joined_uid)
                if joined_speaker:
                    joined_speaker.sync_coordinator = self
                    joined_speaker.group_members = group_members
                    joined_speaker.group_members_ids = group_members_ids
                    joined_speaker.update_player()

            self.logger.debug("Regrouped %s: %s", self.zone_name, self.group_members_ids)
            self.update_player()

        async def _handle_group_event(event: SonosEvent | None) -> None:
            """Get async lock and handle event."""
            async with self.sonos_prov.topology_condition:
                group = await _extract_group(event)
                if self.soco.uid == group[0]:
                    _regroup(group)
                    self.sonos_prov.topology_condition.notify_all()

        return _handle_group_event(event)

    async def wait_for_groups(self, groups: list[list[SonosPlayer]]) -> None:
        """Wait until all groups are present, or timeout."""

        def _test_groups(groups: list[list[SonosPlayer]]) -> bool:
            """Return whether all groups exist now."""
            for group in groups:
                coordinator = group[0]

                # Test that coordinator is coordinating
                current_group = coordinator.group_members
                if coordinator != current_group[0]:
                    return False

                # Test that joined members match
                if set(group[1:]) != set(current_group[1:]):
                    return False

            return True

        try:
            async with asyncio.timeout(5):
                while not _test_groups(groups):
                    await self.sonos_prov.topology_condition.wait()
        except TimeoutError:
            self.logger.warning("Timeout waiting for target groups %s", groups)

        any_speaker = next(iter(self.sonos_prov.sonosplayers.values()))
        any_speaker.soco.zone_group_state.clear_cache()

    def _update_attributes(self) -> None:
        """Update attributes of the MA Player from SoCo state."""
        # generic attributes (player_info)
        self.mass_player.available = self.available

        if not self.available:
            self.mass_player.powered = False
            self.mass_player.state = PlayerState.IDLE
            self.mass_player.synced_to = None
            self.mass_player.group_childs = set()
            return

        # transport info (playback state)
        self.mass_player.state = current_state = _convert_state(self.playback_status)

        # power 'on' player if we detect its playing
        if not self.mass_player.powered and (
            current_state == PlayerState.PLAYING
            or (
                self.sync_coordinator
                and self.sync_coordinator.mass_player.state == PlayerState.PLAYING
            )
        ):
            self.mass_player.powered = True

        # media info (track info)
        self.mass_player.current_item_id = self.uri
        if self.uri and self.mass.streams.base_url in self.uri and self.player_id in self.uri:
            self.mass_player.active_source = self.player_id
        else:
            self.mass_player.active_source = self.source_name
        if self.position is not None and self.position_updated_at is not None:
            self.mass_player.elapsed_time = self.position
            self.mass_player.elapsed_time_last_updated = self.position_updated_at.timestamp()

        # zone topology (syncing/grouping) details
        self.mass_player.can_sync_with = tuple(
            x.player_id
            for x in self.sonos_prov.sonosplayers.values()
            if x.player_id != self.player_id
        )
        if self.sync_coordinator:
            # player is synced to another player
            self.mass_player.synced_to = self.sync_coordinator.player_id
            self.mass_player.group_childs = set()
            self.mass_player.active_source = self.sync_coordinator.mass_player.active_source
        elif len(self.group_members_ids) > 1:
            # this player is the sync leader in a group
            self.mass_player.synced_to = None
            self.mass_player.group_childs = set(self.group_members_ids)
        else:
            # standalone player, not synced
            self.mass_player.synced_to = None
            self.mass_player.group_childs = set()

    def _set_basic_track_info(self, update_position: bool = False) -> None:
        """Query the speaker to update media metadata and position info."""
        self.channel = None
        self.duration = None
        self.image_url = None
        self.source_name = None
        self.title = None
        self.uri = None

        try:
            track_info = self._poll_track_info()
        except SonosUpdateError as err:
            self.logger.warning("Fetching track info failed: %s", err)
            return
        if not track_info["uri"]:
            return
        self.uri = track_info["uri"]

        audio_source = self.soco.music_source_from_uri(self.uri)
        if source := SOURCE_MAPPING.get(audio_source):
            self.source_name = source
            if audio_source in LINEIN_SOURCES:
                self.position = None
                self.position_updated_at = None
                self.title = source
                return

        self.artist = track_info.get("artist")
        self.album_name = track_info.get("album")
        self.title = track_info.get("title")
        self.image_url = track_info.get("album_art")

        playlist_position = int(track_info.get("playlist_position", -1))
        if playlist_position > 0:
            self.queue_position = playlist_position

        self._update_media_position(track_info, force_update=update_position)

    def _update_media_position(
        self, position_info: dict[str, int], force_update: bool = False
    ) -> None:
        """Update state when playing music tracks."""
        duration = position_info.get(DURATION_SECONDS)
        current_position = position_info.get(POSITION_SECONDS)

        if not (duration or current_position):
            self.position = None
            self.position_updated_at = None
            return

        should_update = force_update
        self.duration = duration

        # player started reporting position?
        if current_position is not None and self.position is None:
            should_update = True

        # position jumped?
        if current_position is not None and self.position is not None:
            if self.playback_status == SONOS_STATE_PLAYING:
                assert self.position_updated_at is not None
                time_delta = utc() - self.position_updated_at
                time_diff = time_delta.total_seconds()
            else:
                time_diff = 0

            calculated_position = self.position + time_diff

            if abs(calculated_position - current_position) > 1.5:
                should_update = True

        if current_position is None:
            self.position = None
            self.position_updated_at = None
        elif should_update:
            self.position = current_position
            self.position_updated_at = utc()

    def _speaker_activity(self, source: str) -> None:
        """Track the last activity on this speaker, set availability and resubscribe."""
        if self._resub_cooldown_expires_at:
            if time.monotonic() < self._resub_cooldown_expires_at:
                self.logger.debug(
                    "Activity on %s from %s while in cooldown, ignoring",
                    self.zone_name,
                    source,
                )
                return
            self._resub_cooldown_expires_at = None

        self.logger.log(VERBOSE_LOG_LEVEL, "Activity on %s from %s", self.zone_name, source)
        self._last_activity = time.monotonic()
        was_available = self.available
        self.available = True
        if not was_available:
            self.update_player()
            self.mass.loop.call_soon_threadsafe(self.mass.create_task, self.subscribe())

    @soco_error()
    def _join(self, members: list[SonosPlayer]) -> list[SonosPlayer]:
        if self.sync_coordinator:
            self.unjoin()
            group = [self]
        else:
            group = self.group_members.copy()

        for player in members:
            if player.soco.uid != self.soco.uid and player not in group:
                player.soco.join(self.soco)
                player.sync_coordinator = self
                group.append(player)

        return group

    @soco_error()
    def _unjoin(self) -> None:
        if self.group_members == [self]:
            return
        self.soco.unjoin()
        self.sync_coordinator = None

    @soco_error()
    def _poll_track_info(self) -> dict[str, Any]:
        """Poll the speaker for current track info.

        Add converted position values (NOT async fiendly).
        """
        track_info: dict[str, Any] = self.soco.get_current_track_info()
        track_info[DURATION_SECONDS] = _timespan_secs(track_info.get("duration"))
        track_info[POSITION_SECONDS] = _timespan_secs(track_info.get("position"))
        return track_info


def _convert_state(sonos_state: str) -> PlayerState:
    """Convert Sonos state to PlayerState."""
    if sonos_state == "PLAYING":
        return PlayerState.PLAYING
    if sonos_state == "TRANSITIONING":
        return PlayerState.PLAYING
    if sonos_state == "PAUSED_PLAYBACK":
        return PlayerState.PAUSED
    return PlayerState.IDLE


def _timespan_secs(timespan):
    """Parse a time-span into number of seconds."""
    if timespan in ("", "NOT_IMPLEMENTED", None):
        return None
    return sum(60 ** x[0] * int(x[1]) for x in enumerate(reversed(timespan.split(":"))))
