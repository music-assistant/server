"""Bose SoundTouch player implementation."""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import TYPE_CHECKING

import aiohttp
from music_assistant_models.enums import (
    IdentifierType,
    MediaType,
    PlaybackState,
    PlayerFeature,
    PlayerType,
)
from music_assistant_models.errors import MusicAssistantError
from music_assistant_models.player import DeviceInfo, PlayerSource

from music_assistant.models.player import Player, PlayerMedia

from .client import (
    SoundTouchClient,
    SoundTouchInfo,
    SoundTouchNowPlaying,
    extract_preset_id,
    play_status_is_paused,
    play_status_is_playing,
)
from .config import build_preset_config_entries, preset_media_key, preset_selected_media_key
from .const import (
    CONF_APP_KEY,
    KEY_MUTE,
    KEY_NEXT_TRACK,
    KEY_PAUSE,
    KEY_PLAY,
    KEY_POWER,
    KEY_PREV_TRACK,
    NOTIFICATION_PORT,
    PLAYER_ID_PREFIX,
    PRESET_IDS,
    RECONNECT_DELAY,
    SOURCE_STANDBY,
    WS_HEARTBEAT,
    WS_SUBPROTOCOLS,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry

    from .provider import BoseSoundTouchProvider

IDLE_POLL_INTERVAL = 30
PLAYBACK_POLL_INTERVAL = 10


class BoseSoundTouchPlayer(Player):
    """Bose SoundTouch player in Music Assistant."""

    # preset whose media search should be refreshed on the next config render, set by a
    # search/copy action press (the player config surface re-renders after an action, so
    # the transient search results are surfaced via this instance flag rather than the
    # discarded handle_config_action return)
    _pending_refresh_preset_id: int | None = None

    def __init__(
        self,
        provider: BoseSoundTouchProvider,
        player_id: str,
        client: SoundTouchClient,
        info: SoundTouchInfo,
    ) -> None:
        """Initialize the Player."""
        super().__init__(provider, player_id)
        self._client = client
        self._device_id = info.device_id
        self._attr_name = info.name
        self._attr_type = PlayerType.PLAYER
        # Playback (PLAY_MEDIA) is deliberately omitted: SoundTouch has no usable API to
        # play an arbitrary stream, so audio is routed through a linked playback protocol
        # (such as the DLNA renderer on the same device) via protocol linking.
        self._attr_supported_features = {
            PlayerFeature.POWER,
            PlayerFeature.VOLUME_SET,
            PlayerFeature.VOLUME_MUTE,
            PlayerFeature.PAUSE,
            PlayerFeature.NEXT_PREVIOUS,
            PlayerFeature.SELECT_SOURCE,
            PlayerFeature.SET_MEMBERS,
        }
        # Native announcements require a Bose developer app key; when configured the
        # speaker plays them as an overlay (ducking and resuming the current playback).
        app_key = provider.get_setup_value(CONF_APP_KEY)
        self._app_key = str(app_key) if app_key else None
        if self._app_key:
            self._attr_supported_features.add(PlayerFeature.PLAY_ANNOUNCEMENT)
        self._attr_can_group_with = {provider.instance_id}
        self._attr_device_info = DeviceInfo(
            model=info.model or "Bose SoundTouch",
            manufacturer="Bose",
            software_version=info.software_version,
        )
        self._attr_device_info.add_identifier(IdentifierType.UUID, info.device_id)
        if info.mac_address:
            self._attr_device_info.add_identifier(IdentifierType.MAC_ADDRESS, info.mac_address)
        if info.ip_address:
            self._attr_device_info.add_identifier(IdentifierType.IP_ADDRESS, info.ip_address)
        self._stop_event = asyncio.Event()
        self._listener_task: asyncio.Task[None] | None = None

    # --- Lifecycle ---

    async def setup(self) -> None:
        """Fetch initial state and start listening for device updates."""
        await self._refresh_volume()
        await self._refresh_now_playing()
        await self._refresh_sources()
        self._attr_needs_poll = True
        self._attr_poll_interval = IDLE_POLL_INTERVAL
        self._attr_available = True
        self._listener_task = self.mass.create_task(self._listen())

    async def poll(self) -> None:
        """Poll the speaker as a safety net for missed websocket events."""
        try:
            await self._refresh_now_playing()
            await self._refresh_volume()
        except (aiohttp.ClientError, TimeoutError, OSError) as err:
            self.logger.debug("Poll failed for %s: %s", self.name, err)
            if self._attr_available:
                self._attr_available = False
                self.update_state()
            return
        if not self._attr_available:
            self._attr_available = True
        self._attr_poll_interval = (
            PLAYBACK_POLL_INTERVAL
            if self._attr_playback_state == PlaybackState.PLAYING
            else IDLE_POLL_INTERVAL
        )
        self.update_state()

    async def on_unload(self) -> None:
        """Handle logic when the player is unloaded from the Player controller."""
        self._stop_event.set()
        if self._listener_task:
            self._listener_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._listener_task
            self._listener_task = None
        await super().on_unload()

    async def get_config_entries(self) -> list[ConfigEntry]:
        """Return player-specific config entries (preset button mappings)."""
        # consume the one-shot refresh flag set by a search/copy action press
        refresh_preset_id = self._pending_refresh_preset_id
        self._pending_refresh_preset_id = None
        return await build_preset_config_entries(self, refresh_preset_id=refresh_preset_id)

    async def handle_config_action(self, action: str) -> list[ConfigEntry]:
        """
        Handle a preset search/copy button press and re-render the entries.

        A copy button persists the currently selected search result as that preset's
        media URI; a search button only re-runs that preset's media search. Values are
        read from the (already persisted) stored config; no in-flight form is passed.

        :param action: The action id of the pressed button.
        """
        preset_id, is_copy = _parse_preset_action(action)
        if preset_id is None:
            return await super().handle_config_action(action)
        if is_copy and (
            selected := str(self.get_config_value(preset_selected_media_key(preset_id), "") or "")
        ):
            self.mass.config.set_raw_player_config_value(
                self.player_id, preset_media_key(preset_id), selected
            )
        # the player config surface re-renders via get_config_entries (the controller
        # discards this return), so flag the preset to refresh on that next render
        self._pending_refresh_preset_id = preset_id
        return await build_preset_config_entries(self, refresh_preset_id=preset_id)

    # --- Player commands ---

    async def power(self, powered: bool) -> None:
        """Handle POWER command on the player."""
        now_playing = await self._client.get_now_playing()
        currently_on = now_playing.source != SOURCE_STANDBY
        if powered != currently_on:
            # the POWER key toggles standby, so only send it when a change is needed
            await self._client.press_key(KEY_POWER)
        self._attr_powered = powered
        self.update_state()

    async def volume_set(self, volume_level: int) -> None:
        """Handle VOLUME_SET command on the player."""
        await self._client.set_volume(volume_level)
        self._attr_volume_level = volume_level
        self.update_state()

    async def volume_mute(self, muted: bool) -> None:
        """Handle VOLUME_MUTE command on the player."""
        volume = await self._client.get_volume()
        if volume.muted != muted:
            # the MUTE key toggles mute, so only send it when a change is needed
            await self._client.press_key(KEY_MUTE)
        self._attr_volume_muted = muted
        self.update_state()

    async def play(self) -> None:
        """Handle PLAY (resume) command on a native source."""
        await self._client.press_key(KEY_PLAY)
        self._attr_playback_state = PlaybackState.PLAYING
        self.update_state()

    async def pause(self) -> None:
        """Handle PAUSE command on a native source."""
        await self._client.press_key(KEY_PAUSE)
        self._attr_playback_state = PlaybackState.PAUSED
        self.update_state()

    async def next_track(self) -> None:
        """Handle NEXT_TRACK command on a native source."""
        await self._client.press_key(KEY_NEXT_TRACK)

    async def previous_track(self) -> None:
        """Handle PREVIOUS_TRACK command on a native source."""
        await self._client.press_key(KEY_PREV_TRACK)

    async def select_source(self, source: str) -> None:
        """Handle SELECT_SOURCE command on the player."""
        source_name, _, source_account = source.partition(":")
        await self._client.select_source(source_name, source_account or None)

    async def play_announcement(
        self, announcement: PlayerMedia, volume_level: int | None = None
    ) -> None:
        """Handle (native) playback of an announcement on the player."""
        if not self._app_key:
            return
        self.logger.debug("Playing announcement %s on %s", announcement.uri, self.name)
        await self._client.play_notification(self._app_key, announcement.uri, volume_level)

    async def set_members(
        self,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        """Handle SET_MEMBERS command using the SoundTouch multiroom zone API."""
        if player_ids_to_add:
            members = self._member_tuples(player_ids_to_add)
            if members:
                zone = await self._client.get_zone()
                if zone.master_id == self._device_id:
                    await self._client.add_zone_slaves(self._device_id, members)
                else:
                    own = (self._device_id, self.device_info.ip_address or "")
                    await self._client.set_zone(self._device_id, [own, *members])
        if player_ids_to_remove:
            members = self._member_tuples(player_ids_to_remove)
            if members:
                await self._client.remove_zone_slaves(self._device_id, members)
        await self._refresh_zone()
        for member_id in (player_ids_to_add or []) + (player_ids_to_remove or []):
            self.mass.players.trigger_player_update(member_id)

    # --- Public helpers ---

    @property
    def device_id(self) -> str:
        """Return the Bose SoundTouch device id of this player."""
        return self._device_id

    def update_ip_address(self, ip_address: str) -> None:
        """Update the speaker's IP address after a (re)discovery."""
        if ip_address == self._client.ip_address:
            return
        self.logger.debug("Address updated to %s for player %s", ip_address, self.name)
        self._client.ip_address = ip_address
        self._attr_device_info.add_identifier(IdentifierType.IP_ADDRESS, ip_address)
        self.mass.players.trigger_player_update(self.player_id)

    # --- Private helpers ---

    async def _listen(self) -> None:
        """Connect to the speaker's notification websocket and handle push updates."""
        while not self._stop_event.is_set():
            uri = f"ws://{self._client.ip_address}:{NOTIFICATION_PORT}"
            try:
                async with self.mass.http_session.ws_connect(
                    uri, protocols=WS_SUBPROTOCOLS, heartbeat=WS_HEARTBEAT
                ) as ws:
                    self.logger.debug("Connected to SoundTouch websocket: %s", uri)
                    if not self._attr_available:
                        self._attr_available = True
                        self.update_state()
                    async for msg in ws:
                        if self._stop_event.is_set():
                            break
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await self._handle_message(msg.data)
                        elif msg.type == aiohttp.WSMsgType.BINARY:
                            await self._handle_message(msg.data.decode())
                        elif msg.type in (
                            aiohttp.WSMsgType.ERROR,
                            aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.CLOSED,
                        ):
                            break
            except asyncio.CancelledError:
                raise
            except (aiohttp.ClientError, OSError, TimeoutError, UnicodeDecodeError) as err:
                self.logger.debug(
                    "SoundTouch websocket error for %s: %s. Reconnecting in %ss",
                    self.name,
                    err,
                    RECONNECT_DELAY,
                )
            if not self._stop_event.is_set():
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop_event.wait(), timeout=RECONNECT_DELAY)

    async def _handle_message(self, message: str) -> None:
        """Handle a single websocket notification message."""
        # a physical preset button press maps to the configured Music Assistant media;
        # the bulk "presetsUpdated" notification (which lists all presets) is not a press
        if (
            "presetsUpdated" not in message
            and (preset_id := extract_preset_id(message)) is not None
        ):
            await self._handle_preset(preset_id)
            return
        try:
            if "volumeUpdated" in message:
                await self._refresh_volume()
            if "nowPlayingUpdated" in message:
                await self._refresh_now_playing()
            if "zoneUpdated" in message:
                await self._refresh_zone()
        except (aiohttp.ClientError, TimeoutError, OSError) as err:
            self.logger.debug("Failed to refresh state for %s: %s", self.name, err)

    async def _handle_preset(self, preset_id: int) -> None:
        """Play the Music Assistant media configured for the given preset button."""
        if preset_id not in PRESET_IDS:
            return
        media_id = str(self.config.get_value(preset_media_key(preset_id)) or "")
        if not media_id:
            self.logger.warning(
                "Preset %s pressed on %s but no media is configured", preset_id, self.name
            )
            return
        self.logger.info("Preset %s pressed on %s, playing %s", preset_id, self.name, media_id)
        try:
            await self.mass.player_queues.play_media(queue_id=self.player_id, media=media_id)
        except MusicAssistantError:
            self.logger.exception("Unable to play media for preset %s", preset_id)

    async def _refresh_volume(self) -> None:
        """Refresh volume state from the speaker."""
        volume = await self._client.get_volume()
        self._attr_volume_level = volume.level
        self._attr_volume_muted = volume.muted
        self.update_state()

    async def _refresh_now_playing(self) -> None:
        """Refresh playback state from the speaker."""
        self._update_state_from_now_playing(await self._client.get_now_playing())

    async def _refresh_sources(self) -> None:
        """Refresh the list of selectable native sources from the speaker."""
        try:
            sources = await self._client.get_sources()
        except (aiohttp.ClientError, TimeoutError, OSError) as err:
            self.logger.debug("Failed to fetch sources for %s: %s", self.name, err)
            return
        self._attr_source_list = [
            PlayerSource(
                id=_source_id(source.source, source.source_account),
                name=source.name,
                passive=False,
                can_play_pause=True,
                can_seek=False,
                can_next_previous=True,
            )
            for source in sources
            if source.ready
        ]

    async def _refresh_zone(self) -> None:
        """Refresh multiroom zone (group) membership from the speaker."""
        zone = await self._client.get_zone()
        if zone.master_id == self._device_id and zone.member_ids:
            members = [self.player_id]
            members.extend(
                f"{PLAYER_ID_PREFIX}{member_id}"
                for member_id in zone.member_ids
                if member_id != self._device_id
            )
            self._attr_group_members = members
        else:
            self._attr_group_members = []
        self.update_state()

    def _update_state_from_now_playing(self, now_playing: SoundTouchNowPlaying) -> None:
        """Update player state from a now_playing snapshot."""
        self._attr_powered = now_playing.source != SOURCE_STANDBY
        if not self._attr_powered:
            self._attr_playback_state = PlaybackState.IDLE
            self._attr_active_source = None
            self._attr_current_media = None
            self.update_state()
            return

        if play_status_is_playing(now_playing.play_status):
            self._attr_playback_state = PlaybackState.PLAYING
        elif play_status_is_paused(now_playing.play_status):
            self._attr_playback_state = PlaybackState.PAUSED
        else:
            self._attr_playback_state = PlaybackState.IDLE

        if now_playing.position is not None:
            self._attr_elapsed_time = float(now_playing.position)
            self._attr_elapsed_time_last_updated = time.time()

        active_queue = self.mass.player_queues.get(self.player_id)
        if active_queue and active_queue.current_item:
            # Music Assistant is the active source; audio is rendered via the linked
            # protocol and Music Assistant owns the metadata, so don't override it here.
            self._attr_active_source = self.player_id
        else:
            # a native source (Bluetooth, AUX, Spotify, ...) is playing on the speaker
            self._attr_active_source = _source_id(now_playing.source, now_playing.source_account)
            if now_playing.title or now_playing.artist or now_playing.album:
                self._attr_current_media = PlayerMedia(
                    uri=f"soundtouch://{now_playing.source}",
                    media_type=MediaType.UNKNOWN,
                    title=now_playing.title,
                    artist=now_playing.artist,
                    album=now_playing.album,
                    image_url=now_playing.image_url,
                    duration=now_playing.duration,
                    source_id=self._attr_active_source,
                )
            else:
                self._attr_current_media = None
        self.update_state()

    def _member_tuples(self, player_ids: list[str]) -> list[tuple[str, str]]:
        """Return (device_id, ip_address) tuples for the given SoundTouch player ids."""
        members: list[tuple[str, str]] = []
        for player_id in player_ids:
            player = self.mass.players.get_player(player_id)
            if isinstance(player, BoseSoundTouchPlayer) and (
                ip_address := player.device_info.ip_address
            ):
                members.append((player.device_id, ip_address))
        return members


def _source_id(source: str, source_account: str | None) -> str:
    """Build a stable source id from a SoundTouch source and account."""
    return f"{source}:{source_account}" if source_account else source


def _parse_preset_action(action: str) -> tuple[int | None, bool]:
    """
    Parse a preset action id into its ``(preset_id, is_copy)`` parts.

    Returns ``(None, False)`` for actions that are not preset search/copy buttons.
    """
    for preset_id in PRESET_IDS:
        if action == f"preset_{preset_id}_search_media":
            return preset_id, False
        if action == f"preset_{preset_id}_copy_media":
            return preset_id, True
    return None, False
