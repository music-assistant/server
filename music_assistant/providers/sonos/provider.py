"""
Sonos Player provider for Music Assistant for speakers running the S2 firmware.

Based on the aiosonos library, which leverages the new websockets API of the Sonos S2 firmware.
https://github.com/music-assistant/aiosonos
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aiohttp import web
from aiohttp.client_exceptions import ClientError
from aiosonos.api.models import SonosCapability
from aiosonos.utils import get_discovery_info
from music_assistant_models.enums import PlaybackState
from zeroconf import ServiceStateChange

from music_assistant.constants import (
    CONF_ENTRY_MANUAL_DISCOVERY_IPS,
    MASS_LOGO_ONLINE,
    VERBOSE_LOG_LEVEL,
)
from music_assistant.models.player_provider import PlayerProvider

from .helpers import get_primary_ip_address
from .player import SonosPlayer

if TYPE_CHECKING:
    from music_assistant_models.config_entries import PlayerConfig
    from music_assistant_models.queue_item import QueueItem
    from zeroconf.asyncio import AsyncServiceInfo


class SonosPlayerProvider(PlayerProvider):
    """Sonos Player provider."""

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
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

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        await super().loaded_in_mass()
        # Handle config option for manual IP's
        manual_ip_config: list[str] = self.config.get_value(CONF_ENTRY_MANUAL_DISCOVERY_IPS.key)
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
            sonos_player.device_info.ip_address = ip_address
            await sonos_player.setup()

    async def unload(self, is_removed: bool = False) -> None:
        """Handle close/cleanup of the provider."""
        self.mass.streams.unregister_dynamic_route("/sonos_queue/v2.3/itemWindow")
        self.mass.streams.unregister_dynamic_route("/sonos_queue/v2.3/version")
        self.mass.streams.unregister_dynamic_route("/sonos_queue/v2.3/context")
        self.mass.streams.unregister_dynamic_route("/sonos_queue/v2.3/timePlayed")

    async def on_mdns_service_state_change(
        self, name: str, state_change: ServiceStateChange, info: AsyncServiceInfo | None
    ) -> None:
        """Handle MDNS service state callback."""
        if state_change == ServiceStateChange.Removed:
            # we don't listen for removed players here.
            # instead we just wait for the player connection to fail
            return
        if "uuid" not in info.decoded_properties:
            # not a S2 player
            return
        name = name.split("@", 1)[1] if "@" in name else name
        player_id = info.decoded_properties["uuid"]
        # handle update for existing device
        if sonos_player := self.mass.players.get(player_id):
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
                sonos_player.device_info.ip_address = cur_address
            if not sonos_player.connected:
                self.logger.debug("Player back online: %s", sonos_player.display_name)
                sonos_player.client.player_ip = cur_address
                # schedule reconnect
                sonos_player.reconnect()
            self.mass.players.trigger_player_update(player_id)
            return
        # handle new player setup in a delayed task because mdns announcements
        # can arrive in (duplicated) bursts
        task_id = f"setup_sonos_{player_id}"
        self.mass.call_later(5, self._setup_player, player_id, name, info, task_id=task_id)

    async def on_player_config_change(self, config: PlayerConfig, changed_keys: set[str]) -> None:
        """Call (by config manager) when the configuration of a player changes."""
        await super().on_player_config_change(config, changed_keys)
        if "values/airplay_mode" in changed_keys and (
            (sonos_player := self.mass.players.get(config.player_id))
            and (airplay_player := sonos_player.get_linked_airplay_player(False))
            and airplay_player.playback_state in (PlaybackState.PLAYING, PlaybackState.PAUSED)
        ):
            # edge case: we switched from airplay mode to sonos mode (or vice versa)
            # we need to make sure that playback gets stopped on the airplay player
            await airplay_player.stop()

    async def _setup_player(self, player_id: str, name: str, info: AsyncServiceInfo) -> None:
        """Handle setup of a new player that is discovered using mdns."""
        assert not self.mass.players.get(player_id)
        address = get_primary_ip_address(info)
        if address is None:
            return
        if not self.mass.config.get_raw_player_config_value(player_id, "enabled", True):
            self.logger.debug("Ignoring %s in discovery as it is disabled.", name)
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
        sonos_player.device_info.ip_address = address
        await sonos_player.setup()
        # # trigger update on all existing players to update the group status
        # for _player in self.sonos_players.values():
        #     if _player.player_id != player_id:
        #         _player.on_player_event(None)

    async def _handle_sonos_queue_itemwindow(self, request: web.Request) -> web.Response:
        """
        Handle the Sonos CloudQueue ItemWindow endpoint.

        https://docs.sonos.com/reference/itemwindow
        """
        self.logger.log(VERBOSE_LOG_LEVEL, "Cloud Queue ItemWindow request: %s", request.query)
        sonos_playback_id = request.headers["X-Sonos-Playback-Id"]
        sonos_player_id = sonos_playback_id.split(":")[0]
        queue_version = request.query.get("queueVersion")
        context_version = request.query.get("contextVersion")
        if not (mass_queue := self.mass.player_queues.get_active_queue(sonos_player_id)):
            return web.Response(status=501)
        if item_id := request.query.get("itemId"):
            cur_queue_index = self.mass.player_queues.index_by_id(mass_queue.queue_id, item_id)
        else:
            cur_queue_index = mass_queue.current_index
        if cur_queue_index is None:
            return web.Response(status=501)
        # because Sonos does not show our queue in the app anyways,
        # we just return the current and 2 next items in the queue
        cur_queue_item = self.mass.player_queues.get_item(mass_queue.queue_id, cur_queue_index)
        queue_items = [cur_queue_item]
        if next_queue_item := self.mass.player_queues.get_next_item(
            mass_queue.queue_id, cur_queue_index
        ):
            queue_items.append(next_queue_item)
            if next_next_queue_item := self.mass.player_queues.get_next_item(
                mass_queue.queue_id, next_queue_item.queue_item_id
            ):
                queue_items.append(next_next_queue_item)
        result = {
            "includesBeginningOfQueue": False,
            "includesEndOfQueue": True,
            "contextVersion": context_version,
            "queueVersion": queue_version,
            "items": [await self._parse_sonos_queue_item(item) for item in queue_items],
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
        if not (self.mass.players.get(sonos_player_id)):
            return web.Response(status=501)
        mass_queue = self.mass.player_queues.get_active_queue(sonos_player_id)
        context_version = request.query.get("contextVersion") or "1"
        queue_version = str(int(mass_queue.items_last_updated)) if mass_queue else "0"
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
        if not (self.mass.players.get(sonos_player_id)):
            return web.Response(status=501)
        result = {
            "contextVersion": "1",
            "queueVersion": str(int(mass_queue.items_last_updated)),
            "container": {
                "type": "playlist",
                "name": "Music Assistant",
                "imageUrl": MASS_LOGO_ONLINE,
                "service": {"name": "Music Assistant", "id": "mass"},
                "id": {
                    "serviceId": "mass",
                    "objectId": f"mass:{mass_queue.queue_id}",
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
                "limitedSkips": False,
                "canSkipToItem": False,  # unsure
                "canSkipBack": True,
                # seek needs to be disabled because we dont properly support range requests
                "canSeek": False,
                "canRepeat": False,  # handled by MA queue controller
                "canRepeatOne": True,  # synced from MA queue controller
                "canCrossfade": False,  # handled by MA queue controller
                "canShuffle": False,  # handled by MA queue controller
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
        if not (self.mass.players.get(sonos_player_id)):
            return web.Response(status=501)
        for item in json_body["items"]:
            if item["type"] != "update":
                continue
            if "positionMillis" not in item:
                continue
            if mass_player.current_media and mass_player.current_media.queue_item_id == item["id"]:
                mass_player.update_elapsed_time(item["positionMillis"] / 1000)
            break
        return web.Response(status=204)

    async def _parse_sonos_queue_item(self, queue_item: QueueItem) -> dict[str, Any]:
        """Parse a MusicAssistant QueueItem to a Sonos Media (queue) object."""
        queue = self.mass.player_queues.get(queue_item.queue_id)
        assert queue  # for type checking
        stream_url = await self.mass.streams.resolve_stream_url(queue.session_id, queue_item)
        if streamdetails := queue_item.streamdetails:
            duration = streamdetails.duration or queue_item.duration
            if duration and streamdetails.seek_position:
                duration -= streamdetails.seek_position
        else:
            duration = queue_item.duration

        return {
            "id": queue_item.queue_item_id,
            "deleted": not queue_item.available,
            "policies": {
                "canCrossfade": False,  # crossfading is handled by our streams controller
                "canSkip": True,
                "canSkipBack": True,
                "canSkipToItem": True,
                # seek needs to be disabled because we dont properly support range requests
                "canSeek": False,
                "canRepeat": True,
                "canRepeatOne": True,
                "canShuffle": True,
            },
            "track": {
                "type": "track",
                "mediaUrl": stream_url,
                "contentType": f"audio/{stream_url.split('.')[-1]}",
                "service": {
                    "name": "Music Assistant",
                    "id": "8",
                    "accountId": "",
                    "objectId": queue_item.queue_item_id,
                },
                "name": queue_item.media_item.name if queue_item.media_item else queue_item.name,
                "imageUrl": self.mass.metadata.get_image_url(
                    queue_item.image, prefer_proxy=False, image_format="jpeg"
                )
                if queue_item.image
                else None,
                "durationMillis": duration * 1000 if duration else None,
                "artist": {
                    "name": artist_str,
                }
                if queue_item.media_item
                and (artist_str := getattr(queue_item.media_item, "artist_str", None))
                else None,
                "album": {
                    "name": album.name,
                }
                if queue_item.media_item
                and (album := getattr(queue_item.media_item, "album", None))
                else None,
            },
        }
