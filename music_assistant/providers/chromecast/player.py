"""Chromecast Player implementation."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry, ConfigValueType

from music_assistant_models.enums import (
    IdentifierType,
    MediaType,
    PlaybackState,
    PlayerFeature,
    PlayerType,
)
from music_assistant_models.errors import PlayerUnavailableError
from music_assistant_models.player import PlayerSource
from pychromecast import IDLE_APP_ID
from pychromecast.controllers.media import STREAM_TYPE_BUFFERED, STREAM_TYPE_LIVE
from pychromecast.controllers.multizone import MultizoneController
from pychromecast.socket_client import CONNECTION_STATUS_CONNECTED, CONNECTION_STATUS_DISCONNECTED

from music_assistant.constants import MASS_LOGO_ONLINE, VERBOSE_LOG_LEVEL
from music_assistant.helpers.util import is_valid_mac_address
from music_assistant.models.player import DeviceInfo, Player, PlayerMedia

from .constants import (
    APP_MEDIA_RECEIVER,
    CAST_PLAYER_CONFIG_ENTRIES,
    CONF_ENTRY_SAMPLE_RATES_CAST,
    CONF_ENTRY_SAMPLE_RATES_CAST_GROUP,
    CONF_USE_MASS_APP,
    MASS_APP_ID,
    SENDSPIN_CAST_APP_ID,
)
from .helpers import CastStatusListener, ChromecastInfo

if TYPE_CHECKING:
    from pychromecast import Chromecast
    from pychromecast.controllers.media import MediaStatus
    from pychromecast.controllers.receiver import CastStatus
    from pychromecast.socket_client import ConnectionStatus

    from .provider import ChromecastProvider


class ChromecastPlayer(Player):
    """Chromecast Player."""

    active_cast_group: str | None = None

    def __init__(
        self,
        provider: ChromecastProvider,
        player_id: str,
        cast_info: ChromecastInfo,
        chromecast: Chromecast,
    ) -> None:
        """Init."""
        super().__init__(provider, player_id)
        if cast_info.is_audio_group and cast_info.is_multichannel_group:
            player_type = PlayerType.STEREO_PAIR
        elif cast_info.is_audio_group:
            player_type = PlayerType.GROUP
        elif self._is_google_device(cast_info):
            # Google devices (Chromecast, Nest, Google Home) have native Cast support
            player_type = PlayerType.PLAYER
        else:
            # Non-Google devices are generic Chromecast receivers
            # Will be wrapped in a UniversalPlayer
            player_type = PlayerType.PROTOCOL
        self.cc = chromecast
        self.status_listener: CastStatusListener | None
        self.cast_info = cast_info
        self.mz_controller: MultizoneController | None = None
        self.last_poll = 0.0
        self.flow_meta_checksum: str | None = None
        # set static variables
        self._attr_supported_features = {
            PlayerFeature.PLAY_MEDIA,
            PlayerFeature.POWER,
            PlayerFeature.VOLUME_SET,
            PlayerFeature.VOLUME_MUTE,
            PlayerFeature.PAUSE,
            PlayerFeature.NEXT_PREVIOUS,
            PlayerFeature.ENQUEUE,
            PlayerFeature.SEEK,
        }
        self._attr_name = self.cast_info.friendly_name
        self._attr_available = False
        self._attr_powered = False
        self._attr_needs_poll = True
        self._attr_type = player_type
        # Disable TV's by default
        # (can be enabled manually by the user)
        enabled_by_default = True
        for exclude in ("tv", "/12", "PUS", "OLED"):
            if exclude.lower() in cast_info.friendly_name.lower():
                enabled_by_default = False
        self._attr_enabled_by_default = enabled_by_default

        self._attr_device_info = DeviceInfo(
            model=self.cast_info.model_name,
            manufacturer=self.cast_info.manufacturer or "",
        )
        # add mac/IP identifiers for protocol-matching
        # (but skip for groups since they don't have a real IP/MAC)
        if not cast_info.is_audio_group:
            self._attr_device_info.add_identifier(IdentifierType.IP_ADDRESS, self.cast_info.host)
            # Only add MAC address if it's valid (not 00:00:00:00:00:00)
            if is_valid_mac_address(self.cast_info.mac_address):
                self._attr_device_info.add_identifier(
                    IdentifierType.MAC_ADDRESS, self.cast_info.mac_address
                )
        self._attr_device_info.add_identifier(IdentifierType.UUID, str(self.cast_info.uuid))
        self._attr_device_info.add_identifier(IdentifierType.CAST_UUID, str(self.cast_info.uuid))
        assert provider.mz_mgr is not None  # for type checking
        status_listener = CastStatusListener(self, provider.mz_mgr)
        self.status_listener = status_listener
        if player_type == PlayerType.GROUP:
            mz_controller = MultizoneController(cast_info.uuid)
            self.cc.register_handler(mz_controller)
            self.mz_controller = mz_controller

    async def async_setup(self) -> None:
        """Start the chromecast socket client (must be called after __init__)."""
        await asyncio.to_thread(self.cc.start)

    @staticmethod
    def _is_google_device(cast_info: ChromecastInfo) -> bool:
        """Check if a device is a Google device with native Cast support.

        Google devices (Chromecast, Nest, Google Home) have native Cast support
        and should be exposed as PlayerType.PLAYER. Non-Google devices with Cast
        support should be exposed as PlayerType.PROTOCOL.
        """
        if not cast_info.manufacturer:
            # If no manufacturer, check model name for Google devices
            model = cast_info.model_name.lower() if cast_info.model_name else ""
            return any(google in model for google in ("chromecast", "google", "nest", "home"))
        return cast_info.manufacturer.lower() in ("google", "google inc.")

    async def get_config_entries(
        self,
        action: str | None = None,
        values: dict[str, ConfigValueType] | None = None,
    ) -> list[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the given player (if any)."""
        if self.type == PlayerType.GROUP:
            return [
                *CAST_PLAYER_CONFIG_ENTRIES,
                CONF_ENTRY_SAMPLE_RATES_CAST_GROUP,
            ]

        return [
            *CAST_PLAYER_CONFIG_ENTRIES,
            CONF_ENTRY_SAMPLE_RATES_CAST,
        ]

    async def stop(self) -> None:
        """Send STOP command to given player."""
        await asyncio.to_thread(self.cc.media_controller.stop)

    async def play(self) -> None:
        """Send PLAY command to given player."""
        await asyncio.to_thread(self.cc.media_controller.play)

    async def pause(self) -> None:
        """Send PAUSE command to given player."""
        await asyncio.to_thread(self.cc.media_controller.pause)

    async def next_track(self) -> None:
        """Handle NEXT TRACK command for given player."""
        await asyncio.to_thread(self.cc.media_controller.queue_next)

    async def previous_track(self) -> None:
        """Handle PREVIOUS TRACK command for given player."""
        await asyncio.to_thread(self.cc.media_controller.queue_prev)

    async def seek(self, position: int) -> None:
        """Handle SEEK command on the player."""
        await asyncio.to_thread(self.cc.media_controller.seek, position)

    async def power(self, powered: bool) -> None:
        """Send POWER command to given player."""
        if powered:
            await self._launch_app()
            self._attr_active_source = None
        else:
            self._attr_active_source = None
            await asyncio.to_thread(self.cc.quit_app)
        # optimistically update the state
        self.update_state()

    async def volume_set(self, volume_level: int) -> None:
        """Send VOLUME_SET command to given player."""
        # Round to 2 decimal places to avoid floating-point precision issues
        await asyncio.to_thread(self.cc.set_volume, round(volume_level / 100, 2))

    async def volume_mute(self, muted: bool) -> None:
        """Send VOLUME MUTE command to given player."""
        await asyncio.to_thread(self.cc.set_volume_muted, muted)

    async def play_media(
        self,
        media: PlayerMedia,
    ) -> None:
        """Handle PLAY MEDIA on given player."""
        media.uri = await self.provider.mass.streams.resolve_stream_url(self.player_id, media)
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
        media.uri = await self.provider.mass.streams.resolve_stream_url(self.player_id, media)
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
        except ConnectionResetError as err:
            raise PlayerUnavailableError from err

    async def on_unload(self) -> None:
        """Handle logic when the player is unloaded from the Player controller."""
        await super().on_unload()
        self.mz_controller = None
        if self.status_listener is not None:
            self.status_listener.invalidate()
        self.status_listener = None
        self.logger.debug("Disconnecting from chromecast socket %s", self.display_name)
        if self.mass.closing:
            # Non-blocking disconnect: close socket, don't wait for thread.
            # Socket threads are daemon threads and die on process exit.
            # Blocking disconnect can stall shutdown if threads are slow to exit.
            self.cc.disconnect(blocking=False)
        else:
            await asyncio.to_thread(self.cc.disconnect, 10)

    def _on_player_media_updated(self) -> None:
        """Handle callback when the current media of the player is updated."""
        if not self.powered:
            return
        if not self.cc.media_controller.status.player_is_playing:
            return
        if self.active_cast_group:
            return
        if self._attr_playback_state != PlaybackState.PLAYING:
            return
        if not (current_media := self.current_media):
            return
        if not (
            "/flow/" in self._attr_current_media.uri
            or self.current_media.media_type
            in (
                MediaType.RADIO,
                MediaType.PLUGIN_SOURCE,
            )
        ):
            # only update metadata for streams without known duration
            return

        async def update_flow_metadata() -> None:
            """Update the metadata of a cast player running the flow (or radio) stream."""
            media_controller = self.cc.media_controller
            # update metadata of current item chromecast
            title = current_media.title or "Music Assistant"
            artist = current_media.artist or ""
            album = current_media.album or ""
            image_url = current_media.image_url or MASS_LOGO_ONLINE
            flow_meta_checksum = f"{current_media.uri}-{album}-{artist}-{title}-{image_url}"
            if self.flow_meta_checksum != flow_meta_checksum:
                # only update if something changed
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
                cmd_next_url = self.mass.streams.get_command_url(self.player_id, "next")
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
                await asyncio.to_thread(
                    media_controller.send_message, data=msg, inc_session_id=True
                )

        self.mass.create_task(update_flow_metadata())

    async def _launch_app(self) -> None:
        """Launch the default Media Receiver App on a Chromecast."""
        event = asyncio.Event()

        if self.config.get_value(CONF_USE_MASS_APP, True):
            app_id = MASS_APP_ID
        else:
            app_id = APP_MEDIA_RECEIVER

        if self.cc.app_id == app_id:
            return  # already active

        def launched_callback(success: bool, response: dict[str, Any] | None) -> None:  # noqa: ARG001
            self.mass.loop.call_soon_threadsafe(event.set)

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
        try:
            await asyncio.wait_for(event.wait(), timeout=30.0)
        except TimeoutError:
            self.logger.warning("Timed out waiting for app launch on %s", self.display_name)
            raise PlayerUnavailableError(
                f"Timed out launching app on {self.display_name}"
            ) from None

    ### Callbacks from Chromecast Statuslistener

    def on_new_cast_status(self, status: CastStatus) -> None:
        """Handle updated CastStatus (called from pychromecast socket thread)."""
        if status is None or self.mass.closing:
            return
        # Dispatch to event loop for thread-safe attribute mutation
        self.mass.loop.call_soon_threadsafe(self._handle_cast_status, status)

    def _handle_cast_status(self, status: CastStatus) -> None:
        """Process CastStatus on the event loop thread."""
        if self.mass.closing:
            return
        self.logger.log(
            VERBOSE_LOG_LEVEL,
            "Received cast status for %s - app_id: %s - volume: %s",
            self.display_name,
            status.app_id,
            status.volume_level,
        )
        # handle stereo pairs
        if self.cast_info.is_multichannel_group:
            self._attr_type = PlayerType.STEREO_PAIR
            self._attr_group_members.clear()
        # handle cast groups
        if self.cast_info.is_audio_group and not self.cast_info.is_multichannel_group:
            assert self.mz_controller is not None  # for type checking
            self._attr_type = PlayerType.GROUP
            self._attr_group_members = [str(UUID(x)) for x in self.mz_controller.members]
            self._attr_static_group_members = self._attr_group_members.copy()
            self._attr_supported_features = {
                PlayerFeature.PLAY_MEDIA,
                PlayerFeature.POWER,
                PlayerFeature.VOLUME_SET,
                PlayerFeature.VOLUME_MUTE,
                PlayerFeature.PAUSE,
                PlayerFeature.ENQUEUE,
            }

        # update player status
        self._attr_name = self.cast_info.friendly_name
        self._attr_volume_level = round(status.volume_level * 100)
        self._attr_volume_muted = status.volume_muted
        new_powered = self.cc.app_id is not None and self.cc.app_id != IDLE_APP_ID
        self._attr_powered = new_powered
        if self._attr_powered and not new_powered and self.type == PlayerType.GROUP:
            # group is being powered off, update group childs
            for child_id in self.group_members:
                if child := self.mass.players.get_player(child_id):
                    child.update_state()
        self.update_state()

    def on_new_media_status(self, status: MediaStatus) -> None:
        """Handle updated MediaStatus (called from pychromecast socket thread)."""
        if self.mass.closing:
            return
        # Dispatch to event loop for thread-safe attribute mutation
        self.mass.loop.call_soon_threadsafe(self._handle_media_status, status)

    def _handle_media_status(self, status: MediaStatus) -> None:  # noqa: PLR0915
        """Process MediaStatus on the event loop thread."""
        self.logger.log(
            VERBOSE_LOG_LEVEL,
            "Received media status for %s update: %s",
            self.display_name,
            status.player_state,
        )
        # handle player playing from a group
        group_player: ChromecastPlayer | None = None
        if self.active_cast_group is not None:
            if not (group_player := self.mass.players.get_player(self.active_cast_group)):
                return
            if not isinstance(group_player, ChromecastPlayer):
                return
            status = group_player.cc.media_controller.status

        # player state
        self._attr_elapsed_time_last_updated = time.time()
        if status.player_is_playing:
            self._attr_playback_state = PlaybackState.PLAYING
            self.set_current_media(uri=status.content_id or "", clear_all=True)
        elif status.player_is_paused:
            self._attr_playback_state = PlaybackState.PAUSED
            self._attr_current_media = None
            self._attr_active_source = None
        else:
            self._attr_playback_state = PlaybackState.IDLE
            self._attr_current_media = None
            self._attr_active_source = None

        # elapsed time
        self._attr_elapsed_time_last_updated = time.time()
        self._attr_elapsed_time = status.adjusted_current_time
        if status.player_is_playing:
            self._attr_elapsed_time = status.adjusted_current_time
        else:
            self._attr_elapsed_time = status.current_time

        # active source
        if group_player:
            self._attr_active_source = group_player.active_source or group_player.player_id
        elif self.cc.app_id in (MASS_APP_ID, APP_MEDIA_RECEIVER, SENDSPIN_CAST_APP_ID):
            self._attr_active_source = None
        else:
            app_name = self.cc.app_display_name or "Unknown App"
            app_id = app_name.lower().replace(" ", "_")
            self._attr_active_source = app_id
            has_controls = app_name in ("Spotify", "Qobuz", "YouTube Music", "Deezer", "Tidal")
            if not any(source.id == app_id for source in self._attr_source_list):
                self._attr_source_list.append(
                    PlayerSource(
                        id=app_id,
                        name=app_name,
                        passive=True,
                        can_play_pause=has_controls,
                        can_seek=has_controls,
                        can_next_previous=has_controls,
                    )
                )

        if status.content_id and not status.player_is_idle:
            self.set_current_media(
                uri=status.content_id,
                title=status.title,
                artist=status.artist,
                album=status.album_name,
                image_url=status.images[0].url if status.images else None,
                duration=int(status.duration) if status.duration is not None else None,
                media_type=MediaType.TRACK,
            )
        else:
            self._attr_current_media = None

        # weird workaround which is needed for multichannel group childs
        # (e.g. a stereo pair within a cast group)
        # where it does not receive updates from the group,
        # so we need to update the group child(s) manually
        if self.type == PlayerType.GROUP and self.powered:
            for child_id in self.group_members:
                if child := self.mass.players.get_player(child_id):
                    assert isinstance(child, ChromecastPlayer)  # for type checking
                    if not child.cast_info.is_multichannel_group:
                        continue
                    child._attr_playback_state = self._attr_playback_state
                    child._attr_current_media = self._attr_current_media
                    child._attr_elapsed_time = self._attr_elapsed_time
                    child._attr_elapsed_time_last_updated = self._attr_elapsed_time_last_updated
                    child._attr_active_source = self.active_source
                    child.update_state()
        self.update_state()

    def on_new_connection_status(self, status: ConnectionStatus) -> None:
        """Handle updated ConnectionStatus (called from pychromecast socket thread)."""
        if self.mass.closing:
            return
        # Dispatch to event loop for thread-safe attribute mutation
        self.mass.loop.call_soon_threadsafe(self._handle_connection_status, status)

    def _handle_connection_status(self, status: ConnectionStatus) -> None:
        """Process ConnectionStatus on the event loop thread."""
        self.logger.log(
            VERBOSE_LOG_LEVEL,
            "Received connection status update for %s - status: %s",
            self.display_name,
            status.status,
        )

        if status.status == CONNECTION_STATUS_DISCONNECTED:
            self._attr_available = False
            self.update_state()
            return

        new_available = status.status == CONNECTION_STATUS_CONNECTED
        if new_available != self.available:
            self.logger.debug(
                "[%s] Cast device availability changed: %s",
                self.cast_info.friendly_name,
                status.status,
            )
            self._attr_available = new_available
            self._attr_device_info.model = self.cast_info.model_name
            self._attr_device_info.manufacturer = self.cast_info.manufacturer or ""
            # Groups share a member device's IP/MAC, skip to avoid false protocol matches
            if not self.cast_info.is_audio_group:
                self._attr_device_info.add_identifier(
                    IdentifierType.IP_ADDRESS, self.cast_info.host
                )
                if is_valid_mac_address(self.cast_info.mac_address):
                    self._attr_device_info.add_identifier(
                        IdentifierType.MAC_ADDRESS, self.cast_info.mac_address
                    )
            self._attr_device_info.add_identifier(IdentifierType.UUID, str(self.cast_info.uuid))
            self._attr_device_info.add_identifier(
                IdentifierType.CAST_UUID, str(self.cast_info.uuid)
            )
            self.update_state()

            if new_available and self.type == PlayerType.PLAYER:
                # Poll current group status
                provider = cast("ChromecastProvider", self.provider)
                mz_mgr = provider.mz_mgr
                assert mz_mgr is not None  # for type checking
                for group_uuid in mz_mgr.get_multizone_memberships(self.cast_info.uuid):
                    group_media_controller = mz_mgr.get_multizone_mediacontroller(UUID(group_uuid))
                    if not group_media_controller:
                        continue

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
            },
            "contentType": "audio/flac",
            "streamType": stream_type,
            "metadata": metadata,
            "duration": media.duration,
        }
