"""
Sonos Player provider for Music Assistant for speakers running the S2 firmware.

Based on the aiosonos library, which leverages the new websockets API of the Sonos S2 firmware.
https://github.com/music-assistant/aiosonos
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, cast

from aiohttp import web
from aiohttp.client_exceptions import ClientError
from aiosonos.api.models import SonosCapability
from aiosonos.utils import get_discovery_info
from music_assistant_models.enums import EventType, IdentifierType
from music_assistant_models.errors import MusicAssistantError
from zeroconf import ServiceStateChange

from music_assistant.constants import (
    CONF_ENTRY_MANUAL_DISCOVERY_IPS,
    CONF_LOG_LEVEL,
    MASS_LOGO_ONLINE,
    VERBOSE_LOG_LEVEL,
)
from music_assistant.helpers.audio import get_mime_type
from music_assistant.helpers.json import SerializableType
from music_assistant.models.player_provider import PlayerProvider

from .helpers import get_primary_ip_address
from .player import SonosPlayer, SonosQueueWindow

if TYPE_CHECKING:
    from collections.abc import Callable

    from music_assistant_models.config_entries import ConfigEntry, ProviderConfig
    from music_assistant_models.event import MassEvent
    from music_assistant_models.player import PlayerMedia
    from zeroconf.asyncio import AsyncServiceInfo


def _refresh_task_id(player_id: str) -> str:
    """Return the debounce id for a speaker's pending cloud-queue refresh."""
    return f"sonos_refresh_cloud_queue_{player_id}"


