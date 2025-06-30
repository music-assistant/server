"""Chromecast Player provider for Music Assistant, utilizing the pychromecast library."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

import pychromecast
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import (
    ConfigEntryType,
    MediaType,
    PlaybackState,
    PlayerFeature,
    PlayerType,
)
from music_assistant_models.errors import PlayerUnavailableError
from pychromecast.controllers.media import STREAM_TYPE_BUFFERED, STREAM_TYPE_LIVE, MediaController
from pychromecast.controllers.multizone import MultizoneController, MultizoneManager
from pychromecast.discovery import CastBrowser, SimpleCastListener
from pychromecast.socket_client import CONNECTION_STATUS_CONNECTED, CONNECTION_STATUS_DISCONNECTED

from music_assistant.constants import (
    ATTR_ANNOUNCEMENT_IN_PROGRESS,
    CONF_ENTRY_HTTP_PROFILE,
    CONF_ENTRY_MANUAL_DISCOVERY_IPS,
    CONF_ENTRY_OUTPUT_CODEC,
    CONF_PLAYERS,
    MASS_LOGO_ONLINE,
    VERBOSE_LOG_LEVEL,
    create_sample_rates_config_entry,
)
from music_assistant.models.player import DeviceInfo, Player, PlayerMedia
from music_assistant.models.player_provider import PlayerProvider

from .helpers import CastStatusListener, ChromecastInfo

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest
    from pychromecast.controllers.media import MediaStatus
    from pychromecast.controllers.receiver import CastStatus
    from pychromecast.models import CastInfo
    from pychromecast.socket_client import ConnectionStatus

    from music_assistant import MusicAssistant
    from music_assistant.models import ProviderInstanceType

MASS_APP_ID = "C35B0678"
APP_MEDIA_RECEIVER = "CC1AD845"
CONF_USE_MASS_APP = "use_mass_app"


CAST_PLAYER_CONFIG_ENTRIES = (
    CONF_ENTRY_OUTPUT_CODEC,
    CONF_ENTRY_HTTP_PROFILE,
    ConfigEntry(
        key=CONF_USE_MASS_APP,
        type=ConfigEntryType.BOOLEAN,
        label="Use Music Assistant Cast App",
        default_value=True,
        description="By default, Music Assistant will use a special Music Assistant "
        "Cast Receiver app to play media on cast devices. It is tweaked to provide "
        "better metadata and future expansion. \n\n"
        "If you want to use the official Google Cast Receiver app instead, disable this option, "
        "for example if your device has issues with the Music Assistant app.",
        category="advanced",
    ),
)

# originally/officially cast supports 96k sample rate (even for groups)
# but it seems a (recent?) update broke this ?!
# For now only set safe default values and let the user try out higher values
CONF_ENTRY_SAMPLE_RATES_CAST = create_sample_rates_config_entry(
    max_sample_rate=192000,
    max_bit_depth=24,
    safe_max_sample_rate=48000,
    safe_max_bit_depth=16,
)
CONF_ENTRY_SAMPLE_RATES_CAST_GROUP = create_sample_rates_config_entry(
    max_sample_rate=96000,
    max_bit_depth=24,
    safe_max_sample_rate=48000,
    safe_max_bit_depth=16,
)


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
    return (CONF_ENTRY_MANUAL_DISCOVERY_IPS,)


class ChromecastPlayer(Player):
    """Chromecast Player."""

    def __init__(
        self,
        provider: PlayerProvider,
        player_id: str,
        chromecast: pychromecast.Chromecast,
        cast_info: ChromecastInfo,
    ) -> None:
        """Init."""
        super().__init__(provider, player_id)

        self.cc = chromecast
        self.status_listener: CastStatusListener | None
        self.cast_info = cast_info
        self.mz_controller: MultizoneController | None = None

        self.last_poll = 0.0

        self.flow_meta_checksum: str | None = None

    def setup(
        self, player_type: PlayerType, enabled_by_default: bool, status_listener: CastStatusListener
    ) -> None:
        """Set features."""
        self._attr_supported_features = {
            PlayerFeature.POWER,
            PlayerFeature.VOLUME_MUTE,
            PlayerFeature.VOLUME_SET,
            PlayerFeature.PAUSE,
            PlayerFeature.NEXT_PREVIOUS,
            PlayerFeature.ENQUEUE,
        }
        self._attr_name = self.cast_info.friendly_name
        self._attr_available = False
        self._attr_powered = False
        self._attr_needs_poll = True
        self._attr_type = player_type
        self._attr_enabled_by_default = enabled_by_default
        self.status_listener = status_listener

        self._attr_device_info = DeviceInfo(
            model=self.cast_info.model_name,
            ip_address=f"{self.cast_info.host}:{self.cast_info.port}",
            manufacturer=self.cast_info.manufacturer or "",
        )
        self.update_state()

    async def get_config_entries(self) -> list[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the given player (if any)."""
        base_entries = await super().get_config_entries()
        if self.type == PlayerType.GROUP:
            return [
                *base_entries,
                *CAST_PLAYER_CONFIG_ENTRIES,
                CONF_ENTRY_SAMPLE_RATES_CAST_GROUP,
            ]

        return [*base_entries, *CAST_PLAYER_CONFIG_ENTRIES, CONF_ENTRY_SAMPLE_RATES_CAST]

    async def stop(self) -> None:
        """Send STOP command to given player."""
        await asyncio.to_thread(self.cc.media_controller.stop)

    async def play(self) -> None:
        """Send PLAY command to given player."""
        await asyncio.to_thread(self.cc.media_controller.play)

    async def pause(self) -> None:
        """Send PAUSE command to given player."""
        await asyncio.to_thread(self.cc.media_controller.pause)

    async def next(self) -> None:
        """Handle NEXT TRACK command for given player."""
        await asyncio.to_thread(self.cc.media_controller.queue_next)

    async def previous(self) -> None:
        """Handle PREVIOUS TRACK command for given player."""
        await asyncio.to_thread(self.cc.media_controller.queue_prev)

    async def power(self, powered: bool) -> None:
        """Send POWER command to given player."""
        if powered:
            await self._launch_app()
        else:
            # FIXME: not in _attr
            self.active_group = None
            self._attr_active_source = None
            await asyncio.to_thread(self.cc.quit_app)
            self.update_state()
        # optimistically update the group childs
        if self.type == PlayerType.GROUP:
            active_group = self.active_group or self.player_id
            for child_id in self.group_members:
                if child := self.mass.players.get(child_id):
                    child._attr_powered = powered
                    child.active_group = active_group if powered else None
                    child.update_state()

    async def volume_set(self, volume_level: int) -> None:
        """Send VOLUME_SET command to given player."""
        await asyncio.to_thread(self.cc.set_volume, volume_level / 100)

    async def volume_mute(self, muted: bool) -> None:
        """Send VOLUME MUTE command to given player."""
        await asyncio.to_thread(self.cc.set_volume_muted, muted)

    async def play_media(
        self,
        media: PlayerMedia,
    ) -> None:
        """Handle PLAY MEDIA on given player."""
        queuedata = {
            "type": "LOAD",
            "media": self._create_cc_media_item(media),
        }
        # make sure that our media controller app is launched
        await self._launch_app()
        # send queue info to the CC
        media_controller = self.cc.media_controller
        await asyncio.to_thread(media_controller.send_message, data=queuedata, inc_session_id=True)

    async def enqueue_next_media(self, media: PlayerMedia) -> None:
        """Handle enqueuing of the next item on the player."""
        next_item_id = None
        status = self.cc.media_controller.status
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
        media_controller = self.cc.media_controller
        queuedata["mediaSessionId"] = media_controller.status.media_session_id
        await asyncio.to_thread(media_controller.send_message, data=queuedata, inc_session_id=True)

    async def poll(self) -> None:
        """Poll player for state updates."""
        # only update status of media controller if player is on
        if not self.powered:
            return
        if not self.cc.media_controller.is_active:
            return
        try:
            now = time.time()
            if (now - self.last_poll) >= 60:
                self.last_poll = now
                await asyncio.to_thread(self.cc.media_controller.update_status)
            await self.update_flow_metadata()
        except ConnectionResetError as err:
            raise PlayerUnavailableError from err

    async def update_flow_metadata(self) -> None:
        """Update the metadata of a cast player running the flow stream."""
        if not self.powered:
            self._attr_poll_interval = 300
            return
        if not self.cc.media_controller.status.player_is_playing:
            return
        if self.active_group:
            return
        if self.state != PlaybackState.PLAYING:
            return
        if self.extra_attributes[ATTR_ANNOUNCEMENT_IN_PROGRESS]:
            return
        if not (queue := self.mass.player_queues.get_active_queue(self.player_id)):
            return
        if not (current_item := queue.current_item):
            return
        if not (queue.flow_mode or current_item.media_type == MediaType.RADIO):
            return
        self._attr_poll_interval = 10
        media_controller = self.cc.media_controller
        # update metadata of current item chromecast
        if (
            media_controller.status.media_custom_data.get("queue_item_id")
            != current_item.queue_item_id
        ):
            image_url = (
                self.mass.metadata.get_image_url(current_item.image, size=512)
                if current_item.image
                else MASS_LOGO_ONLINE
            )
            if (streamdetails := current_item.streamdetails) and streamdetails.stream_title:
                assert current_item.media_item is not None  # for type checking
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
            if self.flow_meta_checksum == flow_meta_checksum:
                return
            self.flow_meta_checksum = flow_meta_checksum
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
            await asyncio.to_thread(
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
            await asyncio.to_thread(media_controller.send_message, data=msg, inc_session_id=True)

    async def _launch_app(self) -> None:
        """Launch the default Media Receiver App on a Chromecast."""
        event = asyncio.Event()

        if self.mass.config.get_raw_player_config_value(self.player_id, CONF_USE_MASS_APP, True):
            app_id = MASS_APP_ID
        else:
            app_id = APP_MEDIA_RECEIVER

        if self.cc.app_id == app_id:
            return  # already active

        def launched_callback(success: bool, response: dict[str, Any] | None) -> None:
            self.mass.loop.call_soon(event.set)

        def launch() -> None:
            # Quit the previous app before starting splash screen or media player
            if self.cc.app_id is not None:
                self.cc.quit_app()
            self.logger.debug("Launching App %s.", app_id)
            self.cc.socket_client.receiver_controller.launch_app(
                app_id,
                force_launch=True,
                callback_function=launched_callback,
            )

        await self.mass.loop.run_in_executor(None, launch)
        await event.wait()

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


class ChromecastProvider(PlayerProvider):
    """Player provider for Chromecast based players."""

    mz_mgr: MultizoneManager | None = None
    browser: CastBrowser | None = None
    _discover_lock: threading.Lock

    def __init__(
        self, mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
    ) -> None:
        """Handle async initialization of the provider."""
        super().__init__(mass, manifest, config)
        self._discover_lock = threading.Lock()
        self.mz_mgr = MultizoneManager()
        # Handle config option for manual IP's
        manual_ip_config = cast("list[str]", config.get_value(CONF_ENTRY_MANUAL_DISCOVERY_IPS.key))
        self.browser = CastBrowser(
            SimpleCastListener(
                add_callback=self._on_chromecast_discovered,
                remove_callback=self._on_chromecast_removed,
                update_callback=self._on_chromecast_discovered,
            ),
            self.mass.aiozc.zeroconf,
            known_hosts=manual_ip_config,
        )
        # set-up pychromecast logging
        if self.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
            logging.getLogger("pychromecast").setLevel(logging.DEBUG)
        else:
            logging.getLogger("pychromecast").setLevel(self.logger.level + 10)

    async def discover_players(self) -> None:
        """Discover Cast players on the network."""
        assert self.browser is not None  # for type checking
        await self.mass.loop.run_in_executor(None, self.browser.start_discovery)

    async def unload(self, is_removed: bool = False) -> None:
        """Handle close/cleanup of the provider."""
        if not self.browser:
            return

        # stop discovery
        def stop_discovery() -> None:
            """Stop the chromecast discovery threads."""
            assert self.browser is not None  # for type checking
            if self.browser._zc_browser:
                with contextlib.suppress(RuntimeError):
                    self.browser._zc_browser.cancel()

            self.browser.host_browser.stop.set()
            self.browser.host_browser.join()

        await self.mass.loop.run_in_executor(None, stop_discovery)
        # stop all chromecasts
        for castplayer in self.mass.players.all(provider_filter=self.lookup_key):
            assert isinstance(castplayer, ChromecastPlayer)  # for type checking
            await self._disconnect_chromecast(castplayer)

    ### Discovery callbacks

    def _on_chromecast_discovered(self, uuid, _) -> None:
        """Handle Chromecast discovered callback."""
        if self.mass.closing:
            return

        assert self.browser is not None  # for type checking
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

            castplayer = self.mass.players.get(player_id)
            if castplayer:
                assert isinstance(castplayer, ChromecastPlayer)  # for type checking
                # if player was already added, the player will take care of reconnects itself.
                castplayer.cast_info.update(disc_info)
                self.mass.loop.call_soon(self.mass.players.trigger_player_update, player_id)
                return
            # new player discovered
            cast_info = cast("ChromecastInfo", ChromecastInfo.from_cast_info(disc_info))  # type: ignore
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
            assert self.mz_mgr is not None  # for type checking
            castplayer = ChromecastPlayer(
                self,
                player_id,
                chromecast=pychromecast.get_chromecast_from_cast_info(
                    disc_info,
                    self.mass.aiozc.zeroconf,
                ),
                cast_info=cast_info,
            )

            status_listener = CastStatusListener(self, castplayer, self.mz_mgr)
            castplayer.setup(
                player_type=player_type,
                enabled_by_default=enabled_by_default,
                status_listener=status_listener,
            )

            if player_type == PlayerType.GROUP:
                mz_controller = MultizoneController(cast_info.uuid)
                castplayer.cc.register_handler(mz_controller)
                castplayer.mz_controller = mz_controller

            castplayer.cc.start()
            asyncio.run_coroutine_threadsafe(
                self.mass.players.register_or_update(castplayer), loop=self.mass.loop
            )

    def _on_chromecast_removed(self, uuid, service, cast_info) -> None:
        """Handle zeroconf discovery of a removed Chromecast."""
        player_id = str(service[1])
        friendly_name = service[3]
        self.logger.debug("Chromecast removed: %s - %s", friendly_name, player_id)
        # we ignore this event completely as the Chromecast socket client handles this itself

    ### Callbacks from Chromecast Statuslistener

    def on_new_cast_status(self, castplayer: ChromecastPlayer, status: CastStatus) -> None:
        """Handle updated CastStatus."""
        if status is None:
            return  # guard
        self.logger.log(
            VERBOSE_LOG_LEVEL,
            "Received cast status for %s - app_id: %s - volume: %s",
            castplayer.display_name,
            status.app_id,
            status.volume_level,
        )
        # handle stereo pairs
        if castplayer.cast_info.is_multichannel_group:
            castplayer._attr_type = PlayerType.STEREO_PAIR
            castplayer.group_members.clear()
        # handle cast groups
        if castplayer.cast_info.is_audio_group and not castplayer.cast_info.is_multichannel_group:
            assert castplayer.mz_controller is not None  # for type checking
            castplayer._attr_type = PlayerType.GROUP
            castplayer._attr_group_members = [
                str(UUID(x)) for x in castplayer.mz_controller.members
            ]
            castplayer._attr_supported_features = {
                PlayerFeature.POWER,
                PlayerFeature.VOLUME_SET,
                PlayerFeature.PAUSE,
                PlayerFeature.ENQUEUE,
            }

        # update player status
        castplayer._attr_name = castplayer.cast_info.friendly_name
        castplayer._attr_volume_level = int(status.volume_level * 100)
        castplayer._attr_volume_muted = status.volume_muted
        new_powered = (
            castplayer.cc.app_id is not None and castplayer.cc.app_id != pychromecast.IDLE_APP_ID
        )
        if (
            castplayer._attr_powered
            and not new_powered
            and castplayer._attr_type == PlayerType.GROUP
        ):
            # group is being powered off, update group childs
            for child_id in castplayer.group_members:
                if child := self.mass.players.get(child_id):
                    child._attr_powered = False
                    # FIXME: active group
                    child.active_group = None
                    child._attr_active_source = None
                    child.update_state()
        castplayer._attr_powered = new_powered
        self.mass.loop.call_soon(castplayer.update_state)

    def on_new_media_status(self, castplayer: ChromecastPlayer, status: MediaStatus) -> None:
        """Handle updated MediaStatus."""
        self.logger.log(
            VERBOSE_LOG_LEVEL,
            "Received media status for %s update: %s",
            castplayer.display_name,
            status.player_state,
        )
        # handle castplayer playing from a group
        group_player: ChromecastPlayer | Player | None = None
        if castplayer.active_group is not None:
            if not (group_player := self.mass.players.get(castplayer.active_group)):
                return
            assert isinstance(group_player, ChromecastPlayer)  # for type checking
            status = group_player.cc.media_controller.status

        # player state
        castplayer._attr_elapsed_time_last_updated = time.time()
        if status.player_is_playing:
            castplayer._attr_playback_state = PlaybackState.PLAYING
            # castplayer.player.current_item_id = status.content_id
            castplayer.set_current_media(uri=status.content_id or "", clear_all=True)
        elif status.player_is_paused:
            castplayer._attr_playback_state = PlaybackState.PAUSED
            # castplayer.player.current_item_id = status.content_id
            castplayer._attr_current_media = None
        else:
            castplayer._attr_playback_state = PlaybackState.IDLE
            # castplayer.player.current_item_id = None
            castplayer._attr_current_media = None

        # elapsed time
        castplayer._attr_elapsed_time_last_updated = time.time()
        castplayer._attr_elapsed_time = status.adjusted_current_time
        if status.player_is_playing:
            castplayer._attr_elapsed_time = status.adjusted_current_time
        else:
            castplayer._attr_elapsed_time = status.current_time

        # active source
        if group_player:
            castplayer._attr_active_source = group_player.active_source or group_player.player_id
            # FIXME: active group
            castplayer.active_group = group_player.active_group or group_player.player_id
        elif castplayer.cc.app_id in (MASS_APP_ID, APP_MEDIA_RECEIVER):
            castplayer._attr_active_source = castplayer.player_id
        else:
            castplayer._attr_active_source = castplayer.cc.app_display_name

        if status.content_id and not status.player_is_idle:
            castplayer.set_current_media(
                uri=status.content_id,
                title=status.title,
                artist=status.artist,
                album=status.album_name,
                image_url=status.images[0].url if status.images else None,
                duration=int(status.duration) if status.duration is not None else None,
                media_type=MediaType.TRACK,
            )
        else:
            castplayer._attr_current_media = None

        # weird workaround which is needed for multichannel group childs
        # (e.g. a stereo pair within a cast group)
        # where it does not receive updates from the group,
        # so we need to update the group child(s) manually
        if castplayer.type == PlayerType.GROUP and castplayer.powered:
            for child_id in castplayer.group_members:
                if child := self.mass.players.get(child_id):
                    assert isinstance(child, ChromecastPlayer)  # for type checking
                    if not child.cast_info.is_multichannel_group:
                        continue
                    child._attr_playback_state = castplayer.playback_state
                    child._attr_current_media = castplayer.current_media
                    child._attr_elapsed_time = castplayer.elapsed_time
                    child._attr_elapsed_time_last_updated = castplayer.elapsed_time_last_updated
                    child._attr_active_source = castplayer.active_source
                    # fixme: active group
                    child.active_group = castplayer.active_group

                    self.mass.loop.call_soon(child.update_state)

        self.mass.loop.call_soon(castplayer.update_state)

    def on_new_connection_status(
        self, castplayer: ChromecastPlayer, status: ConnectionStatus
    ) -> None:
        """Handle updated ConnectionStatus."""
        self.logger.log(
            VERBOSE_LOG_LEVEL,
            "Received connection status update for %s - status: %s",
            castplayer.display_name,
            status.status,
        )

        if status.status == CONNECTION_STATUS_DISCONNECTED:
            castplayer._attr_available = False
            self.mass.loop.call_soon(castplayer.update_state)
            return

        new_available = status.status == CONNECTION_STATUS_CONNECTED
        if new_available != castplayer.available:
            self.logger.debug(
                "[%s] Cast device availability changed: %s",
                castplayer.cast_info.friendly_name,
                status.status,
            )
            castplayer._attr_available = new_available
            castplayer._attr_device_info = DeviceInfo(
                model=castplayer.cast_info.model_name,
                ip_address=f"{castplayer.cast_info.host}:{castplayer.cast_info.port}",
                manufacturer=castplayer.cast_info.manufacturer or "",
            )
            self.mass.loop.call_soon(castplayer.update_state)

            if new_available and castplayer.type == PlayerType.PLAYER:
                # Poll current group status
                assert self.mz_mgr is not None  # for type checking
                for group_uuid in self.mz_mgr.get_multizone_memberships(castplayer.cast_info.uuid):
                    group_media_controller = self.mz_mgr.get_multizone_mediacontroller(
                        UUID(group_uuid)
                    )
                    if not group_media_controller:
                        continue

    ### Helpers / utils

    async def _disconnect_chromecast(self, castplayer: ChromecastPlayer) -> None:
        """Disconnect Chromecast object if it is set."""
        self.logger.debug("Disconnecting from chromecast socket %s", castplayer.display_name)
        await self.mass.loop.run_in_executor(None, castplayer.cc.disconnect, 10)
        castplayer.mz_controller = None
        if castplayer.status_listener is not None:
            castplayer.status_listener.invalidate()
        castplayer.status_listener = None
        await self.mass.players.remove(castplayer.player_id)
