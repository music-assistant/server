"""Sample Player provider for Music Assistant"""
from __future__ import annotations
import asyncio
from dataclasses import dataclass, field
import datetime
import time
import logging
from contextlib import suppress
from typing import Any
from soco.events_base import Event as SonosEvent, SubscriptionBase
import xml.etree.ElementTree as ET
import soco
from soco.groups import ZoneGroup

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
from music_assistant.server.models.player_provider import PlayerProvider

from soco import SoCo

DUMMY_PLAYER_ID = "dummy_player"
PLAYER_CONFIG_ENTRIES = tuple()

PLAYER_FEATURES = (
    PlayerFeature.SET_MEMBERS,
    PlayerFeature.SYNC,
    PlayerFeature.VOLUME_MUTE,
    PlayerFeature.VOLUME_SET,
)
PLAYER_CONFIG_ENTRIES = tuple()  # we don't have any player config entries (for now)


@dataclass
class SonosPlayer:
    """Wrapper around Sonos/SoCo with some additional attributes."""

    player_id: str
    soco: SoCo
    player: Player
    is_stereo_pair: bool = False
    next_item: str | None = None
    elapsed_time: int = 0
    current_item_id: str | None = None

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
            print("### POLLING TRANSPORT INFO FOR %s" % self.player.display_name)
            transport_info = self.soco.get_current_transport_info()
            if transport_info.get("current_transport_state") != "TRANSITIONING":
                self.transport_info = transport_info
                self.transport_info_updated = time.time()
        # track info
        if update_track_info:
            print("### POLLING TRACK INFO FOR %s" % self.player.display_name)
            self.track_info = self.soco.get_current_track_info()
            self.track_info_updated = time.time()
            self.elapsed_time = _timespan_secs(self.track_info["position"]) or 0
            try:
                # extract queue_item_id from metadata xml
                xml_root = ET.XML(self.track_info.get("metadata"))
                for match in xml_root.iter(
                    "{urn:schemas-upnp-org:metadata-1-0/upnp/}queueItemId"
                ):
                    item_id = match.text
                    self.current_item_id = item_id
                    break
            except (ET.ParseError, AttributeError):
                self.current_item_id = None
        # speaker info
        if update_speaker_info:
            print("### POLLING SPEAKER INFO FOR %s" % self.player.display_name)
            self.speaker_info = self.soco.get_speaker_info()
            self.speaker_info_updated = time.time()
        # rendering control info
        if update_rendering_control_info:
            print("### POLLING RENDERCONTROL INFO FOR %s" % self.player.display_name)
            self.rendering_control_info["volume"] = self.soco.volume
            self.rendering_control_info["mute"] = self.soco.mute
            self.rendering_control_info_updated = time.time()
        # group info
        if update_group_info:
            print("### POLLING GROUP INFO FOR %s" % self.player.display_name)
            self.group_info = self.soco.group
            self.group_info_updated = time.time()

    def update_attributes(self):
        """Update attributes of the MA Player from SoCo state."""
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
        self.player.elapsed_time = self.elapsed_time
        self.player.elapsed_time_last_updated = self.track_info_updated

        # zone topology (syncing/grouping) details
        if self.group_info.coordinator.uid == self.player_id:
            # this player is the sync leader
            self.player.synced_to = None
            self.player.group_childs = {
                x.uid for x in self.group_info.members if x.uid != self.player_id
            }
        else:
            # player is synced to
            self.player.synced_to = self.group_info.coordinator.uid

    async def check_poll(self) -> bool:
        """Check if any of the endpoints needs to be polled for info."""
        cur_time = time.time()
        update_transport_info = (cur_time - self.transport_info_updated) > 30
        update_track_info = (
            self.transport_info.get("current_transport_state") == "PLAYING"
            and (cur_time - self.track_info_updated) > 10
        ) or (cur_time - self.track_info_updated) > 600
        update_speaker_info = (cur_time - self.speaker_info_updated) > 300
        update_rendering_control_info = (
            cur_time - self.rendering_control_info_updated
        ) > 30
        update_group_info = (cur_time - self.group_info_updated) > 600

        if not (
            update_transport_info
            or update_track_info
            or update_speaker_info
            or update_rendering_control_info
            or update_group_info
        ):
            return False

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            self.update_info,
            update_transport_info,
            update_track_info,
            update_speaker_info,
            update_rendering_control_info,
            update_group_info,
        )
        return True


