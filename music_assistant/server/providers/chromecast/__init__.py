"""Chromecast Player provider for Music Assistant, utilizing the pychromecast library."""
from __future__ import annotations

import asyncio
import time
import calendar
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from pychromecast import (
    APP_BUBBLEUPNP,
    APP_MEDIA_RECEIVER,
    Chromecast,
    get_chromecast_from_cast_info,
)
from music_assistant.server.helpers.compare import compare_strings
from pychromecast.controllers.bubbleupnp import BubbleUPNPController
from pychromecast.controllers.media import STREAM_TYPE_BUFFERED, STREAM_TYPE_LIVE
from pychromecast.controllers.multizone import MultizoneController, MultizoneManager
from pychromecast.discovery import CastBrowser, SimpleCastListener
from pychromecast.models import CastInfo
from pychromecast.socket_client import (
    CONNECTION_STATUS_CONNECTED,
    CONNECTION_STATUS_DISCONNECTED,
)

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
from music_assistant.server.providers.chromecast.helpers import (
    CastStatusListener,
    ChromecastInfo,
)

if TYPE_CHECKING:
    from pychromecast.controllers.media import MediaStatus
    from pychromecast.controllers.receiver import CastStatus
    from pychromecast.socket_client import ConnectionStatus


PLAYER_CONFIG_ENTRIES = tuple()


@dataclass
class CastPlayer:
    """Wrapper around Chromecast with some additional attributes."""

    player_id: str
    cast_info: ChromecastInfo
    cc: Chromecast
    is_stereo_pair: bool = False
    status_listener: CastStatusListener | None = None
    mz_controller: MultizoneController | None = None
    next_item: str | None = None


