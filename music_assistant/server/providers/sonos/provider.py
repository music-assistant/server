"""
Sonos Player provider for Music Assistant for speakers running the S2 firmware.

Based on the aiosonos library, which leverages the new websockets API of the Sonos S2 firmware.
https://github.com/music-assistant/aiosonos
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import shortuuid
from aiohttp import web
from aiohttp.client_exceptions import ClientError
from aiosonos.api.models import SonosCapability
from aiosonos.utils import get_discovery_info
from zeroconf import ServiceStateChange

from music_assistant.common.models.config_entries import (
    CONF_ENTRY_CROSSFADE,
    CONF_ENTRY_ENFORCE_MP3,
    CONF_ENTRY_FLOW_MODE_HIDDEN_DISABLED,
    ConfigEntry,
    create_sample_rates_config_entry,
)
from music_assistant.common.models.enums import ConfigEntryType, ContentType, ProviderFeature
from music_assistant.common.models.errors import PlayerCommandFailed
from music_assistant.common.models.player import DeviceInfo, PlayerMedia
from music_assistant.constants import MASS_LOGO_ONLINE, VERBOSE_LOG_LEVEL
from music_assistant.server.helpers.tags import parse_tags
from music_assistant.server.models.player_provider import PlayerProvider

from .const import CONF_AIRPLAY_MODE
from .helpers import get_primary_ip_address
from .player import SonosPlayer

if TYPE_CHECKING:
    from zeroconf.asyncio import AsyncServiceInfo


class SonosPlayerProvider(PlayerProvider):
    """Sonos Player provider."""

    sonos_players: dict[str, SonosPlayer]

    @property
    def supported_features(self) -> tuple[ProviderFeature, ...]:
        """Return the features supported by this Provider."""
        return (ProviderFeature.SYNC_PLAYERS,)

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
        await asyncio.gather(*(player.unload() for player in self.sonos_players.values()))
        self.sonos_players = None
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
        if sonos_player := self.sonos_players.get(player_id):
            if mass_player := sonos_player.mass_player:
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
                    # schedule reconnect
                    sonos_player.reconnect()
                self.mass.players.update(player_id)
            return
        # handle new player setup in a delayed task because mdns announcements
        # can arrive in (duplicated) bursts
        task_id = f"setup_sonos_{player_id}"
        self.mass.call_later(5, self._setup_player, player_id, name, info, task_id=task_id)

    async def get_player_config_entries(
        self,
        player_id: str,
    ) -> tuple[ConfigEntry, ...]:
        """Return Config Entries for the given player."""
        base_entries = (
            *await super().get_player_config_entries(player_id),
            CONF_ENTRY_CROSSFADE,
            CONF_ENTRY_FLOW_MODE_HIDDEN_DISABLED,
            CONF_ENTRY_ENFORCE_MP3,
            create_sample_rates_config_entry(48000, 24, 48000, 24, True),
        )
        if not (sonos_player := self.sonos_players.get(player_id)):
            # most probably the player is not yet discovered
            return base_entries
        return (
            *base_entries,
            ConfigEntry(
                key=CONF_AIRPLAY_MODE,
                type=ConfigEntryType.BOOLEAN,
                label="Enable Airplay mode (experimental)",
                description="Almost all newer Sonos speakers have Airplay support. "
                "If you have the Airplay provider enabled in Music Assistant, "
                "your Sonos speakers will also be detected as Airplay speakers, meaning "
                "you can group them with other Airplay speakers.\n\n"
                "By default, Music Assistant uses the Sonos protocol for playback but with this "
                "feature enabled, it will use the Airplay protocol instead by redirecting "
                "the playback related commands to the linked Airplay player in Music Assistant, "
                "allowing you to mix and match Sonos speakers with Airplay speakers. \n\n"
                "TIP: When this feature is enabled, it make sense to set the underlying airplay "
                "players to hide in the UI in the player settings to prevent duplicate players.",
                required=False,
                default_value=False,
                hidden=SonosCapability.AIRPLAY
                not in sonos_player.discovery_info["device"]["capabilities"],
            ),
        )

    async def cmd_stop(self, player_id: str) -> None:
        """Send STOP command to given player."""
        if sonos_player := self.sonos_players[player_id]:
            await sonos_player.cmd_stop()

    async def cmd_play(self, player_id: str) -> None:
        """Send PLAY command to given player."""
        if sonos_player := self.sonos_players[player_id]:
            await sonos_player.cmd_play()

    async def cmd_pause(self, player_id: str) -> None:
        """Send PAUSE command to given player."""
        if sonos_player := self.sonos_players[player_id]:
            await sonos_player.cmd_pause()

    async def cmd_volume_set(self, player_id: str, volume_level: int) -> None:
        """Send VOLUME_SET command to given player."""
        if sonos_player := self.sonos_players[player_id]:
            await sonos_player.cmd_volume_set(volume_level)

    async def cmd_volume_mute(self, player_id: str, muted: bool) -> None:
        """Send VOLUME MUTE command to given player."""
        if sonos_player := self.sonos_players[player_id]:
            await sonos_player.cmd_volume_mute(muted)

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
        await sonos_player.client.player.group.modify_group_members(
            player_ids_to_add=child_player_ids, player_ids_to_remove=[]
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
        sonos_player.queue_version = shortuuid.random(8)
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
            mass_player.active_source = airplay.active_source
            # Sonos has an annoying bug (for years already, and they dont seem to care),
            # where it looses its sync childs when airplay playback is (re)started.
            # Try to handle it here with this workaround.
            group_childs = (
                sonos_player.client.player.group_members
                if len(sonos_player.client.player.group_members) > 1
                else []
            )
            if group_childs:
                await self.mass.players.cmd_unsync_many(group_childs)
            await self.mass.players.play_media(airplay.player_id, media)
            if group_childs:
                self.mass.call_later(5, self.cmd_sync_many, player_id, group_childs)
            return

        if media.queue_id and media.queue_id.startswith("ugp_"):
            # Special UGP stream - handle with play URL
            await sonos_player.client.player.group.play_stream_url(media.uri, None)
            return

        if media.queue_id:
            # create a sonos cloud queue and load it
            cloud_queue_url = f"{self.mass.streams.base_url}/sonos_queue/v2.3/"
            await sonos_player.client.player.group.play_cloud_queue(
                cloud_queue_url,
                http_authorization=media.queue_id,
                item_id=media.queue_item_id,
                queue_version=sonos_player.queue_version,
            )
            return

        # play a single uri/url
        # note that this most probably will only work for (long running) radio streams
        if self.mass.config.get_raw_player_config_value(
            player_id, CONF_ENTRY_ENFORCE_MP3.key, CONF_ENTRY_ENFORCE_MP3.default_value
        ):
            media.uri = media.uri.replace(".flac", ".mp3")
        await sonos_player.client.player.group.play_stream_url(
            media.uri, {"name": media.title, "type": "track"}
        )

    async def cmd_next(self, player_id: str) -> None:
        """Handle NEXT TRACK command for given player."""
        if sonos_player := self.sonos_players[player_id]:
            await sonos_player.client.player.group.skip_to_next_track()

    async def cmd_previous(self, player_id: str) -> None:
        """Handle PREVIOUS TRACK command for given player."""
        if sonos_player := self.sonos_players[player_id]:
            await sonos_player.client.player.group.skip_to_previous_track()

    async def enqueue_next_media(self, player_id: str, media: PlayerMedia) -> None:
        """Handle enqueuing of the next queue item on the player."""
        # We do nothing here as we handle the queue in the cloud queue endpoint.
        # For sonos s2, instead of enqueuing tracks one by one, the sonos player itself
        # can interact with our queue directly through the cloud queue endpoint.

    async def play_announcement(
        self, player_id: str, announcement: PlayerMedia, volume_level: int | None = None
    ) -> None:
        """Handle (provider native) playback of an announcement on given player."""
        sonos_player = self.sonos_players[player_id]
        self.logger.debug(
            "Playing announcement %s on %s",
            announcement.uri,
            sonos_player.mass_player.display_name,
        )
        volume_level = self.mass.players.get_announcement_volume(player_id, volume_level)
        await sonos_player.client.player.play_audio_clip(
            announcement.uri, volume_level, name="Announcement"
        )
        # Wait until the announcement is finished playing
        # This is helpful for people who want to play announcements in a sequence
        # yeah we can also setup a subscription on the sonos player for this, but this is easier
        media_info = await parse_tags(announcement.uri)
        duration = media_info.duration or 10
        await asyncio.sleep(duration)

    async def _setup_player(self, player_id: str, name: str, info: AsyncServiceInfo) -> None:
        """Handle setup of a new player that is discovered using mdns."""
        assert player_id not in self.sonos_players
        address = get_primary_ip_address(info)
        if address is None:
            return
        if not self.mass.config.get_raw_player_config_value(player_id, "enabled", True):
            self.logger.debug("Ignoring %s in discovery as it is disabled.", name)
            return
        try:
            discovery_info = await get_discovery_info(self.mass.http_session, address)
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
        self.sonos_players[player_id] = sonos_player = SonosPlayer(
            self, player_id, discovery_info=discovery_info, ip_address=address
        )
        await sonos_player.setup()

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
            queue_index = mass_queue.current_index
        if queue_index is None:
            return web.Response(status=501)
        offset = max(queue_index - previous_window_size, 0)
        queue_items = self.mass.player_queues.items(
            mass_queue.queue_id,
            limit=upcoming_window_size + previous_window_size,
            offset=max(queue_index - previous_window_size, 0),
        )
        enforce_mp3 = self.mass.config.get_raw_player_config_value(
            sonos_player_id, CONF_ENTRY_ENFORCE_MP3.key, CONF_ENTRY_ENFORCE_MP3.default_value
        )
        sonos_queue_items = [
            {
                "id": item.queue_item_id,
                "deleted": not item.media_item.available,
                "policies": {},
                "track": {
                    "type": "track",
                    "mediaUrl": self.mass.streams.resolve_stream_url(
                        item, output_codec=ContentType.MP3 if enforce_mp3 else ContentType.FLAC
                    ),
                    "contentType": "audio/flac",
                    "service": {
                        "name": "Music Assistant",
                        "id": "8",
                        "accountId": "",
                        "objectId": item.queue_item_id,
                    },
                    "name": item.name,
                    "imageUrl": self.mass.metadata.get_image_url(
                        item.image, prefer_proxy=False, image_format="jpeg"
                    )
                    if item.image
                    else None,
                    "durationMillis": item.duration * 1000 if item.duration else None,
                    "artist": {
                        "name": artist_str,
                    }
                    if item.media_item
                    and (artist_str := getattr(item.media_item, "artist_str", None))
                    else None,
                    "album": {
                        "name": album.name,
                    }
                    if item.media_item and (album := getattr(item.media_item, "album", None))
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
        if not (sonos_player := self.sonos_players.get(sonos_player_id)):
            return web.Response(status=501)
        context_version = request.query.get("contextVersion") or "1"
        queue_version = sonos_player.queue_version
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
        if not (sonos_player := self.sonos_players.get(sonos_player_id)):
            return web.Response(status=501)
        result = {
            "contextVersion": "1",
            "queueVersion": sonos_player.queue_version,
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
        if not (sonos_player := self.sonos_players.get(sonos_player_id)):
            return web.Response(status=501)
        for item in json_body["items"]:
            if item["queueVersion"] != sonos_player.queue_version:
                continue
            if item["type"] != "update":
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