class SonosPlayerProvider(PlayerProvider):
    """Sonos Player provider."""

    _ignored_disabled_players: set[str]
    _pending_setup_tasks: set[str]
    _pending_refresh_tasks: set[str]
    _unloaded: bool
    _unsub_queue_items_updated: Callable[[], None] | None = None

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to setup this provider."""
        return (CONF_ENTRY_MANUAL_DISCOVERY_IPS,)

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self._ignored_disabled_players = set()
        self._pending_setup_tasks = set()
        self._pending_refresh_tasks = set()
        self._unloaded = False
        self._set_aiosonos_log_level()
        self.mass.streams.register_dynamic_route(
            "/sonos_queue/*", self._handle_sonos_cloud_queue_request
        )
        self._unsub_queue_items_updated = self.mass.subscribe(
            self._handle_queue_items_updated, EventType.QUEUE_ITEMS_UPDATED
        )

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        await super().loaded_in_mass()
        # Handle config option for manual IP's
        manual_ip_config = cast(
            "list[str]", self.config.get_value(CONF_ENTRY_MANUAL_DISCOVERY_IPS.key)
        )
        for ip_address in manual_ip_config:
            try:
                # get discovery info from SONOS speaker so we can provide an ID & other info
                discovery_info = await get_discovery_info(self.mass.http_session_no_ssl, ip_address)
            except ClientError as err:
                self.logger.debug(
                    "Ignoring %s (manual IP) as it is not reachable: %s", ip_address, str(err)
                )
                continue
            player_id = discovery_info["device"]["id"]
            sonos_player = SonosPlayer(self, player_id, discovery_info=discovery_info)
            sonos_player.device_info.add_identifier(IdentifierType.IP_ADDRESS, ip_address)
            await sonos_player.setup()

    async def unload(self, is_removed: bool = False) -> None:
        """Handle close/cleanup of the provider."""
        self._unloaded = True
        self.mass.streams.unregister_dynamic_route("/sonos_queue/*")
        if self._unsub_queue_items_updated is not None:
            self._unsub_queue_items_updated()
            self._unsub_queue_items_updated = None
        for task_id in self._pending_setup_tasks | self._pending_refresh_tasks:
            # a timer that already fired lives on as a task under the same id,
            # so both are needed to cover the pending and the running case
            self.mass.cancel_timer(task_id)
            self.mass.cancel_task(task_id)
        self._pending_setup_tasks.clear()
        self._pending_refresh_tasks.clear()

    async def update_config(self, config: ProviderConfig, changed_keys: set[str]) -> None:
        """Handle logic when the config is updated."""
        await super().update_config(config, changed_keys)
        # a log level(-only) change does not reload the provider,
        # so realign aiosonos's logger here
        if f"values/{CONF_LOG_LEVEL}" in changed_keys:
            self._set_aiosonos_log_level()

    async def get_diagnostics(self) -> dict[str, SerializableType]:
        """Return diagnostics info for this provider to include in diagnostics reports."""
        sonos_players = [player for player in self.players if isinstance(player, SonosPlayer)]
        # active_output_protocol holds "native" or the player id of the protocol player in use
        active_protocols = [
            player.active_output_protocol
            for player in sonos_players
            if player.active_output_protocol
        ]
        return {
            "speakers_total": len(sonos_players),
            "speakers_connected": sum(player.connected for player in sonos_players),
            "coordinators": sum(
                player.client.player.is_coordinator for player in sonos_players if player.connected
            ),
            "native_playback": sum(protocol == "native" for protocol in active_protocols),
            "protocol_playback": sum(protocol != "native" for protocol in active_protocols),
        }

    async def on_mdns_service_state_change(
        self, name: str, state_change: ServiceStateChange, info: AsyncServiceInfo | None
    ) -> None:
        """Handle MDNS service state callback."""
        if self._unloaded:
            # discovery resolves an announcement before it dispatches it, so a callback
            # picked up before the unload can still arrive after it
            return
        if state_change == ServiceStateChange.Removed:
            # we don't listen for removed players here.
            # instead we just wait for the player connection to fail
            return
        assert info is not None  # for type checking
        if "uuid" not in info.decoded_properties:
            # not a S2 player
            return
        name = name.split("@", 1)[1] if "@" in name else name
        player_id = info.decoded_properties["uuid"]
        assert isinstance(player_id, str)  # for type checking
        # handle update for existing device
        if sonos_player := self.mass.players.get_player(player_id):
            assert isinstance(sonos_player, SonosPlayer), (
                "Player ID already exists but is not a SonosPlayer"
            )
            # if mass_player := sonos_player.mass_player:
            cur_address = get_primary_ip_address(info)
            if cur_address and cur_address != sonos_player.device_info.ip_address:
                sonos_player.logger.debug(
                    "Address updated from %s to %s",
                    sonos_player.device_info.ip_address,
                    cur_address,
                )
                sonos_player.device_info.add_identifier(IdentifierType.IP_ADDRESS, cur_address)
            if not sonos_player.connected and cur_address:
                self.logger.debug("Player back online: %s", sonos_player.display_name)
                sonos_player.client.player_ip = cur_address
                # schedule reconnect
                sonos_player.reconnect()
            self.mass.players.trigger_player_update(player_id)
            return
        if self._ignore_disabled_discovery(player_id, name):
            return
        # handle new player setup in a delayed task because mdns announcements
        # can arrive in (duplicated) bursts
        task_id = f"setup_sonos_{player_id}"
        self._pending_setup_tasks.add(task_id)
        self.mass.call_later(5, self._setup_player, player_id, name, info, task_id=task_id)

    def _handle_queue_items_updated(self, event: MassEvent) -> None:
        """Tell the speakers playing a queue that its contents changed."""
        for player in self.players:
            if not isinstance(player, SonosPlayer) or player.cloud_queue_id != event.object_id:
                continue
            # invalidate straight away: a window served before the command goes out must not
            # carry a version the speaker reads as "nothing changed"
            player.bump_cloud_queue_version()
            # anything that touches the items fires this - an insert, an autoplay refill, a
            # duration that was filled in - and one edit can fan out into several, so coalesce
            # the command itself into one per speaker
            task_id = _refresh_task_id(player.player_id)
            self._pending_refresh_tasks.add(task_id)
            self.mass.call_later(1, player.refresh_cloud_queue, task_id=task_id)

    def _set_aiosonos_log_level(self) -> None:
        """Align aiosonos's log level with the provider's log level."""
        # aiosonos is very chatty at debug level, so only pass through its
        # debug logging when verbose logging is enabled
        if self.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
            logging.getLogger("aiosonos").setLevel(logging.DEBUG)
        else:
            logging.getLogger("aiosonos").setLevel(self.logger.level + 10)

    async def _setup_player(self, player_id: str, name: str, info: AsyncServiceInfo) -> None:
        """Handle setup of a new player that is discovered using mdns."""
        if self.mass.players.get_player(player_id):
            msg = f"Player {player_id} already exists"
            raise ValueError(msg)
        if self._ignore_disabled_discovery(player_id, name):
            return
        address = get_primary_ip_address(info)
        if address is None:
            return
        try:
            discovery_info = await get_discovery_info(self.mass.http_session_no_ssl, address)
        except ClientError as err:
            self.logger.debug("Ignoring %s in discovery as it is not reachable: %s", name, str(err))
            return
        display_name = discovery_info["device"].get("name") or name
        if SonosCapability.PLAYBACK not in discovery_info["device"]["capabilities"]:
            # this will happen for satellite speakers in a surround/stereo setup
            self.logger.debug(
                "Ignoring %s in discovery as it is a passive satellite.", display_name
            )
            return
        self.logger.debug("Discovered Sonos device %s on %s", name, address)
        sonos_player = SonosPlayer(self, player_id, discovery_info=discovery_info)
        sonos_player.device_info.add_identifier(IdentifierType.IP_ADDRESS, address)
        await sonos_player.setup()

    def _ignore_disabled_discovery(self, player_id: str, name: str) -> bool:
        """
        Return whether discovery should ignore a disabled player.

        :param player_id: The discovered Sonos player ID.
        :param name: The discovered Sonos service name.
        """
        if self.mass.config.get_raw_player_config_value(player_id, "enabled", True):
            self._ignored_disabled_players.discard(player_id)
            return False
        if player_id not in self._ignored_disabled_players:
            self.logger.debug("Ignoring %s in discovery as it is disabled.", name)
            self._ignored_disabled_players.add(player_id)
        return True

    async def _handle_sonos_cloud_queue_request(self, request: web.Request) -> web.Response:
        """
        Handle the Sonos CloudQueue request.

        https://docs.sonos.com/reference/itemwindow
        """
        self.logger.log(
            VERBOSE_LOG_LEVEL,
            "Cloud Queue request\n - path: %s\n - query: %s\n",
            request.path,
            request.query,
        )
        path_parts = request.path.strip("/").split("/")
        if len(path_parts) != 4 or path_parts[0] != "sonos_queue":
            return web.Response(status=404)
        player_id = path_parts[1]
        if not (sonos_player := self.mass.players.get_player(player_id)):
            return web.Response(status=501)
        if TYPE_CHECKING:
            assert isinstance(sonos_player, SonosPlayer)
        endpoint = path_parts[3]
        if endpoint == "itemWindow":
            return await self._handle_sonos_queue_itemwindow(sonos_player, request)
        if endpoint == "version":
            return await self._handle_sonos_queue_version(sonos_player, request)
        if endpoint == "context":
            return await self._handle_sonos_queue_context(sonos_player, request)
        if endpoint == "timePlayed":
            return await self._handle_sonos_queue_time_played(sonos_player, request)
        return web.Response(status=404)

    async def _handle_sonos_queue_itemwindow(
        self, player: SonosPlayer, request: web.Request
    ) -> web.Response:
        """
        Handle the Sonos CloudQueue ItemWindow endpoint.

        https://docs.sonos.com/reference/itemwindow
        """
        context_version = request.query.get("contextVersion", "1")
        # read the version before building, so the items and the version we label them with
        # always come from the same queue: a bump landing in between would tell the speaker
        # its cache is current while it holds the older window
        queue_version = player.cloud_queue_version
        # the window is built from the queue as it is right now. The speaker fetches on its
        # own schedule and caches what it gets, so answering from the live queue is what keeps
        # a track added mid-playback (a party request, a reorder) from being played over.
        # the sizes the speaker asks for are maxima and we deliberately serve far fewer, so it
        # comes back for every track - see PREVIOUS_ITEMS/UPCOMING_ITEMS.
        # the beginning/end flags must be honest: signalling end-of-queue tells Sonos to
        # drop any older items it may still have cached past our window, which is what
        # prevents stale tracks from resurrecting after a queue rewrite (e.g. replace_next).
        try:
            window = await player.build_cloud_queue_window(request.query.get("itemId") or None)
        except MusicAssistantError as err:
            # the queue went away underneath us (a stop that never reached this speaker, so it
            # keeps polling). An empty end-of-queue window is what should happen next anyway,
            # and it beats answering every poll with a 500.
            self.logger.debug("Cannot describe the queue for %s: %s", player.display_name, err)
            window = SonosQueueWindow(includes_beginning=True, includes_end=True)
        result = {
            "includesBeginningOfQueue": window.includes_beginning,
            "includesEndOfQueue": window.includes_end,
            "contextVersion": context_version,
            # report the version of the items we actually serve instead of echoing the
            # player's requested version, otherwise a changed queue keeps a stale version
            # label and Sonos never realises it changed.
            "queueVersion": str(queue_version),
            "items": [self._parse_sonos_queue_item(x) for x in window.items],
        }
        return web.json_response(result)

    async def _handle_sonos_queue_version(
        self, player: SonosPlayer, request: web.Request
    ) -> web.Response:
        """
        Handle the Sonos CloudQueue Version endpoint.

        https://docs.sonos.com/reference/version
        """
        context_version = request.query.get("contextVersion") or "1"
        # keep sub-second resolution: the queue can change several times within the same
        # second and Sonos treats an unchanged queueVersion as "nothing changed" (stale window).
        result = {
            "contextVersion": context_version,
            "queueVersion": str(player.cloud_queue_version),
        }
        return web.json_response(result)

    async def _handle_sonos_queue_context(
        self, player: SonosPlayer, request: web.Request
    ) -> web.Response:
        """
        Handle the Sonos CloudQueue Context endpoint.

        https://docs.sonos.com/reference/context
        """
        result = {
            "contextVersion": "1",
            "queueVersion": str(player.cloud_queue_version),
            "container": {
                "type": "trackList",
                "name": "Music Assistant",
                "imageUrl": MASS_LOGO_ONLINE,
                "service": {"name": "Music Assistant", "id": "mass"},
                "id": {
                    "serviceId": "mass",
                    "objectId": f"mass:{player.cloud_queue_id or 'unknown'}",
                    "accountId": "",
                },
            },
            "reports": {
                "sendUpdateAfterMillis": 1000,
                "periodicIntervalMillis": 30000,
                "sendPlaybackActions": True,
            },
            "playbackPolicies": {
                "canSkip": True,
                "limitedSkips": True,
                "canSkipToItem": True,  # unsure
                "canSkipBack": True,
                # seek needs to be disabled because we dont properly support range requests
                "canSeek": False,
                "canRepeat": False,  # handled by MA queue controller
                "canRepeatOne": False,  # synced from MA queue controller
                "canCrossfade": False,  # handled by MA queue controller
                "canShuffle": False,  # handled by MA queue controller
            },
        }
        return web.json_response(result)

    async def _handle_sonos_queue_time_played(
        self, player: SonosPlayer, request: web.Request
    ) -> web.Response:
        """
        Handle the Sonos CloudQueue TimePlayed endpoint.

        https://docs.sonos.com/reference/timeplayed
        """
        json_body = await request.json()
        for item in json_body["items"]:
            if item["type"] != "update":
                continue
            if "positionMillis" not in item:
                continue
            if player.current_media and player.current_media.queue_item_id == item["id"]:
                player.update_elapsed_time(item["positionMillis"] / 1000)
            break
        return web.Response(status=204)

    def _parse_sonos_queue_item(self, media: PlayerMedia) -> dict[str, Any]:
        """Parse MusicAssistant PlayerMedia to a Sonos Media (queue) object."""
        # the speaker tracks its position within the audio we serve, which is
        # shorter than the media item when playback starts at a seek position
        duration = media.stream_duration or media.duration
        return {
            "id": media.queue_item_id or media.uri,
            "track": {
                "type": "track",
                "mediaUrl": media.uri,
                "contentType": get_mime_type(media.uri.split(".")[-1]),
                "service": {"name": "Music Assistant", "id": "mass"},
                "name": media.title,
                "imageUrl": media.image_url,
                "durationMillis": int(duration * 1000) if duration else 0,
                "artist": {
                    "name": media.artist,
                }
                if media.artist
                else None,
                "album": {
                    "name": media.album,
                }
                if media.album
                else None,
            },
        }
