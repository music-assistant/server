"""
Sonos Player provider for Music Assistant.

Note that large parts of this code are copied over from the Home Assistant
integratioon for Sonos.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import soco.config as soco_config
from requests.exceptions import RequestException
from soco import events_asyncio, zonegroupstate
from soco.discovery import discover

from music_assistant.common.models.config_entries import (
    CONF_ENTRY_CROSSFADE,
    ConfigEntry,
    ConfigValueType,
)
from music_assistant.common.models.enums import (
    ConfigEntryType,
    ContentType,
    PlayerFeature,
    PlayerType,
    ProviderFeature,
)
from music_assistant.common.models.errors import PlayerCommandFailed, PlayerUnavailableError
from music_assistant.common.models.player import DeviceInfo, Player
from music_assistant.constants import CONF_CROSSFADE
from music_assistant.server.helpers.didl_lite import create_didl_metadata
from music_assistant.server.models.player_provider import PlayerProvider

from .player import SonosPlayer

if TYPE_CHECKING:
    from soco.core import SoCo

    from music_assistant.common.models.config_entries import PlayerConfig, ProviderConfig
    from music_assistant.common.models.provider import ProviderManifest
    from music_assistant.common.models.queue_item import QueueItem
    from music_assistant.server import MusicAssistant
    from music_assistant.server.controllers.streams import MultiClientStreamJob
    from music_assistant.server.models import ProviderInstanceType


PLAYER_FEATURES = (
    PlayerFeature.SYNC,
    PlayerFeature.VOLUME_MUTE,
    PlayerFeature.VOLUME_SET,
    PlayerFeature.ENQUEUE_NEXT,
    PlayerFeature.PAUSE,
)

CONF_NETWORK_SCAN = "network_scan"
SUBSCRIPTION_TIMEOUT = 1200
ZGS_SUBSCRIPTION_TIMEOUT = 2


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
    "Sonos Era 100",
    "Sonos Era 300",
)


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    # set event listener port to something other than 1400
    # to allow coextistence with HA on the same host
    soco_config.EVENT_LISTENER_PORT = 1700
    soco_config.EVENTS_MODULE = events_asyncio
    soco_config.REQUEST_TIMEOUT = 9.5
    soco_config.ZGT_EVENT_FALLBACK = False
    zonegroupstate.EVENT_CACHE_TIMEOUT = SUBSCRIPTION_TIMEOUT
    prov = SonosPlayerProvider(mass, manifest, config)
    # set-up soco logging
    if prov.log_level == "VERBOSE":
        logging.getLogger("soco").setLevel(logging.DEBUG)
        logging.getLogger("urllib3.connectionpool").setLevel(logging.INFO)
    else:
        logging.getLogger("pychromecast").setLevel(prov.logger.level + 10)
        logging.getLogger("urllib3.connectionpool").setLevel(logging.WARNING)
    await prov.handle_async_init()
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
class UnjoinData:
    """Class to track data necessary for unjoin coalescing."""

    players: list[SonosPlayer]
    event: asyncio.Event = field(default_factory=asyncio.Event)


class SonosPlayerProvider(PlayerProvider):
    """Sonos Player provider."""

    sonosplayers: dict[str, SonosPlayer] | None = None
    _discovery_running: bool = False
    _discovery_reschedule_timer: asyncio.TimerHandle | None = None

    @property
    def supported_features(self) -> tuple[ProviderFeature, ...]:
        """Return the features supported by this Provider."""
        return (ProviderFeature.SYNC_PLAYERS,)

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self.sonosplayers: OrderedDict[str, SonosPlayer] = OrderedDict()
        self.topology_condition = asyncio.Condition()
        self.boot_counts: dict[str, int] = {}
        self.mdns_names: dict[str, str] = {}
        self.unjoin_data: dict[str, UnjoinData] = {}
        self._discovery_running = False
        self.hosts_in_error: dict[str, bool] = {}
        self.discovery_lock = asyncio.Lock()
        self.creation_lock = asyncio.Lock()
        self._known_invisible: set[SoCo] = set()

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        await self._run_discovery()

    async def unload(self) -> None:
        """Handle close/cleanup of the provider."""
        if self._discovery_reschedule_timer:
            self._discovery_reschedule_timer.cancel()
            self._discovery_reschedule_timer = None
        # await any in-progress discovery
        while self._discovery_running:
            await asyncio.sleep(0.5)
        await asyncio.gather(*(player.offline() for player in self.sonosplayers.values()))
        if events_asyncio.event_listener:
            await events_asyncio.event_listener.async_stop()
        self.sonosplayers = None

    async def get_player_config_entries(
        self,
        player_id: str,
    ) -> tuple[ConfigEntry, ...]:
        """Return Config Entries for the given player."""
        base_entries = await super().get_player_config_entries(player_id)
        if not (sonos_player := self.sonosplayers.get(player_id)):
            # most probably a syncgroup
            return base_entries
        return (
            *base_entries,
            CONF_ENTRY_CROSSFADE,
            ConfigEntry(
                key="sonos_bass",
                type=ConfigEntryType.INTEGER,
                label="Bass",
                default_value=sonos_player.bass,
                value=sonos_player.bass,
                range=(-10, 10),
                description="Set the Bass level for the Sonos player",
                advanced=True,
            ),
            ConfigEntry(
                key="sonos_treble",
                type=ConfigEntryType.INTEGER,
                label="Treble",
                default_value=sonos_player.treble,
                value=sonos_player.treble,
                range=(-10, 10),
                description="Set the Treble level for the Sonos player",
                advanced=True,
            ),
            ConfigEntry(
                key="sonos_loudness",
                type=ConfigEntryType.BOOLEAN,
                label="Loudness compensation",
                default_value=sonos_player.loudness,
                value=sonos_player.loudness,
                description="Enable loudness compensation on the Sonos player",
                advanced=True,
            ),
        )

    def on_player_config_changed(
        self,
        config: PlayerConfig,
        changed_keys: set[str],
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

    def is_device_invisible(self, ip_address: str) -> bool:
        """Check if device at provided IP is known to be invisible."""
        return any(x for x in self._known_invisible if x.ip_address == ip_address)

    async def cmd_stop(self, player_id: str) -> None:
        """Send STOP command to given player."""
        sonos_player = self.sonosplayers[player_id]
        if sonos_player.sync_coordinator:
            self.logger.debug(
                "Ignore STOP command for %s: Player is synced to another player.",
                player_id,
            )
            return
        await self.mass.create_task(sonos_player.soco.stop)

    async def cmd_play(self, player_id: str) -> None:
        """Send PLAY command to given player."""
        sonos_player = self.sonosplayers[player_id]
        if sonos_player.sync_coordinator:
            self.logger.debug(
                "Ignore PLAY command for %s: Player is synced to another player.",
                player_id,
            )
            return
        await self.mass.create_task(sonos_player.soco.play)

    async def cmd_pause(self, player_id: str) -> None:
        """Send PAUSE command to given player."""
        sonos_player = self.sonosplayers[player_id]
        if sonos_player.sync_coordinator:
            self.logger.debug(
                "Ignore PLAY command for %s: Player is synced to another player.",
                player_id,
            )
            return
        if "Pause" not in sonos_player.soco.available_actions:
            # pause not possible
            await self.cmd_stop(player_id)
            return
        await self.mass.create_task(sonos_player.soco.pause)

    async def cmd_volume_set(self, player_id: str, volume_level: int) -> None:
        """Send VOLUME_SET command to given player."""

        def set_volume_level(player_id: str, volume_level: int) -> None:
            sonos_player = self.sonosplayers[player_id]
            sonos_player.soco.volume = volume_level

        await self.mass.create_task(set_volume_level, player_id, volume_level)

    async def cmd_volume_mute(self, player_id: str, muted: bool) -> None:
        """Send VOLUME MUTE command to given player."""

        def set_volume_mute(player_id: str, muted: bool) -> None:
            sonos_player = self.sonosplayers[player_id]
            sonos_player.soco.mute = muted

        await self.mass.create_task(set_volume_mute, player_id, muted)

    async def cmd_sync(self, player_id: str, target_player: str) -> None:
        """Handle SYNC command for given player.

        Join/add the given player(id) to the given (master) player/sync group.

            - player_id: player_id of the player to handle the command.
            - target_player: player_id of the syncgroup master or group player.
        """
        sonos_player = self.sonosplayers[player_id]
        sonos_master_player = self.sonosplayers[target_player]
        await sonos_master_player.join([sonos_player])

    async def cmd_unsync(self, player_id: str) -> None:
        """Handle UNSYNC command for given player.

        Remove the given player from any syncgroups it currently is synced to.

            - player_id: player_id of the player to handle the command.
        """
        sonos_player = self.sonosplayers[player_id]
        await sonos_player.unjoin()

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
        )
        sonos_player = self.sonosplayers[player_id]
        mass_player = self.mass.players.get(player_id)
        if sonos_player.sync_coordinator:
            # this should be already handled by the player manager, but just in case...
            msg = (
                f"Player {mass_player.display_name} can not "
                "accept play_media command, it is synced to another player."
            )
            raise PlayerCommandFailed(msg)
        metadata = create_didl_metadata(self.mass, url, queue_item)
        await self.mass.create_task(sonos_player.soco.play_uri, url, meta=metadata)

    async def play_stream(self, player_id: str, stream_job: MultiClientStreamJob) -> None:
        """Handle PLAY STREAM on given player.

        This is a special feature from the Universal Group provider.
        """
        url = stream_job.resolve_stream_url(player_id, ContentType.FLAC)
        sonos_player = self.sonosplayers[player_id]
        mass_player = self.mass.players.get(player_id)
        if sonos_player.sync_coordinator:
            # this should be already handled by the player manager, but just in case...
            msg = (
                f"Player {mass_player.display_name} can not "
                "accept play_stream command, it is synced to another player."
            )
            raise PlayerCommandFailed(msg)
        metadata = create_didl_metadata(self.mass, url, None)
        # sonos players do not like our multi client stream
        # add to the workaround players list
        self.mass.streams.workaround_players.add(player_id)
        await self.mass.create_task(sonos_player.soco.play_uri, url, meta=metadata)
        # optimistically set this timestamp to help figure out elapsed time later
        mass_player.elapsed_time = 0
        mass_player.elapsed_time_last_updated = time.time()

    async def enqueue_next_queue_item(self, player_id: str, queue_item: QueueItem) -> None:
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
        if sonos_player.crossfade != crossfade:

            def set_crossfade() -> None:
                try:
                    sonos_player.soco.cross_fade = crossfade
                    sonos_player.crossfade = crossfade
                except Exception as err:
                    self.logger.warning(
                        "Unable to set crossfade for player %s: %s", sonos_player.zone_name, err
                    )

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
            sonos_player.update_player(signal_update=False)
        except ConnectionResetError as err:
            raise PlayerUnavailableError from err

    async def _run_discovery(self) -> None:
        """Discover Sonos players on the network."""
        if self._discovery_running:
            return

        allow_network_scan = self.config.get_value(CONF_NETWORK_SCAN)

        def do_discover() -> None:
            """Run discovery and add players in executor thread."""
            self._discovery_running = True
            try:
                self.logger.debug("Sonos discovery started...")
                discovered_devices: set[SoCo] = discover(allow_network_scan=allow_network_scan)
                if discovered_devices is None:
                    discovered_devices = set()
                # process new players
                for soco in discovered_devices:
                    try:
                        self._add_player(soco)
                    except RequestException as err:
                        # player is offline
                        self.logger.debug("Failed to add SonosPlayer %s: %s", soco, err)
                    except Exception as err:
                        self.logger.warning(
                            "Failed to add SonosPlayer %s: %s",
                            soco,
                            err,
                            exc_info=err if self.logger.isEnabledFor(10) else None,
                        )
            finally:
                self._discovery_running = False

        await self.mass.create_task(do_discover)

        def reschedule() -> None:
            self._discovery_reschedule_timer = None
            self.mass.create_task(self._run_discovery())

        # reschedule self once finished
        self._discovery_reschedule_timer = self.mass.loop.call_later(1800, reschedule)

    def _add_player(self, soco: SoCo) -> None:
        """Add discovered Sonos player."""
        player_id = soco.uid
        # check if existing player changed IP
        if existing := self.sonosplayers.get(player_id):
            if existing.soco.ip_address != soco.ip_address:
                existing.update_ip(soco.ip_address)
            return
        if not soco.is_visible:
            return
        enabled = self.mass.config.get_raw_player_config_value(player_id, "enabled", True)
        if not enabled:
            self.logger.debug("Ignoring disabled player: %s", player_id)
            return

        speaker_info = soco.get_speaker_info(True, timeout=7)
        if soco.uid not in self.boot_counts:
            self.boot_counts[soco.uid] = soco.boot_seqnum
        self.logger.debug("Adding new player: %s", speaker_info)
        support_hires = speaker_info["model_name"] in HIRES_MODELS
        if not (mass_player := self.mass.players.get(soco.uid)):
            mass_player = Player(
                player_id=soco.uid,
                provider=self.instance_id,
                type=PlayerType.PLAYER,
                name=soco.player_name,
                available=True,
                powered=False,
                supported_features=PLAYER_FEATURES,
                device_info=DeviceInfo(
                    model=speaker_info["model_name"],
                    address=soco.ip_address,
                    manufacturer="SONOS",
                ),
                max_sample_rate=48000 if support_hires else 44100,
                supports_24bit=support_hires,
            )
        self.sonosplayers[player_id] = sonos_player = SonosPlayer(
            self,
            soco=soco,
            mass_player=mass_player,
        )
        sonos_player.setup()
        self.mass.loop.call_soon_threadsafe(
            self.mass.players.register_or_update, sonos_player.mass_player
        )

    async def _enqueue_item(
        self,
        sonos_player: SonosPlayer,
        url: str,
        queue_item: QueueItem | None,
    ) -> None:
        """Enqueue a queue item to the Sonos player Queue."""
        metadata = create_didl_metadata(self.mass, url, queue_item)
        try:
            await asyncio.to_thread(
                sonos_player.soco.avTransport.SetNextAVTransportURI,
                [("InstanceID", 0), ("NextURI", url), ("NextURIMetaData", metadata)],
                timeout=60,
            )
        except Exception as err:
            self.logger.warning(
                "Unable to enqueue next track on player: %s: %s", sonos_player.zone_name, err
            )
        else:
            self.logger.debug(
                "Enqued next track (%s) to player %s",
                queue_item.name if queue_item else url,
                sonos_player.soco.player_name,
            )