class SonosPlayerProvider(PlayerProvider):
    """Sonos Player provider."""

    sonosplayers: dict[str, SonosPlayer] | None = None
    _discovery_running: bool = False

    async def setup(self) -> None:
        """Handle async initialization of the provider."""
        self.sonosplayers = {}
        # silence the soco logger a bit
        logging.getLogger("soco").setLevel(logging.INFO)
        self.mass.create_task(self._run_discovery())

    async def close(self) -> None:
        """Handle close/cleanup of the provider."""

    async def cmd_stop(self, player_id: str) -> None:
        """
        Send STOP command to given player.
            - player_id: player_id of the player to handle the command.
        """
        sonos_player = self.sonosplayers[player_id]
        if not sonos_player.soco.is_coordinator:
            self.logger.debug(
                "Ignore STOP command for %s: Player is synced to another player.",
                player_id,
            )
            return
        # clear queue instead of stop
        await self.mass.run_in_executor(sonos_player.soco.clear_queue)

    async def cmd_play(self, player_id: str) -> None:
        """
        Send PLAY command to given player.
            - player_id: player_id of the player to handle the command.
        """
        sonos_player = self.sonosplayers[player_id]
        if not sonos_player.soco.is_coordinator:
            self.logger.debug(
                "Ignore PLAY command for %s: Player is synced to another player.",
                player_id,
            )
            return
        await self.mass.run_in_executor(sonos_player.soco.play)

    async def cmd_play_media(
        self,
        player_id: str,
        queue_item: QueueItem,
        seek_position: int = 0,
        fade_in: bool = False,
    ) -> None:
        """Send PLAY MEDIA command to given player."""
        sonos_player = self.sonosplayers[player_id]
        if not sonos_player.soco.is_coordinator:
            self.logger.debug(
                "Ignore PLAY_MEDIA command for %s: Player is synced to another player.",
                player_id,
            )
            return
        # always clear queue first
        sonos_player.next_item = None
        await self.mass.run_in_executor(sonos_player.soco.clear_queue)

        is_radio = queue_item.media_type != MediaType.TRACK
        url = await self.mass.streams.resolve_stream_url(
            queue_item=queue_item,
            player_id=sonos_player.player_id,
            seek_position=seek_position,
            fade_in=fade_in,
            content_type=ContentType.MP3 if is_radio else ContentType.FLAC,
        )

        await self._enqueue_item(sonos_player, queue_item=queue_item, url=url)
        await self.mass.run_in_executor(sonos_player.soco.play_from_queue, 0)

    async def cmd_pause(self, player_id: str) -> None:
        """Send PAUSE command to given player."""
        sonos_player = self.sonosplayers[player_id]
        if not sonos_player.soco.is_coordinator:
            self.logger.debug(
                "Ignore PLAY command for %s: Player is synced to another player.",
                player_id,
            )
            return
        await self.mass.run_in_executor(sonos_player.soco.pause)

    async def cmd_volume_set(self, player_id: str, volume_level: int) -> None:
        """Send VOLUME_SET command to given player."""

        def set_volume_level(player_id: str, volume_level: int) -> None:
            sonos_player = self.sonosplayers[player_id]
            sonos_player.soco.volume = volume_level

        await self.mass.run_in_executor(set_volume_level, player_id, volume_level)

    async def cmd_volume_mute(self, player_id: str, muted: bool) -> None:
        """Send VOLUME MUTE command to given player."""

        def set_volume_mute(player_id: str, muted: bool) -> None:
            sonos_player = self.sonosplayers[player_id]
            sonos_player.soco.mute = muted

        await self.mass.run_in_executor(set_volume_mute, player_id, muted)

    async def poll_player(self, player_id: str) -> None:
        """
        Poll player for state updates.

        This is called by the Player Manager;
        - every 360 secods if the player if not powered
        - every 30 seconds if the player is powered
        - every 10 seconds if the player is playing

        Use this method to request any info that is not automatically updated and/or
        to detect if the player is still alive.
        If this method raises the PlayerUnavailable exception,
        the player is marked as unavailable until
        the next succesfull poll or event where it becomes available again.
        If the player does not need any polling, simply do not override this method.
        """
        sonos_player = self.sonosplayers[player_id]
        try:
            await sonos_player.check_poll()
        except ConnectionResetError as err:
            raise PlayerUnavailableError from err

    async def _run_discovery(self) -> None:
        """Discover Sonos players on the network."""
        if self._discovery_running:
            return
        try:
            self._discovery_running = True
            self.logger.debug("Sonos discovery started...")
            discovered_devices: set[SoCo] = await self.mass.run_in_executor(
                soco.discover, 10
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

            # handle groups
            # if soco_player := next(iter(discovered_devices), None):
            #     self._process_groups(soco_player.all_groups)
            # else:
            #         self._process_groups(set())

        finally:
            self._discovery_running = False

        def reschedule():
            self.mass.create_task(self._run_discovery())

        # reschedule self once finished
        self.mass.loop.call_later(300, reschedule)

    async def _device_discovered(self, soco_device: soco.SoCo) -> None:
        """Handle discovered Sonos player."""
        player_id = soco_device.uid
        speaker_info = await self.mass.run_in_executor(
            soco_device.get_speaker_info, True
        )
        assert player_id not in self.sonosplayers

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

        self.mass.players.register(sonos_player.player)

    def _handle_av_transport_event(self, sonos_player: SonosPlayer, event: SonosEvent):
        """Handle a SoCo AVTransport event."""
        self.logger.debug(
            "AVTransport event for Player %s: %s",
            sonos_player.soco.player_name,
            event.variables,
        )

        if "transport_state" in event.variables:
            new_state = event.variables["transport_state"]
            if new_state == "TRANSITIONING":
                return
            sonos_player.transport_info["current_transport_state"] = new_state

        if "current_track_uri" in event.variables:
            sonos_player.transport_info["uri"] = event.variables["current_track_uri"]

        sonos_player.transport_info_updated = time.time()
        asyncio.run_coroutine_threadsafe(
            self._update_player(sonos_player), self.mass.loop
        )

    def _handle_rendering_control_event(
        self, sonos_player: SonosPlayer, event: SonosEvent
    ):
        """Handle a SoCo RenderingControl event."""
        self.logger.debug(
            "RenderingControl event for Player %s: %s",
            sonos_player.soco.player_name,
            event.variables,
        )
        if "volume" in event.variables:
            sonos_player.rendering_control_info["volume"] = event.variables["volume"][
                "Master"
            ]
        if "mute" in event.variables:
            sonos_player.rendering_control_info["mute"] = bool(
                event.variables["mute"]["Master"]
            )
        sonos_player.rendering_control_info_updated = time.time()
        asyncio.run_coroutine_threadsafe(
            self._update_player(sonos_player), self.mass.loop
        )

    def _handle_zone_group_topology_event(
        self, sonos_player: SonosPlayer, event: SonosEvent
    ):
        """Handle a SoCo ZoneGroupTopology event."""
        self.logger.debug(
            "ZoneGroupTopology event for Player %s: %s",
            sonos_player.soco.player_name,
            event.variables,
        )
        sonos_player.group_info = sonos_player.soco.group
        sonos_player.group_info_updated = time.time()
        asyncio.run_coroutine_threadsafe(
            self._update_player(sonos_player), self.mass.loop
        )

    def _process_groups(self, sonos_groups: list[soco.SoCo]) -> None:
        """Process all sonos groups."""
        all_group_ids = set()
        for sonos_player in sonos_groups:
            all_group_ids.add(sonos_player.uid)
            if sonos_player.uid not in self.sonosplayers:
                # unknown player ?!
                continue

            # mass_player = self.mass.players.get(sonos_player.uid)
            # sonos_player.is_coordinator
            # # check members
            # group_player.is_group_player = True
            # group_player.name = group.label
            # group_player.group_childs = [item.uid for item in group.members]
            # create_task(self.mass.players.update_player(group_player))

    async def _enqueue_next_track(
        self, sonos_player: SonosPlayer, current_queue_item_id: str
    ) -> None:
        """Enqueue the next track of the MA queue on the CC queue."""
        if not current_queue_item_id:
            return  # guard
        if not self.mass.players.queues.get_item(
            sonos_player.player_id, current_queue_item_id
        ):
            return  # guard
        try:
            next_item, crossfade = self.mass.players.queues.player_ready_for_next_track(
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

            await self.mass.run_in_executor(set_crossfade)

        # send queue item to sonos queue
        is_radio = next_item.media_type != MediaType.TRACK
        url = await self.mass.streams.resolve_stream_url(
            queue_item=next_item,
            player_id=sonos_player.player_id,
            content_type=ContentType.MP3 if is_radio else ContentType.FLAC,
            # Sonos pre-caches pretty aggressively so do not yet start the runner
            auto_start_runner=False,
        )
        await self._enqueue_item(sonos_player, queue_item=next_item, url=url)

    async def _enqueue_item(
        self,
        sonos_player: SonosPlayer,
        queue_item: QueueItem,
        url: str,
    ) -> None:
        """Enqueue a queue item to the Sonos player Queue."""
        is_radio = queue_item.media_type != MediaType.TRACK

        metadata = _create_didl_metadata(url, queue_item, is_radio)
        await self.mass.run_in_executor(
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
            queue_item.name,
            sonos_player.player.display_name,
        )

    async def _update_player(self, sonos_player: SonosPlayer) -> None:
        """Update Sonos Player."""
        prev_item_id = sonos_player.current_item_id
        prev_url = sonos_player.player.current_url
        prev_state = sonos_player.player.state
        sonos_player.update_attributes()
        current_url = sonos_player.player.current_url
        current_state = sonos_player.player.state

        if (prev_url != current_url) or (prev_state != current_state):
            # fetch track details on state or url change
            await self.mass.run_in_executor(
                sonos_player.update_info,
                update_track_info=True,
            )
            sonos_player.update_attributes()

        self.mass.players.update(sonos_player.player_id)
        if sonos_player.player.state == PlayerState.PLAYING and (
            prev_item_id != sonos_player.current_item_id or not sonos_player.next_item
        ):
            # queue item changed, enqueue next item
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


def _create_didl_metadata(url: str, queue_item: QueueItem, radio: bool = False) -> str:
    """Create DIDL metadata string from url and QueueItem."""
    ext = url.split(".")[-1]
    if radio:
        return (
            '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" xmlns:dlna="urn:schemas-dlna-org:metadata-1-0/">'
            f'<item id="{queue_item.queue_item_id}" parentID="0" restricted="1">'
            f"<dc:title>{_escape_str(queue_item.name)}</dc:title>"
            "<dc:creator></dc:creator>"
            "<upnp:album></upnp:album>"
            f"<upnp:albumArtURI>{queue_item.image.url}</upnp:albumArtURI>"
            "<upnp:channelName>Music Assistant</upnp:channelName>"
            "<upnp:channelNr>0</upnp:channelNr>"
            f"<upnp:id>{queue_item.queue_item_id}</upnp:id>"
            "<upnp:class>object.item.audioItem.audioBroadcast</upnp:class>"
            f'<res protocolInfo="x-rincon-mp3radio:*:audio/{ext}:DLNA.ORG_OP=00;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=0d500000000000000000000000000000">{url}</res>'
            "</item>"
            "</DIDL-Lite>"
        )
    title = _escape_str(queue_item.media_item.name)
    artist = _escape_str(queue_item.media_item.artist.name)
    album = _escape_str(queue_item.media_item.album.name)
    item_class = "object.item.audioItem.musicTrack"
    duration_str = str(datetime.timedelta(seconds=queue_item.duration))
    return (
        '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" xmlns:dlna="urn:schemas-dlna-org:metadata-1-0/">'
        f'<item id="{queue_item.queue_item_id}" parentID="0" restricted="1">'
        f"<dc:title>{title}</dc:title>"
        f"<dc:creator>{artist}</dc:creator>"
        f"<upnp:album>{album}</upnp:album>"
        f"<upnp:artist>{artist}</upnp:artist>"
        "<upnp:canSkip>false</upnp:canSkip>"
        f"<upnp:duration>{queue_item.duration}</upnp:duration>"
        f"<upnp:channelName>Music Assistant</upnp:channelName>"
        "<upnp:channelNr>0</upnp:channelNr>"
        f"<upnp:queueItemId>{queue_item.queue_item_id}</upnp:queueItemId>"
        f"<upnp:albumArtURI>{queue_item.image.url}</upnp:albumArtURI>"
        f"<upnp:class>{item_class}</upnp:class>"
        f"<upnp:mimeType>audio/{ext}</upnp:mimeType>"
        f'<res duration="{duration_str}" protocolInfo="sonos.com-http:*:audio/{ext}:*:DLNA.ORG_OP=00;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=0d500000000000000000000000000000">{url}</res>'
        "</item>"
        "</DIDL-Lite>"
    )


def _timespan_secs(timespan):
    """Parse a time-span into number of seconds."""
    if timespan in ("", "NOT_IMPLEMENTED", None):
        return None
    return sum(60 ** x[0] * int(x[1]) for x in enumerate(reversed(timespan.split(":"))))


def _escape_str(data: str) -> str:
    """Create DIDL-safe string."""
    data = data.replace("&", "&amp;")
    data = data.replace(">", "&gt;")
    data = data.replace("<", "&lt;")
    return data


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

    def put(self, info: Any, block=True, timeout=None) -> None:
        """Process event."""
        # pylint: disable=unused-argument
        self._callback_handler(self._sonos_player, info)
