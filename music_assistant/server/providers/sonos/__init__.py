"""Sonos Player provider for Music Assistant for speakers running the S2 firmware."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Final

from aiohttp import web
from aiosonos.api.models import DiscoveryInfo as SonosDiscoveryInfo
from aiosonos.api.models import PlayBackState as SonosPlayBackState
from aiosonos.api.models import SonosCapability
from aiosonos.client import SonosLocalApiClient
from aiosonos.const import EventType as SonosEventType
from aiosonos.const import SonosEvent
from aiosonos.exceptions import FailedCommand
from aiosonos.utils import get_discovery_info
from zeroconf import IPVersion, ServiceStateChange

from music_assistant.common.models.config_entries import (
    CONF_ENTRY_CROSSFADE,
    CONF_ENTRY_FLOW_MODE_HIDDEN_DISABLED,
    ConfigEntry,
    ConfigValueType,
    create_sample_rates_config_entry,
)
from music_assistant.common.models.enums import (
    ConfigEntryType,
    EventType,
    PlayerFeature,
    PlayerState,
    PlayerType,
    ProviderFeature,
    RepeatMode,
)
from music_assistant.common.models.errors import PlayerCommandFailed
from music_assistant.common.models.event import MassEvent
from music_assistant.common.models.player import DeviceInfo, Player, PlayerMedia
from music_assistant.constants import (
    CONF_CROSSFADE,
    MASS_LOGO_ONLINE,
    SYNCGROUP_PREFIX,
    VERBOSE_LOG_LEVEL,
)
from music_assistant.server.helpers.util import TaskManager
from music_assistant.server.models.player_provider import PlayerProvider

if TYPE_CHECKING:
    from zeroconf.asyncio import AsyncServiceInfo

    from music_assistant.common.models.config_entries import ProviderConfig
    from music_assistant.common.models.provider import ProviderManifest
    from music_assistant.server import MusicAssistant
    from music_assistant.server.models import ProviderInstanceType

PLAYBACK_STATE_MAP = {
    SonosPlayBackState.PLAYBACK_STATE_BUFFERING: PlayerState.PLAYING,
    SonosPlayBackState.PLAYBACK_STATE_IDLE: PlayerState.IDLE,
    SonosPlayBackState.PLAYBACK_STATE_PAUSED: PlayerState.PAUSED,
    SonosPlayBackState.PLAYBACK_STATE_PLAYING: PlayerState.PLAYING,
}

PLAYER_FEATURES_BASE = {
    PlayerFeature.SYNC,
    PlayerFeature.VOLUME_MUTE,
    PlayerFeature.ENQUEUE_NEXT,
    PlayerFeature.PAUSE,
}

SOURCE_LINE_IN = "line_in"
SOURCE_AIRPLAY = "airplay"
SOURCE_SPOTIFY = "spotify"
SOURCE_UNKNOWN = "unknown"
SOURCE_RADIO = "radio"

CONF_SMART_AIRPLAY: Final[str] = "smart_airplay"


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    prov = SonosPlayerProvider(mass, manifest, config)
    # set-up aiosonos logging
    if prov.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
        logging.getLogger("aiosonos").setLevel(logging.DEBUG)
    else:
        logging.getLogger("aiosonos").setLevel(prov.logger.level + 10)
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
    return ()


class SonosPlayer:
    """Holds the details of the (discovered) Sonosplayer."""

    def __init__(
        self,
        prov: SonosPlayerProvider,
        player_id: str,
        discovery_info: SonosDiscoveryInfo,
        ip_address: str,
    ) -> None:
        """Initialize the SonosPlayer."""
        self.prov = prov
        self.mass = prov.mass
        self.player_id = player_id
        self.discovery_info = discovery_info
        self.ip_address = ip_address
        self.logger = prov.logger.getChild(player_id)
        self.connected: bool = False
        self.client = SonosLocalApiClient(self.ip_address, self.mass.http_session)
        self.mass_player: Player | None = None
        self._listen_task: asyncio.Task | None = None
        # Sonos speakers can optionally have airplay (most S2 speakers do)
        # and this airplay player can also be a player within MA
        # we can do some smart stuff if we link them together where possible
        # the player if we can just guess from the sonos player id (mac address)
        self.airplay_player_id = f"ap{self.player_id[7:-5].lower()}"
        self.stream_mode: str = "sonos"

    def get_linked_airplay_player(self, active_only: bool = True) -> Player | None:
        """Return the linked airplay player if available/enabled."""
        if not self.mass.config.get_raw_player_config_value(
            self.player_id, CONF_SMART_AIRPLAY, False
        ):
            return None
        if not (airplay := self.mass.players.get(self.airplay_player_id)):
            return None
        if not airplay.available:
            return None
        if not active_only:
            return airplay
        if (airplay.powered and airplay.group_childs) or airplay.state == PlayerState.PLAYING:
            # only when the airplay player has synced members,
            # it makes sense to link it to the sonos player
            return airplay
        return None

    async def setup(self) -> None:
        """Handle setup of the player."""
        # connect the player first so we can fail early
        await self.connect()

        # collect supported features
        supported_features = set(PLAYER_FEATURES_BASE)
        if SonosCapability.AUDIO_CLIP in self.discovery_info["device"]["capabilities"]:
            supported_features.add(PlayerFeature.PLAY_ANNOUNCEMENT)
        if not self.client.player.has_fixed_volume:
            supported_features.add(PlayerFeature.VOLUME_SET)

        # instantiate the MA player
        self.mass_player = mass_player = Player(
            player_id=self.player_id,
            provider=self.prov.instance_id,
            type=PlayerType.PLAYER,
            name=self.discovery_info["device"]["name"]
            or self.discovery_info["device"]["modelDisplayName"],
            available=True,
            # treat as powered at start if the player is playing/paused
            powered=self.client.player.group.playback_state
            in (
                SonosPlayBackState.PLAYBACK_STATE_PLAYING,
                SonosPlayBackState.PLAYBACK_STATE_BUFFERING,
                SonosPlayBackState.PLAYBACK_STATE_PAUSED,
            ),
            device_info=DeviceInfo(
                model=self.discovery_info["device"]["modelDisplayName"],
                manufacturer=self.prov.manifest.name,
                address=self.ip_address,
            ),
            supported_features=tuple(supported_features),
        )
        self.update_attributes()
        self.mass.players.register_or_update(mass_player)

        # register callback for state changed
        self.client.subscribe(
            self._on_player_event,
            (
                SonosEventType.GROUP_UPDATED,
                SonosEventType.PLAYER_UPDATED,
            ),
        )
        # register callback for airplay player state changes
        self.mass.subscribe(
            self._on_airplay_player_event,
            (EventType.PLAYER_UPDATED, EventType.PLAYER_ADDED),
            self.airplay_player_id,
        )

    async def connect(self) -> None:
        """Connect to the Sonos player."""
        if self._listen_task and not self._listen_task.done():
            self.logger.debug("Already connected to Sonos player: %s", self.player_id)
            return
        await self.client.connect()
        self.connected = True
        self.logger.debug("Connected to player API")
        init_ready = asyncio.Event()

        async def _listener() -> None:
            try:
                await self.client.start_listening(init_ready)
            except Exception as err:
                self.logger.exception("Error in Sonos player listener: %s", err)
            finally:
                self.logger.info("Disconnected from player API")
                if self.connected:
                    # we didn't explicitly disconnect, try to reconnect
                    # this should simply try to reconnect once and if that fails
                    # we rely on mdns to pick it up again later
                    self.mass.call_later(5, self.connect)
                self.connected = False

        self._listen_task = asyncio.create_task(_listener())
        await init_ready.wait()

    async def disconnect(self) -> None:
        """Disconnect the client and cleanup."""
        self.connected = False
        if self._listen_task and not self._listen_task.done():
            self._listen_task.cancel()
        if self.client:
            await self.client.disconnect()
        self.logger.debug("Disconnected from player API")

    def update_attributes(self) -> None:  # noqa: PLR0915
        """Update the player attributes."""
        if not self.mass_player:
            return
        if not self.connected:
            self.mass_player.available = False
            return
        if self.client.player.has_fixed_volume:
            self.mass_player.volume_level = 100
        else:
            self.mass_player.volume_level = self.client.player.volume_level or 100
        self.mass_player.volume_muted = self.client.player.volume_muted

        group_parent = None
        if self.client.player.is_coordinator:
            # player is group coordinator
            active_group = self.client.player.group
            self.mass_player.group_childs = (
                self.client.player.group_members
                if len(self.client.player.group_members) > 1
                else set()
            )
            self.mass_player.synced_to = None
            # work out 'can sync with' for this player
            self.mass_player.can_sync_with = tuple(
                x
                for x in self.prov.sonos_players
                if x != self.player_id
                and x in self.prov.sonos_players
                and self.prov.sonos_players[x].client.household_id == self.client.household_id
            )
            if airplay := self.get_linked_airplay_player(False):
                self.mass_player.group_childs.update(
                    x for x in airplay.group_childs if x != self.airplay_player_id
                )
        else:
            # player is group child (synced to another player)
            group_parent = self.prov.sonos_players.get(self.client.player.group.coordinator_id)
            if not group_parent:
                # handle race condition where the group parent is not yet discovered
                return
            active_group = group_parent.client.player.group
            self.mass_player.group_childs = set()
            self.mass_player.synced_to = active_group.coordinator_id
            self.mass_player.active_source = active_group.coordinator_id
            self.mass_player.can_sync_with = ()

        if self.stream_mode == "airplay" and (airplay := self.get_linked_airplay_player(True)):
            # linked airplay player is active, update media from there
            self.mass_player.state = airplay.state
            self.mass_player.powered = airplay.powered
            self.mass_player.active_source = airplay.active_source
            self.mass_player.elapsed_time = airplay.elapsed_time
            self.mass_player.elapsed_time_last_updated = airplay.elapsed_time_last_updated
            return

        # map playback state
        self.mass_player.state = PLAYBACK_STATE_MAP[active_group.playback_state]
        if (
            not self.mass_player.powered
            and active_group.playback_state == SonosPlayBackState.PLAYBACK_STATE_PLAYING
        ):
            self.mass_player.powered = True

        self.mass_player.elapsed_time = active_group.position

        # figure out the active source based on the container
        is_playing = active_group.playback_state in (
            SonosPlayBackState.PLAYBACK_STATE_PLAYING,
            SonosPlayBackState.PLAYBACK_STATE_BUFFERING,
            SonosPlayBackState.PLAYBACK_STATE_PAUSED,
        )
        if container := active_group.playback_metadata.get("container"):
            if group_parent and group_parent.mass_player:
                self.mass_player.active_source = group_parent.mass_player.active_source
            elif (
                container.get("type") == "linein"
                and active_group.playback_state == SonosPlayBackState.PLAYBACK_STATE_PLAYING
            ):
                self.mass_player.active_source = SOURCE_LINE_IN
            elif is_playing and container.get("type") == "linein.airplay":
                # check if the MA airplay player is active
                airplay_player = self.mass.players.get(self.airplay_player_id)
                if airplay_player and airplay_player.powered:
                    self.mass_player.active_source = airplay_player.active_source
                else:
                    self.mass_player.active_source = SOURCE_AIRPLAY
            elif is_playing and container.get("type") == "station":
                self.mass_player.active_source = SOURCE_RADIO
            elif container.get("id", {}).get("objectId") == f"mass:queue:{self.player_id}":
                # mass queue is active
                self.mass_player.active_source = self.player_id
            elif is_playing and container.get("id", {}).get("serviceId") == "9":
                self.mass_player.active_source = SOURCE_SPOTIFY
            elif is_playing:
                self.mass_player.active_source = container.get("type", SOURCE_UNKNOWN)
            else:
                self.mass_player.active_source = self.player_id

        if self.mass_player.active_source == self.player_id and active_group.active_session_id:
            # active source is the mass queue
            # media details are updated through the time played callback
            return

        # parse current media
        self.mass_player.elapsed_time = self.client.player.group.position
        self.mass_player.elapsed_time_last_updated = time.time()
        if (current_item := active_group.playback_metadata.get("currentItem")) and (
            (track := current_item.get("track")) and track.get("name")
        ):
            track_images = track.get("images", [])
            track_image_url = track_images[0].get("url") if track_images else None
            track_duration_millis = track.get("durationMillis")
            self.mass_player.current_media = PlayerMedia(
                uri=track.get("id", {}).get("objectId") or track.get("mediaUrl"),
                title=track["name"],
                artist=track.get("artist", {}).get("name"),
                album=track.get("album", {}).get("name"),
                duration=track_duration_millis / 1000 if track_duration_millis else None,
                image_url=track_image_url,
            )
        elif (
            container and container.get("name") and active_group.playback_metadata.get("streamInfo")
        ):
            images = container.get("images", [])
            image_url = images[0].get("url") if images else None
            self.mass_player.current_media = PlayerMedia(
                uri=container.get("id", {}).get("objectId"),
                title=active_group.playback_metadata["streamInfo"],
                album=container["name"],
                image_url=image_url,
            )
        elif container and container.get("name") and container.get("id"):
            images = container.get("images", [])
            image_url = images[0].get("url") if images else None
            self.mass_player.current_media = PlayerMedia(
                uri=container["id"]["objectId"],
                title=container["name"],
                image_url=image_url,
            )
        else:
            self.mass_player.current_media = None

    def _on_player_event(self, event: SonosEvent) -> None:
        """Handle incoming event from player."""
        self.update_attributes()
        self.mass.players.update(self.player_id)

    def _on_airplay_player_event(self, event: MassEvent) -> None:
        """Handle incoming event from linked airplay player."""
        if not self.mass.config.get_raw_player_config_value(
            self.player_id, CONF_SMART_AIRPLAY, False
        ):
            return
        if event.object_id != self.airplay_player_id:
            return
        airplay_player: Player = event.data
        if (
            self.stream_mode == "sonos"
            and airplay_player.group_childs
            and self.mass_player.state == PlayerState.PLAYING
            and airplay_player.state != PlayerState.PLAYING
            and self.mass_player.active_source == self.player_id
            and not self.mass_player.synced_to
        ):
            # linked airplay player became active, transfer playback to playback
            self.logger.info(
                "Linked Airplay player became active, transferring playback.",
            )
            self.stream_mode = "airplay"
            self.mass.create_task(self.mass.player_queues.resume(self.player_id))
        elif (
            self.stream_mode == "airplay"
            and not airplay_player.group_childs
            and airplay_player.state == PlayerState.PLAYING
        ):
            # linked airplay player became active, transfer playback to sonos
            self.logger.info(
                "Linked Airplay player became idle, transferring playback.",
            )
            self.stream_mode = "sonos"
            self.mass.create_task(self.mass.players.cmd_power(self.airplay_player_id, False))
            self.mass.create_task(self.mass.player_queues.resume(self.player_id))
        else:
            self.update_attributes()
            self.mass.players.update(self.player_id)


class SonosPlayerProvider(PlayerProvider):
    """Sonos Player provider."""

    sonos_players: dict[str, SonosPlayer]

    @property
    def supported_features(self) -> tuple[ProviderFeature, ...]:
        """Return the features supported by this Provider."""
        return (ProviderFeature.SYNC_PLAYERS, ProviderFeature.PLAYER_GROUP_CREATE)

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self.sonos_players: dict[str, SonosPlayer] = {}
        self.mass.streams.register_dynamic_route(
            "/sonos_queue/v2.3/itemWindow", self._handle_sonos_queue_itemwindow
        )
        self.mass.streams.register_dynamic_route(
            "/sonos_queue/v2.3/version", self._handle_sonos_queue_version
        )
        self.mass.streams.register_dynamic_route(
            "/sonos_queue/v2.3/context", self._handle_sonos_queue_context
        )
        self.mass.streams.register_dynamic_route(
            "/sonos_queue/v2.3/timePlayed", self._handle_sonos_queue_time_played
        )

    async def unload(self) -> None:
        """Handle close/cleanup of the provider."""
        # disconnect all players
        await asyncio.gather(*(player.disconnect() for player in self.sonos_players.values()))
        self.sonos_players = None
        self.mass.streams.unregister_dynamic_route("/sonos_queue/v2.3/itemWindow")
        self.mass.streams.unregister_dynamic_route("/sonos_queue/v2.3/version")
        self.mass.streams.unregister_dynamic_route("/sonos_queue/v2.3/context")
        self.mass.streams.unregister_dynamic_route("/sonos_queue/v2.3/timePlayed")

    async def on_mdns_service_state_change(
        self, name: str, state_change: ServiceStateChange, info: AsyncServiceInfo | None
    ) -> None:
        """Handle MDNS service state callback."""
        if not info:
            self.logger.error(
                "No info in MDNS service state change for %s - state change: %s", name, state_change
            )
            return
        if "uuid" not in info.decoded_properties:
            # not a S2 player
            return
        name = name.split("@", 1)[1] if "@" in name else name
        player_id = info.decoded_properties["uuid"]
        # handle removed player
        if state_change == ServiceStateChange.Removed:
            if mass_player := self.mass.players.get(player_id):
                if not mass_player.available:
                    return
                # the player has become unavailable
                self.logger.debug("Player offline: %s", mass_player.display_name)
                mass_player.available = False
                self.mass.players.update(player_id)
            return
        # handle update for existing device
        if sonos_player := self.sonos_players.get(player_id):
            if mass_player := self.mass.players.get(player_id):
                cur_address = get_primary_ip_address(info)
                if cur_address and cur_address != sonos_player.ip_address:
                    sonos_player.logger.debug(
                        "Address updated from %s to %s", sonos_player.ip_address, cur_address
                    )
                    sonos_player.ip_address = cur_address
                    mass_player.device_info = DeviceInfo(
                        model=mass_player.device_info.model,
                        manufacturer=mass_player.device_info.manufacturer,
                        address=str(cur_address),
                    )
                if not sonos_player.connected:
                    self.logger.debug("Player back online: %s", mass_player.display_name)
                    sonos_player.client.player_ip = cur_address
                    await sonos_player.connect(self.mass.http_session)
                self.mass.players.update(player_id)
            return
        # handle new player
        await self._setup_player(player_id, name, info)

    async def get_player_config_entries(
        self,
        player_id: str,
    ) -> tuple[ConfigEntry, ...]:
        """Return Config Entries for the given player."""
        base_entries = await super().get_player_config_entries(player_id)
        if not (sonos_player := self.sonos_players.get(player_id)):
            # most probably a syncgroup or the player is not yet discovered
            return (*base_entries, CONF_ENTRY_CROSSFADE, CONF_ENTRY_FLOW_MODE_HIDDEN_DISABLED)
        return (
            *base_entries,
            CONF_ENTRY_CROSSFADE,
            CONF_ENTRY_FLOW_MODE_HIDDEN_DISABLED,
            create_sample_rates_config_entry(48000, 24, 48000, 24, True),
            ConfigEntry(
                key=CONF_SMART_AIRPLAY,
                type=ConfigEntryType.BOOLEAN,
                label="Enable smart Airplay mode (experimental)",
                description="Almost all newer Sonos speakers have Airplay support. "
                "If you have the Airplay provider enabled in Music Assistant, "
                "your Sonos speakers will also be detected as Airplay speakers, meaning "
                "you can group them with other Airplay speakers."
                "The downside of that is that you'll end up with a duplicated player. \n\n"
                "This feature will try to circumvent that by linking the Sonos "
                "player to the Airplay player: \n\nIf the AirPlay player is powered on, "
                "and has other players grouped to it or is playing, any playback related  commands "
                "will be automatically redirected to the Airplay player. \n\n"
                "Effectively this will mean that the Sonos player holds the queue and for "
                "playback, Music Assistant will automatically pick either "
                "the Sonos or the Airplay protocol.",
                required=False,
                default_value=False,
                hidden=SonosCapability.AIRPLAY
                not in sonos_player.discovery_info["device"]["capabilities"],
            ),
        )

    async def cmd_stop(self, player_id: str) -> None:
        """Send STOP command to given player."""
        sonos_player = self.sonos_players[player_id]
        if sonos_player.client.player.is_passive:
            self.logger.debug(
                "Ignore STOP command for %s: Player is synced to another player.",
                player_id,
            )
            return
        if airplay := sonos_player.get_linked_airplay_player(True):
            # linked airplay player is active, redirect the command
            self.logger.debug("Redirecting STOP command to linked airplay player.")
            await self.mass.players.cmd_stop(airplay.player_id)
            return
        try:
            await sonos_player.client.player.group.stop()
        except FailedCommand:
            # no session to stop, fallback to pause
            await self.mass.players.cmd_pause(player_id)

    async def cmd_play(self, player_id: str) -> None:
        """Send PLAY command to given player."""
        sonos_player = self.sonos_players[player_id]
        if sonos_player.client.player.is_passive:
            self.logger.debug(
                "Ignore PLAY command for %s: Player is synced to another player.",
                player_id,
            )
            return
        if airplay := sonos_player.get_linked_airplay_player(True):
            # linked airplay player is active, redirect the command
            self.logger.debug("Redirecting PLAY command to linked airplay player.")
            await self.mass.players.cmd_play(airplay.player_id)
            return
        await sonos_player.client.player.group.play()

    async def cmd_pause(self, player_id: str) -> None:
        """Send PAUSE command to given player."""
        sonos_player = self.sonos_players[player_id]
        if sonos_player.client.player.is_passive:
            self.logger.debug(
                "Ignore PLAY command for %s: Player is synced to another player.",
                player_id,
            )
            return
        if airplay := sonos_player.get_linked_airplay_player(True):
            # linked airplay player is active, redirect the command
            self.logger.debug("Redirecting PAUSE command to linked airplay player.")
            await self.mass.players.cmd_pause(airplay.player_id)
            return
        await sonos_player.client.player.group.pause()

    async def cmd_volume_set(self, player_id: str, volume_level: int) -> None:
        """Send VOLUME_SET command to given player."""
        sonos_player = self.sonos_players[player_id]
        await sonos_player.client.player.set_volume(volume_level)
        # sync volume level with airplay player if linked
        if airplay := sonos_player.get_linked_airplay_player(False):
            airplay.volume_level = volume_level

    async def cmd_volume_mute(self, player_id: str, muted: bool) -> None:
        """Send VOLUME MUTE command to given player."""
        sonos_player = self.sonos_players[player_id]
        await sonos_player.client.player.set_volume(muted=muted)

    async def cmd_sync(self, player_id: str, target_player: str) -> None:
        """Handle SYNC command for given player.

        Join/add the given player(id) to the given (master) player/sync group.

            - player_id: player_id of the player to handle the command.
            - target_player: player_id of the syncgroup master or group player.
        """
        await self.cmd_sync_many(target_player, [player_id])

    async def cmd_sync_many(self, target_player: str, child_player_ids: list[str]) -> None:
        """Create temporary sync group by joining given players to target player."""
        sonos_player = self.sonos_players[target_player]
        if airplay_child_ids := [x for x in child_player_ids if x.startswith("ap")]:
            # handle airplay players
            await self.mass.players.cmd_sync_many(sonos_player.airplay_player_id, airplay_child_ids)
        if sonos_child_ids := [x for x in child_player_ids if not x.startswith("ap")]:
            await sonos_player.client.player.group.modify_group_members(
                player_ids_to_add=sonos_child_ids, player_ids_to_remove=[]
            )

    async def cmd_unsync(self, player_id: str) -> None:
        """Handle UNSYNC command for given player.

        Remove the given player from any syncgroups it currently is synced to.

            - player_id: player_id of the player to handle the command.
        """
        sonos_player = self.sonos_players[player_id]
        await sonos_player.client.player.leave_group()

    async def play_media(
        self,
        player_id: str,
        media: PlayerMedia,
    ) -> None:
        """Handle PLAY MEDIA on given player."""
        sonos_player = self.sonos_players[player_id]
        mass_player = self.mass.players.get(player_id)
        if sonos_player.client.player.is_passive:
            # this should be already handled by the player manager, but just in case...
            msg = (
                f"Player {mass_player.display_name} can not "
                "accept play_media command, it is synced to another player."
            )
            raise PlayerCommandFailed(msg)

        if airplay := sonos_player.get_linked_airplay_player(True):
            # linked airplay player is active, redirect the command
            self.logger.debug("Redirecting PLAY_MEDIA command to linked airplay player.")
            self.stream_mode = "airplay"
            mass_player.active_source = airplay.player_id
            # TODO: sonos has an annoying bug where it looses its sync childs
            # when airplay playback is started
            await self.mass.players.play_media(airplay.player_id, media)
            return

        if media.queue_id:
            # create a sonos cloud queue and load it
            self.stream_mode = "sonos"
            await sonos_player.client.player.group.create_playback_session()
            mass_queue = self.mass.player_queues.get(media.queue_id)
            cloud_queue_url = f"{self.mass.streams.base_url}/sonos_queue/v2.3/"
            await sonos_player.client.player.group.play_cloud_queue(
                cloud_queue_url,
                http_authorization=media.queue_id,
                item_id=media.queue_item_id,
                queue_version=mass_queue.queue_version,
            )
            return

        # play a single uri/url
        self.stream_mode = "sonos"
        await sonos_player.client.player.group.play_stream_url(
            media.uri, {"name": media.title, "type": "track"}
        )

    async def enqueue_next_media(self, player_id: str, media: PlayerMedia) -> None:
        """Handle enqueuing of the next queue item on the player."""
        sonos_player = self.sonos_players[player_id]
        if airplay := sonos_player.get_linked_airplay_player(True):
            # linked airplay player is active, redirect the command
            self.logger.debug("Redirecting ENQUEUE_NEXT command to linked airplay player.")
            await self.mass.players.enqueue_next_media(airplay.player_id, media)
            return

        if session_id := sonos_player.client.player.group.active_session_id:
            await sonos_player.client.api.playback_session.refresh_cloud_queue(session_id)
        # sync play modes from player queue --> sonos
        mass_queue = self.mass.player_queues.get(media.queue_id)
        crossfade = await self.mass.config.get_player_config_value(player_id, CONF_CROSSFADE)
        repeat_single_enabled = mass_queue.repeat_mode == RepeatMode.ONE
        repeat_all_enabled = mass_queue.repeat_mode == RepeatMode.ALL
        play_modes = sonos_player.client.player.group.play_modes
        if (
            play_modes.crossfade != crossfade
            or play_modes.repeat != repeat_all_enabled
            or play_modes.repeat_one != repeat_single_enabled
        ):
            await sonos_player.client.player.group.set_play_modes(
                crossfade=crossfade, repeat=repeat_all_enabled, repeat_one=repeat_single_enabled
            )

    async def play_announcement(
        self, player_id: str, announcement: PlayerMedia, volume_level: int | None = None
    ) -> None:
        """Handle (provider native) playback of an announcement on given player."""
        if player_id.startswith(SYNCGROUP_PREFIX):
            # handle syncgroup, unwrap to all underlying child's
            async with TaskManager(self.mass) as tg:
                if group_player := self.mass.players.get(player_id):
                    # execute on all child players
                    for child_player_id in group_player.group_childs:
                        tg.create_task(
                            self.play_announcement(child_player_id, announcement, volume_level)
                        )
            return
        sonos_player = self.sonos_players[player_id]
        self.logger.debug(
            "Playing announcement %s using websocket audioclip on %s",
            announcement.uri,
            sonos_player.zone_name,
        )
        volume_level = self.mass.players.get_announcement_volume(player_id, volume_level)
        await sonos_player.client.player.play_audio_clip(announcement.uri, volume_level)

    async def _setup_player(self, player_id: str, name: str, info: AsyncServiceInfo) -> None:
        """Handle setup of a new player that is discovered using mdns."""
        address = get_primary_ip_address(info)
        if address is None:
            return
        if not self.mass.config.get_raw_player_config_value(player_id, "enabled", True):
            self.logger.debug("Ignoring %s in discovery as it is disabled.", name)
            return
        if not (discovery_info := await get_discovery_info(self.mass.http_session, address)):
            self.logger.debug("Ignoring %s in discovery as it is not reachable.", name)
            return
        display_name = discovery_info["device"].get("name") or name
        if SonosCapability.PLAYBACK not in discovery_info["device"]["capabilities"]:
            # this will happen for satellite speakers in a surround/stereo setup
            self.logger.debug(
                "Ignoring %s in discovery as it is a passive satellite.", display_name
            )
            return
        self.logger.debug("Discovered Sonos device %s on %s", name, address)
        self.sonos_players[player_id] = sonos_player = SonosPlayer(
            self, player_id, discovery_info=discovery_info, ip_address=address
        )
        await sonos_player.setup()
        # when we add a new player, update 'can_sync_with' for all other players
        for other_player_id in self.sonos_players:
            if other_player_id == player_id:
                continue
            self.sonos_players[other_player_id].update_attributes()

    async def _handle_sonos_queue_itemwindow(self, request: web.Request) -> web.Response:
        """
        Handle the Sonos CloudQueue ItemWindow endpoint.

        https://docs.sonos.com/reference/itemwindow
        """
        self.logger.log(VERBOSE_LOG_LEVEL, "Cloud Queue ItemWindow request: %s", request.query)
        sonos_playback_id = request.headers["X-Sonos-Playback-Id"]
        sonos_player_id = sonos_playback_id.split(":")[0]
        upcoming_window_size = int(request.query.get("upcomingWindowSize") or 10)
        previous_window_size = int(request.query.get("previousWindowSize") or 10)
        queue_version = request.query.get("queueVersion")
        context_version = request.query.get("contextVersion")
        if not (mass_queue := self.mass.player_queues.get_active_queue(sonos_player_id)):
            return web.Response(status=501)
        if item_id := request.query.get("itemId"):
            queue_index = self.mass.player_queues.index_by_id(mass_queue.queue_id, item_id)
        else:
            queue_index = mass_queue.current_index or 0
        offset = max(queue_index - previous_window_size, 0)
        queue_items = self.mass.player_queues.items(
            sonos_player_id,
            limit=upcoming_window_size + previous_window_size,
            offset=max(queue_index - previous_window_size, 0),
        )
        sonos_queue_items = [
            {
                "id": item.queue_item_id,
                "deleted": not item.media_item.available,
                "policies": {},
                "track": {
                    "type": "track",
                    "mediaUrl": self.mass.streams.resolve_stream_url(item),
                    "contentType": "audio/flac",
                    "service": {"name": "Music Assistant", "id": "8", "accountId": ""},
                    "name": item.name,
                    "imageUrl": self.mass.metadata.get_image_url(
                        item.image, prefer_proxy=False, image_format="jpeg"
                    )
                    if item.image
                    else None,
                    "durationMillis": item.duration * 1000 if item.duration else None,
                    "artist": {
                        "name": item.media_item.artist_str,
                    }
                    if item.media_item and item.media_item.artist_str
                    else None,
                    "album": {
                        "name": item.media_item.album.name,
                    }
                    if item.media_item and item.media_item.album
                    else None,
                    "quality": {
                        "bitDepth": item.streamdetails.audio_format.bit_depth,
                        "sampleRate": item.streamdetails.audio_format.sample_rate,
                        "codec": item.streamdetails.audio_format.content_type.value,
                        "lossless": item.streamdetails.audio_format.content_type.is_lossless(),
                    }
                    if item.streamdetails
                    else None,
                },
            }
            for item in queue_items
        ]
        result = {
            "includesBeginningOfQueue": offset == 0,
            "includesEndOfQueue": mass_queue.items <= (queue_index + len(sonos_queue_items)),
            "contextVersion": context_version,
            "queueVersion": queue_version,
            "items": sonos_queue_items,
        }
        return web.json_response(result)

    async def _handle_sonos_queue_version(self, request: web.Request) -> web.Response:
        """
        Handle the Sonos CloudQueue Version endpoint.

        https://docs.sonos.com/reference/version
        """
        self.logger.log(VERBOSE_LOG_LEVEL, "Cloud Queue Version request: %s", request.query)
        sonos_playback_id = request.headers["X-Sonos-Playback-Id"]
        sonos_player_id = sonos_playback_id.split(":")[0]
        if not (mass_queue := self.mass.player_queues.get_active_queue(sonos_player_id)):
            return web.Response(status=501)
        context_version = request.query.get("contextVersion") or "1"
        queue_version = mass_queue.queue_version
        result = {"contextVersion": context_version, "queueVersion": queue_version}
        return web.json_response(result)

    async def _handle_sonos_queue_context(self, request: web.Request) -> web.Response:
        """
        Handle the Sonos CloudQueue Context endpoint.

        https://docs.sonos.com/reference/context
        """
        self.logger.log(VERBOSE_LOG_LEVEL, "Cloud Queue Context request: %s", request.query)
        sonos_playback_id = request.headers["X-Sonos-Playback-Id"]
        sonos_player_id = sonos_playback_id.split(":")[0]
        if not (mass_queue := self.mass.player_queues.get_active_queue(sonos_player_id)):
            return web.Response(status=501)
        result = {
            "contextVersion": "1",
            "queueVersion": mass_queue.queue_version,
            "container": {
                "type": "playlist",
                "name": "Music Assistant",
                "imageUrl": MASS_LOGO_ONLINE,
                "service": {"name": "Music Assistant", "id": "mass"},
                "id": {
                    "serviceId": "mass",
                    "objectId": f"mass:queue:{mass_queue.queue_id}",
                    "accountId": "",
                },
            },
            "reports": {
                "sendUpdateAfterMillis": 0,
                "periodicIntervalMillis": 10000,
                "sendPlaybackActions": True,
            },
            "playbackPolicies": {
                "canSkip": True,
                "limitedSkips": False,
                "canSkipToItem": True,
                "canSkipBack": True,
                "canSeek": False,  # somehow not working correctly, investigate later
                "canRepeat": True,
                "canRepeatOne": True,
                "canCrossfade": True,
                "canShuffle": False,  # handled by our queue controller itself
                "showNNextTracks": 5,
                "showNPreviousTracks": 5,
            },
        }
        return web.json_response(result)

    async def _handle_sonos_queue_time_played(self, request: web.Request) -> web.Response:
        """
        Handle the Sonos CloudQueue TimePlayed endpoint.

        https://docs.sonos.com/reference/timeplayed
        """
        self.logger.log(VERBOSE_LOG_LEVEL, "Cloud Queue TimePlayed request: %s", request.query)
        json_body = await request.json()
        sonos_playback_id = request.headers["X-Sonos-Playback-Id"]
        sonos_player_id = sonos_playback_id.split(":")[0]
        if not (mass_player := self.mass.players.get(sonos_player_id)):
            return web.Response(status=501)
        if not (mass_queue := self.mass.player_queues.get(mass_player.active_source)):
            return web.Response(status=501)
        for item in json_body["items"]:
            if item["queueVersion"] != mass_queue.queue_version:
                continue
            if "positionMillis" not in item:
                continue
            mass_player.current_media = PlayerMedia(
                uri=item["mediaUrl"], queue_id=sonos_playback_id, queue_item_id=item["id"]
            )
            mass_player.elapsed_time = item["positionMillis"] / 1000
            mass_player.elapsed_time_last_updated = time.time()
            self.mass.players.update(sonos_player_id)
            break
        return web.Response(status=204)


def get_primary_ip_address(discovery_info: AsyncServiceInfo) -> str | None:
    """Get primary IP address from zeroconf discovery info."""
    for address in discovery_info.parsed_addresses(IPVersion.V4Only):
        if address.startswith("127"):
            # filter out loopback address
            continue
        if address.startswith("169.254"):
            # filter out APIPA address
            continue
        return address
    return None
