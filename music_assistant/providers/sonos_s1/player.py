"""
Sonos Player provider for Music Assistant: SonosPlayer object/model.

Note that large parts of this code are copied over from the Home Assistant
integration for Sonos.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any, cast

from music_assistant_models.enums import PlaybackState, PlayerState, PlayerType
from music_assistant_models.errors import PlayerCommandFailed
from soco import SoCoException
from soco.core import MUSIC_SRC_RADIO, SoCo
from soco.data_structures import DidlAudioBroadcast

from music_assistant.constants import (
    CONF_ENTRY_FLOW_MODE_HIDDEN_DISABLED,
    CONF_ENTRY_HTTP_PROFILE_DEFAULT_1,
    CONF_ENTRY_OUTPUT_CODEC,
    VERBOSE_LOG_LEVEL,
    create_sample_rates_config_entry,
)
from music_assistant.helpers.upnp import create_didl_metadata
from music_assistant.models.player import DeviceInfo, Player, PlayerMedia

from .constants import (
    DURATION_SECONDS,
    LINEIN_SOURCES,
    NEVER_TIME,
    PLAYER_FEATURES,
    POSITION_SECONDS,
    RESUB_COOLDOWN_SECONDS,
    SONOS_STATE_TRANSITIONING,
    SOURCE_MAPPING,
    SUBSCRIPTION_SERVICES,
    SUBSCRIPTION_TIMEOUT,
)
from .helpers import SonosUpdateError, soco_error

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry, ConfigValueType
    from soco.events_base import Event as SonosEvent
    from soco.events_base import SubscriptionBase

    from .provider import SonosPlayerProvider

CALLBACK_TYPE = Callable[[], None]
LOGGER = logging.getLogger(__name__)


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
        self._attr_needs_poll = True
        self._attr_poll_interval = 5
        self._attr_available = True
        self._attr_can_group_with = {provider.instance_id}

        # Subscriptions and events
        self._subscriptions: list[SubscriptionBase] = []
        self._subscription_lock: asyncio.Lock | None = None
        self._last_activity: float = NEVER_TIME
        self._resub_cooldown_expires_at: float | None = None

    @property
    def missing_subscriptions(self) -> set[str]:
        """Return a list of missing service subscriptions."""
        subscribed_services = {sub.service.service_type for sub in self._subscriptions}
        return SUBSCRIPTION_SERVICES - subscribed_services

    async def setup(self) -> None:
        """Set up the player."""
        self._attr_volume_level = self.soco.volume
        self._attr_volume_muted = self.soco.mute
        self.update_groups()
        if not self.synced_to:
            self.poll_media()
        await self.subscribe()
        await self.mass.players.register_or_update(self)

    async def offline(self) -> None:
        """Handle removal of speaker when unavailable."""
        if not self._attr_available:
            return

        if self._resub_cooldown_expires_at is None and not self.mass.closing:
            self._resub_cooldown_expires_at = time.monotonic() + RESUB_COOLDOWN_SECONDS
            self.logger.debug("Starting resubscription cooldown for %s", self.display_name)

        self._attr_available = False
        self._share_link_plugin = None

        self.update_state()
        await self.unsubscribe()

    async def get_config_entries(
        self,
        action: str | None = None,
        values: dict[str, ConfigValueType] | None = None,
    ) -> list[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the player."""
        return [
            *await super().get_config_entries(action=action, values=values),
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
        if self.synced_to:
            self.logger.debug(
                "Ignore STOP command for %s: Player is synced to another player.",
                self.player_id,
            )
            return
        await asyncio.to_thread(self.soco.stop)
        self.mass.call_later(2, self.poll)
        self.update_state()

    async def play(self) -> None:
        """Send PLAY command to the player."""
        if self.synced_to:
            self.logger.debug(
                "Ignore PLAY command for %s: Player is synced to another player.",
                self.player_id,
            )
            return
        await asyncio.to_thread(self.soco.play)
        self.mass.call_later(2, self.poll)

    async def pause(self) -> None:
        """Send PAUSE command to the player."""
        if self.synced_to:
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
        self.mass.call_later(2, self.poll)

    async def volume_set(self, volume_level: int) -> None:
        """Send VOLUME_SET command to the player."""

        def set_volume_level(volume_level: int) -> None:
            self.soco.volume = volume_level

        await asyncio.to_thread(set_volume_level, volume_level)
        self.mass.call_later(2, self.poll)

    async def volume_mute(self, muted: bool) -> None:
        """Send VOLUME MUTE command to the player."""

        def set_volume_mute(muted: bool) -> None:
            self.soco.mute = muted

        await asyncio.to_thread(set_volume_mute, muted)
        self.mass.call_later(2, self.poll)

    async def play_media(self, media: PlayerMedia) -> None:
        """Handle PLAY MEDIA on the player."""
        if self.synced_to:
            # this should be already handled by the player manager, but just in case...
            msg = (
                f"Player {self.display_name} can not "
                "accept play_media command, it is synced to another player."
            )
            raise PlayerCommandFailed(msg)

        if not media.duration:
            # Sonos really does not like FLAC streams without duration
            media.uri = media.uri.replace(".flac", ".mp3")

        didl_metadata = create_didl_metadata(media)

        await asyncio.to_thread(
            self.soco.play_uri, media.uri, meta=didl_metadata, force_radio=not media.duration
        )
        self.mass.call_later(2, self.poll)

    async def enqueue_next_media(self, media: PlayerMedia) -> None:
        """Handle enqueuing next media item."""
        if self.synced_to:
            # this should be already handled by the player manager, but just in case...
            msg = (
                f"Player {self.display_name} can not "
                "accept enqueue command, it is synced to another player."
            )
            raise PlayerCommandFailed(msg)

        didl_metadata = create_didl_metadata(media)

        def add_to_queue() -> None:
            self.soco.avTransport.SetNextAVTransportURI(
                [
                    ("InstanceID", 0),
                    ("NextURI", media.uri),
                    ("NextURIMetaData", didl_metadata),
                ]
            )

        await asyncio.to_thread(add_to_queue)
        self.mass.call_later(2, self.poll)

    @soco_error()
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
            return
        player_ids_to_add = player_ids_to_add or []
        player_ids_to_remove = player_ids_to_remove or []

        if player_ids_to_remove:
            for player_id in player_ids_to_remove:
                if player_to_remove := cast("SonosPlayer", self.mass.players.get(player_id)):
                    await asyncio.to_thread(player_to_remove.soco.unjoin)
                    self.mass.call_later(2, player_to_remove.poll)

        if player_ids_to_add:
            for player_id in player_ids_to_add:
                if player_to_add := cast("SonosPlayer", self.mass.players.get(player_id)):
                    await asyncio.to_thread(player_to_add.soco.join, self.soco)
                    self.mass.call_later(2, player_to_add.poll)

    async def poll(self) -> None:
        """Poll player for state updates."""

        def _poll() -> None:
            """Poll the speaker for updates (NOT async friendly)."""
            self.update_groups()
            self.poll_media()
            self._attr_volume_level = self.soco.volume
            self._attr_volume_muted = self.soco.mute

        await self._check_availability()
        if self._attr_available:
            await asyncio.to_thread(_poll)

    @soco_error()
    def poll_media(self) -> None:
        """Poll information about currently playing media."""
        transport_info = self.soco.get_current_transport_info()
        new_status = transport_info["current_transport_state"]

        if new_status == SONOS_STATE_TRANSITIONING:
            return

        new_status = _convert_state(new_status)
        update_position = new_status != self._attr_playback_state
        self._attr_playback_state = new_status
        self._set_basic_track_info(update_position=update_position)
        self.update_player()

    def update_ip(self, ip_address: str) -> None:
        """Handle updated IP of a Sonos player (NOT async friendly)."""
        if self._attr_available:
            return
        self.logger.debug(
            "Player IP-address changed from %s to %s", self.soco.ip_address, ip_address
        )
        try:
            self.ping()
        except SonosUpdateError:
            return
        self.soco.ip_address = ip_address
        asyncio.run_coroutine_threadsafe(self.setup(), self.mass.loop)
        self._attr_device_info = DeviceInfo(
            model=self._attr_device_info.model,
            manufacturer=self._attr_device_info.manufacturer,
            ip_address=ip_address,
        )
        self.update_player()

    async def _check_availability(self) -> None:
        """Check if the player is still available."""
        try:
            await asyncio.to_thread(self.ping)
            self._speaker_activity("ping")
        except SonosUpdateError:
            if not self._attr_available:
                return
            self.logger.warning(
                "No recent activity and cannot reach %s, marking unavailable",
                self.display_name,
            )
            await self.offline()

    @soco_error()
    def ping(self) -> None:
        """Test device availability. Failure will raise SonosUpdateError."""
        self.soco.renderingControl.GetVolume([("InstanceID", 0), ("Channel", "Master")], timeout=1)

    @soco_error()
    def _poll_track_info(self) -> dict[str, Any]:
        """Poll the speaker for current track info.

        Add converted position values (NOT async fiendly).
        """
        track_info: dict[str, Any] = self.soco.get_current_track_info()
        track_info[DURATION_SECONDS] = _timespan_secs(track_info.get("duration"))
        track_info[POSITION_SECONDS] = _timespan_secs(track_info.get("position"))
        return track_info

    def update_player(self, signal_update: bool = True) -> None:
        """Update Sonos Player."""
        self._update_attributes()
        if signal_update:
            # send update to the player manager right away only if we are triggered from an event
            # when we're just updating from a manual poll, the player manager
            # will detect changes to the player object itself
            self.mass.loop.call_soon_threadsafe(self.update_state)

    async def _subscribe_target(
        self, target: SubscriptionBase, sub_callback: Callable[[SonosEvent], None]
    ) -> None:
        """Create a Sonos subscription for given target."""

        def on_renew_failed(exception: Exception) -> None:
            """Handle a failed subscription renewal callback."""
            self.mass.create_task(self._renew_failed(exception))

        # Use events_asyncio which makes subscribe() async-awaitable
        subscription = await target.subscribe(
            auto_renew=True, requested_timeout=SUBSCRIPTION_TIMEOUT
        )
        subscription.callback = sub_callback
        subscription.auto_renew_fail = on_renew_failed
        self._subscriptions.append(subscription)

    async def _renew_failed(self, exception: Exception) -> None:
        """Mark the speaker as offline after a subscription renewal failure.

        This is to reset the state to allow a future clean subscription attempt.
        """
        if not self._attr_available:
            return

        self.log_subscription_result(exception, "Subscription renewal", logging.WARNING)
        await self.offline()

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
            self.display_name,
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
                self.logger.log(
                    VERBOSE_LOG_LEVEL, "Creating subscriptions for %s", self.display_name
                )
                results = await asyncio.gather(*subscriptions, return_exceptions=True)
                for result in results:
                    self.log_subscription_result(result, "Creating subscription", logging.WARNING)
                if any(isinstance(result, Exception) for result in results):
                    raise SonosSubscriptionsFailed
            except SonosSubscriptionsFailed:
                self.logger.warning("Creating subscriptions failed for %s", self.display_name)
                assert self._subscription_lock is not None
                async with self._subscription_lock:
                    await self.offline()

    async def unsubscribe(self) -> None:
        """Cancel all subscriptions."""
        if not self._subscriptions:
            return
        self.logger.log(VERBOSE_LOG_LEVEL, "Unsubscribing from events for %s", self.display_name)
        results = await asyncio.gather(
            *(subscription.unsubscribe() for subscription in self._subscriptions),
            return_exceptions=True,
        )
        for result in results:
            self.log_subscription_result(result, "Unsubscribe")
        self._subscriptions = []

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

        # Missing transport_state indicates a transient error
        if (new_status := event.variables.get("transport_state")) is None:
            return

        # Ignore transitions, we should get the target state soon
        if new_status == SONOS_STATE_TRANSITIONING:
            return

        evars = event.variables
        new_status = _convert_state(evars["transport_state"])
        state_changed = new_status != self._attr_playback_state

        self._attr_playback_state = new_status

        track_uri = evars["enqueued_transport_uri"] or evars["current_track_uri"]
        audio_source = self.soco.music_source_from_uri(track_uri)

        self._set_basic_track_info(update_position=state_changed)
        ct_md = evars["current_track_meta_data"]

        et_uri_md = evars["enqueued_transport_uri_meta_data"]

        channel = ""
        if audio_source == MUSIC_SRC_RADIO:
            if et_uri_md:
                channel = et_uri_md.title

            # Extra guards for S1 compatibility
            if ct_md and hasattr(ct_md, "radio_show") and ct_md.radio_show:
                radio_show = ct_md.radio_show.split(",")[0]
                channel = " • ".join(filter(None, [channel, radio_show]))

            if isinstance(et_uri_md, DidlAudioBroadcast) and self._attr_current_media:
                self._attr_current_media.title = self._attr_current_media.title or channel

        self.update_player()

    def _handle_rendering_control_event(self, event: SonosEvent) -> None:
        """Update information about currently volume settings."""
        variables = event.variables

        if "volume" in variables:
            volume = variables["volume"]
            self._attr_volume_level = int(volume["Master"])

        if mute := variables.get("mute"):
            self._attr_volume_muted = mute["Master"] == "1"

        self.update_player()

    def _handle_zone_group_topology_event(self, event: SonosEvent) -> None:
        """Handle callback for topology change event."""
        if "zone_player_uui_ds_in_group" not in event.variables:
            return
        asyncio.run_coroutine_threadsafe(self.create_update_groups_coro(event), self.mass.loop)

    def _update_attributes(self) -> None:
        """Update attributes of the MA Player from SoCo state."""
        if not self._attr_available:
            self._attr_playback_state = PlayerState.IDLE
            self._attr_group_members.clear()
            return

    def _set_basic_track_info(self, update_position: bool = False) -> None:
        """Query the speaker to update media metadata and position info."""
        try:
            track_info = self._poll_track_info()
        except SonosUpdateError as err:
            self.logger.warning("Fetching track info failed: %s", err)
            return
        if not track_info["uri"]:
            return
        uri = track_info["uri"]

        audio_source = self.soco.music_source_from_uri(uri)
        if SOURCE_MAPPING.get(audio_source) and audio_source in LINEIN_SOURCES:
            self._attr_elapsed_time = None
            self._attr_elapsed_time_last_updated = None
            return

        current_media = PlayerMedia(
            uri=uri,
            artist=track_info.get("artist"),
            album=track_info.get("album"),
            title=track_info.get("title"),
            image_url=track_info.get("album_art"),
        )
        self._attr_current_media = current_media
        self._update_media_position(track_info, force_update=update_position)

    def _update_media_position(
        self, position_info: dict[str, int], force_update: bool = False
    ) -> None:
        """Update state when playing music tracks."""
        duration = position_info.get(DURATION_SECONDS)
        current_position = position_info.get(POSITION_SECONDS)

        if not (duration or current_position):
            self._attr_elapsed_time = None
            self._attr_elapsed_time_last_updated = None
            return

        should_update = force_update
        if self._attr_current_media:
            self._attr_current_media.duration = duration

        # player started reporting position?
        if current_position is not None and self._attr_elapsed_time is None:
            should_update = True

        # position jumped?
        if current_position is not None and self._attr_elapsed_time is not None:
            if self._attr_playback_state == PlaybackState.PLAYING:
                assert self._attr_elapsed_time_last_updated is not None
                time_diff = time.time() - self._attr_elapsed_time_last_updated
            else:
                time_diff = 0

            calculated_position = self._attr_elapsed_time + time_diff

            if abs(calculated_position - current_position) > 1.5:
                should_update = True

        if current_position is None:
            self._attr_elapsed_time = None
            self._attr_elapsed_time_last_updated = None
        elif should_update:
            self._attr_elapsed_time = current_position
            self._attr_elapsed_time_last_updated = time.time()

    def _speaker_activity(self, source: str) -> None:
        """Track the last activity on this speaker, set availability and resubscribe."""
        if self._resub_cooldown_expires_at:
            if time.monotonic() < self._resub_cooldown_expires_at:
                self.logger.debug(
                    "Activity on %s from %s while in cooldown, ignoring",
                    self.display_name,
                    source,
                )
                return
            self._resub_cooldown_expires_at = None

        self.logger.log(VERBOSE_LOG_LEVEL, "Activity on %s from %s", self.display_name, source)
        self._last_activity = time.monotonic()
        was_available = self._attr_available
        self._attr_available = True
        if not was_available:
            self.update_player()
            self.mass.loop.call_soon_threadsafe(self.mass.create_task, self.subscribe())

    def update_groups(self) -> None:
        """Update group topology when polling."""
        asyncio.run_coroutine_threadsafe(self.create_update_groups_coro(), self.mass.loop)

    def create_update_groups_coro(
        self, event: SonosEvent | None = None
    ) -> Coroutine[Any, Any, None]:
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
            return await asyncio.to_thread(_get_soco_group)

        def _regroup(group: list[str]) -> None:
            """Rebuild internal group layout (async safe)."""
            if group == [self.soco.uid] and not self._attr_group_members:
                # Skip updating existing single speakers in polling mode
                return

            group_members_ids = []

            for uid in group:
                speaker = self.mass.players.get(uid)
                if speaker:
                    group_members_ids.append(uid)
                else:
                    self.logger.debug(
                        "%s group member unavailable (%s), will try again",
                        self.display_name,
                        uid,
                    )
                    return

            if self._attr_group_members == group_members_ids:
                # Useful in polling mode for speakers with stereo pairs or surrounds
                # as those "invisible" speakers will bypass the single speaker check
                return

            self._attr_group_members = group_members_ids
            self.mass.loop.call_soon_threadsafe(self.update_state)

            self.logger.debug("Regrouped %s: %s", self.display_name, self._attr_group_members)
            self.update_player()

        async def _handle_group_event(event: SonosEvent | None) -> None:
            """Get async lock and handle event."""
            _provider = cast("SonosPlayerProvider", self._provider)
            async with _provider.topology_condition:
                group = await _extract_group(event)
                if self.soco.uid == group[0]:
                    _regroup(group)
                    _provider.topology_condition.notify_all()

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

        _provider = cast("SonosPlayerProvider", self._provider)
        try:
            async with asyncio.timeout(5):
                while not _test_groups(groups):
                    await _provider.topology_condition.wait()
        except TimeoutError:
            self.logger.warning("Timeout waiting for target groups %s", groups)

        if players := self.mass.players.all(provider_filter=_provider.instance_id):
            any_speaker = cast("SonosPlayer", players[0])
            any_speaker.soco.zone_group_state.clear_cache()


def _convert_state(sonos_state: str | None) -> PlayerState:
    """Convert Sonos state to PlayerState."""
    if sonos_state == "PLAYING":
        return PlayerState.PLAYING
    if sonos_state == "TRANSITIONING":
        return PlayerState.PLAYING
    if sonos_state == "PAUSED_PLAYBACK":
        return PlayerState.PAUSED
    return PlayerState.IDLE


def _timespan_secs(timespan: str | None) -> int | None:
    """Parse a time-span into number of seconds."""
    if timespan in ("", "NOT_IMPLEMENTED"):
        return None
    if timespan is None:
        return None
    return int(sum(60 ** x[0] * int(x[1]) for x in enumerate(reversed(timespan.split(":")))))