class ChromecastProvider(PlayerProvider):
    """Player provider for Chromecast based players."""

    mz_mgr: MultizoneManager | None = None
    browser: CastBrowser | None = None
    castplayers: dict[str, CastPlayer] | None = None

    async def setup(self) -> None:
        """Handle async initialization of the provider."""
        self.castplayers = {}
        self.mz_mgr = MultizoneManager()
        self.browser = CastBrowser(
            SimpleCastListener(
                add_callback=self._on_chromecast_discovered,
                remove_callback=self._on_chromecast_removed,
                update_callback=self._on_chromecast_discovered,
            ),
            self.mass.zeroconf,
        )
        # start discovery in executor
        self.mass.loop.run_in_executor(None, self.browser.start_discovery)

    async def close(self) -> None:
        """Handle close/cleanup of the provider."""
        if not self.browser:
            return
        # stop discovery
        self.mass.loop.run_in_executor(None, self.browser.stop_discovery)
        # stop all chromecasts
        for castplayer in list(self.castplayers.values()):
            await self._disconnect_chromecast(castplayer)

    async def cmd_stop(self, player_id: str) -> None:
        """
        Send STOP command to given player.
            - player_id: player_id of the player to handle the command.
        """
        castplayer = self.castplayers[player_id]
        await self.mass.loop.run_in_executor(None, castplayer.cc.media_controller.stop)

    async def cmd_play(self, player_id: str) -> None:
        """
        Send PLAY command to given player.
            - player_id: player_id of the player to handle the command.
        """
        castplayer = self.castplayers[player_id]
        await self.mass.loop.run_in_executor(None, castplayer.cc.media_controller.play)

    async def cmd_play_media(
        self,
        player_id: str,
        queue_item: QueueItem,
        seek_position: int = 0,
        fade_in: bool = False,
    ) -> None:
        """Send PLAY MEDIA command to given player."""
        castplayer = self.castplayers[player_id]
        url = await self.mass.streams.resolve_stream_url(
            queue_item=queue_item,
            player_id=player_id,
            seek_position=seek_position,
            fade_in=fade_in,
            # prefer FLAC as it seems to work on all CC players
            content_type=ContentType.FLAC,
        )
        cc_queue_items = [self._create_queue_item(queue_item, url)]
        queuedata = {
            "type": "QUEUE_LOAD",
            "repeatMode": "REPEAT_OFF",  # handled by our queue controller
            "shuffle": False,  # handled by our queue controller
            "queueType": "PLAYLIST",
            "startIndex": 0,  # Item index to play after this request or keep same item if undefined
            "items": cc_queue_items,
        }
        # make sure that media controller app is launched
        await self._launch_app(castplayer)
        # send queue info to the CC
        castplayer.next_item = None
        media_controller = castplayer.cc.media_controller
        await self.mass.loop.run_in_executor(
            None, media_controller.send_message, queuedata, True
        )

    async def cmd_pause(self, player_id: str) -> None:
        """Send PAUSE command to given player."""
        castplayer = self.castplayers[player_id]
        await self.mass.loop.run_in_executor(None, castplayer.cc.media_controller.pause)

    async def cmd_power(self, player_id: str, powered: bool) -> None:
        """Send POWER command to given player."""
        castplayer = self.castplayers[player_id]
        if powered:
            await self._launch_app(castplayer)
        else:
            await self.mass.loop.run_in_executor(None, castplayer.cc.quit_app)

    async def cmd_volume_set(self, player_id: str, volume_level: int) -> None:
        """Send VOLUME_SET command to given player."""
        castplayer = self.castplayers[player_id]
        await self.mass.loop.run_in_executor(
            None, castplayer.cc.set_volume, volume_level / 100
        )

    async def cmd_volume_mute(self, player_id: str, muted: bool) -> None:
        """Send VOLUME MUTE command to given player."""
        castplayer = self.castplayers[player_id]
        await self.mass.loop.run_in_executor(
            None, castplayer.cc.set_volume_muted, muted
        )

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
        castplayer = self.castplayers[player_id]
        try:
            await self.mass.loop.run_in_executor(
                None, castplayer.cc.media_controller.update_status
            )
        except ConnectionResetError as err:
            raise PlayerUnavailableError from err

    ### Discovery callbacks

    def _on_chromecast_discovered(self, uuid, _):
        """Handle Chromecast discovered callback."""
        disc_info: CastInfo = self.browser.devices[uuid]

        if disc_info.uuid is None:
            self.logger.error("Discovered chromecast without uuid %s", disc_info)
            return

        self.logger.debug("Discovered new or updated chromecast %s", disc_info)
        player_id = str(disc_info.uuid)

        castplayer = self.castplayers.get(player_id)
        if not castplayer:
            cast_info = ChromecastInfo.from_cast_info(disc_info)
            cast_info.fill_out_missing_chromecast_info(self.mass.zeroconf)
            if cast_info.is_dynamic_group:
                self.logger.warning(
                    "Discovered dynamic cast group which will be ignored."
                )
                return

            # Instantiate chromecast object
            self.castplayers[player_id] = castplayer = CastPlayer(
                player_id,
                cast_info=cast_info,
                cc=get_chromecast_from_cast_info(
                    disc_info,
                    self.mass.zeroconf,
                ),
            )
            castplayer.status_listener = CastStatusListener(
                self, castplayer, self.mz_mgr
            )
            if cast_info.is_audio_group:
                mz_controller = MultizoneController(cast_info.uuid)
                castplayer.cc.register_handler(mz_controller)
                castplayer.mz_controller = mz_controller
            castplayer.cc.start()

        player = self.mass.players.get(player_id)
        if not player:
            player = Player(
                player_id=player_id,
                provider=self.domain,
                type=PlayerType.GROUP
                if cast_info.is_audio_group
                else PlayerType.PLAYER,
                name=cast_info.friendly_name,
                available=False,
                powered=False,
                device_info=DeviceInfo(
                    model=cast_info.model_name,
                    address=cast_info.host,
                    manufacturer=cast_info.manufacturer,
                ),
                supported_features=(
                    PlayerFeature.POWER,
                    PlayerFeature.VOLUME_MUTE,
                    PlayerFeature.VOLUME_SET,
                ),
            )
            self.mass.loop.call_soon_threadsafe(self.mass.players.register, player)

        # if player was already added, the player will take care of reconnects itself.
        castplayer.cast_info.update(disc_info)
        self.mass.loop.call_soon_threadsafe(self.mass.players.update, player_id)

    def _on_chromecast_removed(self, uuid, service, cast_info):
        """Handle zeroconf discovery of a removed Chromecast."""
        # pylint: disable=unused-argument
        player_id = str(service[1])
        friendly_name = service[3]
        self.logger.debug("Chromecast removed: %s - %s", friendly_name, player_id)
        # we ignore this event completely as the Chromecast socket client handles this itself

    ### Callbacks from Chromecast Statuslistener

    def on_new_cast_status(self, castplayer: CastPlayer, status: CastStatus) -> None:
        """Handle updated CastStatus."""
        player = self.mass.players.get(castplayer.player_id)
        if player is None:
            return  # race condition
        player.name = castplayer.cast_info.friendly_name
        player.powered = status.app_id in (
            "705D30C6",
            APP_MEDIA_RECEIVER,
            APP_BUBBLEUPNP,
        )
        castplayer.is_stereo_pair = (
            castplayer.cast_info.is_audio_group
            and castplayer.mz_controller
            and castplayer.mz_controller.members
            and compare_strings(
                castplayer.mz_controller.members[0], castplayer.player_id
            )
        )
        player.volume_level = int(status.volume_level * 100)
        player.volume_muted = status.volume_muted
        if castplayer.is_stereo_pair:
            player.type = PlayerType.PLAYER
        self.mass.loop.call_soon_threadsafe(
            self.mass.players.update, castplayer.player_id
        )

    def on_new_media_status(self, castplayer: CastPlayer, status: MediaStatus):
        """Handle updated MediaStatus."""
        player = self.mass.players.get(castplayer.player_id)
        prev_state = player.state
        # player state
        if status.player_is_playing:
            player.state = PlayerState.PLAYING
        elif status.player_is_paused:
            player.state = PlayerState.PAUSED
        else:
            player.state = PlayerState.IDLE

        # elapsed time
        player.elapsed_time_last_updated = time.time()
        if status.player_is_playing:
            player.elapsed_time = status.adjusted_current_time
        else:
            player.elapsed_time = status.current_time

        # current media
        queue_item_id = status.media_custom_data.get("queue_item_id")
        player.current_item_id = queue_item_id
        player.current_url = status.content_id
        self.mass.loop.call_soon_threadsafe(
            self.mass.players.update, castplayer.player_id
        )

        # enqueue next queue item when a new one started playing
        if player.state == PlayerState.PLAYING and (
            queue_item_id == castplayer.next_item or castplayer.next_item is None
        ):
            asyncio.run_coroutine_threadsafe(
                self._enqueue_next_track(castplayer, queue_item_id), self.mass.loop
            )

    def on_new_connection_status(
        self, castplayer: CastPlayer, status: ConnectionStatus
    ) -> None:
        """Handle updated ConnectionStatus."""
        player = self.mass.players.get(castplayer.player_id)
        if player is None:
            return  # race condition

        if status.status == CONNECTION_STATUS_DISCONNECTED:
            player.available = False
            self.mass.loop.call_soon_threadsafe(
                self.mass.players.update, castplayer.player_id
            )
            return

        new_available = status.status == CONNECTION_STATUS_CONNECTED
        if new_available != player.available:
            self.logger.debug(
                "[%s] Cast device availability changed: %s",
                castplayer.cast_info.friendly_name,
                status.status,
            )
            player.available = new_available
            player.device_info = DeviceInfo(
                model=castplayer.cast_info.model_name,
                address=castplayer.cast_info.host,
                manufacturer=castplayer.cast_info.manufacturer,
            )
            self.mass.loop.call_soon_threadsafe(
                self.mass.players.update, castplayer.player_id
            )
            if new_available and not castplayer.cast_info.is_audio_group:
                # Poll current group status
                for group_uuid in self.mz_mgr.get_multizone_memberships(
                    castplayer.cast_info.uuid
                ):
                    group_media_controller = self.mz_mgr.get_multizone_mediacontroller(
                        group_uuid
                    )
                    if not group_media_controller:
                        continue
                    self.on_multizone_new_media_status(
                        castplayer, group_uuid, group_media_controller.status
                    )

    def on_multizone_new_media_status(
        self, castplayer: CastPlayer, group_uuid: UUID, media_status: MediaStatus
    ):
        """Handle updates of audio group media status."""
        self.logger.debug(
            "[%s %s] Multizone %s media status: %s",
            castplayer.cast_info.uuid,
            castplayer.cast_info.friendly_name,
            group_uuid,
            media_status,
        )
        # self.mz_media_status[group_uuid] = media_status
        # self.mz_media_status_received[group_uuid] = dt_util.utcnow()
        # self.schedule_update_ha_state()

    ### Helpers / utils

    async def _enqueue_next_track(
        self, castplayer: CastPlayer, current_queue_item_id: str
    ) -> None:
        """Enqueue the next track of the MA queue on the CC queue."""
        if not current_queue_item_id:
            return  # guard
        try:
            next_item, crossfade = self.mass.players.queues.player_ready_for_next_track(
                castplayer.player_id, current_queue_item_id
            )
        except QueueEmpty:
            return

        if castplayer.next_item == next_item.queue_item_id:
            return  # already set ?!
        castplayer.next_item = next_item.queue_item_id

        if crossfade:
            self.logger.warning(
                "Crossfade requested but Chromecast does not support crossfading,"
                " consider using flow mode to enable crossfade on a Chromecast."
            )

        url = await self.mass.streams.resolve_stream_url(
            queue_item=next_item,
            player_id=castplayer.player_id,
            content_type=ContentType.FLAC,
            auto_start_runner=False,
        )
        cc_queue_items = [self._create_queue_item(next_item, url)]

        queuedata = {
            "type": "QUEUE_INSERT",
            "insertBefore": None,
            "items": cc_queue_items,
        }
        media_controller = castplayer.cc.media_controller
        queuedata["mediaSessionId"] = media_controller.status.media_session_id

        await asyncio.sleep(0.5)  # throttle commands to CC a bit or it will crash
        await self.mass.loop.run_in_executor(
            None, media_controller.send_message, queuedata, True
        )

    async def _launch_app(self, castplayer: CastPlayer) -> None:
        """Launch the default Media Receiver App on a Chromecast."""
        event = asyncio.Event()

        def launched_callback():
            self.mass.loop.call_soon_threadsafe(event.set)

        def launch():
            controller = BubbleUPNPController()
            castplayer.cc.register_handler(controller)
            controller.launch(launched_callback)

        await self.mass.loop.run_in_executor(None, launch)
        await event.wait()

    async def _disconnect_chromecast(self, castplayer: CastPlayer) -> None:
        """Disconnect Chromecast object if it is set."""
        self.logger.debug(
            "[%s %s] Disconnecting from chromecast socket",
            castplayer.player_id,
            castplayer.cast_info.friendly_name,
        )
        await self.mass.loop.run_in_executor(None, castplayer.cc.disconnect)
        castplayer.mz_controller = None
        castplayer.status_listener.invalidate()
        castplayer.status_listener = None
        self.castplayers.pop(castplayer.player_id, None)

    @staticmethod
    def _create_queue_item(queue_item: QueueItem, stream_url: str):
        """Create CC queue item from MA QueueItem."""
        duration = int(queue_item.duration) if queue_item.duration else None
        if queue_item.media_type == MediaType.TRACK:
            stream_type = STREAM_TYPE_BUFFERED
            metadata = {
                "metadataType": 3,
                "albumName": queue_item.media_item.album.name,
                "songName": queue_item.media_item.name,
                "artist": queue_item.media_item.artist.name,
                "title": queue_item.name,
                "images": [{"url": queue_item.image.url}] if queue_item.image else None,
            }
        else:
            stream_type = STREAM_TYPE_LIVE
            metadata = {
                "metadataType": 0,
                "title": queue_item.name,
                "images": [{"url": queue_item.image.url}] if queue_item.image else None,
            }
        return {
            "autoplay": True,
            "preloadTime": 5,
            "playbackDuration": duration,
            "startTime": 0,
            "activeTrackIds": [],
            "media": {
                "contentId": stream_url,
                "customData": {
                    "uri": queue_item.uri,
                    "queue_item_id": queue_item.queue_item_id,
                },
                "contentType": "audio/flac",
                "streamType": stream_type,
                "metadata": metadata,
                "duration": duration,
            },
        }
