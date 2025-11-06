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
    from music_assistant_models.player import PlayerMedia
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
            # We also need to run setup again on the Sonos player to ensure the supported
            # features are updated.
            await sonos_player.setup()

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

    async def _handle_sonos_queue_itemwindow(self, request: web.Request) -> web.Response:
        """
        Handle the Sonos CloudQueue ItemWindow endpoint.

        https://docs.sonos.com/reference/itemwindow
        """
        self.logger.log(VERBOSE_LOG_LEVEL, "Cloud Queue ItemWindow request: %s", request.query)
        sonos_playback_id = request.headers["X-Sonos-Playback-Id"]
        sonos_player_id = sonos_playback_id.split(":")[0]
        if not (sonos_player := self.mass.players.get(sonos_player_id)):
            return web.Response(status=501)
        if TYPE_CHECKING:
            assert isinstance(sonos_player, SonosPlayer)

        context_version = request.query.get("contextVersion", "1")
        queue_version = request.query.get(
            "queueVersion", str(int(sonos_player.sonos_queue.last_updated))
        )
        # because Sonos does not show our queue in the app anyways,
        # we just return the previous, current and next item in the queue
        items = list(sonos_player.sonos_queue.items)
        result = {
            "includesBeginningOfQueue": False,
            "includesEndOfQueue": False,
            "contextVersion": context_version,
            "queueVersion": queue_version,
            "items": [self._parse_sonos_queue_item(x) for x in items],
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
        if not (sonos_player := self.mass.players.get(sonos_player_id)):
            return web.Response(status=501)
        if TYPE_CHECKING:
            assert isinstance(sonos_player, SonosPlayer)

        context_version = request.query.get("contextVersion") or "1"
        result = {
            "contextVersion": context_version,
            "queueVersion": str(int(sonos_player.sonos_queue.last_updated)),
        }
        return web.json_response(result)

    async def _handle_sonos_queue_context(self, request: web.Request) -> web.Response:
        """
        Handle the Sonos CloudQueue Context endpoint.

        https://docs.sonos.com/reference/context
        """
        self.logger.log(VERBOSE_LOG_LEVEL, "Cloud Queue Context request: %s", request.query)
        sonos_playback_id = request.headers["X-Sonos-Playback-Id"]
        sonos_player_id = sonos_playback_id.split(":")[0]
        if not (sonos_player := self.mass.players.get(sonos_player_id)):
            return web.Response(status=501)
        if TYPE_CHECKING:
            assert isinstance(sonos_player, SonosPlayer)

        result = {
            "contextVersion": "1",
            "queueVersion": str(int(sonos_player.sonos_queue.last_updated)),
            "container": {
                "type": "trackList",
                "name": "Music Assistant",
                "imageUrl": MASS_LOGO_ONLINE,
                "service": {"name": "Music Assistant", "id": "mass"},
                "id": {
                    "serviceId": "mass",
                    "objectId": f"mass:{sonos_player.sonos_queue.items[-1].source_id}"
                    if sonos_player.sonos_queue.items
                    else "mass:unknown",
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

    async def _handle_sonos_queue_time_played(self, request: web.Request) -> web.Response:
        """
        Handle the Sonos CloudQueue TimePlayed endpoint.

        https://docs.sonos.com/reference/timeplayed
        """
        self.logger.log(VERBOSE_LOG_LEVEL, "Cloud Queue TimePlayed request: %s", request.query)
        json_body = await request.json()
        sonos_playback_id = request.headers["X-Sonos-Playback-Id"]
        sonos_player_id = sonos_playback_id.split(":")[0]
        if not (sonos_player := self.mass.players.get(sonos_player_id)):
            return web.Response(status=501)
        if TYPE_CHECKING:
            assert isinstance(sonos_player, SonosPlayer)
        for item in json_body["items"]:
            if item["type"] != "update":
                continue
            if "positionMillis" not in item:
                continue
            if (
                sonos_player.current_media
                and sonos_player.current_media.queue_item_id == item["id"]
            ):
                sonos_player.update_elapsed_time(item["positionMillis"] / 1000)
            break
        return web.Response(status=204)

    def _parse_sonos_queue_item(self, media: PlayerMedia) -> dict[str, Any]:
        """Parse MusicAssistant PlayerMedia to a Sonos Media (queue) object."""
        return {
            "id": media.queue_item_id or media.uri,
            "track": {
                "type": "track",
                "mediaUrl": media.uri,
                "contentType": f"audio/{media.uri.split('.')[-1]}",
                "service": {"name": "Music Assistant", "id": "mass"},
                "name": media.title,
                "imageUrl": media.image_url,
                "durationMillis": int(media.duration * 1000) if media.duration else 0,
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
