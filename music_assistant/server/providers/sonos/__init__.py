"""Sonos Player provider for Music Assistant for speakers running the S2 firmware."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from aiohttp import web
from aiosonos.api.models import DiscoveryInfo as SonosDiscoveryInfo
from aiosonos.api.models import PlayBackState as SonosPlayBackState
from aiosonos.api.models import SonosCapability
from aiosonos.client import SonosLocalApiClient
from aiosonos.const import EventType as SonosEventType
from aiosonos.const import SonosEvent
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
    PlayerFeature,
    PlayerState,
    PlayerType,
    ProviderFeature,
)
from music_assistant.common.models.errors import PlayerCommandFailed
from music_assistant.common.models.player import DeviceInfo, Player, PlayerMedia
from music_assistant.constants import SYNCGROUP_PREFIX, VERBOSE_LOG_LEVEL
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

CONF_ENTRY_SAMPLE_RATES_SONOS_S2 = create_sample_rates_config_entry(48000, 24, 48000, 24, True)
CONF_ENTRY_SAMPLE_RATES_SONOS_S1 = create_sample_rates_config_entry(48000, 16, 48000, 16, True)

SOURCE_LINE_IN = "line_in"
SOURCE_AIRPLAY = "airplay"


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
            finally:
                self.connected = False

        self._listen_task = asyncio.create_task(_listener())
        await init_ready.wait()

    async def disconnect(self) -> None:
        """Disconnect the client and cleanup."""
        if self._listen_task and not self._listen_task.done():
            self._listen_task.cancel()
        if self.client:
            await self.client.disconnect()
        self.connected = False
        self.logger.debug("Disconnected from player API")

    def update_attributes(self) -> None:
        """Update the player attributes."""
        if not self.mass_player:
            return
        if self.client.player.has_fixed_volume:
            self.mass_player.volume_level = 100
        else:
            self.mass_player.volume_level = self.client.player.volume_level or 100
        self.mass_player.volume_muted = self.client.player.volume_muted
        active_group = self.client.player.group
        if self.client.player.is_coordinator:
            self.mass_player.group_childs = (
                self.client.player.group_members
                if len(self.client.player.group_members) > 1
                else set()
            )
            self.mass_player.synced_to = None

            if container := active_group.playback_metadata.get("container"):
                print(active_group.playback_metadata)  # noqa: T201
                if container.get("type") == "linein":
                    self.mass_player.active_source = SOURCE_LINE_IN
                elif container.get("type") == "linein.airplay":
                    self.mass_player.active_source = SOURCE_AIRPLAY
                else:
                    self.mass_player.active_source = None

                if (current_item := container.get("currentItem")) and (
                    track := current_item.get("track")
                ):
                    track_images = track.get("images", [])
                    track_image_url = track_images[0].get("url") if track_images else None
                    track_duration_millis = track.get("durationMillis")
                    self.mass_player.current_media = PlayerMedia(
                        uri=current_item.get("uri"),
                        title=track["name"],
                        artist=track.get("artist", {}).get("name"),
                        album=track.get("album", {}).get("name"),
                        duration=track_duration_millis / 1000 if track_duration_millis else None,
                        image_url=track_image_url,
                    )
                elif container.get("name") and container.get("id"):
                    images = container.get("images", [])
                    image_url = images[0].get("url") if images else None
                    self.mass_player.current_media = PlayerMedia(
                        uri=container["id"]["objectId"],
                        title=container["name"],
                        image_url=image_url,
                    )
                else:
                    self.mass_player.current_media = None

        else:
            self.mass_player.group_childs = set()
            self.mass_player.synced_to = active_group.coordinator_id
            self.mass_player.active_source = active_group.coordinator_id
        self.mass_player.state = PLAYBACK_STATE_MAP[active_group.playback_state]
        self.mass_player.elapsed_time = active_group.position
        self.mass_player.can_sync_with = (
            tuple(x for x in self.prov.sonos_players if x != self.player_id),
        )


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
                if not mass_player.available:
                    self.logger.debug("Player back online: %s", mass_player.display_name)
                    sonos_player.client.player_ip = cur_address
                    await sonos_player.connect(self.mass.http_session)
                    mass_player.available = True
            # always update the latest discovery info
            sonos_player.discovery_info = info
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
            # most probably a syncgroup
            return (*base_entries, CONF_ENTRY_CROSSFADE, CONF_ENTRY_FLOW_MODE_HIDDEN_DISABLED)
        sw_gen = sonos_player.discovery_info["device"].get("swGen", 1)
        return (
            *base_entries,
            CONF_ENTRY_CROSSFADE,
            CONF_ENTRY_FLOW_MODE_HIDDEN_DISABLED,
            CONF_ENTRY_SAMPLE_RATES_SONOS_S1 if sw_gen == 1 else CONF_ENTRY_SAMPLE_RATES_SONOS_S2,
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
        # TODO: replace with playbackSession.suspend()
        await sonos_player.client.player.group.pause()

    async def cmd_play(self, player_id: str) -> None:
        """Send PLAY command to given player."""
        sonos_player = self.sonos_players[player_id]
        if sonos_player.client.player.is_passive:
            self.logger.debug(
                "Ignore PLAY command for %s: Player is synced to another player.",
                player_id,
            )
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
        await sonos_player.client.player.group.pause()

    async def cmd_volume_set(self, player_id: str, volume_level: int) -> None:
        """Send VOLUME_SET command to given player."""
        sonos_player = self.sonos_players[player_id]
        await sonos_player.client.player.set_volume(volume_level)

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
        sonos_player = self.sonos_players[player_id]
        await sonos_player.client.player.join_group(target_player)

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

        cloud_queue_url = f"{self.mass.streams.base_url}/sonos_queue/v2.3/"
        await sonos_player.client.player.group.play_cloud_queue(
            cloud_queue_url, http_authorization=media.queue_id, queue_version=player_id
        )

    async def enqueue_next_media(self, player_id: str, media: PlayerMedia) -> None:
        """Handle enqueuing of the next queue item on the player."""

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

    async def poll_player(self, player_id: str) -> None:
        """Poll player for state updates."""

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
        # connect the player first so we can fail early
        await sonos_player.connect()

        # collect supported features
        supported_features = set(PLAYER_FEATURES_BASE)
        if SonosCapability.AUDIO_CLIP in discovery_info["device"]["capabilities"]:
            supported_features.add(PlayerFeature.PLAY_ANNOUNCEMENT)
        if not sonos_player.client.player.has_fixed_volume:
            supported_features.add(PlayerFeature.VOLUME_SET)

        sonos_player.mass_player = mass_player = Player(
            player_id=player_id,
            provider=self.instance_id,
            type=PlayerType.PLAYER,
            name=display_name,
            available=True,
            powered=False,
            device_info=DeviceInfo(
                model=discovery_info["device"]["modelDisplayName"],
                manufacturer=self.manifest.name,
                address=address,
            ),
            supported_features=tuple(supported_features),
        )
        sonos_player.update_attributes()
        self.mass.players.register_or_update(mass_player)

        # register callback for state changed
        def on_player_event(event: SonosEvent) -> None:
            """Handle incoming event from player."""
            sonos_player.update_attributes()
            self.mass.players.update(player_id)

        sonos_player.client.subscribe(
            on_player_event, (SonosEventType.GROUP_UPDATED, SonosEventType.PLAYER_UPDATED)
        )

    async def _handle_sonos_queue_itemwindow(self, request: web.Request) -> web.Response:
        """
        Handle the Sonos CloudQueue ItemWindow endpoint.

        https://docs.sonos.com/reference/itemwindow
        """
        print("### Sonos Cloud Queue - VersItemWindowion endpoint ###")  # noqa: T201
        print(request.headers)  # noqa: T201
        print(request.query)  # noqa: T201
        print()  # noqa: T201
        queue_id = request.headers["Authorization"]
        reason = request.query["reason"]  # noqa: F841
        item_id = request.query.get("itemId")  # noqa: F841
        previous_window_size = request.query.get("previousWindowSize")  # noqa: F841
        upcoming_window_size = request.query.get("upcomingWindowSize")
        queue_version = request.query.get("queueVersion")
        context_version = request.query.get("contextVersion")
        is_explicit = request.query.get("isExplicit")  # noqa: F841
        self.logger.info("_handle_sonos_queue_itemwindow: %s", request.query)

        queue = self.mass.player_queues.get(queue_id)  # noqa: F841
        queue_items = self.mass.player_queues.items(queue_id, upcoming_window_size)
        sonos_queue_items = [
            {
                "id": item.queue_item_id,
                "deleted": False,
                "policies": {"canSkip": True, "canCrossfade": True},
                "track": {
                    "mediaUrl": self.mass.streams.resolve_stream_url(item),
                    "contentType": "audio/flac",
                    "name": item.name,
                    "imageUrl": self.mass.metadata.get_image_url(item, prefer_proxy=True),
                    "durationMillis": item.duration * 1000 if item.duration else None,
                    "artist": {
                        "name": item.media_item.artist_str,
                    },
                    "album": {
                        "name": item.media_item.album.name,
                    }
                    if item.media_item.album
                    else None,
                },
            }
            for item in queue_items
        ]
        result = {
            "includesBeginningOfQueue": True,
            "includesEndOfQueue": False,
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
        print("### Sonos Cloud Queue - Version endpoint ###")  # noqa: T201
        print(request.headers)  # noqa: T201
        print(request.query)  # noqa: T201
        print()  # noqa: T201
        queue_id = request.headers["Authorization"]
        queue = self.mass.player_queues.get(queue_id)
        result = {"contextVersion": str(queue.items), "queueVersion": str(queue.items)}
        return web.json_response(result)

    async def _handle_sonos_queue_context(self, request: web.Request) -> web.Response:
        """
        Handle the Sonos CloudQueue Context endpoint.

        https://docs.sonos.com/reference/context
        """
        print("### Sonos Cloud Queue - Context endpoint ###")  # noqa: T201
        print(request.headers)  # noqa: T201
        print(request.query)  # noqa: T201
        print()  # noqa: T201
        sonos_request_id = request.headers["X-Sonos-Playback-Id"]
        sonos_player_id = sonos_request_id.split(":")[0]  # noqa: F841
        context_version = request.query.get("contextVersion")
        queue_version = request.query.get("queue_version")
        result = {
            "contextVersion": context_version,
            "queueVersion": queue_version,
            "container": {
                "type": "playlist",
                "name": "Music Assistant",
                "service": {"name": "mass"},
            },
            "reports": {"sendUpdateAfterMillis": 30000, "sendPlaybackActions": True},
            "id": {
                "serviceId": "8",
                "objectId": "music:user:john.musiclover:playlist:5t33Mtrb6rFBqvgz0U2DQe",
                "accountId": "john.musiclover",
            },
            "playbackPolicies": {
                "canSkip": True,
                "limitedSkips": False,
                "canSkipToItem": True,
                "canSkipBack": True,
                "canSeek": False,
                "canRepeat": False,
                "canRepeatOne": False,
                "canCrossfade": True,
                "canShuffle": False,
                "showNNextTracks": 10,
                "showNPreviousTracks": 0,
            },
        }
        return web.json_response(result)

    async def _handle_sonos_queue_time_played(self, request: web.Request) -> web.Response:
        """
        Handle the Sonos CloudQueue TimePlayed endpoint.

        https://docs.sonos.com/reference/timeplayed
        """
        print("### Sonos Cloud Queue - TimePlayed endpoint ###")  # noqa: T201
        print(request.headers)  # noqa: T201
        print(request.query)  # noqa: T201
        json_body = await request.json()
        print(json_body)  # noqa: T201
        print()  # noqa: T201


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
