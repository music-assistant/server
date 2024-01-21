"""Sample Player provider for Music Assistant."""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import soco
from soco import config
from soco.events_base import Event as SonosEvent
from soco.events_base import SubscriptionBase
from soco.groups import ZoneGroup

from music_assistant.common.models.config_entries import (
    CONF_ENTRY_CROSSFADE,
    ConfigEntry,
    ConfigValueType,
)
from music_assistant.common.models.enums import (
    ConfigEntryType,
    ContentType,
    PlayerFeature,
    PlayerState,
    PlayerType,
    ProviderFeature,
)
from music_assistant.common.models.errors import PlayerCommandFailed, PlayerUnavailableError
from music_assistant.common.models.player import DeviceInfo, Player
from music_assistant.common.models.queue_item import QueueItem
from music_assistant.constants import CONF_CROSSFADE, CONF_PLAYERS
from music_assistant.server.helpers.didl_lite import create_didl_metadata
from music_assistant.server.models.player_provider import PlayerProvider

if TYPE_CHECKING:
    from music_assistant.common.models.config_entries import PlayerConfig, ProviderConfig
    from music_assistant.common.models.provider import ProviderManifest
    from music_assistant.server import MusicAssistant
    from music_assistant.server.controllers.streams import MultiClientStreamJob
    from music_assistant.server.models import ProviderInstanceType


PLAYER_FEATURES = (
    PlayerFeature.SYNC,
    PlayerFeature.VOLUME_MUTE,
    PlayerFeature.VOLUME_SET,
    PlayerFeature.ENQUEUE_NEXT,
)

CONF_NETWORK_SCAN = "network_scan"

# set event listener port to something other than 1400
# to allow coextistence with HA on the same host
config.EVENT_LISTENER_PORT = 1700

