"""
Sonos Player provider for Music Assistant for speakers running the S2 firmware.

Based on the aiosonos library, which leverages the new websockets API of the Sonos S2 firmware.
https://github.com/music-assistant/aiosonos

SonosPlayer: Holds the details of the (discovered) Sonosplayer.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import deque
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlparse

from aiohttp import ClientError
from aiosonos.api.models import (
    AudioClipStatus,
    Container,
    ContainerType,
    GroupStatus,
    MusicService,
    SonosCapability,
)
from aiosonos.client import SonosLocalApiClient
from aiosonos.const import EventType as SonosEventType
from aiosonos.const import SonosEvent
from aiosonos.exceptions import CannotConnect, CommandTimeout, ConnectionFailed, FailedCommand
from music_assistant_models.constants import PLAYER_CONTROL_NATIVE
from music_assistant_models.enums import (
    EventType,
    IdentifierType,
    MediaType,
    PlaybackState,
    PlayerFeature,
    RepeatMode,
)
from music_assistant_models.errors import PlayerCommandFailed, PlayerUnavailableError
from music_assistant_models.player import OutputProtocol, PlayerMedia

from music_assistant.constants import (
    CONF_ENTRY_HTTP_PROFILE_DEFAULT_2,
    CONF_ENTRY_PREFER_WAV_FOR_LIVE_SOURCES_DEFAULT_ENABLED,
    EXTERNAL_PAUSE_IDLE_TIMEOUT,
    VERBOSE_LOG_LEVEL,
    create_sample_rates_config_entry,
)
from music_assistant.helpers.util import is_valid_mac_address
from music_assistant.helpers.wake_on_lan import send_magic_packet
from music_assistant.models.player import AnnouncementFeature, Player, PlayerSource
from music_assistant.providers.sonos.const import (
    DEVICE_FEATURE_WAKEABLE,
    NON_HIRES_MODELS,
    PLAYBACK_STATE_MAP,
    PLAYER_SOURCE_MAP,
    PREVIOUS_ITEMS,
    SOURCE_AIRPLAY,
    SOURCE_LINE_IN,
    SOURCE_RADIO,
    SOURCE_SPOTIFY,
    SOURCE_TV,
    UNSUPPORTED_MODELS_NATIVE_ANNOUNCEMENTS,
    UPCOMING_ITEMS,
)

if TYPE_CHECKING:
    from aiosonos.api.models import (
        AudioClipStatusEvent,
        GroupCoordinatorChanged,
        PlaybackError,
        SessionError,
    )
    from aiosonos.api.models import DiscoveryInfo as SonosDiscoveryInfo
    from aiosonos.group import SonosGroup
    from music_assistant_models.config_entries import ConfigEntry
    from music_assistant_models.queue_item import QueueItem

    from .provider import SonosPlayerProvider

SUPPORTED_FEATURES = {
    PlayerFeature.PLAY_MEDIA,
    PlayerFeature.PAUSE,
    PlayerFeature.SEEK,
    PlayerFeature.SELECT_SOURCE,
    PlayerFeature.SET_MEMBERS,
    PlayerFeature.GAPLESS_PLAYBACK,
}


@dataclass
class SonosQueueWindow:
    """A window of queue items as served to a Sonos speaker."""

    items: list[PlayerMedia] = field(default_factory=list)
    includes_beginning: bool = False
    includes_end: bool = False


# Failures remembered per speaker. One report can carry several, and the speaker resends
# the whole batch until it gives up on the item.
REPORTED_ERROR_HISTORY = 16

# seconds for a woken speaker's radio to come up, and for a queue load to start it playing
WAKE_TIMEOUT = 15
WAKE_PLAY_KICK_TIMEOUT = 2
# seconds the new coordinator of a group moved outside MA gets to report the role, and
# our queue to stop playing here
MOVED_GROUP_TIMEOUT = 5
# seconds a queue released on a session eviction still counts for a coordinator move:
# the speaker may send the eviction just before the move
EVICTED_QUEUE_GRACE = 5
# seconds past an announcement's own duration to wait for the speaker to report it done
ANNOUNCEMENT_STATUS_MARGIN = 5
# clip states after which the speaker no longer plays the clip
FINISHED_CLIP_STATES = (
    AudioClipStatus.DONE,
    AudioClipStatus.DISMISSED,
    AudioClipStatus.INTERRUPTED,
    AudioClipStatus.ERROR,
)
SESSION_EVICTED_ERROR = "ERROR_SESSION_EVICTED"


class SonosPlayer(Player):
    """Holds the details of the (discovered) Sonosplayer."""

    _attr_external_pause_idle_timeout = EXTERNAL_PAUSE_IDLE_TIMEOUT
    # the speaker names the service it plays itself, so its source list is trusted
    _attr_trusts_reported_source = True
    # the speaker plays out of a cached copy of our cloud queue that it refreshes on its
    # own schedule; when it fetches a track the queue has since moved away from the
    # playhead, the server must refuse it so the speaker re-reads the queue
    _attr_strict_queue_item_requests = True

    def __init__(
        self,
        prov: SonosPlayerProvider,
        player_id: str,
        discovery_info: SonosDiscoveryInfo,
    ) -> None:
        """Initialize the SonosPlayer."""
        super().__init__(prov, player_id)
        self.discovery_info = discovery_info
        self.connected: bool = False
        self._listen_task: asyncio.Task[None] | None = None
        self._connect_lock = asyncio.Lock()
        mac_address = self._extract_mac_from_player_id()
        self._wol_mac = mac_address if mac_address and is_valid_mac_address(mac_address) else None
        self._wakeable = self._wol_mac is not None and _is_wakeable(discovery_info)
        # a dropped connection confirmed as sleep, as opposed to a passing blip
        self._marked_asleep = False
        # the connection followed a sleep; the speaker then restores its session paused
        self._woken_from_sleep = False
        # the MA queue the loaded cloud queue serves, and the version the speaker
        # compares against to decide whether its cached copy is still valid
        self.cloud_queue_id: str | None = None
        self.cloud_queue_version: float = time.time()
        # advanced on every play_media: item ids are served suffixed with it, so a
        # reload of the item the speaker is already playing gets a fresh identity.
        # Clock-seeded (nanoseconds), so a recreated player never reuses a
        # generation the speaker may still hold items under
        self.cloud_queue_item_generation = time.time_ns()
        # failures already logged, so the speaker's resends are not logged again
        self.reported_playback_errors: deque[str] = deque(maxlen=REPORTED_ERROR_HISTORY)
        self._announcement_media: PlayerMedia | None = None
        # the queue a session eviction released, and when (time.monotonic)
        self._evicted_cloud_queue: tuple[str, float] | None = None
        # the MA queue the speaker plays as one stream (flow mode): no cloud queue is loaded
        self._flow_queue_id: str | None = None
        # the playback session our queue was loaded into
        self._queue_session_id: str | None = None
        # hand-overs of a group the speakers moved outside MA, one per move
        self._hand_over_tasks: set[asyncio.Task[None]] = set()

    @property
    def source_list(self) -> list[PlayerSource]:
        """Return this player's sources; a service we did not map is listed only while it plays."""
        # the entry of such a service can outlive the source it stands for: once a paused
        # session is given up on, the speaker keeps reporting it and rebuilds the entry
        return [
            x
            for x in self._attr_source_list
            if x.id in PLAYER_SOURCE_MAP or x.id == self._attr_active_source
        ]

    @property
    def announcement_features(self) -> set[AnnouncementFeature]:
        """Return SUPPORTS_VOLUME and COORDINATES_START: clips honour the level and start in step."""
        return {AnnouncementFeature.SUPPORTS_VOLUME, AnnouncementFeature.COORDINATES_START}

    @property
    def group_controller(self) -> SonosGroup:
        """Get the group controller, raising if unavailable."""
        if self.client.player.group is None:
            msg = "Group controller unavailable"
            raise RuntimeError(msg)
        return self.client.player.group

    @property
    def synced_to(self) -> str | None:
        """
        Return the id of the player this player is synced to (sync leader).

        If this player is not synced to another player (or is the sync leader itself),
        this should return None.
        If it is part of a (permanent) group, this should also return None.
        """
        if self.client.player.is_coordinator:
            return None
        if self.client.player.group:
            return self.client.player.group.coordinator_id
        return None

    async def setup(self) -> None:
        """Handle setup of the player."""
        assert self.device_info.ip_address is not None  # for type checking
        # connect the player first so we can fail early
        self.client = SonosLocalApiClient(
            self.device_info.ip_address, self.mass.http_session_no_ssl
        )
        await self._connect(False)

        # collect supported features
        _supported_features = SUPPORTED_FEATURES.copy()
        if (
            SonosCapability.AUDIO_CLIP in self.discovery_info["device"]["capabilities"]
            and self.discovery_info["device"]["modelDisplayName"]
            not in UNSUPPORTED_MODELS_NATIVE_ANNOUNCEMENTS
        ):
            _supported_features.add(PlayerFeature.PLAY_ANNOUNCEMENT)
        if not self.client.player.has_fixed_volume:
            _supported_features.add(PlayerFeature.VOLUME_SET)
            _supported_features.add(PlayerFeature.VOLUME_MUTE)
        _supported_features.add(PlayerFeature.NEXT_PREVIOUS)
        _supported_features.add(PlayerFeature.ENQUEUE)
        if self._wakeable:
            _supported_features.add(PlayerFeature.POWER)
            self._attr_powered = True
        self._attr_supported_features = _supported_features

        self._attr_name = (
            self.discovery_info["device"]["name"]
            or self.discovery_info["device"]["modelDisplayName"]
        )
        self._attr_device_info.model = self.discovery_info["device"]["modelDisplayName"]
        self._attr_device_info.manufacturer = self._provider.manifest.name
        self._attr_can_group_with = {self._provider.instance_id}

        # Add identifiers for matching with other protocols (like AirPlay, DLNA)
        # The player_id is the Sonos UUID (e.g., RINCON_xxxxxxxxxxxx)
        self._attr_device_info.add_identifier(IdentifierType.UUID, self.player_id)
        # Extract MAC address from Sonos player_id (RINCON_XXXXXXXXXXXX01400)
        # The middle part contains the MAC address (last 6 bytes in hex)
        mac_address = self._extract_mac_from_player_id()
        # Only add MAC address if it's valid (not 00:00:00:00:00:00)
        if mac_address and is_valid_mac_address(mac_address):
            self._attr_device_info.add_identifier(IdentifierType.MAC_ADDRESS, mac_address)

        if SonosCapability.LINE_IN in self.discovery_info["device"]["capabilities"]:
            self._attr_source_list.append(PLAYER_SOURCE_MAP[SOURCE_LINE_IN])
        if SonosCapability.HT_PLAYBACK in self.discovery_info["device"]["capabilities"]:
            self._attr_source_list.append(PLAYER_SOURCE_MAP[SOURCE_TV])
        if SonosCapability.AIRPLAY in self.discovery_info["device"]["capabilities"]:
            self._attr_source_list.append(PLAYER_SOURCE_MAP[SOURCE_AIRPLAY])

        self.update_attributes()
        await self.mass.players.register_or_update(self)

        # register callback for state changed
        self._on_unload_callbacks.append(
            self.client.subscribe(
                self.on_player_event,
                (
                    SonosEventType.GROUP_UPDATED,
                    SonosEventType.PLAYER_UPDATED,
                ),
            )
        )
        self._on_unload_callbacks.append(
            self.client.subscribe(self._on_playback_error, SonosEventType.PLAYBACK_ERROR)
        )
        self._on_unload_callbacks.append(
            self.client.subscribe(
                self._on_group_coordinator_changed, SonosEventType.GROUP_COORDINATOR_CHANGED
            )
        )
        self._on_unload_callbacks.append(
            self.client.subscribe(self._on_session_error, SonosEventType.SESSION_ERROR)
        )

    async def get_config_entries(self) -> list[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the player."""
        # Sonos takes 44.1/48 kHz; NON_HIRES_MODELS are not verified for 24 bit, so 16 bit.
        # The defaults cover that full hardware range, so the output format only narrows
        # when the user selects fewer rates.
        hi_res = self._attr_device_info.model not in NON_HIRES_MODELS
        return [
            CONF_ENTRY_HTTP_PROFILE_DEFAULT_2,
            CONF_ENTRY_PREFER_WAV_FOR_LIVE_SOURCES_DEFAULT_ENABLED,
            create_sample_rates_config_entry(
                supported_sample_rates=[44100, 48000],
                supported_bit_depths=[16, 24] if hi_res else [16],
                safe_max_sample_rate=48000,
                safe_max_bit_depth=24 if hi_res else 16,
            ),
        ]

    async def on_unload(self) -> None:
        """Handle logic when the player is unloaded from the Player controller."""
        await super().on_unload()
        for task_id in (
            f"sonos_reconnect_{self.player_id}",
            f"restore_airplay_group_{self.player_id}",
        ):
            # a timer that already fired lives on as a task under the same id,
            # so both are needed to cover the pending and the running case
            self.mass.cancel_timer(task_id)
            self.mass.cancel_task(task_id)
        for task in list(self._hand_over_tasks):
            task.cancel()
        try:
            await self._disconnect()
        except Exception:
            self.logger.exception("Error disconnecting from Sonos player %s", self.name)

    async def power(self, powered: bool) -> None:
        """
        Handle POWER command on the player.

        Powering on wakes a sleeping speaker with a Wake-on-LAN packet and waits for it to
        reconnect. Powering off only updates local state: Sonos has no remote sleep command,
        the speaker sleeps on its own idle timer.

        :param powered: bool if player should be powered on or off.
        """
        if powered and not self.connected:
            await self._wake()
            self._woken_from_sleep = True
        self._attr_powered = powered
        self.update_state()

    async def on_mdns_goodbye(self) -> None:
        """Treat the speaker withdrawing its mDNS announcement as it going to sleep."""
        if not self._sleeps_as_power_off or not self.connected:
            return
        await self._disconnect()
        self._mark_asleep()
        self.update_state()

    async def volume_set(self, volume_level: int) -> None:
        """
        Handle VOLUME_SET command on the player.

        Will only be called if the PlayerFeature.VOLUME_SET is supported.

        :param volume_level: volume level (0..100) to set on the player.
        """
        await self.client.player.set_volume(volume_level)

    async def volume_mute(self, muted: bool) -> None:
        """
        Handle VOLUME MUTE command on the player.

        Will only be called if the PlayerFeature.VOLUME_MUTE is supported.

        :param muted: bool if player should be muted.
        """
        await self.client.player.set_volume(muted=muted)

    async def play(self) -> None:
        """Handle PLAY command on the player."""
        if self.client.player.is_passive:
            self.logger.debug("Ignore PLAY command: Player is synced to another player.")
            return
        try:
            await self.group_controller.play()
        except FailedCommand as err:
            if self._attr_active_source is None or "groupCoordinatorChanged" in str(err):
                # only a source Sonos loaded itself can go away like this, and a coordinator
                # change is a race condition rather than a source that disappeared
                raise
            # the loaded source refused to resume, so it is not merely paused after all
            self.logger.debug(
                "Source %s on Sonos player %s can not be resumed: %s",
                self._attr_active_source,
                self.player_id,
                err,
            )
            self.mark_external_source_ended()
            self.update_state()

    async def stop(self) -> None:
        """Handle STOP command on the player."""
        self.mark_stop_called()
        if self.client.player.is_passive:
            self.logger.debug("Ignore STOP command: Player is synced to another player.")
            return
        await self.group_controller.stop()
        self._forget_queue()
        self._announcement_media = None
        self.update_state()

    async def pause(self) -> None:
        """
        Handle PAUSE command on the player.

        Will only be called if the player reports PlayerFeature.PAUSE is supported.
        """
        if self.client.player.is_passive:
            self.logger.debug("Ignore PAUSE command: Player is synced to another player.")
            return
        active_source = self.state.active_source
        if active_source and self.mass.player_queues.get(active_source):
            # Sonos seems to be bugged when playing our queue tracks and we send pause,
            # it can't resume the current track and simply aborts/skips it
            # so we stop the player instead.
            # https://github.com/music-assistant/support/issues/3758
            # TODO: revisit this later once we implemented support for range requests
            # as I have the feeling the pause issue is related to seek support (=range requests)
            await self.stop()
            return
        if not self.group_controller.playback_actions.can_pause:
            await self.stop()
            return
        await self.group_controller.pause()

    async def next_track(self) -> None:
        """
        Handle NEXT_TRACK command on the player.

        Will only be called if the player reports PlayerFeature.NEXT_PREVIOUS
        is supported and the player is not currently playing a MA queue.
        """
        if not self.group_controller.playback_actions.can_skip:
            # the button stays while either direction works; the speaker refuses this one
            self.logger.debug("%s offers no next track, not skipping", self.display_name)
            return
        await self.group_controller.skip_to_next_track()

    async def previous_track(self) -> None:
        """
        Handle PREVIOUS_TRACK command on the player.

        Will only be called if the player reports PlayerFeature.NEXT_PREVIOUS
        is supported and the player is not currently playing a MA queue.
        """
        if not self.group_controller.playback_actions.can_skip_to_previous:
            # the button stays while either direction works; the speaker refuses this one
            self.logger.debug("%s offers no previous track, not skipping", self.display_name)
            return
        await self.group_controller.skip_to_previous_track()

    async def seek(self, position: int) -> None:
        """
        Handle SEEK command on the player.

        Seek to a specific position in the current track.
        Will only be called if the player reports PlayerFeature.SEEK is
        supported and the player is NOT currently playing a MA queue.

        :param position: The position to seek to, in seconds.
        """
        # sonos expects milliseconds
        await self.group_controller.seek(position * 1000)

    async def set_shuffle(self, shuffle_enabled: bool) -> None:
        """
        Handle SET SHUFFLE command on the player.

        Will only be called if the player's currently active source declares
        ``can_shuffle``.

        :param shuffle_enabled: Whether the source should play its content shuffled.
        """
        await self.group_controller.set_play_modes(shuffle=shuffle_enabled)

    async def set_repeat(self, repeat_mode: RepeatMode) -> None:
        """
        Handle SET REPEAT command on the player.

        Will only be called if the player's currently active source declares
        ``can_repeat``.

        :param repeat_mode: The repeat mode the source should apply.
        """
        await self.group_controller.set_play_modes(
            repeat=repeat_mode == RepeatMode.ALL,
            repeat_one=repeat_mode == RepeatMode.ONE,
        )

    async def play_media(
        self,
        media: PlayerMedia,
    ) -> None:
        """
        Handle PLAY MEDIA command on given player.

        This is called by the Player controller to start playing Media on the player,
        which can be a MA queue item/stream or a native source.
        The provider's own implementation should work out how to handle this request.

        :param media: Details of the item that needs to be played on the player.
        """
        woken = self._woken_from_sleep
        self._woken_from_sleep = False
        if self.client.player.is_passive:
            # this should be already handled by the player manager, but just in case...
            msg = (
                f"Player {self.display_name} can not "
                "accept play_media command, it is synced to another player."
            )
            raise PlayerCommandFailed(
                msg,
                translation_key="player_synced_cannot_play",
                translation_owner=self.translation_owner,
                translation_args=[self.display_name],
            )
        self._evicted_cloud_queue = None
        # what is playing stays described until its replacement is loaded below: there are
        # awaits in between, and an empty window served in that gap stops the current queue
        self._announcement_media = None
        self.cloud_queue_item_generation += 1
        self.bump_cloud_queue_version()

        if media.media_type == MediaType.ANNOUNCEMENT:
            # We cannot use play_stream_url for announcements because Sonos treats those
            # as duration less radio streams and will retry/loop them.
            media.duration = await self.mass.streams.get_announcement_duration(media)
            media.queue_item_id = "announcement"
            self._announcement_media = media
            cloud_queue_url = f"{self.mass.streams.base_url}/sonos_queue/{self.player_id}/v2.3/"
            try:
                await self.group_controller.play_cloud_queue(
                    cloud_queue_url,
                    item_id=self.wire_item_id(media.queue_item_id),
                )
            except Exception:
                # the speaker never got the queue, so describing one is worse than
                # admitting there is none
                self._announcement_media = None
                raise
            return

        if not self.flow_mode and media.source_id and media.queue_item_id:
            # Regular Queue item playback
            # create a sonos cloud queue and load it, into the session already playing if any
            previous_queue_id = self._playing_queue_id()
            self._queue_session_id = None
            self._flow_queue_id = None
            self.cloud_queue_id = media.source_id
            cloud_queue_url = f"{self.mass.streams.base_url}/sonos_queue/{self.player_id}/v2.3/"
            try:
                await self.group_controller.play_cloud_queue(
                    cloud_queue_url,
                    item_id=self.wire_item_id(media.queue_item_id),
                )
            except (Exception, asyncio.CancelledError) as err:
                self._settle_failed_load(err, media.source_id, previous_queue_id)
                raise
            if not self._bind_loaded_session(media.source_id):
                return
            # only now that the speaker accepted the load: it keeps playing the old
            # audio until the cutover, but it will not act on the load while its
            # current stream connection is still open, and a response blocked in a
            # write never notices on its own that its session is gone
            if media.source_id and media.queue_session_id:
                self.mass.streams.close_superseded_item_streams(
                    media.source_id, media.queue_session_id
                )
            if woken:
                await self._ensure_playing_after_wake()
            return

        await self._play_stream(media)

    async def select_source(self, source: str) -> None:
        """
        Handle SELECT SOURCE command on the player.

        Will only be called if the PlayerFeature.SELECT_SOURCE is supported.

        :param source: The source(id) to select, as defined in the source_list.
        """
        # whatever the source turns out to be, it is not our queue any more
        self._forget_queue()
        self._announcement_media = None
        if source == SOURCE_LINE_IN:
            await self.group_controller.load_line_in(play_on_completion=True)
        elif source == SOURCE_TV:
            await self.client.player.load_home_theater_playback()
        else:
            # unsupported source - try to clear the queue/player
            await self.stop()

    async def enqueue_next_media(self, media: PlayerMedia) -> None:
        """
        Handle enqueuing of the next (queue) item on the player.

        Called when player reports it started buffering a queue item
        and when the queue items updated.

        A PlayerProvider implementation is in itself responsible for handling this
        so that the queue items keep playing until its empty or the player stopped.

        Will only be called if the player reports PlayerFeature.ENQUEUE is
        supported and the player is currently playing a MA queue.

        This will NOT be called if the end of the queue is reached (and repeat disabled).
        This will NOT be called if the player is using flow mode to playback the queue.

         :param media: Details of the item that needs to be enqueued on the player.
        """
        if media.source_id:
            self.cloud_queue_id = media.source_id
        await self.refresh_cloud_queue()

    def wire_item_id(self, queue_item_id: str | None, generation: int | None = None) -> str | None:
        """
        Return the id a queue item is served under in the speaker's cloud queue.

        :param queue_item_id: The MA queue item id, passed through when empty.
        :param generation: The load generation to serve under, for a response that
            must stay on the generation it started with; the current one otherwise.
        """
        # The speaker caches the track it loaded per item id: reloading the item it
        # is already playing under the same id (a seek within the track) leaves it
        # waiting out its stale stream for seconds before it fetches the new one.
        # A generation suffix makes every reload cut over like a track change.
        if not queue_item_id:
            return queue_item_id
        if generation is None:
            generation = self.cloud_queue_item_generation
        return f"{queue_item_id}@{generation}"

    @staticmethod
    def bare_item_id(item_id: str) -> str:
        """
        Return the MA queue item id behind an id the speaker echoes back.

        :param item_id: The id as served to the speaker, tolerating unsuffixed ids.
        """
        return item_id.rsplit("@", 1)[0]

    def release_failed_item(self, wire_item_id: str) -> None:
        """
        Hand the source of a queue item the speaker failed to play over to the next track.

        :param wire_item_id: The failed item's id as the speaker reports it, which is the
            id it was served under.
        """
        if not (queue_id := self.cloud_queue_id):
            return
        queue_item_id = self.bare_item_id(wire_item_id)
        # only the current load's wire id (or a legacy bare id) names the source the item
        # holds now: a report from before a same-track reload is about the load that the
        # reload replaced, and must not take the fresh source down with it
        if wire_item_id not in (queue_item_id, self.wire_item_id(queue_item_id)):
            self.logger.debug(
                "Ignoring the failure of %s: the item has been reloaded since", wire_item_id
            )
            return
        # the speaker moves on to the next track by itself, while the failed track's
        # source may still be filling its buffer and holding the stream slot that
        # next track needs on a music service that allows a single stream
        self.mass.create_task(
            self.mass.player_queues.release_failed_item_source(queue_id, queue_item_id),
            task_name=f"sonos_release_failed_item_{self.player_id}",
        )

    def bump_cloud_queue_version(self) -> None:
        """
        Advance the version the speaker compares its cached queue against.

        An unchanged queueVersion reads as "nothing changed", so this must happen the moment
        the queue does: a window served in between would carry a version read as current.
        """
        self.cloud_queue_version = time.time()

    async def refresh_cloud_queue(self) -> None:
        """Signal the speaker that the queue it is playing changed."""
        self.bump_cloud_queue_version()
        if not self.connected:
            self.logger.debug("Not refreshing the cloud queue: not connected to the speaker")
            return
        group = self.client.player.group
        if group is None or not group.active_session_id:
            # the session id lives in aiosonos and does not survive a reconnect or regroup,
            # while the speaker's session (and its cached queue window) plays on - from here
            # the stream request gate is what keeps a stale cached track off the speaker
            self.logger.debug("Not refreshing the cloud queue: no active playback session known")
            return
        try:
            await self.client.api.playback_session.refresh_cloud_queue(group.active_session_id)
        except FailedCommand as err:
            # an app outside MA can take the session over, leaving us with a session id the
            # speaker no longer knows. Only a nudge is lost: it reads a live window regardless.
            self.logger.debug("Could not refresh the cloud queue: %s", err)
        else:
            self.logger.debug("Refreshed the cloud queue for session %s", group.active_session_id)

    async def build_cloud_queue_window(
        self,
        item_id: str | None,
        max_previous: int = PREVIOUS_ITEMS,
        max_upcoming: int = UPCOMING_ITEMS,
    ) -> SonosQueueWindow:
        """
        Return the requested item, the one before it and the items after it, as the queue is now.

        :param item_id: queue_item_id the speaker asked about; an omitted or empty one asks
            for the start of the queue.
        :param max_previous: Ceiling on the items before the centre, if the speaker asked for
            fewer than we would otherwise serve.
        :param max_upcoming: Ceiling on the items after the centre, same.
        """
        if self._announcement_media is not None:
            # an announcement is a queue of exactly one item
            return SonosQueueWindow(
                items=[self._announcement_media], includes_beginning=True, includes_end=True
            )
        queue_id = self.cloud_queue_id
        if not queue_id or not (queue := self.mass.player_queues.get(queue_id)):
            # nothing to describe: both ends flagged, or the speaker holds what it cached
            return SonosQueueWindow(includes_beginning=True, includes_end=True)

        if not item_id:
            # an omitted or empty itemId asks for the start of the queue
            center_index = 0
        elif (found := self.mass.player_queues.index_by_id(queue_id, item_id)) is not None:
            center_index = found
        else:
            # an item the queue no longer holds: answer around the playing one (not
            # index_in_buffer, which runs an item ahead with crossfade)
            center_index = (
                queue.current_index
                if queue.current_index is not None
                else (queue.index_in_buffer or 0)
            )

        items: list[PlayerMedia] = []
        offset = max(0, center_index - min(PREVIOUS_ITEMS, max_previous))
        for idx in range(offset, center_index + 1):
            queue_item = self.mass.player_queues.get_item(queue_id, idx)
            if queue_item and queue_item.available:
                items.append(await self._player_media_for_speaker(queue_item))

        # get_next_item accounts for repeat mode, so this is the item that will really
        # play next rather than whatever sits at the next index
        last_index: int | str = center_index
        for _ in range(min(UPCOMING_ITEMS, max_upcoming)):
            next_item = self.mass.player_queues.get_next_item(queue_id, last_index)
            if next_item is None:
                break
            items.append(await self._player_media_for_speaker(next_item))
            if next_item.queue_item_id in (x.queue_item_id for x in items[-3:-1]):
                # a track or a pair that repeats itself gets one more round, not a window
                # full: the speaker is never refused the track it plays or the one before
                # it, so it would keep repeating them after repeat was switched off
                break
            last_index = next_item.queue_item_id

        window = SonosQueueWindow(
            items=items,
            includes_beginning=offset == 0,
            # check after the loop in case the window filled exactly up to the last item
            includes_end=self.mass.player_queues.get_next_item(queue_id, last_index) is None,
        )
        self.logger.log(
            VERBOSE_LOG_LEVEL,
            "Serving Sonos queue window for %s on player %s: %s",
            queue_id,
            self.player_id,
            [x.title for x in window.items],
        )
        return window

    async def set_members(
        self,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        """
        Handle SET_MEMBERS command on the player.

        Group or ungroup the given child player(s) to/from this player.
        Will only be called if the PlayerFeature.SET_MEMBERS is supported.

        :param player_ids_to_add: List of player_id's to add to the group.
        :param player_ids_to_remove: List of player_id's to remove from the group.
        """
        player_ids_to_add = player_ids_to_add or []
        player_ids_to_remove = player_ids_to_remove or []
        if player_ids_to_add or player_ids_to_remove:
            await self.group_controller.modify_group_members(
                player_ids_to_add=player_ids_to_add,
                player_ids_to_remove=player_ids_to_remove,
            )
            await self._wait_for_group_members(player_ids_to_add, player_ids_to_remove)

    async def ungroup(self) -> None:
        """
        Handle UNGROUP command on the player.

        Remove the player from any (sync)groups it currently is grouped to.
        If this player is the sync leader (or group player),
        all child's will be ungrouped and the group dissolved.

        Will only be called if the PlayerFeature.SET_MEMBERS is supported.
        """
        await self.client.player.leave_group()

    async def play_announcement(
        self, announcement: PlayerMedia, volume_level: int | None = None
    ) -> None:
        """
        Handle (native) playback of an announcement on the player.

        Will only be called if the PlayerFeature.PLAY_ANNOUNCEMENT is supported.

        :param announcement: Details of the announcement that needs to be played on the player.
        :param volume_level: The volume level to play the announcement at (0..100).
            If not set, the player should use the current volume level.
        """
        self.logger.debug(
            "Playing announcement %s on %s",
            announcement.uri,
            self.display_name,
        )
        finished = asyncio.Event()
        clip_id: str | None = None
        # listeners run after the speaker's next status already replaced the one they were
        # sent with, and the clip's id is only known once the speaker accepted it
        finished_clip_ids: set[str] = set()

        def _check_clip(event: SonosEvent | None = None) -> None:
            if event is not None:
                status = cast("AudioClipStatusEvent", event.data)
                finished_clip_ids.update(
                    clip["id"]
                    for clip in status.get("audioClips", [])
                    if clip.get("status") in FINISHED_CLIP_STATES
                )
            if clip_id and (
                clip_id in finished_clip_ids
                or self.client.player.audio_clip_status(clip_id) in FINISHED_CLIP_STATES
            ):
                finished.set()

        # Wait until the speaker reports the announcement finished playing; this is
        # helpful for people who want to play announcements in a sequence. Its expected
        # duration (plus a margin) bounds the wait should the speaker report nothing
        unsubscribe = self.client.subscribe(
            _check_clip, SonosEventType.AUDIO_CLIP_STATUS, self.player_id
        )
        try:
            clip = await self.client.player.play_audio_clip(
                announcement.uri, volume_level, name="Announcement"
            )
            clip_id = clip["id"]
            _check_clip()
            # finding the duration can take as long as rendering the announcement; a clip the
            # speaker reports finished (or dismissed) before that is known waits for neither
            duration_lookup = asyncio.ensure_future(
                self.mass.streams.get_announcement_duration(announcement)
            )
            finished_wait = asyncio.ensure_future(finished.wait())
            racers: list[asyncio.Future[Any]] = [duration_lookup, finished_wait]
            try:
                await asyncio.wait(racers, return_when=asyncio.FIRST_COMPLETED)
                if not finished_wait.done():
                    duration = duration_lookup.result()
                    with contextlib.suppress(TimeoutError):
                        async with asyncio.timeout((duration or 10) + ANNOUNCEMENT_STATUS_MARGIN):
                            await finished_wait
            finally:
                pending = [racer for racer in racers if not racer.done()]
                for racer in pending:
                    racer.cancel()
                if pending:
                    # waits the cancellations out without absorbing one of this call
                    await asyncio.wait(pending)
                if (
                    finished.is_set()
                    and duration_lookup.done()
                    and not duration_lookup.cancelled()
                    and (lookup_error := duration_lookup.exception())
                ):
                    # the clip ended first, so the failed lookup did not hold anything up
                    self.logger.debug(
                        "Could not look up the announcement duration: %s", lookup_error
                    )
        finally:
            unsubscribe()

    def on_player_event(self, event: SonosEvent | None) -> None:
        """Handle incoming event from player."""
        try:
            self.update_attributes()
        except Exception:
            self.logger.exception("Failed to update player attributes")
            return
        try:
            self.update_state()
        except Exception:
            self.logger.exception("Failed to update player state")

    def update_attributes(self) -> None:  # noqa: C901, PLR0912, PLR0915
        """Update the player attributes."""
        if not self._sleeps_as_power_off:
            # for a player that sleeps as powered off, the connect and disconnect paths own this
            self._attr_available = self.connected
        if not self.connected:
            return
        # guard against the race where a volume event arrives before aiosonos'
        # async_init has populated _volume_data (the accessors raise AttributeError
        # on None). The next event re-runs once the data is there.
        try:
            has_fixed_volume = self.client.player.has_fixed_volume
            volume_muted = self.client.player.volume_muted
            volume_level = self.client.player.volume_level
        except AttributeError:
            pass
        else:
            if has_fixed_volume:
                self._attr_volume_level = 100
            elif not volume_muted or volume_level:
                self._attr_volume_level = volume_level or 0
            self._attr_volume_muted = volume_muted

        group_parent: SonosPlayer | None = None
        active_group: SonosGroup | None
        if self.client.player.is_coordinator:
            # player is group coordinator - always report native group members
            active_group = self.group_controller
            if len(self.client.player.group_members) > 1:
                self._attr_group_members = list(self.client.player.group_members)
            else:
                self._attr_group_members.clear()
            self._attr_can_group_with = {self._provider.instance_id}
        else:
            # player is group child (synced to another player)
            group_parent = cast(
                "SonosPlayer | None",
                self.mass.players.get_player(self.group_controller.coordinator_id),
            )
            if not group_parent or not group_parent.client or not group_parent.client.player:
                # handle race condition where the group parent is not yet discovered
                return
            active_group = group_parent.client.player.group
            self._attr_group_members.clear()

        if not active_group:
            # should not happen, but guard it anyways
            return

        # map playback state
        self._attr_playback_state = PLAYBACK_STATE_MAP[active_group.playback_state]
        self._attr_elapsed_time = active_group.position

        # figure out the active source based on the container
        container_type = active_group.container_type
        active_service = self._active_service(active_group)
        container = active_group.playback_metadata.get("container")
        if container_type == ContainerType.LINEIN:
            self._attr_active_source = SOURCE_LINE_IN
        elif container_type in (ContainerType.HOME_THEATER_HDMI, ContainerType.HOME_THEATER_SPDIF):
            self._attr_active_source = SOURCE_TV
        elif container_type == ContainerType.AIRPLAY and self.active_output_protocol not in (
            "airplay",
            "sendspin",
        ):
            self._attr_active_source = SOURCE_AIRPLAY
        elif (
            container_type == ContainerType.STATION
            and active_service != MusicService.MUSIC_ASSISTANT
        ):
            self._attr_active_source = SOURCE_RADIO
            # add radio to source list if not yet there
            if SOURCE_RADIO not in [x.id for x in self._attr_source_list]:
                self._attr_source_list.append(PLAYER_SOURCE_MAP[SOURCE_RADIO])
        elif active_service == MusicService.SPOTIFY:
            self._attr_active_source = SOURCE_SPOTIFY
            # add spotify to source list if not yet there
            if SOURCE_SPOTIFY not in [x.id for x in self._attr_source_list]:
                self._attr_source_list.append(PLAYER_SOURCE_MAP[SOURCE_SPOTIFY])
        elif active_service == MusicService.MUSIC_ASSISTANT:
            # setting active source to None is fine
            self._attr_active_source = None
        # its playing some service we did not yet map
        elif container and container.get("service", {}).get("name"):
            self._attr_active_source = container["service"]["name"]
        elif container and container.get("name"):
            self._attr_active_source = container["name"]
        elif active_service:
            self._attr_active_source = active_service
        elif container_type:
            self._attr_active_source = container_type
        else:
            # the player has nothing loaded at all (empty queue and no service active)
            self._attr_active_source = None

        self._reflect_source_play_modes(active_group)

        # special case: Sonos reports PAUSED state when MA stopped playback
        if (
            active_service == MusicService.MUSIC_ASSISTANT
            and self._attr_playback_state == PlaybackState.PAUSED
        ):
            self._attr_playback_state = PlaybackState.IDLE

        # parse current media
        self._attr_elapsed_time = active_group.position
        self._attr_elapsed_time_last_updated = time.time()
        current_media = None
        if (current_item := active_group.playback_metadata.get("currentItem")) and (
            (track := current_item.get("track")) and track.get("name")
        ):
            track_images = track.get("images", [])
            track_image_url = track_images[0].get("url") if track_images else None
            track_duration_millis = track.get("durationMillis")
            current_media = PlayerMedia(
                uri=track.get("id", {}).get("objectId") or track.get("mediaUrl") or "",
                media_type=MediaType.TRACK,
                title=track["name"],
                artist=track.get("artist", {}).get("name"),
                album=track.get("album", {}).get("name"),
                duration=int(track_duration_millis / 1000) if track_duration_millis else None,
                image_url=track_image_url,
            )
            if active_service == MusicService.MUSIC_ASSISTANT:
                current_media.source_id = self._attr_active_source
                current_media.queue_item_id = self.bare_item_id(current_item["id"])
        # radio stream info
        if container and container.get("name") and active_group.playback_metadata.get("streamInfo"):
            images = container.get("images", [])
            image_url = images[0].get("url") if images else None
            current_media = PlayerMedia(
                uri=container.get("id", {}).get("objectId") or "",
                media_type=MediaType.RADIO,
                title=active_group.playback_metadata["streamInfo"],
                album=container["name"],
                image_url=image_url,
            )
        # generic info from container (also when MA is playing!)
        if container and container.get("name") and container.get("id"):
            if not current_media:
                current_media = PlayerMedia(
                    uri=container["id"]["objectId"], media_type=MediaType.UNKNOWN
                )
            if not current_media.image_url:
                images = container.get("images", [])
                current_media.image_url = images[0].get("url") if images else None
            if not current_media.title:
                current_media.title = container["name"]
            if not current_media.uri:
                current_media.uri = container["id"]["objectId"]

        self._attr_current_media = current_media

    async def on_protocol_playback(
        self,
        output_protocol: OutputProtocol,
    ) -> None:
        """Handle callback when playback starts on a protocol output."""
        # Only handle AirPlay, directly or as the base of a derived transport (e.g. Sendspin bridge)
        if not self._streams_over_airplay(output_protocol):
            return

        # Only if this player is a coordinator with group members
        if not self.client.player.is_coordinator:
            return

        current_members = list(self.client.player.group_members)
        if len(current_members) <= 1:
            # No group members to worry about
            return

        # Workaround for Sonos AirPlay ungrouping bug: when AirPlay playback starts
        # on a Sonos speaker that has native group members, Sonos dissolves the group.
        # We capture the group state here and restore it after a delay.

        self.logger.debug(
            "AirPlay playback starting on %s with native group members %s - "
            "scheduling restoration to work around Sonos ungrouping bug",
            self.name,
            current_members,
        )
        members_to_restore = [m for m in current_members if m != self.player_id]

        async def _restore_airplay_group() -> None:
            try:
                self.logger.info(
                    "Restoring AirPlay group for %s with members %s",
                    self.name,
                    members_to_restore,
                )
                # we call set_members on the PlayerController here so it
                # can try to regroup via the preferred protocol (which may be AirPlay),
                await self.set_members(player_ids_to_add=members_to_restore)
            except Exception as err:
                self.logger.warning("Failed to restore AirPlay group: %s", err)

        # Schedule restoration after 6 seconds to let AirPlay settle
        self.mass.call_later(
            6,
            _restore_airplay_group,
            task_id=f"restore_airplay_group_{self.player_id}",
        )

    def update_elapsed_time(self, elapsed_time: float | None = None) -> None:
        """Update the elapsed time of the current media."""
        if elapsed_time is not None:
            self._attr_elapsed_time = elapsed_time
        last_updated = time.time()
        self._attr_elapsed_time_last_updated = last_updated
        self.update_state()

    def reconnect(self, delay: float = 1) -> None:
        """Reconnect the player."""
        if self.mass.closing:
            return
        # use a task_id to prevent multiple reconnects
        task_id = f"sonos_reconnect_{self.player_id}"
        self.mass.call_later(delay, self._connect, delay, task_id=task_id)

    async def _connect(self, retry_on_fail: int = 0) -> None:
        """Connect to the Sonos player."""
        if self.mass.closing:
            return
        async with self._connect_lock:
            # a cancelled listener can linger for a cycle, so a live task alone is not
            # proof of a connection
            if self.connected and self._listen_task and not self._listen_task.done():
                self.logger.debug("Already connected to Sonos player: %s", self.player_id)
                return
            try:
                await self.client.connect()
            except (ConnectionFailed, CannotConnect, ClientError) as err:
                self.logger.warning("Failed to connect to Sonos player: %s", err)
                if not retry_on_fail or not self.mass.players.get_player(self.player_id):
                    raise
                if self._sleeps_as_power_off:
                    # a portable that stopped answering has gone to sleep; power() wakes it
                    self._mark_asleep()
                    self.update_state()
                    return
                self._attr_available = False
                self.update_state()
                self.reconnect(min(retry_on_fail + 30, 3600))
                return
            self.connected = True
            self._attr_available = True
            if self._marked_asleep:
                self._attr_powered = True
                self._marked_asleep = False
                self._woken_from_sleep = True
            self.logger.debug("Connected to player API")
            init_ready = asyncio.Event()

            async def _listener() -> None:
                try:
                    await self.client.start_listening(init_ready)
                except Exception as err:
                    if not isinstance(err, ConnectionFailed | asyncio.CancelledError):
                        self.logger.exception("Error in Sonos player listener")
                finally:
                    self.logger.info("Disconnected from player API")
                    if self.connected and not self.mass.closing:
                        # we didn't explicitly disconnect, try to reconnect
                        # this should simply try to reconnect once and if that fails
                        # we rely on mdns to pick it up again later
                        await self._disconnect()
                        self._attr_available = False
                        self.update_state()
                        self.reconnect(5)

            self._listen_task = self.mass.create_task(
                _listener(), task_name=f"sonos_listener_{self.player_id}"
            )
            listen_task = self._listen_task
        # wait for the initial state fetch outside the lock: a listener that dies mid-init
        # never sets init_ready, and the reconnect it schedules needs the lock again
        await self._wait_for_listener_init(init_ready, listen_task, retry_on_fail)

    async def _wait_for_listener_init(
        self, init_ready: asyncio.Event, listen_task: asyncio.Task[None], retry_on_fail: int
    ) -> None:
        """Wait for the listener's initial state fetch, raising when the listener dies first."""
        init_ready_wait = self.mass.create_task(init_ready.wait())
        try:
            await asyncio.wait({init_ready_wait, listen_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            init_ready_wait.cancel()
        if init_ready.is_set():
            return
        if retry_on_fail and self.mass.players.get_player(self.player_id):
            # the listener's own cleanup schedules the retry
            return
        msg = f"Sonos player {self.player_id} disconnected during initialization"
        raise ConnectionFailed(ConnectionError(msg))

    async def _disconnect(self) -> None:
        """Disconnect the client and cleanup."""
        self.connected = False
        if (
            self._listen_task
            and not self._listen_task.done()
            # the listener calls this from its own cleanup; a self-cancel would abort
            # that cleanup at its next await, before the reconnect is scheduled
            and self._listen_task is not asyncio.current_task()
        ):
            self._listen_task.cancel()
        if self.client:
            await self.client.disconnect()
        self.logger.debug("Disconnected from player API")

    def _reflect_source_play_modes(self, active_group: SonosGroup) -> None:
        """Report the source the speaker runs itself, with its play modes, on its source list."""
        # a source that stopped playing must lose its live state, so every entry starts from
        # its template again; the templates are shared between players, so never mutated.
        # a service we did not map only has an entry while the speaker plays it, so it is
        # dropped here and rebuilt below
        self._attr_source_list = [
            PLAYER_SOURCE_MAP[x.id] for x in self._attr_source_list if x.id in PLAYER_SOURCE_MAP
        ]
        if self._attr_active_source is None:
            # MA playback has no entry here, the MA queue carries its own play modes
            return
        actions = active_group.playback_actions.raw_data
        source_index = next(
            (
                index
                for index, source in enumerate(self._attr_source_list)
                if source.id == self._attr_active_source
            ),
            None,
        )
        if source_index is None:
            if self._attr_active_source in PLAYER_SOURCE_MAP:
                # a mapped source this player does not offer itself, such as the line-in of
                # the coordinator it is synced to
                return
            # a service we did not map: the user can not start it from MA and its transport
            # is whatever the speaker reports for it
            source_id = str(self._attr_active_source)
            self._attr_source_list.append(
                PlayerSource(
                    id=source_id,
                    name=source_id,
                    passive=True,
                    # the speaker reports the action for its current state only
                    can_play_pause=actions.get("canPlay", False) or actions.get("canPause", False),
                    can_seek=actions.get("canSeek", False),
                    # one flag for both buttons: kept while either direction works, as the
                    # speaker offers no previous on the first track and no next on the last
                    can_next_previous=active_group.playback_actions.can_skip
                    or active_group.playback_actions.can_skip_to_previous,
                )
            )
            source_index = len(self._attr_source_list) - 1
        modes = active_group.play_modes
        repeat_mode: RepeatMode | None
        if modes.repeat is None and modes.repeat_one is None:
            # Sonos did not report a repeat mode for this source
            repeat_mode = None
        elif modes.repeat_one:
            repeat_mode = RepeatMode.ONE
        elif modes.repeat:
            repeat_mode = RepeatMode.ALL
        else:
            repeat_mode = RepeatMode.OFF
        self._attr_source_list[source_index] = replace(
            self._attr_source_list[source_index],
            can_shuffle=actions.get("canShuffle", False),
            can_repeat=actions.get("canRepeat", False),
            shuffle_enabled=modes.shuffle,
            repeat_mode=repeat_mode,
        )

    def _on_playback_error(self, event: SonosEvent) -> None:
        """Handle a playback failure the speaker reported for the item it tried to play."""
        if self.synced_to:
            # the coordinator plays for the whole group and reports for it
            return
        error = cast("PlaybackError", event.data)
        stream_server = urlparse(self.mass.streams.base_url).netloc
        if error.get("httpStatus") == 404 and error.get("serviceName") == stream_server:
            # our own stream server refused the item: a track the queue moved past or no
            # longer holds. The speaker tries each track it cached before reading the
            # queue again, so these come in bursts
            self.logger.debug(
                "Speaker %s was refused %s by the stream server",
                self.display_name,
                error.get("itemId"),
            )
            return
        self.logger.warning(
            "Speaker %s could not play %s and reported %s (%s)",
            self.display_name,
            error.get("trackName") or error.get("itemId"),
            error["errorCode"],
            error.get("reason", "no reason given"),
        )
        if item_id := error.get("itemId"):
            self.release_failed_item(item_id)

    def _on_group_coordinator_changed(self, event: SonosEvent) -> None:
        """Hand our queue to the player the speakers moved this player's group to."""
        # when the coordinator leaves its group outside Music Assistant (the Sonos app, an
        # Arc switching to TV), Sonos elects a member that carries on with a copy of what
        # played; as when the leader of a group is removed in MA, the queue follows it
        data = cast("GroupCoordinatorChanged", event.data)
        if data["groupStatus"] != GroupStatus.MOVED or not (queue_id := self._moved_queue_id()):
            # a group change Music Assistant made itself stops our queue first
            return
        new_coordinator_id = data.get("playerId")
        if not new_coordinator_id or new_coordinator_id == self.player_id:
            return
        new_coordinator = self.mass.players.get_player(new_coordinator_id)
        if not isinstance(new_coordinator, SonosPlayer) or not new_coordinator.connected:
            self.logger.debug(
                "The group of %s moved to %s, which Music Assistant does not control",
                self.display_name,
                new_coordinator_id,
            )
            return
        generation = self.cloud_queue_item_generation
        # one hand-over per move: a later move (after a new play here) is not the same one
        task = self.mass.create_task(
            self._hand_over_moved_group(
                new_coordinator,
                queue_id,
                generation,
                new_coordinator.cloud_queue_item_generation,
            ),
            task_id=f"sonos_hand_over_moved_group_{self.player_id}_{generation}",
        )
        self._hand_over_tasks.add(task)
        task.add_done_callback(self._hand_over_tasks.discard)

    async def _hand_over_moved_group(
        self,
        new_coordinator: SonosPlayer,
        queue_id: str,
        generation: int,
        new_coordinator_generation: int,
    ) -> None:
        """Hand the queue of this player's former group to the player that coordinates it now."""
        # only the player's own queue moves, as core hands over only the leader's own
        # queue; a group player's queue stops with the group instead
        own_queue = queue_id == self.player_id
        if not await self._wait_for_moved_group(new_coordinator, queue_id, own_queue):
            return
        if self.cloud_queue_item_generation != generation or self._playing_queue_id() not in (
            queue_id,
            None,
        ):
            # this player was given something (else) to play in the meantime
            return
        if not self._leads_moved_group(new_coordinator):
            # it gave the role up again before this hand-over got to run
            self.logger.debug(
                "%s no longer leads the moved group, leaving it alone",
                new_coordinator.display_name,
            )
            return
        group = new_coordinator.group_controller
        if (
            new_coordinator.cloud_queue_item_generation != new_coordinator_generation
            or not self._plays_queue(group, queue_id)
        ):
            # so was the new coordinator: what it plays now is not what moved
            self.logger.debug(
                "%s plays something else by now, leaving it alone", new_coordinator.display_name
            )
            return
        self._evicted_cloud_queue = None
        self._forget_queue()
        self.update_state()
        if not own_queue:
            self.logger.info(
                "The speakers moved the group of %s to %s outside Music Assistant, "
                "stopping playback",
                self.display_name,
                new_coordinator.display_name,
            )
            try:
                await new_coordinator.stop()
            except FailedCommand as err:
                self.logger.warning(
                    "Could not stop playback on %s: %s", new_coordinator.display_name, err
                )
            return
        playing = PLAYBACK_STATE_MAP.get(group.playback_state) == PlaybackState.PLAYING
        self.logger.info(
            "The speakers moved the group of %s to %s outside Music Assistant, "
            "moving its queue there",
            self.display_name,
            new_coordinator.display_name,
        )
        await self.mass.player_queues.transfer_queue(
            queue_id, new_coordinator.player_id, auto_play=False
        )
        if not playing:
            return
        # core's resume takes a queue that reads as playing at its own clock, and the new
        # coordinator's does (its speaker plays the moved copy): start it where ours stopped
        queue = self.mass.player_queues.get(new_coordinator.player_id)
        if queue is None or (item := queue.current_item) is None:
            return
        # a radio stream cannot be started part way in
        position = 0 if item.media_type == MediaType.RADIO else int(queue.resume_pos)
        await self.mass.player_queues.play_index(
            new_coordinator.player_id, item.queue_item_id, position
        )

    async def _wait_for_moved_group(
        self, new_coordinator: SonosPlayer, queue_id: str, own_queue: bool
    ) -> bool:
        """Wait until the new coordinator leads the moved group, and our queue is done here."""
        ready = asyncio.Event()

        def _check(_event: object = None) -> None:
            if not self._leads_moved_group(new_coordinator):
                return
            # a hand-over stops a queue that still plays here, and with it whatever took
            # this player over (an Arc switched to TV): the queue follows the speaker's
            # report a moment later
            queue = self.mass.player_queues.get(queue_id)
            if own_queue and queue is not None and queue.state != PlaybackState.IDLE:
                return
            ready.set()

        unsubscribe_speaker = new_coordinator.client.subscribe(
            _check,
            (
                SonosEventType.GROUP_ADDED,
                SonosEventType.GROUP_UPDATED,
                SonosEventType.PLAYER_UPDATED,
            ),
        )
        unsubscribe_queue = self.mass.subscribe(_check, EventType.QUEUE_UPDATED, queue_id)
        try:
            _check()
            async with asyncio.timeout(MOVED_GROUP_TIMEOUT):
                await ready.wait()
        except TimeoutError:
            self.logger.debug(
                "%s did not take over the group of %s, or its queue kept playing here",
                new_coordinator.display_name,
                self.display_name,
            )
            return False
        finally:
            unsubscribe_speaker()
            unsubscribe_queue()
        return True

    @staticmethod
    def _leads_moved_group(new_coordinator: SonosPlayer) -> bool:
        """Return whether a player leads the group the speakers moved to it."""
        # its own connection reports the role, and then reads what the group plays, a
        # moment after the former coordinator hears of the move
        player = new_coordinator.client.player
        return (
            new_coordinator.connected
            and player is not None
            and player.is_coordinator
            and player.group is not None
            and player.group.is_subscribed
        )

    @staticmethod
    def _active_service(group: SonosGroup) -> MusicService | str | None:
        """
        Return the service a group plays, a Music Assistant queue included.

        :param group: The group, as its coordinator's connection reports it.
        """
        if active_service := group.active_service:
            return active_service
        container = group.playback_metadata.get("container")
        if container and container.get("service", {}).get("id") == MusicService.MUSIC_ASSISTANT:
            return MusicService.MUSIC_ASSISTANT
        return None

    @staticmethod
    def _plays_queue(group: SonosGroup, queue_id: str) -> bool:
        """Return whether a group plays the given MA queue, as a cloud queue or as one stream."""
        # the speaker reports the container we served: mass:<queue> for a cloud queue,
        # mass:<queue>:<item> for a stream
        container = group.playback_metadata.get("container")
        object_id = container.get("id", {}).get("objectId", "") if container else ""
        return object_id == f"mass:{queue_id}" or object_id.startswith(f"mass:{queue_id}:")

    def _playing_queue_id(self) -> str | None:
        """Return the MA queue the speaker plays, as a cloud queue or as one stream."""
        return self.cloud_queue_id or self._flow_queue_id

    def _forget_queue(self) -> None:
        """Forget the MA queue the speaker played, on either path."""
        self.cloud_queue_id = None
        self._flow_queue_id = None

    def _moved_queue_id(self) -> str | None:
        """Return the queue this player serves, or released on an eviction just before."""
        if queue_id := self._playing_queue_id():
            return queue_id
        if self._evicted_cloud_queue is None:
            return None
        queue_id, evicted_at = self._evicted_cloud_queue
        if time.monotonic() - evicted_at > EVICTED_QUEUE_GRACE:
            return None
        return queue_id

    def _on_session_error(self, event: SonosEvent) -> None:
        """Release our queue when another app or source took the speaker over."""
        error = cast("SessionError", event.data)
        if (
            error.get("errorCode") != SESSION_EVICTED_ERROR
            or not (queue_id := self._playing_queue_id())
            or error.get("sessionId") != self._queue_session_id
        ):
            return
        self._release_evicted_queue(queue_id)

    def _bind_loaded_session(self, queue_id: str) -> bool:
        """Tie our queue to the playback session the speaker loaded it into."""
        # an eviction counts only for the session the queue went into: the speaker
        # can report one for the session it replaces after this load has started
        self._queue_session_id = self.group_controller.active_session_id
        if self._queue_session_id is None:
            # evicted while the queue loaded: the speaker does not play it
            self._release_evicted_queue(queue_id)
            return False
        return True

    def _settle_failed_load(
        self,
        err: BaseException,
        queue_id: str,
        previous_queue_id: str | None,
        replaced_session_id: str | None = None,
    ) -> None:
        """Settle our queue after a load of it failed or was cancelled."""
        if isinstance(err, FailedCommand) and err.error_code == SESSION_EVICTED_ERROR:
            # the speakers carry on with what played before, so a coordinator move that
            # follows still finds that queue
            self._release_evicted_queue(previous_queue_id or queue_id)
            return
        group = self.client.player.group
        session_id = group.active_session_id if group is not None else None
        if (
            isinstance(err, (CommandTimeout, asyncio.CancelledError))
            and session_id
            and session_id != replaced_session_id
        ):
            # unanswered or cut off, the load may still have reached the speaker, which
            # then plays our queue in the session it kept
            self._queue_session_id = session_id
            return
        # the speaker never got the queue, so describing one is worse than admitting
        # there is none
        self._forget_queue()

    def _release_evicted_queue(self, queue_id: str) -> None:
        """Let go of our queue on a speaker another app or source took over."""
        # the speaker no longer plays our queue and must not be sent commands for it;
        # Music Assistant does not take it back without the user asking
        self.logger.info(
            "Another app or source took over %s, releasing the Music Assistant queue",
            self.display_name,
        )
        self._forget_queue()
        self._evicted_cloud_queue = (queue_id, time.monotonic())
        self.update_state()

    async def _play_stream(self, media: PlayerMedia) -> None:
        """Play media as one stream: a queue in flow mode, or media that is no MA queue item."""
        # this path loads no cloud queue, so the speaker must not be signalled about one
        previous_queue_id = self._playing_queue_id()
        self._queue_session_id = None
        self._forget_queue()
        flow_queue_id = media.source_id if media.source_id and media.queue_item_id else None
        self._flow_queue_id = flow_queue_id
        # enforce AAC here because Sonos really does not support FLAC streams without duration
        stream_url = await self.provider.mass.streams.resolve_stream_url(self.player_id, media)
        stream_url = stream_url.replace(".flac", ".aac").replace(".wav", ".aac")
        if media.source_id and media.queue_item_id:
            object_id = f"mass:{media.source_id}:{media.queue_item_id}"
        else:
            object_id = stream_url
        container: Container = {
            "_objectType": "container",
            "name": media.title or "",
            "type": "track",
            "id": {
                "_objectType": "id",
                "objectId": object_id,
            },
            "service": {
                "_objectType": "service",
                "name": "Music Assistant",
                "id": "mass",
            },
        }
        if media.image_url:
            container["imageUrl"] = media.image_url
        # a stream always gets a session of its own, so the one held now is not ours
        held_group = self.client.player.group
        replaced_session_id = held_group.active_session_id if held_group is not None else None
        try:
            await self.group_controller.play_stream_url(stream_url, container)
        except (Exception, asyncio.CancelledError) as err:
            if flow_queue_id:
                self._settle_failed_load(err, flow_queue_id, previous_queue_id, replaced_session_id)
            raise
        if flow_queue_id and not self._bind_loaded_session(flow_queue_id):
            return
        # same post-success sweep as the cloud-queue branch: a forced-flow queue
        # (overlay) streams through here and its replaced session's response must
        # die now that the speaker accepted the new stream
        if media.source_id and media.queue_session_id:
            self.mass.streams.close_superseded_item_streams(media.source_id, media.queue_session_id)

    async def _player_media_for_speaker(self, queue_item: QueueItem) -> PlayerMedia:
        """Return the media for a queue item, with its stream URL resolved for this player."""
        media = await self.mass.player_queues.player_media_from_queue_item(queue_item)
        media.uri = await self.mass.streams.resolve_stream_url(self.player_id, media)
        return media

    def _streams_over_airplay(self, output_protocol: OutputProtocol) -> bool:
        """Return whether the output protocol reaches this speaker over AirPlay."""
        if output_protocol.protocol_domain == "airplay":
            return True
        derived_from = output_protocol.derived_from
        if not derived_from or derived_from == "native":
            return False
        base_player = self.mass.players.get_player(derived_from)
        return base_player is not None and base_player.provider.domain == "airplay"

    async def _wait_for_group_members(
        self, player_ids_to_add: list[str], player_ids_to_remove: list[str]
    ) -> None:
        """Wait until the Sonos group reports the requested members, or give up after a timeout."""
        removed = set(player_ids_to_remove) - {self.player_id}

        def _reported() -> bool:
            members = set(self.client.player.group_members)
            return members.issuperset(player_ids_to_add) and members.isdisjoint(removed)

        try:
            async with asyncio.timeout(5):
                while not _reported():
                    await asyncio.sleep(0.1)
        except TimeoutError:
            self.logger.debug(
                "Timed out waiting for the Sonos group of %s to report members %s",
                self.name,
                player_ids_to_add,
            )

    def _extract_mac_from_player_id(self) -> str | None:
        """
        Extract MAC address from Sonos player_id.

        Sonos player_ids follow the format RINCON_XXXXXXXXXXXX01400 where
        the middle 12 hex characters represent the MAC address.

        :return: MAC address string in XX:XX:XX:XX:XX:XX format, or None if not extractable.
        """
        # Remove RINCON_ prefix if present
        player_id = self.player_id
        player_id = player_id.removeprefix("RINCON_")  # Remove "RINCON_"

        # Remove the 01400 suffix (or similar) - should be last 5 chars
        if len(player_id) >= 17:  # 12 hex chars for MAC + 5 chars suffix
            mac_hex = player_id[:12]
        else:
            return None

        # Validate it looks like a MAC (all hex characters)
        try:
            int(mac_hex, 16)
        except ValueError:
            return None

        # Format as XX:XX:XX:XX:XX:XX
        return ":".join(mac_hex[i : i + 2].upper() for i in range(0, 12, 2))

    @property
    def _sleeps_as_power_off(self) -> bool:
        """Return whether the player reports sleep as powered off, MA controlling power natively."""
        return self._wakeable and self.power_control == PLAYER_CONTROL_NATIVE

    def _mark_asleep(self) -> None:
        """Reflect the speaker having gone to sleep: off and idle, but still available."""
        self._attr_available = True
        self._attr_powered = False
        # a stale PAUSED would make the controller unpause the client instead of powering on
        self._attr_playback_state = PlaybackState.IDLE
        self._marked_asleep = True

    async def _wake(self) -> None:
        """Send the wake-on-LAN packet and connect once the speaker's radio answers."""
        assert self._wol_mac is not None  # for type checking
        await send_magic_packet(self._wol_mac)
        try:
            async with asyncio.timeout(WAKE_TIMEOUT):
                # a connect to a still-dark radio hangs on the stale ARP entry, so probe first
                while not await self._is_reachable():
                    await asyncio.sleep(1)
                # the same budget bounds the connect, so a stalled handshake cannot
                # hang the power command past the promised timeout
                await self._connect()
        except TimeoutError:
            msg = f"{self.display_name} did not respond to wake-on-LAN"
            raise PlayerUnavailableError(
                msg,
                translation_key="wake_on_lan_timeout",
                translation_owner=self.translation_owner,
                translation_args=[self.display_name],
            ) from None

    async def _is_reachable(self) -> bool:
        """Return whether the player API port answers a TCP connect."""
        try:
            async with asyncio.timeout(1):
                # 1443 is the port aiosonos' websocket connects to
                _, writer = await asyncio.open_connection(self.device_info.ip_address, 1443)
        except OSError, TimeoutError:
            return False
        writer.close()
        await writer.wait_closed()
        return True

    async def _ensure_playing_after_wake(self) -> None:
        """Nudge a woken speaker with play() when its restored paused session ignores the load."""
        deadline = time.monotonic() + WAKE_PLAY_KICK_TIMEOUT
        while time.monotonic() < deadline:
            state = PLAYBACK_STATE_MAP.get(self.group_controller.playback_state)
            if state == PlaybackState.PLAYING:
                return
            await asyncio.sleep(0.25)
        with contextlib.suppress(FailedCommand):
            await self.group_controller.play()


def _is_wakeable(discovery_info: SonosDiscoveryInfo) -> bool:
    """Return whether the discovered device features advertise Wake-on-LAN support."""
    # deviceFeatures is not modeled in aiosonos
    features = cast("dict[str, Any]", discovery_info["device"]).get("deviceFeatures", [])
    return any(
        isinstance(feature, dict) and feature.get("name") == DEVICE_FEATURE_WAKEABLE
        for feature in features
    )
