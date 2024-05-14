"""Chromecast Player provider for Music Assistant, utilizing the pychromecast library."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

import pychromecast
from pychromecast.controllers.media import STREAM_TYPE_BUFFERED, STREAM_TYPE_LIVE, MediaController
from pychromecast.controllers.multizone import MultizoneController, MultizoneManager
from pychromecast.discovery import CastBrowser, SimpleCastListener
from pychromecast.socket_client import CONNECTION_STATUS_CONNECTED, CONNECTION_STATUS_DISCONNECTED

from music_assistant.common.models.config_entries import (
    CONF_ENTRY_CROSSFADE_DURATION,
    CONF_ENTRY_CROSSFADE_FLOW_MODE_REQUIRED,
    ConfigEntry,
    ConfigValueType,
    create_sample_rates_config_entry,
)
from music_assistant.common.models.enums import MediaType, PlayerFeature, PlayerState, PlayerType
from music_assistant.common.models.errors import PlayerUnavailableError
from music_assistant.common.models.player import DeviceInfo, Player, PlayerMedia
from music_assistant.constants import CONF_PLAYERS, MASS_LOGO_ONLINE, VERBOSE_LOG_LEVEL
from music_assistant.server.models.player_provider import PlayerProvider

from .helpers import CastStatusListener, ChromecastInfo

if TYPE_CHECKING:
    from pychromecast.controllers.media import MediaStatus
    from pychromecast.controllers.receiver import CastStatus
    from pychromecast.models import CastInfo
    from pychromecast.socket_client import ConnectionStatus

    from music_assistant.common.models.config_entries import PlayerConfig, ProviderConfig
    from music_assistant.common.models.provider import ProviderManifest
    from music_assistant.server import MusicAssistant
    from music_assistant.server.models import ProviderInstanceType


PLAYER_CONFIG_ENTRIES = (
    CONF_ENTRY_CROSSFADE_FLOW_MODE_REQUIRED,
    CONF_ENTRY_CROSSFADE_DURATION,
)

# originally/officially cast supports 96k sample rate (even for groups)
# but it seems a (recent?) update broke this ?!
# For now only set safe default values and let the user try out higher values
CONF_ENTRY_SAMPLE_RATES_CAST = create_sample_rates_config_entry(96000, 24, 48000, 24)
CONF_ENTRY_SAMPLE_RATES_CAST_GROUP = create_sample_rates_config_entry(96000, 24, 44100, 16)


MASS_APP_ID = "C35B0678"


# Monkey patch the Media controller here to store the queue items
_patched_process_media_status_org = MediaController._process_media_status


def _patched_process_media_status(self, data) -> None:
    """Process STATUS message(s) of the media controller."""
    _patched_process_media_status_org(self, data)
    for status_msg in data.get("status", []):
        if items := status_msg.get("items"):
            self.status.current_item_id = status_msg.get("currentItemId", 0)
            self.status.items = items


MediaController._process_media_status = _patched_process_media_status


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return ChromecastProvider(mass, manifest, config)


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
    return ()  # we do not have any config entries (yet)


@dataclass
class CastPlayer:
    """Wrapper around Chromecast with some additional attributes."""

    player_id: str
    cast_info: ChromecastInfo
    cc: pychromecast.Chromecast
    player: Player
    status_listener: CastStatusListener | None = None
    mz_controller: MultizoneController | None = None
    active_group: str | None = None
    last_poll: float = 0
    flow_meta_checksum: str | None = None


class ChromecastProvider(PlayerProvider):
    """Player provider for Chromecast based players."""

    mz_mgr: MultizoneManager | None = None
    browser: CastBrowser | None = None
    castplayers: dict[str, CastPlayer]
    _discover_lock: threading.Lock

    def __init__(
        self, mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
    ) -> None:
        """Handle async initialization of the provider."""
        super().__init__(mass, manifest, config)
        self._discover_lock = threading.Lock()
        self.castplayers = {}
        self.mz_mgr = MultizoneManager()
        self.browser = CastBrowser(
            SimpleCastListener(
                add_callback=self._on_chromecast_discovered,
                remove_callback=self._on_chromecast_removed,
                update_callback=self._on_chromecast_discovered,
            ),
            self.mass.aiozc.zeroconf,
        )
        # set-up pychromecast logging
        if self.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
            logging.getLogger("pychromecast").setLevel(logging.DEBUG)
        else:
            logging.getLogger("pychromecast").setLevel(self.logger.level + 10)

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        # start discovery in executor
        await self.mass.loop.run_in_executor(None, self.browser.start_discovery)

    async def unload(self) -> None:
        """Handle close/cleanup of the provider."""
        if not self.browser:
            return

        # stop discovery
        def stop_discovery() -> None:
            """Stop the chromecast discovery threads."""
            if self.browser._zc_browser:
                with contextlib.suppress(RuntimeError):
                    self.browser._zc_browser.cancel()

            self.browser.host_browser.stop.set()
            self.browser.host_browser.join()

        await self.mass.loop.run_in_executor(None, stop_discovery)
        # stop all chromecasts
        for castplayer in list(self.castplayers.values()):
            await self._disconnect_chromecast(castplayer)

    async def get_player_config_entries(self, player_id: str) -> tuple[ConfigEntry, ...]:
        """Return all (provider/player specific) Config Entries for the given player (if any)."""
        cast_player = self.castplayers.get(player_id)
        base_entries = await super().get_player_config_entries(player_id)
        if cast_player and cast_player.cast_info.is_audio_group:
            return (*base_entries, *PLAYER_CONFIG_ENTRIES, CONF_ENTRY_SAMPLE_RATES_CAST_GROUP)
        return (*base_entries, *PLAYER_CONFIG_ENTRIES, CONF_ENTRY_SAMPLE_RATES_CAST)

    def on_player_config_changed(
        self,
        config: PlayerConfig,
        changed_keys: set[str],
    ) -> None:
        """Call (by config manager) when the configuration of a player changes."""
        super().on_player_config_changed(config, changed_keys)
        if "enabled" in changed_keys and config.player_id not in self.castplayers:
            self.mass.create_task(self.mass.config.reload_provider, self.instance_id)

    async def cmd_stop(self, player_id: str) -> None:
        """Send STOP command to given player."""
        castplayer = self.castplayers[player_id]
        await asyncio.to_thread(castplayer.cc.media_controller.stop)

    async def cmd_play(self, player_id: str) -> None:
        """Send PLAY command to given player."""
        castplayer = self.castplayers[player_id]
        await asyncio.to_thread(castplayer.cc.media_controller.play)

    async def cmd_pause(self, player_id: str) -> None:
        """Send PAUSE command to given player."""
        castplayer = self.castplayers[player_id]
        await asyncio.to_thread(castplayer.cc.media_controller.pause)

    async def cmd_power(self, player_id: str, powered: bool) -> None:
        """Send POWER command to given player."""
        castplayer = self.castplayers[player_id]
        if powered:
            await self._launch_app(castplayer)
        else:
            await asyncio.to_thread(castplayer.cc.quit_app)

    async def cmd_volume_set(self, player_id: str, volume_level: int) -> None:
        """Send VOLUME_SET command to given player."""
        castplayer = self.castplayers[player_id]
        await asyncio.to_thread(castplayer.cc.set_volume, volume_level / 100)

    async def cmd_volume_mute(self, player_id: str, muted: bool) -> None:
        """Send VOLUME MUTE command to given player."""
        castplayer = self.castplayers[player_id]
        await asyncio.to_thread(castplayer.cc.set_volume_muted, muted)

    async def play_media(
        self,
        player_id: str,
        media: PlayerMedia,
    ) -> None:
        """Handle PLAY MEDIA on given player."""
        castplayer = self.castplayers[player_id]
        queuedata = {
            "type": "LOAD",
            "media": self._create_cc_media_item(media),
        }
        # make sure that our media controller app is launched
        await self._launch_app(castplayer)
        # send queue info to the CC
        media_controller = castplayer.cc.media_controller
        await asyncio.to_thread(media_controller.send_message, data=queuedata, inc_session_id=True)

    async def enqueue_next_media(self, player_id: str, media: PlayerMedia) -> None:
        """Handle enqueuing of the next item on the player."""
        castplayer = self.castplayers[player_id]
        next_item_id = None
        status = castplayer.cc.media_controller.status
        # lookup position of current track in cast queue
        cast_current_item_id = getattr(status, "current_item_id", 0)
        cast_queue_items = getattr(status, "items", [])
        cur_item_found = False
        for item in cast_queue_items:
            if item["itemId"] == cast_current_item_id:
                cur_item_found = True
                continue
            if not cur_item_found:
                continue
            next_item_id = item["itemId"]
            # check if the next queue item isn't already queued
            if item.get("media", {}).get("customData", {}).get("uri") == media.uri:
                return
        queuedata = {
            "type": "QUEUE_INSERT",
            "insertBefore": next_item_id,
            "items": [
                {
                    "autoplay": True,
                    "startTime": 0,
                    "preloadTime": 0,
                    "media": self._create_cc_media_item(media),
                }
            ],
        }
        media_controller = castplayer.cc.media_controller
        queuedata["mediaSessionId"] = media_controller.status.media_session_id
        self.mass.create_task(media_controller.send_message, data=queuedata, inc_session_id=True)
        self.logger.debug(
            "Enqued next track (%s) to player %s",
            media.title or media.uri,
            castplayer.player.display_name,
        )

    async def poll_player(self, player_id: str) -> None:
        """Poll player for state updates."""
        castplayer = self.castplayers[player_id]
        # only update status of media controller if player is on
        if not castplayer.player.powered:
            return
        if not castplayer.cc.media_controller.is_active:
            return
        try:
            now = time.time()
            if (now - castplayer.last_poll) >= 60:
                castplayer.last_poll = now
                await asyncio.to_thread(castplayer.cc.media_controller.update_status)
            await self.update_flow_metadata(castplayer)
        except ConnectionResetError as err:
            raise PlayerUnavailableError from err

    ### Discovery callbacks

    def _on_chromecast_discovered(self, uuid, _) -> None:
        """Handle Chromecast discovered callback."""
        if self.mass.closing:
            return

        with self._discover_lock:
            disc_info: CastInfo = self.browser.devices[uuid]

            if disc_info.uuid is None:
                self.logger.error("Discovered chromecast without uuid %s", disc_info)
                return

            player_id = str(disc_info.uuid)

            enabled = self.mass.config.get(f"{CONF_PLAYERS}/{player_id}/enabled", True)
            if not enabled:
                self.logger.debug("Ignoring disabled player: %s", player_id)
                return

            self.logger.debug("Discovered new or updated chromecast %s", disc_info)

            castplayer = self.castplayers.get(player_id)
            if castplayer:
                # if player was already added, the player will take care of reconnects itself.
                castplayer.cast_info.update(disc_info)
                self.mass.loop.call_soon_threadsafe(self.mass.players.update, player_id)
                return
            # new player discovered
            cast_info = ChromecastInfo.from_cast_info(disc_info)
            cast_info.fill_out_missing_chromecast_info(self.mass.aiozc.zeroconf)
            if cast_info.is_dynamic_group:
                self.logger.debug("Discovered a dynamic cast group which will be ignored.")
                return
            if cast_info.is_multichannel_child:
                self.logger.debug(
                    "Discovered a passive (multichannel) endpoint which will be ignored."
                )
                return

            # Disable TV's by default
            # (can be enabled manually by the user)
            enabled_by_default = True
            for exclude in ("tv", "/12", "PUS", "OLED"):
                if exclude.lower() in cast_info.friendly_name.lower():
                    enabled_by_default = False

            if cast_info.is_audio_group and cast_info.is_multichannel_group:
                player_type = PlayerType.STEREO_PAIR
            elif cast_info.is_audio_group:
                player_type = PlayerType.GROUP
            else:
                player_type = PlayerType.PLAYER
            # Instantiate chromecast object
            castplayer = CastPlayer(
                player_id,
                cast_info=cast_info,
                cc=pychromecast.get_chromecast_from_cast_info(
                    disc_info,
                    self.mass.aiozc.zeroconf,
                ),
                player=Player(
                    player_id=player_id,
                    provider=self.instance_id,
                    type=player_type,
                    name=cast_info.friendly_name,
                    available=False,
                    powered=False,
                    device_info=DeviceInfo(
                        model=cast_info.model_name,
                        address=f"{cast_info.host}:{cast_info.port}",
                        manufacturer=cast_info.manufacturer,
                    ),
                    supported_features=(
                        PlayerFeature.POWER,
                        PlayerFeature.VOLUME_MUTE,
                        PlayerFeature.VOLUME_SET,
                        PlayerFeature.ENQUEUE_NEXT,
                        PlayerFeature.PAUSE,
                    ),
                    enabled_by_default=enabled_by_default,
                    needs_poll=True,
                ),
            )
            self.castplayers[player_id] = castplayer

            castplayer.status_listener = CastStatusListener(self, castplayer, self.mz_mgr)
            if cast_info.is_audio_group and not cast_info.is_multichannel_group:
                mz_controller = MultizoneController(cast_info.uuid)
                castplayer.cc.register_handler(mz_controller)
                castplayer.mz_controller = mz_controller

            castplayer.cc.start()
            self.mass.loop.call_soon_threadsafe(
                self.mass.players.register_or_update, castplayer.player
            )

    def _on_chromecast_removed(self, uuid, service, cast_info) -> None:
        """Handle zeroconf discovery of a removed Chromecast."""
        player_id = str(service[1])
        friendly_name = service[3]
        self.logger.debug("Chromecast removed: %s - %s", friendly_name, player_id)
        # we ignore this event completely as the Chromecast socket client handles this itself

    ### Callbacks from Chromecast Statuslistener

    def on_new_cast_status(self, castplayer: CastPlayer, status: CastStatus) -> None:
        """Handle updated CastStatus."""
        if status is None:
            return  # guard
        self.logger.log(
            VERBOSE_LOG_LEVEL,
            "Received cast status for %s - app_id: %s - volume: %s",
            castplayer.player.display_name,
            status.app_id,
            status.volume_level,
        )
        castplayer.player.name = castplayer.cast_info.friendly_name
        castplayer.player.volume_level = int(status.volume_level * 100)
        castplayer.player.volume_muted = status.volume_muted
        castplayer.player.powered = (
            castplayer.cc.app_id is not None and castplayer.cc.app_id != pychromecast.IDLE_APP_ID
        )
        # handle stereo pairs
        if castplayer.cast_info.is_multichannel_group:
            castplayer.player.type = PlayerType.STEREO_PAIR
            castplayer.player.group_childs = set()
        # handle cast groups
        if castplayer.cast_info.is_audio_group and not castplayer.cast_info.is_multichannel_group:
            castplayer.player.type = PlayerType.GROUP
            castplayer.player.group_childs = {
                str(UUID(x)) for x in castplayer.mz_controller.members
            }
            castplayer.player.supported_features = (
                PlayerFeature.POWER,
                PlayerFeature.VOLUME_SET,
                PlayerFeature.ENQUEUE_NEXT,
                PlayerFeature.PAUSE,
            )

        # send update to player manager
        self.mass.loop.call_soon_threadsafe(self.mass.players.update, castplayer.player_id)

    def on_new_media_status(self, castplayer: CastPlayer, status: MediaStatus) -> None:
        """Handle updated MediaStatus."""
        self.logger.log(
            VERBOSE_LOG_LEVEL,
            "Received media status for %s update: %s",
            castplayer.player.display_name,
            status.player_state,
        )
        # handle castplayer playing from a group
        if castplayer.active_group is not None:
            if not (group_player := self.castplayers.get(castplayer.active_group)):
                return
            status = group_player.cc.media_controller.status

        # player state
        castplayer.player.elapsed_time_last_updated = time.time()
        if status.player_is_playing:
            castplayer.player.state = PlayerState.PLAYING
            castplayer.player.current_item_id = status.content_id
        elif status.player_is_paused:
            castplayer.player.state = PlayerState.PAUSED
            castplayer.player.current_item_id = status.content_id
        else:
            castplayer.player.state = PlayerState.IDLE
            castplayer.player.current_item_id = None

        # elapsed time
        castplayer.player.elapsed_time_last_updated = time.time()
        castplayer.player.elapsed_time = status.adjusted_current_time
        if status.player_is_playing:
            castplayer.player.elapsed_time = status.adjusted_current_time
        else:
            castplayer.player.elapsed_time = status.current_time

        # active source
        if castplayer.cc.app_id == MASS_APP_ID:
            castplayer.player.active_source = castplayer.player_id
        else:
            castplayer.player.active_source = castplayer.cc.app_display_name

        # current media
        self.mass.loop.call_soon_threadsafe(self.mass.players.update, castplayer.player_id)

    def on_new_connection_status(self, castplayer: CastPlayer, status: ConnectionStatus) -> None:
        """Handle updated ConnectionStatus."""
        self.logger.log(
            VERBOSE_LOG_LEVEL,
            "Received connection status update for %s - status: %s",
            castplayer.player.display_name,
            status.status,
        )

        if status.status == CONNECTION_STATUS_DISCONNECTED:
            castplayer.player.available = False
            self.mass.loop.call_soon_threadsafe(self.mass.players.update, castplayer.player_id)
            return

        new_available = status.status == CONNECTION_STATUS_CONNECTED
        if new_available != castplayer.player.available:
            self.logger.debug(
                "[%s] Cast device availability changed: %s",
                castplayer.cast_info.friendly_name,
                status.status,
            )
            castplayer.player.available = new_available
            castplayer.player.device_info = DeviceInfo(
                model=castplayer.cast_info.model_name,
                address=f"{castplayer.cast_info.host}:{castplayer.cast_info.port}",
                manufacturer=castplayer.cast_info.manufacturer,
            )
            self.mass.loop.call_soon_threadsafe(self.mass.players.update, castplayer.player_id)
            if new_available and not castplayer.cast_info.is_audio_group:
                # Poll current group status
                for group_uuid in self.mz_mgr.get_multizone_memberships(castplayer.cast_info.uuid):
                    group_media_controller = self.mz_mgr.get_multizone_mediacontroller(group_uuid)
                    if not group_media_controller:
                        continue

    ### Helpers / utils

    async def _launch_app(self, castplayer: CastPlayer, app_id: str = MASS_APP_ID) -> None:
        """Launch the default Media Receiver App on a Chromecast."""
        event = asyncio.Event()

        if castplayer.cc.app_id == app_id:
            return  # already active

        def launched_callback(success: bool, response: dict[str, Any] | None) -> None:
            self.mass.loop.call_soon_threadsafe(event.set)

        def launch() -> None:
            # Quit the previous app before starting splash screen or media player
            if castplayer.cc.app_id is not None:
                castplayer.cc.quit_app()
            self.logger.debug("Launching App %s.", app_id)
            castplayer.cc.socket_client.receiver_controller.launch_app(
                app_id,
                force_launch=True,
                callback_function=launched_callback,
            )

        await self.mass.loop.run_in_executor(None, launch)
        await event.wait()

    async def _disconnect_chromecast(self, castplayer: CastPlayer) -> None:
        """Disconnect Chromecast object if it is set."""
        self.logger.debug("Disconnecting from chromecast socket %s", castplayer.player.display_name)
        await self.mass.loop.run_in_executor(None, castplayer.cc.disconnect, 10)
        castplayer.mz_controller = None
        castplayer.status_listener.invalidate()
        castplayer.status_listener = None
        self.castplayers.pop(castplayer.player_id, None)

    def _create_cc_media_item(self, media: PlayerMedia) -> dict[str, Any]:
        """Create CC media item from MA PlayerMedia."""
        if media.media_type == MediaType.TRACK:
            stream_type = STREAM_TYPE_BUFFERED
        else:
            stream_type = STREAM_TYPE_LIVE
        metadata = {
            "metadataType": 3,
            "albumName": media.album or "",
            "songName": media.title or "",
            "artist": media.artist or "",
            "title": media.title or "",
            "images": [{"url": media.image_url}] if media.image_url else None,
        }
        return {
            "contentId": media.uri,
            "customData": {
                "uri": media.uri,
                "queue_item_id": media.uri,
                "deviceName": "Music Assistant",
            },
            "contentType": "audio/flac",
            "streamType": stream_type,
            "metadata": metadata,
            "duration": media.duration,
        }

    async def update_flow_metadata(self, castplayer: CastPlayer) -> None:
        """Update the metadata of a cast player running the flow stream."""
        if not castplayer.player.powered:
            castplayer.player.poll_interval = 300
            return
        if not castplayer.cc.media_controller.status.player_is_playing:
            return
        if castplayer.active_group:
            return
        if castplayer.player.state != PlayerState.PLAYING:
            return
        if castplayer.player.announcement_in_progress:
            return
        if not (queue := self.mass.player_queues.get_active_queue(castplayer.player_id)):
            return
        if not (current_item := queue.current_item):
            return
        if not (queue.flow_mode or current_item.media_type == MediaType.RADIO):
            return
        castplayer.player.poll_interval = 10
        media_controller = castplayer.cc.media_controller
        # update metadata of current item chromecast
        if media_controller.status.media_custom_data["queue_item_id"] != current_item.queue_item_id:
            image_url = (
                self.mass.metadata.get_image_url(current_item.image)
                if current_item.image
                else MASS_LOGO_ONLINE
            )
            if (streamdetails := current_item.streamdetails) and streamdetails.stream_title:
                album = current_item.media_item.name
                if " - " in streamdetails.stream_title:
                    artist, title = streamdetails.stream_title.split(" - ", 1)
                else:
                    artist = ""
                    title = streamdetails.stream_title
            elif media_item := current_item.media_item:
                album = _album.name if (_album := getattr(media_item, "album", None)) else ""
                artist = getattr(media_item, "artist_str", "")
                title = media_item.name
            else:
                album = ""
                artist = ""
                title = current_item.name
            flow_meta_checksum = title + image_url
            if castplayer.flow_meta_checksum == flow_meta_checksum:
                return
            castplayer.flow_meta_checksum = flow_meta_checksum
            queuedata = {
                "type": "PLAY",
                "mediaSessionId": media_controller.status.media_session_id,
                "customData": {
                    "metadata": {
                        "metadataType": 3,
                        "albumName": album,
                        "songName": title,
                        "artist": artist,
                        "title": title,
                        "images": [{"url": image_url}],
                    }
                },
            }
            self.mass.create_task(
                media_controller.send_message, data=queuedata, inc_session_id=True
            )

        if len(getattr(media_controller.status, "items", [])) < 2:
            # In flow mode, all queue tracks are sent to the player as continuous stream.
            # add a special 'command' item to the queue
            # this allows for on-player next buttons/commands to still work
            cmd_next_url = self.mass.streams.get_command_url(queue.queue_id, "next")
            msg = {
                "type": "QUEUE_INSERT",
                "mediaSessionId": media_controller.status.media_session_id,
                "items": [
                    {
                        "media": {
                            "contentId": cmd_next_url,
                            "customData": {
                                "uri": cmd_next_url,
                                "queue_item_id": cmd_next_url,
                                "deviceName": "Music Assistant",
                            },
                            "contentType": "audio/flac",
                            "streamType": STREAM_TYPE_LIVE,
                            "metadata": {},
                        },
                        "autoplay": True,
                        "startTime": 0,
                        "preloadTime": 0,
                    }
                ],
            }
            self.mass.create_task(media_controller.send_message, data=msg, inc_session_id=True)
