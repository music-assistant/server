"""Chromecast Player implementation."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType
    from music_assistant_models.event import MassEvent

from music_assistant_models.enums import (
    ConfigEntryType,
    EventType,
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
from music_assistant.models.player import DeviceInfo, Player, PlayerMedia

from .constants import (
    APP_MEDIA_RECEIVER,
    CAST_PLAYER_CONFIG_ENTRIES,
    CONF_ENTRY_SAMPLE_RATES_CAST,
    CONF_ENTRY_SAMPLE_RATES_CAST_GROUP,
    CONF_SENDSPIN_CODEC,
    CONF_SENDSPIN_SYNC_DELAY,
    CONF_USE_MASS_APP,
    CONF_USE_SENDSPIN_MODE,
    DEFAULT_SENDSPIN_CODEC,
    DEFAULT_SENDSPIN_SYNC_DELAY,
    MASS_APP_ID,
    SENDSPIN_CAST_APP_ID,
    SENDSPIN_CAST_NAMESPACE,
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
        else:
            player_type = PlayerType.PLAYER
        self.cc = chromecast
        self.status_listener: CastStatusListener | None
        self.cast_info = cast_info
        self.mz_controller: MultizoneController | None = None
        self.last_poll = 0.0
        self.flow_meta_checksum: str | None = None
        # set static variables
        self._attr_supported_features = {
            PlayerFeature.POWER,
            PlayerFeature.VOLUME_SET,
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
            ip_address=f"{self.cast_info.host}:{self.cast_info.port}",
            manufacturer=self.cast_info.manufacturer or "",
        )
        assert provider.mz_mgr is not None  # for type checking
        status_listener = CastStatusListener(self, provider.mz_mgr)
        self.status_listener = status_listener
        if player_type == PlayerType.GROUP:
            mz_controller = MultizoneController(cast_info.uuid)
            self.cc.register_handler(mz_controller)
            self.mz_controller = mz_controller
        self.cc.start()

        # Chromecast players can optionally use Sendspin for streaming
        # when the sendspin-over-cast receiver app is used.
        # Generate a predictable sendspin player id from the chromecast uuid.
        # Format: "cast-XXXXXXXX" where X is derived from the UUID
        uuid_str = player_id.replace("-", "")
        self.sendspin_player_id = f"cast-{uuid_str[:8].lower()}"
        self._last_sent_sync_delay: int | None = None
        self._last_sent_codec: str | None = None

        # Subscribe to sendspin player events for state syncing
        self._on_unload_callbacks.append(
            self.mass.subscribe(
                self._on_sendspin_player_event,
                (EventType.PLAYER_UPDATED,),
                self.sendspin_player_id,
            )
        )

    @property
    def sendspin_mode_enabled(self) -> bool:
        """Return if sendspin mode is enabled for the player."""
        return bool(
            self.mass.config.get_raw_player_config_value(
                self.player_id, CONF_USE_SENDSPIN_MODE, False
            )
        )

    def get_linked_sendspin_player(self, enabled_only: bool = True) -> Player | None:
        """Return the linked sendspin player if available/enabled."""
        if enabled_only and not self.sendspin_mode_enabled:
            return None
        if not (sendspin_player := self.mass.players.get(self.sendspin_player_id)):
            return None
        if not sendspin_player.available:
            return None
        return sendspin_player

    @property
    def supported_features(self) -> set[PlayerFeature]:
        """Return the supported features for this player."""
        try:
            if self.sendspin_mode_enabled:
                # Features for Sendspin mode - grouping happens via Sendspin player
                return {
                    PlayerFeature.POWER,
                    PlayerFeature.VOLUME_SET,
                    PlayerFeature.VOLUME_MUTE,
                    PlayerFeature.PAUSE,
                }
        except Exception:  # noqa: S110
            pass  # May fail during early initialization
        return self._attr_supported_features

    def _translate_from_sendspin_player_id(self, sendspin_player_id: str) -> str | None:
        """Translate a Sendspin player ID back to its Chromecast player ID if applicable."""
        # Sendspin player IDs for Chromecast are "cast-XXXXXXXX" where X is from UUID
        if not sendspin_player_id.startswith("cast-"):
            return None
        # Search for a Chromecast player with matching sendspin_player_id
        for player in self.mass.players.all():
            if hasattr(player, "sendspin_player_id"):
                if player.sendspin_player_id == sendspin_player_id:
                    return player.player_id
        return None

    async def _on_sendspin_player_event(self, event: MassEvent) -> None:
        """Handle incoming event from linked sendspin player."""
        if not self.sendspin_mode_enabled:
            return
        if event.object_id != self.sendspin_player_id:
            return
        # Sync state from sendspin player to this player
        if sendspin_player := self.get_linked_sendspin_player(False):
            self._attr_playback_state = sendspin_player.playback_state
            self._attr_current_media = sendspin_player.current_media
            self._attr_elapsed_time = sendspin_player.elapsed_time
            self._attr_elapsed_time_last_updated = sendspin_player.elapsed_time_last_updated
            # Sync active_source so queue lookup works correctly
            self._attr_active_source = sendspin_player.active_source
            # Translate group_members from Sendspin player IDs to Chromecast player IDs
            translated_members = []
            for member_id in sendspin_player.group_members:
                if cc_id := self._translate_from_sendspin_player_id(member_id):
                    translated_members.append(cc_id)
                else:
                    # Keep original if no translation (e.g. non-Chromecast Sendspin player)
                    translated_members.append(member_id)
            self._attr_group_members = translated_members
            # Translate synced_to from Sendspin player ID to Chromecast player ID
            if sendspin_player.synced_to:
                self._attr_synced_to = (
                    self._translate_from_sendspin_player_id(sendspin_player.synced_to)
                    or sendspin_player.synced_to
                )
            else:
                self._attr_synced_to = None
            self.update_state()
            # Check if sync delay config changed and resend if needed
            current_sync_delay = int(
                self.mass.config.get_raw_player_config_value(
                    self.player_id, CONF_SENDSPIN_SYNC_DELAY, DEFAULT_SENDSPIN_SYNC_DELAY
                )
            )
            if self._last_sent_sync_delay != current_sync_delay:
                # Update immediately to prevent duplicate sends from concurrent events
                self._last_sent_sync_delay = current_sync_delay
                self.mass.create_task(self._send_sendspin_sync_delay(current_sync_delay))

    async def get_config_entries(
        self,
        action: str | None = None,
        values: dict[str, ConfigValueType] | None = None,
    ) -> list[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the given player (if any)."""
        base_entries = await super().get_config_entries(action=action, values=values)

        # Check if Sendspin provider is available
        sendspin_available = any(
            prov.domain == "sendspin" for prov in self.mass.get_providers("player")
        )

        # Sendspin mode config entry
        sendspin_config = ConfigEntry(
            key=CONF_USE_SENDSPIN_MODE,
            type=ConfigEntryType.BOOLEAN,
            label="Enable experimental Sendspin mode",
            description="When enabled, Music Assistant will use the Sendspin protocol "
            "for synchronized audio streaming instead of the standard Chromecast protocol. "
            "This allows grouping Chromecast devices with other Sendspin-compatible players "
            "for multi-room synchronized playback.\n\n"
            "NOTE: Requires the Sendspin provider to be enabled.",
            required=False,
            default_value=False,
            hidden=not sendspin_available or self.type == PlayerType.GROUP,
        )

        # Sync delay config entry (only visible when sendspin provider is available)
        sendspin_sync_delay_config = ConfigEntry(
            key=CONF_SENDSPIN_SYNC_DELAY,
            type=ConfigEntryType.INTEGER,
            label="Sendspin sync delay (ms)",
            description="Static delay in milliseconds to adjust audio synchronization. "
            "Positive values delay playback, negative values advance it. "
            "Use this to compensate for device-specific audio latency. "
            "Changes take effect immediately.",
            required=False,
            default_value=DEFAULT_SENDSPIN_SYNC_DELAY,
            range=(-1000, 1000),
            hidden=not sendspin_available or self.type == PlayerType.GROUP,
            immediate_apply=True,
        )

        # Codec config entry (only visible when sendspin provider is available)
        sendspin_codec_config = ConfigEntry(
            key=CONF_SENDSPIN_CODEC,
            type=ConfigEntryType.STRING,
            label="Sendspin audio codec",
            description="Audio codec used for the experimental Sendspin mode. "
            "FLAC offers good compression with lossless quality. "
            "Opus provides better compression but may have compatibility issues. "
            "PCM is uncompressed and uses more bandwidth.",
            required=False,
            default_value=DEFAULT_SENDSPIN_CODEC,
            options=[
                ConfigValueOption("FLAC (lossless, compressed)", "flac"),
                ConfigValueOption("Opus (lossy, experimental)", "opus"),
                ConfigValueOption("PCM (lossless, uncompressed)", "pcm"),
            ],
            hidden=not sendspin_available or self.type == PlayerType.GROUP,
        )

        if self.type == PlayerType.GROUP:
            return [
                *base_entries,
                *CAST_PLAYER_CONFIG_ENTRIES,
                CONF_ENTRY_SAMPLE_RATES_CAST_GROUP,
            ]

        return [
            *base_entries,
            *CAST_PLAYER_CONFIG_ENTRIES,
            CONF_ENTRY_SAMPLE_RATES_CAST,
            sendspin_config,
            sendspin_sync_delay_config,
            sendspin_codec_config,
        ]

    async def on_config_updated(self) -> None:
        """Handle config updates - resend Sendspin config if needed."""
        if not self.sendspin_mode_enabled:
            return

        # Get current config values
        current_sync_delay = int(
            self.mass.config.get_raw_player_config_value(
                self.player_id, CONF_SENDSPIN_SYNC_DELAY, DEFAULT_SENDSPIN_SYNC_DELAY
            )
        )
        current_codec = str(
            self.mass.config.get_raw_player_config_value(
                self.player_id, CONF_SENDSPIN_CODEC, DEFAULT_SENDSPIN_CODEC
            )
        )

        sync_delay_changed = self._last_sent_sync_delay != current_sync_delay
        codec_changed = self._last_sent_codec != current_codec

        if sync_delay_changed or codec_changed:
            # Store old values for logging before updating state
            old_codec = self._last_sent_codec
            # Update immediately to prevent duplicate sends from concurrent events
            self._last_sent_sync_delay = current_sync_delay
            self._last_sent_codec = current_codec
            try:
                if codec_changed:
                    # Codec changed - need full reconnection
                    self.logger.debug(
                        "Sendspin codec changed (%s -> %s), sending full config",
                        old_codec,
                        current_codec,
                    )
                    await self._send_sendspin_server_url()
                else:
                    # Only sync delay changed, don't reconnect, just send updated delay
                    await self._send_sendspin_sync_delay(current_sync_delay)
            except Exception as err:
                self.logger.warning("Failed to send updated Sendspin config to Chromecast: %s", err)

    async def stop(self) -> None:
        """Send STOP command to given player."""
        if sendspin_player := self.get_linked_sendspin_player(True):
            # Sendspin mode is active - direct call to stop (NOT cmd_stop to avoid recursion)
            self.logger.debug("Redirecting STOP command to linked sendspin player.")
            await sendspin_player.stop()
            return
        await asyncio.to_thread(self.cc.media_controller.stop)

    async def play(self) -> None:
        """Send PLAY command to given player."""
        await asyncio.to_thread(self.cc.media_controller.play)

    async def pause(self) -> None:
        """Send PAUSE command to given player."""
        if self.sendspin_mode_enabled:
            # In Sendspin mode, there's no native Cast media session to pause.
            # Sendspin doesn't support pause, so stop the stream instead.
            if sendspin_player := self.get_linked_sendspin_player(True):
                self.logger.debug("Sendspin mode: stopping stream (pause not supported)")
                await sendspin_player.stop()
            return
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
            if self.sendspin_mode_enabled:
                # Launch Sendspin app and connect to server
                self.logger.info("Powering on with Sendspin mode enabled.")
                launch_success = await self._launch_sendspin_app()
                if launch_success:
                    await asyncio.sleep(1)  # Give app time to initialize
                    await self._send_sendspin_server_url()
                    # Wait for the Sendspin player to connect
                    sendspin_player = await self._wait_for_sendspin_player()
                    if sendspin_player:
                        self.logger.info(
                            "Sendspin player %s connected successfully.",
                            sendspin_player.player_id,
                        )
                    else:
                        self.logger.warning("Sendspin player did not connect, but app is running.")
                else:
                    raise PlayerUnavailableError("Failed to launch Sendspin Cast App")
            else:
                await self._launch_app()
            self._attr_active_source = self.player_id
        else:
            self._attr_active_source = None
            await asyncio.to_thread(self.cc.quit_app)
        # optimistically update the state
        self.mass.loop.call_soon_threadsafe(self.update_state)

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
        if self.sendspin_mode_enabled:
            # Sendspin mode is enabled, launch sendspin-over-cast app and redirect
            self.logger.info("Redirecting PLAY_MEDIA command to sendspin mode.")
            await self._play_media_sendspin(media)
            return

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
        except ConnectionResetError as err:
            raise PlayerUnavailableError from err

    async def on_unload(self) -> None:
        """Handle logic when the player is unloaded from the Player controller."""
        await super().on_unload()
        self.logger.debug("Disconnecting from chromecast socket %s", self.display_name)
        await self.mass.loop.run_in_executor(None, self.cc.disconnect, 10)
        self.mz_controller = None
        if self.status_listener is not None:
            self.status_listener.invalidate()
        self.status_listener = None

    def _on_player_media_updated(self) -> None:
        """Handle callback when the current media of the player is updated."""
        if not self.powered:
            return
        if not self.cc.media_controller.status.player_is_playing:
            return
        if self.active_cast_group:
            return
        if self.playback_state != PlaybackState.PLAYING:
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
        await event.wait()

    ### Callbacks from Chromecast Statuslistener

    def on_new_cast_status(self, status: CastStatus) -> None:
        """Handle updated CastStatus."""
        if status is None:
            return  # guard
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
            self.group_members.clear()
        # handle cast groups
        if self.cast_info.is_audio_group and not self.cast_info.is_multichannel_group:
            assert self.mz_controller is not None  # for type checking
            self._attr_type = PlayerType.GROUP
            self._attr_group_members = [str(UUID(x)) for x in self.mz_controller.members]
            self._attr_supported_features = {
                PlayerFeature.POWER,
                PlayerFeature.VOLUME_SET,
                PlayerFeature.PAUSE,
                PlayerFeature.ENQUEUE,
            }

        # update player status
        self._attr_name = self.cast_info.friendly_name
        self._attr_volume_level = round(status.volume_level * 100)
        self._attr_volume_muted = status.volume_muted
        new_powered = self.cc.app_id is not None and self.cc.app_id != IDLE_APP_ID
        self._attr_powered = new_powered
        if self._attr_powered and not new_powered and self._attr_type == PlayerType.GROUP:
            # group is being powered off, update group childs
            for child_id in self.group_members:
                if child := self.mass.players.get(child_id):
                    self.mass.loop.call_soon_threadsafe(child.update_state)
        self.mass.loop.call_soon_threadsafe(self.update_state)

    def on_new_media_status(self, status: MediaStatus) -> None:  # noqa: PLR0915
        """Handle updated MediaStatus."""
        self.logger.log(
            VERBOSE_LOG_LEVEL,
            "Received media status for %s update: %s",
            self.display_name,
            status.player_state,
        )
        # In Sendspin mode, state is synced from the Sendspin player - skip Cast media status
        if self.sendspin_mode_enabled:
            return
        # handle player playing from a group
        group_player: ChromecastPlayer | None = None
        if self.active_cast_group is not None:
            if not (group_player := self.mass.players.get(self.active_cast_group)):
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
        elif self.cc.app_id in (MASS_APP_ID, APP_MEDIA_RECEIVER):
            self._attr_active_source = self.player_id
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
                if child := self.mass.players.get(child_id):
                    assert isinstance(child, ChromecastPlayer)  # for type checking
                    if not child.cast_info.is_multichannel_group:
                        continue
                    child._attr_playback_state = self.playback_state
                    child._attr_current_media = self.current_media
                    child._attr_elapsed_time = self.elapsed_time
                    child._attr_elapsed_time_last_updated = self.elapsed_time_last_updated
                    child._attr_active_source = self.active_source
                    self.mass.loop.call_soon_threadsafe(child.update_state)
        self.mass.loop.call_soon_threadsafe(self.update_state)

    def on_new_connection_status(self, status: ConnectionStatus) -> None:
        """Handle updated ConnectionStatus."""
        self.logger.log(
            VERBOSE_LOG_LEVEL,
            "Received connection status update for %s - status: %s",
            self.display_name,
            status.status,
        )

        if status.status == CONNECTION_STATUS_DISCONNECTED:
            self._attr_available = False
            self.mass.loop.call_soon_threadsafe(self.update_state)
            return

        new_available = status.status == CONNECTION_STATUS_CONNECTED
        if new_available != self.available:
            self.logger.debug(
                "[%s] Cast device availability changed: %s",
                self.cast_info.friendly_name,
                status.status,
            )
            self._attr_available = new_available
            self._attr_device_info = DeviceInfo(
                model=self.cast_info.model_name,
                ip_address=f"{self.cast_info.host}:{self.cast_info.port}",
                manufacturer=self.cast_info.manufacturer or "",
            )
            self.mass.loop.call_soon_threadsafe(self.update_state)

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

    async def _launch_sendspin_app(self) -> bool:
        """Launch the sendspin-over-cast receiver app on the Chromecast.

        :return: True if app launched successfully, False otherwise.
        """
        event = asyncio.Event()
        launch_success = False

        if self.cc.app_id == SENDSPIN_CAST_APP_ID:
            self.logger.debug("Sendspin Cast App already active.")
            return True

        def launched_callback(success: bool, response: dict[str, Any] | None) -> None:
            nonlocal launch_success
            launch_success = success
            if not success:
                self.logger.warning("Failed to launch Sendspin Cast App: %s", response)
            else:
                self.logger.debug("Sendspin Cast App launched successfully.")
            self.mass.loop.call_soon_threadsafe(event.set)

        def launch() -> None:
            # Quit the previous app before starting sendspin receiver
            if self.cc.app_id is not None:
                self.cc.quit_app()
            self.logger.info(
                "Launching Sendspin Cast App %s on %s.",
                SENDSPIN_CAST_APP_ID,
                self.display_name,
            )
            self.cc.socket_client.receiver_controller.launch_app(
                SENDSPIN_CAST_APP_ID,
                force_launch=True,
                callback_function=launched_callback,
            )

        await self.mass.loop.run_in_executor(None, launch)
        try:
            await asyncio.wait_for(event.wait(), timeout=10.0)
        except TimeoutError:
            self.logger.error("Timeout waiting for Sendspin Cast App to launch.")
            return False
        return launch_success

    async def _send_sendspin_server_url(self) -> None:
        """Send the Sendspin server URL to the Cast receiver via custom messaging."""
        # Get the Sendspin server URL from the streams controller
        server_url = f"http://{self.mass.streams.publish_ip}:8927"
        # Player name with (Sendspin) suffix for the Sendspin player
        player_name = f"{self._attr_name} (Sendspin)"
        # Get sync delay from config (in milliseconds)
        sync_delay = int(
            self.mass.config.get_raw_player_config_value(
                self.player_id, CONF_SENDSPIN_SYNC_DELAY, DEFAULT_SENDSPIN_SYNC_DELAY
            )
        )
        # Get codec from config (default to flac)
        codec = str(
            self.mass.config.get_raw_player_config_value(
                self.player_id, CONF_SENDSPIN_CODEC, DEFAULT_SENDSPIN_CODEC
            )
        )
        codecs = [codec]

        def send_message() -> None:
            # Send custom message to receiver with server URL, player ID, name, sync delay, codecs
            self.cc.socket_client.send_app_message(
                SENDSPIN_CAST_NAMESPACE,
                {
                    "serverUrl": server_url,
                    "playerId": self.sendspin_player_id,
                    "playerName": player_name,
                    "syncDelay": sync_delay,
                    "codecs": codecs,
                },
            )

        self.logger.debug(
            "Sending Sendspin config to Cast receiver: url=%s, name=%s, syncDelay=%dms, codecs=%s",
            server_url,
            player_name,
            sync_delay,
            codecs,
        )
        await self.mass.loop.run_in_executor(None, send_message)
        self._last_sent_sync_delay = sync_delay
        self._last_sent_codec = codec

    async def _send_sendspin_sync_delay(self, sync_delay: int) -> None:
        """Send only the sync delay update to the Cast receiver (no reconnection)."""

        def send_message() -> None:
            self.cc.socket_client.send_app_message(
                SENDSPIN_CAST_NAMESPACE,
                {"syncDelay": sync_delay},
            )

        self.logger.debug(
            "Sending Sendspin sync delay update to Cast receiver: syncDelay=%dms",
            sync_delay,
        )
        await self.mass.loop.run_in_executor(None, send_message)
        self._last_sent_sync_delay = sync_delay

    async def _wait_for_sendspin_player(self, timeout: float = 15.0) -> Player | None:
        """Wait for the Sendspin player to connect and become available."""
        start_time = time.time()
        while (time.time() - start_time) < timeout:
            if sendspin_player := self.mass.players.get(self.sendspin_player_id):
                if sendspin_player.available:
                    self.logger.debug(
                        "Sendspin player %s is now available", self.sendspin_player_id
                    )
                    return sendspin_player
            await asyncio.sleep(0.5)
        self.logger.warning(
            "Timeout waiting for Sendspin player %s to become available",
            self.sendspin_player_id,
        )
        return None

    async def _play_media_sendspin(self, media: PlayerMedia) -> None:
        """Handle PLAY MEDIA using the Sendspin protocol via sendspin-over-cast."""
        self.logger.info(
            "Starting Sendspin playback on %s (sendspin_player_id=%s)",
            self.display_name,
            self.sendspin_player_id,
        )

        # Check if the sendspin player is already connected (available)
        if sendspin_player := self.get_linked_sendspin_player(False):
            # Sendspin player is already connected, just redirect the media
            self.logger.debug(
                "Sendspin player already connected (state=%s), redirecting media.",
                sendspin_player.playback_state,
            )
            await self.mass.players.play_media(sendspin_player.player_id, media)
            return

        # Sendspin player not connected yet - launch app and connect
        launch_success = await self._launch_sendspin_app()
        if not launch_success:
            raise PlayerUnavailableError("Failed to launch Sendspin Cast App")

        # Give the app a moment to initialize
        await asyncio.sleep(1)
        # Send the Sendspin server URL to the receiver
        await self._send_sendspin_server_url()
        # Wait for the Sendspin player to connect
        sendspin_player = await self._wait_for_sendspin_player()
        if not sendspin_player:
            raise PlayerUnavailableError("Failed to establish Sendspin connection")

        # Redirect playback to the Sendspin player
        self.logger.info("Starting playback on Sendspin player %s", sendspin_player.player_id)
        await self.mass.players.play_media(sendspin_player.player_id, media)
