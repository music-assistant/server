"""Sample Player provider for Music Assistant."""
from __future__ import annotations

import asyncio
import logging
import time
import xml.etree.ElementTree as ET  # noqa: N817
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import soco
from soco.events_base import Event as SonosEvent
from soco.events_base import SubscriptionBase
from soco.groups import ZoneGroup

from music_assistant.common.models.config_entries import ConfigEntry, ConfigValueType
from music_assistant.common.models.enums import (
    ContentType,
    MediaType,
    PlayerFeature,
    PlayerState,
    PlayerType,
)
from music_assistant.common.models.errors import PlayerUnavailableError, QueueEmpty
from music_assistant.common.models.player import DeviceInfo, Player
from music_assistant.common.models.queue_item import QueueItem
from music_assistant.constants import CONF_PLAYERS
from music_assistant.server.helpers.didl_lite import create_didl_metadata
from music_assistant.server.models.player_provider import PlayerProvider

if TYPE_CHECKING:
    from music_assistant.common.models.config_entries import PlayerConfig, ProviderConfig
    from music_assistant.common.models.provider import ProviderManifest
    from music_assistant.server import MusicAssistant
    from music_assistant.server.models import ProviderInstanceType


PLAYER_FEATURES = (
    PlayerFeature.SET_MEMBERS,
    PlayerFeature.SYNC,
    PlayerFeature.VOLUME_MUTE,
    PlayerFeature.VOLUME_SET,
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
    return tuple()  # we do not have any config entries (yet)


@dataclass
class SonosPlayer:
    """Wrapper around Sonos/SoCo with some additional attributes."""

    player_id: str
    soco: soco.SoCo
    player: Player
    is_stereo_pair: bool = False
    next_item: str | None = None
    elapsed_time: int = 0
    current_item_id: str | None = None
    radio_mode_started: float | None = None

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
            self.track_info_updated = time.time()
            self.elapsed_time = _timespan_secs(self.track_info["position"]) or 0

            current_item_id = None
            if track_metadata := self.track_info.get("metadata"):
                # extract queue_item_id from metadata xml
                try:
                    xml_root = ET.XML(track_metadata)
                    for match in xml_root.iter("{http://purl.org/dc/elements/1.1/}queueItemId"):
                        item_id = match.text
                        current_item_id = item_id
                        break
                except (ET.ParseError, AttributeError):
                    pass
            self.current_item_id = current_item_id

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
        # generic attributes (speaker_info)
        self.player.name = self.speaker_info["zone_name"]
        self.player.volume_level = int(self.rendering_control_info["volume"])
        self.player.volume_muted = self.rendering_control_info["mute"]

        # transport info (playback state)
        current_transport_state = self.transport_info["current_transport_state"]
        new_state = _convert_state(current_transport_state)
        self.player.state = new_state

        # media info (track info)
        self.player.current_url = self.track_info["uri"]
        self.player.current_item_id = self.current_item_id

        if self.radio_mode_started is not None:
            # sonos reports bullshit elapsed time while playing radio,
            # trying to be "smart" and resetting the counter when new ICY metadata is detected
            if new_state == PlayerState.PLAYING:
                now = time.time()
                self.player.elapsed_time = int(now - self.radio_mode_started + 0.5)
                self.player.elapsed_time_last_updated = now
        else:
            self.player.elapsed_time = self.elapsed_time
            self.player.elapsed_time_last_updated = self.track_info_updated

        # zone topology (syncing/grouping) details
        if self.group_info and self.group_info.coordinator.uid == self.player_id:
            # this player is the sync leader
            self.player.synced_to = None
            self.player.group_childs = {
                x.uid for x in self.group_info.members if x.uid != self.player_id and x.is_visible
            }
            if not self.player.group_childs:
                self.player.type = PlayerType.STEREO_PAIR
        elif self.group_info and self.group_info.coordinator:
            # player is synced to
            self.player.synced_to = self.group_info.coordinator.uid

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

    sonosplayers: dict[str, SonosPlayer]
    _discovery_running: bool

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
        if hasattr(self, "sonosplayers"):
            for player in self.sonosplayers.values():
                player.soco.end_direct_control_session

    def on_player_config_changed(
        self, config: PlayerConfig, changed_keys: set[str]  # noqa: ARG002
    ) -> None:
        """Call (by config manager) when the configuration of a player changes."""
        # run discovery to catch any re-enabled players
        self.mass.create_task(self._run_discovery())

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

    async def cmd_play_media(
        self,
        player_id: str,
        queue_item: QueueItem,
        seek_position: int = 0,
        fade_in: bool = False,
        flow_mode: bool = False,
    ) -> None:
        """Send PLAY MEDIA command to given player."""
        sonos_player = self.sonosplayers[player_id]
        if not sonos_player.soco.is_coordinator:
            self.logger.debug(
                "Ignore PLAY_MEDIA command for %s: Player is synced to another player.",
                player_id,
            )
            return
        # always stop and clear queue first
        sonos_player.next_item = None
        await asyncio.to_thread(sonos_player.soco.stop)
        await asyncio.to_thread(sonos_player.soco.clear_queue)

        radio_mode = (
            flow_mode or not queue_item.duration or queue_item.media_type == MediaType.RADIO
        )
        url = await self.mass.streams.resolve_stream_url(
            queue_item=queue_item,
            player_id=sonos_player.player_id,
            seek_position=seek_position,
            fade_in=fade_in,
            flow_mode=flow_mode,
            output_codec=ContentType.MP3 if radio_mode else None,
        )
        if radio_mode:
            sonos_player.radio_mode_started = time.time()
            url = url.replace("http", "x-rincon-mp3radio")
            metadata = create_didl_metadata(self.mass, url, queue_item, flow_mode)
            # sonos does multiple get requests if no duration is known
            # our stream engine does not like that, hence the workaround
            self.mass.streams.workaround_players.add(sonos_player.player_id)
            await asyncio.to_thread(sonos_player.soco.play_uri, url, meta=metadata)
        else:
            sonos_player.radio_mode_started = None
            await self._enqueue_item(
                sonos_player, queue_item=queue_item, url=url, flow_mode=flow_mode
            )
            await asyncio.to_thread(sonos_player.soco.play_from_queue, 0)

    async def cmd_pause(self, player_id: str) -> None:
        """Send PAUSE command to given player."""
        sonos_player = self.sonosplayers[player_id]
        if not sonos_player.soco.is_coordinator:
            self.logger.debug(
                "Ignore PLAY command for %s: Player is synced to another player.",
                player_id,
            )
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
        await asyncio.to_thread(sonos_player.soco.join, self.sonosplayers[target_player].soco)

    async def cmd_unsync(self, player_id: str) -> None:
        """Handle UNSYNC command for given player.

        Remove the given player from any syncgroups it currently is synced to.

            - player_id: player_id of the player to handle the command.
        """
        sonos_player = self.sonosplayers[player_id]
        await asyncio.to_thread(sonos_player.soco.unjoin)

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
                soco.discover, 120, allow_network_scan=True
            )
            if discovered_devices is None:
                discovered_devices = set()
            new_device_ids = {item.uid for item in discovered_devices}
            cur_player_ids = set(self.sonosplayers.keys())
            added_devices = new_device_ids.difference(cur_player_ids)
            removed_devices = cur_player_ids.difference(new_device_ids)

            # mark any disconnected players as unavailable...
            for player_id in removed_devices:
                if player := self.mass.players.get(player_id):
                    player.available = False
                    self.mass.players.update(player_id)

            # process new players
            for device in discovered_devices:
                if device.uid not in added_devices:
                    continue
                await self._device_discovered(device)

        finally:
            self._discovery_running = False

        def reschedule():
            self.mass.create_task(self._run_discovery())

        # reschedule self once finished
        self.mass.loop.call_later(300, reschedule)

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
                powered=True,
                supported_features=PLAYER_FEATURES,
                device_info=DeviceInfo(
                    model=speaker_info["model_name"],
                    address=speaker_info["mac_address"],
                    manufacturer=self.name,
                ),
                max_sample_rate=48000,
            ),
            speaker_info=speaker_info,
            speaker_info_updated=time.time(),
        )
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

    async def _enqueue_next_track(
        self, sonos_player: SonosPlayer, current_queue_item_id: str
    ) -> None:
        """Enqueue the next track of the MA queue on the CC queue."""
        if not current_queue_item_id:
            return  # guard
        if not self.mass.players.queues.get_item(sonos_player.player_id, current_queue_item_id):
            return  # guard
        try:
            next_item, crossfade = await self.mass.players.queues.player_ready_for_next_track(
                sonos_player.player_id, current_queue_item_id
            )
        except QueueEmpty:
            return

        if sonos_player.next_item == next_item.queue_item_id:
            return  # already set ?!
        sonos_player.next_item = next_item.queue_item_id

        # set crossfade according to queue mode
        if sonos_player.soco.cross_fade != crossfade:

            def set_crossfade():
                with suppress(Exception):
                    sonos_player.soco.cross_fade = crossfade

            await asyncio.to_thread(set_crossfade)

        # send queue item to sonos queue
        is_radio = next_item.media_type != MediaType.TRACK
        url = await self.mass.streams.resolve_stream_url(
            queue_item=next_item,
            player_id=sonos_player.player_id,
            # Sonos pre-caches pretty aggressively so do not yet start the runner
            auto_start_runner=False,
            output_codec=ContentType.MP3 if is_radio else None,
        )
        await self._enqueue_item(sonos_player, queue_item=next_item, url=url)

    async def _enqueue_item(
        self,
        sonos_player: SonosPlayer,
        queue_item: QueueItem,
        url: str,
        flow_mode: bool = False,
    ) -> None:
        """Enqueue a queue item to the Sonos player Queue."""
        metadata = create_didl_metadata(self.mass, url, queue_item, flow_mode)
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
        if sonos_player.player_id in self.mass.streams.workaround_players:
            self.mass.streams.workaround_players.remove(sonos_player.player_id)
        self.logger.debug(
            "Enqued track (%s) to player %s",
            queue_item.name,
            sonos_player.player.display_name,
        )

    async def _update_player(self, sonos_player: SonosPlayer, signal_update: bool = True) -> None:
        """Update Sonos Player."""
        prev_item_id = sonos_player.current_item_id
        prev_url = sonos_player.player.current_url
        prev_state = sonos_player.player.state
        sonos_player.update_attributes()
        sonos_player.player.can_sync_with = tuple(
            x for x in self.sonosplayers if x != sonos_player.player_id
        )
        current_url = sonos_player.player.current_url
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
            # when we're just updating from a manual poll, the player manager will
            # update will detect changes to the player object itself
            self.mass.players.update(sonos_player.player_id)

        # enqueue next item if needed
        if sonos_player.player.state == PlayerState.PLAYING and (
            prev_item_id != sonos_player.current_item_id
            or not sonos_player.next_item
            or sonos_player.next_item == sonos_player.current_item_id
        ):
            self.mass.create_task(
                self._enqueue_next_track(sonos_player, sonos_player.current_item_id)
            )


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