HIRES_MODELS = (
    "Sonos Roam",
    "Sonos Arc",
    "Sonos Beam",
    "Sonos Five",
    "Sonos Move",
    "Sonos One SL",
    "Sonos Port",
    "Sonos Amp",
    "SYMFONISK Bookshelf",
    "SYMFONISK Table Lamp",
)


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    prov = SonosPlayerProvider(mass, manifest, config)
    await prov.handle_setup()
    return prov


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    instance_id: id of an existing provider instance (None if new instance setup).
    action: [optional] action key called from config entries UI.
    values: the (intermediate) raw values for config entries sent with the action.
    """
    # ruff: noqa: ARG001
    return (
        ConfigEntry(
            key=CONF_NETWORK_SCAN,
            type=ConfigEntryType.BOOLEAN,
            label="Enable network scan for discovery",
            default_value=False,
            description="Enable network scan for discovery of players. \n"
            "Can be used if (some of) your players are not automatically discovered.",
        ),
    )


@dataclass
class SonosPlayer:
    """Wrapper around Sonos/SoCo with some additional attributes."""

    player_id: str
    soco: soco.SoCo
    player: Player
    is_stereo_pair: bool = False
    elapsed_time: int = 0
    playback_started: float | None = None
    need_elapsed_time_workaround: bool = False

    subscriptions: list[SubscriptionBase] = field(default_factory=list)

    transport_info: dict = field(default_factory=dict)
    track_info: dict = field(default_factory=dict)
    speaker_info: dict = field(default_factory=dict)
    rendering_control_info: dict = field(default_factory=dict)
    group_info: ZoneGroup | None = None

    speaker_info_updated: float = 0.0
    transport_info_updated: float = 0.0
    track_info_updated: float = 0.0
    rendering_control_info_updated: float = 0.0
    group_info_updated: float = 0.0

    def update_info(
        self,
        update_transport_info: bool = False,
        update_track_info: bool = False,
        update_speaker_info: bool = False,
        update_rendering_control_info: bool = False,
        update_group_info: bool = False,
    ):
        """Poll all info from player (must be run in executor thread)."""
        # transport info
        if update_transport_info:
            transport_info = self.soco.get_current_transport_info()
            if transport_info.get("current_transport_state") != "TRANSITIONING":
                self.transport_info = transport_info
                self.transport_info_updated = time.time()
        # track info
        if update_track_info:
            self.track_info = self.soco.get_current_track_info()
            # sonos reports bullshit elapsed time while playing radio (or flow mode),
            # trying to be "smart" and resetting the counter when new ICY metadata is detected
            # we try to detect this and work around it
            self.need_elapsed_time_workaround = self.track_info["duration"] == "0:00:00"
            if not self.need_elapsed_time_workaround:
                self.elapsed_time = _timespan_secs(self.track_info["position"]) or 0
            self.track_info_updated = time.time()

        # speaker info
        if update_speaker_info:
            self.speaker_info = self.soco.get_speaker_info()
            self.speaker_info_updated = time.time()
        # rendering control info
        if update_rendering_control_info:
            self.rendering_control_info["volume"] = self.soco.volume
            self.rendering_control_info["mute"] = self.soco.mute
            self.rendering_control_info_updated = time.time()
        # group info
        if update_group_info:
            self.group_info = self.soco.group
            self.group_info_updated = time.time()

    def update_attributes(self):
        """Update attributes of the MA Player from soco.SoCo state."""
        now = time.time()
        # generic attributes (speaker_info)
        self.player.available = True
        self.player.name = self.speaker_info["zone_name"]
        self.player.volume_level = int(self.rendering_control_info["volume"])
        self.player.volume_muted = self.rendering_control_info["mute"]

        # transport info (playback state)
        current_transport_state = self.transport_info["current_transport_state"]
        self.player.state = current_state = _convert_state(current_transport_state)

        if self.playback_started is not None and current_state == PlayerState.IDLE:
            self.playback_started = None
        elif self.playback_started is None and current_state == PlayerState.PLAYING:
            self.playback_started = now

        # media info (track info)
        self.player.current_item_id = self.track_info["uri"]
        if self.player.player_id in self.player.current_item_id:
            self.player.active_source = self.player.player_id
        elif "spotify" in self.player.current_item_id:
            self.player.active_source = "spotify"
        elif self.player.current_item_id.startswith("http"):
            self.player.active_source = "http"
        else:
            # TODO: handle other possible sources here
            self.player.active_source = None
        if not self.need_elapsed_time_workaround:
            self.player.elapsed_time = self.elapsed_time
            self.player.elapsed_time_last_updated = self.track_info_updated

        # zone topology (syncing/grouping) details
        if (
            self.group_info
            and self.group_info.coordinator
            and self.group_info.coordinator.uid == self.player_id
        ):
            # this player is the sync leader
            self.player.synced_to = None
            group_members = {x.uid for x in self.group_info.members if x.is_visible}
            if not group_members:
                # not sure about this ?!
                self.player.type = PlayerType.PLAYER
            elif group_members == {self.player_id}:
                self.player.group_childs = set()
            else:
                self.player.group_childs = group_members
        elif self.group_info and self.group_info.coordinator:
            # player is synced to
            self.player.group_childs = set()
            self.player.synced_to = self.group_info.coordinator.uid
        else:
            # unsure
            self.player.group_childs = set()

    async def check_poll(self) -> None:
        """Check if any of the endpoints needs to be polled for info."""
        cur_time = time.time()
        update_transport_info = (cur_time - self.transport_info_updated) > 30
        update_track_info = self.transport_info.get("current_transport_state") == "PLAYING" or (
            (cur_time - self.track_info_updated) > 300
        )
        update_speaker_info = (cur_time - self.speaker_info_updated) > 300
        update_rendering_control_info = (cur_time - self.rendering_control_info_updated) > 30
        update_group_info = (cur_time - self.group_info_updated) > 300

        if not (
            update_transport_info
            or update_track_info
            or update_speaker_info
            or update_rendering_control_info
            or update_group_info
        ):
            return

        await asyncio.to_thread(
            self.update_info,
            update_transport_info,
            update_track_info,
            update_speaker_info,
            update_rendering_control_info,
            update_group_info,
        )


class SonosPlayerProvider(PlayerProvider):
    """Sonos Player provider."""

    sonosplayers: dict[str, SonosPlayer] | None = None
    _discovery_running: bool = False
    _discovery_reschedule_timer: asyncio.TimerHandle | None = None

    @property
    def supported_features(self) -> tuple[ProviderFeature, ...]:
        """Return the features supported by this Provider."""
        return (ProviderFeature.SYNC_PLAYERS,)

    async def handle_setup(self) -> None:
        """Handle async initialization of the provider."""
        self.sonosplayers = {}
        self._discovery_running = False
        # silence the soco logger a bit
        logging.getLogger("soco").setLevel(logging.INFO)
        logging.getLogger("urllib3.connectionpool").setLevel(logging.INFO)
        self.mass.create_task(self._run_discovery())

    async def unload(self) -> None:
        """Handle close/cleanup of the provider."""
        if self._discovery_reschedule_timer:
            self._discovery_reschedule_timer.cancel()
            self._discovery_reschedule_timer = None
        # await any in-progress discovery
        while self._discovery_running:
            await asyncio.sleep(0.5)
        # cleanup players
        if self.sonosplayers:
            for player_id in list(self.sonosplayers):
                player = self.sonosplayers.pop(player_id)
                player.player.available = False
                if player.soco.is_coordinator:
                    player.soco.end_direct_control_session()
        self.sonosplayers = None

    async def get_player_config_entries(
        self, player_id: str  # noqa: ARG002
    ) -> tuple[ConfigEntry, ...]:
        """Return Config Entries for the given player."""
        base_entries = await super().get_player_config_entries(player_id)
        if not (sonos_player := self.sonosplayers.get(player_id)):
            return base_entries
        return base_entries + (
            CONF_ENTRY_CROSSFADE,
            ConfigEntry(
                key="sonos_bass",
                type=ConfigEntryType.INTEGER,
                label="Bass",
                default_value=0,
                range=(-10, 10),
                description="Set the Bass level for the Sonos player",
                value=sonos_player.soco.bass,
                advanced=True,
            ),
            ConfigEntry(
                key="sonos_treble",
                type=ConfigEntryType.INTEGER,
                label="Treble",
                default_value=0,
                range=(-10, 10),
                description="Set the Treble level for the Sonos player",
                value=sonos_player.soco.treble,
                advanced=True,
            ),
            ConfigEntry(
                key="sonos_loudness",
                type=ConfigEntryType.BOOLEAN,
                label="Loudness compensation",
                default_value=True,
                description="Enable loudness compensation on the Sonos player",
                value=sonos_player.soco.loudness,
                advanced=True,
            ),
        )

    def on_player_config_changed(
        self, config: PlayerConfig, changed_keys: set[str]  # noqa: ARG002
    ) -> None:
        """Call (by config manager) when the configuration of a player changes."""
        super().on_player_config_changed(config, changed_keys)
        if "enabled" in changed_keys:
            # run discovery to catch any re-enabled players
            self.mass.create_task(self._run_discovery())
        if not (sonos_player := self.sonosplayers.get(config.player_id)):
            return
        if "values/sonos_bass" in changed_keys:
            self.mass.create_task(
                sonos_player.soco.renderingControl.SetBass,
                [("InstanceID", 0), ("DesiredBass", config.get_value("sonos_bass"))],
            )
        if "values/sonos_treble" in changed_keys:
            self.mass.create_task(
                sonos_player.soco.renderingControl.SetTreble,
                [("InstanceID", 0), ("DesiredTreble", config.get_value("sonos_treble"))],
            )
        if "values/sonos_loudness" in changed_keys:
            loudness_value = "1" if config.get_value("sonos_loudness") else "0"
            self.mass.create_task(
                sonos_player.soco.renderingControl.SetLoudness,
                [
                    ("InstanceID", 0),
                    ("Channel", "Master"),
                    ("DesiredLoudness", loudness_value),
                ],
            )

    async def cmd_stop(self, player_id: str) -> None:
        """Send STOP command to given player."""
        sonos_player = self.sonosplayers[player_id]
        if not sonos_player.soco.is_coordinator:
            self.logger.debug(
                "Ignore STOP command for %s: Player is synced to another player.",
                player_id,
            )
            return
        await asyncio.to_thread(sonos_player.soco.stop)
        await asyncio.to_thread(sonos_player.soco.clear_queue)
        sonos_player.playback_started = None

    async def cmd_play(self, player_id: str) -> None:
        """Send PLAY command to given player."""
        sonos_player = self.sonosplayers[player_id]
        if not sonos_player.soco.is_coordinator:
            self.logger.debug(
                "Ignore PLAY command for %s: Player is synced to another player.",
                player_id,
            )
            return
        await asyncio.to_thread(sonos_player.soco.play)

    async def cmd_pause(self, player_id: str) -> None:
        """Send PAUSE command to given player."""
        sonos_player = self.sonosplayers[player_id]
        if not sonos_player.soco.is_coordinator:
            self.logger.debug(
                "Ignore PLAY command for %s: Player is synced to another player.",
                player_id,
            )
            return
        if sonos_player.need_elapsed_time_workaround:
            # no pause allowed when radio/flow mode is active
            await self.cmd_stop(player_id)
            return
        await asyncio.to_thread(sonos_player.soco.pause)

    async def cmd_volume_set(self, player_id: str, volume_level: int) -> None:
        """Send VOLUME_SET command to given player."""

        def set_volume_level(player_id: str, volume_level: int) -> None:
            sonos_player = self.sonosplayers[player_id]
            sonos_player.soco.volume = volume_level

        await asyncio.to_thread(set_volume_level, player_id, volume_level)

    async def cmd_volume_mute(self, player_id: str, muted: bool) -> None:
        """Send VOLUME MUTE command to given player."""

        def set_volume_mute(player_id: str, muted: bool) -> None:
            sonos_player = self.sonosplayers[player_id]
            sonos_player.soco.mute = muted

        await asyncio.to_thread(set_volume_mute, player_id, muted)

    async def cmd_sync(self, player_id: str, target_player: str) -> None:
        """Handle SYNC command for given player.

        Join/add the given player(id) to the given (master) player/sync group.

            - player_id: player_id of the player to handle the command.
            - target_player: player_id of the syncgroup master or group player.
        """
        sonos_player = self.sonosplayers[player_id]
        retries = 0
        while True:
            try:
                await asyncio.to_thread(
                    sonos_player.soco.join, self.sonosplayers[target_player].soco
                )
                break
            except soco.exceptions.SoCoUPnPException as err:
                if retries >= 3:
                    raise err
                retries += 1
                await asyncio.sleep(1)
        await asyncio.to_thread(
            sonos_player.update_info,
            update_group_info=True,
        )

    async def cmd_unsync(self, player_id: str) -> None:
        """Handle UNSYNC command for given player.

        Remove the given player from any syncgroups it currently is synced to.

            - player_id: player_id of the player to handle the command.
        """
        sonos_player = self.sonosplayers[player_id]
        await asyncio.to_thread(sonos_player.soco.unjoin)
        await asyncio.to_thread(
            sonos_player.update_info,
            update_group_info=True,
        )

    async def play_media(
        self,
        player_id: str,
        queue_item: QueueItem,
        seek_position: int,
        fade_in: bool,
    ) -> None:
        """Handle PLAY MEDIA on given player.

        This is called by the Queue controller to start playing a queue item on the given player.
        The provider's own implementation should work out how to handle this request.

            - player_id: player_id of the player to handle the command.
            - queue_item: The QueueItem that needs to be played on the player.
            - seek_position: Optional seek to this position.
            - fade_in: Optionally fade in the item at playback start.
        """
        url = await self.mass.streams.resolve_stream_url(
            queue_item=queue_item,
            output_codec=ContentType.FLAC,
            seek_position=seek_position,
            fade_in=fade_in,
            flow_mode=False,
        )
        sonos_player = self.sonosplayers[player_id]
        if not sonos_player.soco.is_coordinator:
            # this should be already handled by the player manager, but just in case...
            raise PlayerCommandFailed(
                f"Player {sonos_player.player.display_name} can not "
                "accept play_media command, it is synced to another player."
            )
        # always stop and clear queue first
        await asyncio.to_thread(sonos_player.soco.stop)
        await asyncio.to_thread(sonos_player.soco.clear_queue)
        await self._enqueue_item(sonos_player, url=url, queue_item=queue_item)
        await asyncio.to_thread(sonos_player.soco.play_from_queue, 0)
        # optimistically set this timestamp to help figure out elapsed time later
        now = time.time()
        sonos_player.playback_started = now
        sonos_player.player.elapsed_time = 0
        sonos_player.player.elapsed_time_last_updated = now

    async def play_stream(self, player_id: str, stream_job: MultiClientStreamJob) -> None:
        """Handle PLAY STREAM on given player.

        This is a special feature from the Universal Group provider.
        """
        url = stream_job.resolve_stream_url(player_id, ContentType.MP3)
        sonos_player = self.sonosplayers[player_id]
        if not sonos_player.soco.is_coordinator:
            # this should be already handled by the player manager, but just in case...
            raise PlayerCommandFailed(
                f"Player {sonos_player.player.display_name} can not "
                "accept play_stream command, it is synced to another player."
            )
        # always stop and clear queue first
        await asyncio.to_thread(sonos_player.soco.stop)
        await asyncio.to_thread(sonos_player.soco.clear_queue)
        await asyncio.to_thread(sonos_player.soco.play_uri, url, force_radio=True)
        # optimistically set this timestamp to help figure out elapsed time later
        now = time.time()
        sonos_player.playback_started = now
        sonos_player.player.elapsed_time = 0
        sonos_player.player.elapsed_time_last_updated = now

    async def enqueue_next_queue_item(self, player_id: str, queue_item: QueueItem):
        """
        Handle enqueuing of the next queue item on the player.

        If the player supports PlayerFeature.ENQUE_NEXT:
          This will be called about 10 seconds before the end of the track.
        If the player does NOT report support for PlayerFeature.ENQUE_NEXT:
          This will be called when the end of the track is reached.

        A PlayerProvider implementation is in itself responsible for handling this
        so that the queue items keep playing until its empty or the player stopped.

        This will NOT be called if the end of the queue is reached (and repeat disabled).
        This will NOT be called if flow mode is enabled on the queue.
        """
        sonos_player = self.sonosplayers[player_id]
        url = await self.mass.streams.resolve_stream_url(
            queue_item=queue_item,
            output_codec=ContentType.FLAC,
        )
        # set crossfade according to player setting
        crossfade = await self.mass.config.get_player_config_value(player_id, CONF_CROSSFADE)
        if sonos_player.soco.cross_fade != crossfade:

            def set_crossfade():
                with suppress(Exception):
                    sonos_player.soco.cross_fade = crossfade

            await asyncio.to_thread(set_crossfade)

        await self._enqueue_item(sonos_player, url=url, queue_item=queue_item)

    async def poll_player(self, player_id: str) -> None:
        """Poll player for state updates.

        This is called by the Player Manager;
        - every 360 seconds if the player if not powered
        - every 30 seconds if the player is powered
        - every 10 seconds if the player is playing

        Use this method to request any info that is not automatically updated and/or
        to detect if the player is still alive.
        If this method raises the PlayerUnavailable exception,
        the player is marked as unavailable until
        the next successful poll or event where it becomes available again.
        If the player does not need any polling, simply do not override this method.
        """
        if player_id not in self.sonosplayers:
            return
        sonos_player = self.sonosplayers[player_id]
        try:
            # the check_poll logic will work out what endpoints need polling now
            # based on when we last received info from the device
            await sonos_player.check_poll()
            # always update the attributes
            await self._update_player(sonos_player, signal_update=False)
        except ConnectionResetError as err:
            raise PlayerUnavailableError from err

    async def _run_discovery(self) -> None:
        """Discover Sonos players on the network."""
        if self._discovery_running:
            return
        try:
            self._discovery_running = True
            self.logger.debug("Sonos discovery started...")
            discovered_devices: set[soco.SoCo] = await asyncio.to_thread(
                soco.discover, allow_network_scan=self.config.get_value(CONF_NETWORK_SCAN)
            )
            if discovered_devices is None:
                discovered_devices = set()
            new_device_ids = {item.uid for item in discovered_devices}
            cur_player_ids = set(self.sonosplayers.keys())
            added_devices = new_device_ids.difference(cur_player_ids)

            # process new players
            for device in discovered_devices:
                if device.uid not in added_devices:
                    continue
                try:
                    await self._device_discovered(device)
                except Exception as err:
                    self.logger.exception(str(err), exc_info=err)

        finally:
            self._discovery_running = False

        def reschedule():
            self._discovery_reschedule_timer = None
            self.mass.create_task(self._run_discovery())

        # reschedule self once finished
        self._discovery_reschedule_timer = self.mass.loop.call_later(300, reschedule)

    async def _device_discovered(self, soco_device: soco.SoCo) -> None:
        """Handle discovered Sonos player."""
        player_id = soco_device.uid

        enabled = self.mass.config.get(f"{CONF_PLAYERS}/{player_id}/enabled", True)
        if not enabled:
            self.logger.debug("Ignoring disabled player: %s", player_id)
            return

        speaker_info = await asyncio.to_thread(soco_device.get_speaker_info, True)
        assert player_id not in self.sonosplayers

        if soco_device not in soco_device.visible_zones:
            return

        sonos_player = SonosPlayer(
            player_id=player_id,
            soco=soco_device,
            player=Player(
                player_id=soco_device.uid,
                provider=self.domain,
                type=PlayerType.PLAYER,
                name=soco_device.player_name,
                available=True,
                powered=False,
                supported_features=PLAYER_FEATURES,
                device_info=DeviceInfo(
                    model=speaker_info["model_name"],
                    address=soco_device.ip_address,
                    manufacturer=self.name,
                ),
                max_sample_rate=441000,
                supports_24bit=False,
            ),
            speaker_info=speaker_info,
            speaker_info_updated=time.time(),
        )
        if speaker_info["model_name"] in HIRES_MODELS:
            sonos_player.player.max_sample_rate = 48000
            sonos_player.player.supports_24bit = True

        # poll all endpoints once and update attributes
        await sonos_player.check_poll()
        sonos_player.update_attributes()

        # handle subscriptions to events
        def subscribe(service, _callback):
            queue = ProcessSonosEventQueue(sonos_player, _callback)
            sub = service.subscribe(auto_renew=True, event_queue=queue)
            sonos_player.subscriptions.append(sub)

        subscribe(soco_device.avTransport, self._handle_av_transport_event)
        subscribe(soco_device.renderingControl, self._handle_rendering_control_event)
        subscribe(soco_device.zoneGroupTopology, self._handle_zone_group_topology_event)

        self.sonosplayers[player_id] = sonos_player

        self.mass.players.register_or_update(sonos_player.player)

    def _handle_av_transport_event(self, sonos_player: SonosPlayer, event: SonosEvent):
        """Handle a soco.SoCo AVTransport event."""
        if self.mass.closing:
            return
        self.logger.debug("Received AVTransport event for Player %s", sonos_player.soco.player_name)

        if "transport_state" in event.variables:
            new_state = event.variables["transport_state"]
            if new_state == "TRANSITIONING":
                return
            sonos_player.transport_info["current_transport_state"] = new_state

        if "current_track_uri" in event.variables:
            sonos_player.transport_info["uri"] = event.variables["current_track_uri"]

        sonos_player.transport_info_updated = time.time()
        asyncio.run_coroutine_threadsafe(self._update_player(sonos_player), self.mass.loop)

    def _handle_rendering_control_event(self, sonos_player: SonosPlayer, event: SonosEvent):
        """Handle a soco.SoCo RenderingControl event."""
        if self.mass.closing:
            return
        self.logger.debug(
            "Received RenderingControl event for Player %s",
            sonos_player.soco.player_name,
        )
        if "volume" in event.variables:
            sonos_player.rendering_control_info["volume"] = event.variables["volume"]["Master"]
        if "mute" in event.variables:
            sonos_player.rendering_control_info["mute"] = bool(event.variables["mute"]["Master"])
        sonos_player.rendering_control_info_updated = time.time()
        asyncio.run_coroutine_threadsafe(self._update_player(sonos_player), self.mass.loop)

    def _handle_zone_group_topology_event(
        self, sonos_player: SonosPlayer, event: SonosEvent  # noqa: ARG002
    ):
        """Handle a soco.SoCo ZoneGroupTopology event."""
        if self.mass.closing:
            return
        self.logger.debug(
            "Received ZoneGroupTopology event for Player %s",
            sonos_player.soco.player_name,
        )
        sonos_player.group_info = sonos_player.soco.group
        sonos_player.group_info_updated = time.time()
        asyncio.run_coroutine_threadsafe(self._update_player(sonos_player), self.mass.loop)

    async def _enqueue_item(
        self,
        sonos_player: SonosPlayer,
        url: str,
        queue_item: QueueItem,
    ) -> None:
        """Enqueue a queue item to the Sonos player Queue."""
        metadata = create_didl_metadata(self.mass, url, queue_item)
        await asyncio.to_thread(
            sonos_player.soco.avTransport.AddURIToQueue,
            [
                ("InstanceID", 0),
                ("EnqueuedURI", url),
                ("EnqueuedURIMetaData", metadata),
                ("DesiredFirstTrackNumberEnqueued", 0),
                ("EnqueueAsNext", 0),
            ],
            timeout=60,
        )
        self.logger.debug(
            "Enqued track (%s) to player %s",
            queue_item.name if queue_item else url,
            sonos_player.player.display_name,
        )

    async def _update_player(self, sonos_player: SonosPlayer, signal_update: bool = True) -> None:
        """Update Sonos Player."""
        prev_url = sonos_player.player.current_item_id
        prev_state = sonos_player.player.state
        sonos_player.update_attributes()
        sonos_player.player.can_sync_with = tuple(
            x for x in self.sonosplayers if x != sonos_player.player_id
        )
        current_url = sonos_player.player.current_item_id
        current_state = sonos_player.player.state

        if (prev_url != current_url) or (prev_state != current_state):
            # fetch track details on state or url change
            await asyncio.to_thread(
                sonos_player.update_info,
                update_track_info=True,
            )
            sonos_player.update_attributes()

        if signal_update:
            # send update to the player manager right away only if we are triggered from an event
            # when we're just updating from a manual poll, the player manager
            # will detect changes to the player object itself
            self.mass.players.update(sonos_player.player_id)


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


class ProcessSonosEventQueue:
    """Queue like object for dispatching sonos events."""

    def __init__(
        self,
        sonos_player: SonosPlayer,
        callback_handler: callable[[SonosPlayer, dict], None],
    ) -> None:
        """Initialize Sonos event queue."""
        self._callback_handler = callback_handler
        self._sonos_player = sonos_player

    def put(self, info: Any, block=True, timeout=None) -> None:  # noqa: ARG002
        """Process event."""
        # noqa: ARG001
        self._callback_handler(self._sonos_player, info)
