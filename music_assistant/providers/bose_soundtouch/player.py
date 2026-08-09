"""Bose SoundTouch player implementation."""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import TYPE_CHECKING, cast

import aiohttp
from music_assistant_models.enums import (
    IdentifierType,
    MediaType,
    PlaybackState,
    PlayerFeature,
    PlayerType,
)
from music_assistant_models.errors import MusicAssistantError
from music_assistant_models.player import (
    DeviceInfo,
    PlayerOption,
    PlayerOptionType,
    PlayerOptionValueType,
    PlayerSource,
)

from music_assistant.constants import (
    CONF_ENTRY_FLOW_MODE,
    CONF_ENTRY_HTTP_PROFILE_DEFAULT_2,
    create_sample_rates_config_entry,
)
from music_assistant.models.player import Player, PlayerMedia
from music_assistant.providers.bose_soundtouch.avt_helpers import avt_play, avt_set_url, avt_stop

from .client.exceptions import ApiError
from .client.schema.enums import Key, PlayStatus, SourceStatus
from .client.schema.models import Info, NowPlaying, Zone, ZoneMember
from .const import (
    CONF_APP_KEY,
    IDLE_POLL_INTERVAL,
    NOTIFICATION_PORT,
    PLAYBACK_POLL_INTERVAL,
    PLAYER_ID_PREFIX,
    PRESET_IDS,
    RECONNECT_DELAY,
    SOURCE_INVALID,
    SOURCE_STANDBY,
    WS_HEARTBEAT,
    WS_SUBPROTOCOLS,
    PlayerOptionKeys,
)
from .helpers import extract_preset_id, source_id

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry

    from .client import SoundtouchDevice
    from .provider import BoseSoundTouchProvider


class BoseSoundTouchPlayer(Player):
    """Bose SoundTouch player in Music Assistant."""

    def __init__(
        self,
        provider: BoseSoundTouchProvider,
        player_id: str,
        client: SoundtouchDevice,
        info: Info,
    ) -> None:
        """Initialize the Player."""
        super().__init__(provider, player_id)
        self._client = client
        self._device_id = info.device_id
        # Native announcements require a Bose developer app key; when configured the
        # speaker plays them as an overlay (ducking and resuming the current playback).
        app_key = self.provider.get_setup_value(CONF_APP_KEY)
        self._app_key = str(app_key) if app_key else None
        self._update_lock = asyncio.Lock()

        self._stop_event = asyncio.Event()
        self._listener_task: asyncio.Task[None] | None = None

        self._supported_player_options: set[PlayerOptionKeys] = set()

    async def setup(self, info: Info) -> None:
        """Fetch initial state and start listening for device updates."""
        self.set_static_attributes(info)

        # initial refresh no lock needed
        await self._refresh_volume()
        await self._refresh_now_playing()
        await self._refresh_options()
        await self._refresh_zone()
        await self._refresh_sources()

        self.update_state()

        self._listener_task = self.mass.create_task(self._listen())

    def set_static_attributes(self, info: Info) -> None:
        """Set static attributes."""
        self._attr_needs_poll = True
        self._attr_poll_interval = IDLE_POLL_INTERVAL
        self._attr_available = True
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
            PlayerFeature.OPTIONS,
            PlayerFeature.PLAY_MEDIA,
        }
        if self._app_key:
            self._attr_supported_features.add(PlayerFeature.PLAY_ANNOUNCEMENT)

        self._attr_name = info.name
        self._attr_can_group_with = {self.provider.instance_id}
        self._attr_device_info = DeviceInfo(
            model=info.model or "Bose SoundTouch",
            manufacturer="Bose",
            software_version=info.software_version,
        )
        self._attr_device_info.add_identifier(IdentifierType.UUID, info.device_id)
        if info.mac_addresses:
            for mac_address in info.mac_addresses:
                self._attr_device_info.add_identifier(IdentifierType.MAC_ADDRESS, mac_address)
        if info.ip_addresses:
            for ip_address in info.ip_addresses:
                self._attr_device_info.add_identifier(IdentifierType.IP_ADDRESS, ip_address)

    async def poll(self) -> None:
        """Poll the speaker as a safety net for missed websocket events."""
        async with self._update_lock:
            try:
                await self._refresh_now_playing()
                await self._refresh_volume()
                await self._refresh_zone()
                await self._refresh_options()
            except (aiohttp.ClientError, TimeoutError, OSError) as err:
                self.logger.debug("Poll failed for %s: %s", self.name, err)
                self._attr_available = False
                self.update_state()
                return
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

    # --- Player commands ---

    async def power(self, powered: bool) -> None:
        """Handle POWER command on the player."""
        now_playing = await self._client.get_now_playing()
        currently_on = False
        if now_playing.content_item and now_playing.content_item.source:
            currently_on = now_playing.content_item.source != SOURCE_STANDBY
        if powered != currently_on:
            # the POWER key toggles standby, so only send it when a change is needed
            await self._client.press_key(Key.POWER)
        self._attr_powered = powered
        self.update_state()

    async def volume_set(self, volume_level: int) -> None:
        """Handle VOLUME_SET command on the player."""
        await self._client.set_volume(volume_level, mute=False)
        self._attr_volume_level = volume_level
        self.update_state()

    async def volume_mute(self, muted: bool) -> None:
        """Handle VOLUME_MUTE command on the player."""
        volume = await self._client.get_volume()
        if volume.mute_enabled != muted:
            # the MUTE key toggles mute, so only send it when a change is needed
            await self._client.press_key(Key.MUTE)
        self._attr_volume_muted = muted
        self.update_state()

    async def play(self) -> None:
        """Handle PLAY (resume) command on a native source."""
        await self._client.press_key(Key.PLAY)
        self._attr_playback_state = PlaybackState.PLAYING
        self.update_state()

    async def play_media(self, media: PlayerMedia) -> None:
        """Play media."""
        async with self._update_lock:
            media.uri = await self.provider.mass.streams.resolve_stream_url(self.player_id, media)
            # clear any pending AVT state to avoid wedging on rapid play_media
            self.logger.debug("Starting to play %s.", media.title)
            await avt_stop(self.mass.http_session, self)
            await avt_set_url(self.mass.http_session, self, player_media=media)
            await avt_play(self.mass.http_session, self)
            self._attr_poll_interval = PLAYBACK_POLL_INTERVAL

    async def stop(self) -> None:
        """Stop command."""
        await self._client.press_key(Key.STOP)

    async def pause(self) -> None:
        """Handle PAUSE command on a native source."""
        await self._client.press_key(Key.PAUSE)
        self._attr_playback_state = PlaybackState.PAUSED
        self.update_state()

    async def next_track(self) -> None:
        """Handle NEXT_TRACK command on a native source."""
        await self._client.press_key(Key.NEXT_TRACK)

    async def previous_track(self) -> None:
        """Handle PREVIOUS_TRACK command on a native source."""
        await self._client.press_key(Key.PREV_TRACK)

    async def select_source(self, source: str) -> None:
        """Handle SELECT_SOURCE command on the player."""
        source_name, _, source_account = source.partition(":")
        await self._client.select_source(source_name, source_account or None)
        await self._refresh_now_playing()

    async def play_announcement(
        self, announcement: PlayerMedia, volume_level: int | None = None
    ) -> None:
        """Handle (native) playback of an announcement on the player."""
        if not self._app_key:
            return
        self.logger.debug("Playing announcement %s on %s", announcement.uri, self.name)
        await self._client.play_notification(self._app_key, announcement.uri, volume_level)

    async def set_option(self, option_key: str, option_value: PlayerOptionValueType) -> None:
        """Set player option."""
        match option_key:
            case PlayerOptionKeys.NETWORK_NAME:
                await self._client.set_name(str(option_value))
            case PlayerOptionKeys.BASS:
                await self._client.set_bass(int(option_value))

        await self._refresh_options()

    async def set_members(
        self,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        """Handle SET_MEMBERS command using the SoundTouch multiroom zone API."""

        def get_zone(player_ids: list[str]) -> Zone:
            members: list[ZoneMember] = []
            for player_id in player_ids:
                player = self.mass.players.get_player(player_id)
                if isinstance(player, BoseSoundTouchPlayer) and (
                    ip_address := player._client.session_config.ip
                ):
                    members.append(ZoneMember(ip=ip_address, mac=player.device_id))
            return Zone(
                leader=ZoneMember(ip=self._client.session_config.ip, mac=self._device_id),
                members=members,
            )

        current_zone = await self._client.get_zone()
        if not current_zone.members and player_ids_to_add:
            # create a new zone
            self.logger.debug("Creating a new zone.")
            zone = get_zone(player_ids_to_add)
            await self._client.set_zone(zone)
            await self._refresh_zone()
            return
        self.logger.debug("Updating an existing zone.")

        # update a current zone
        if player_ids_to_add:
            zone = get_zone(player_ids_to_add)
            await self._client.add_zone_members(zone)

        if player_ids_to_remove:
            zone = get_zone(player_ids_to_remove)
            await self._client.remove_zone_members(zone)

        await self._refresh_zone()

    # --- Public helpers ---

    @property
    def device_id(self) -> str:
        """Return the Bose SoundTouch device id of this player."""
        return self._device_id

    def update_ip_address(self, ip_address: str) -> None:
        """Update the speaker's IP address after a (re)discovery."""
        if ip_address == self._client.session_config.ip:
            return
        self.logger.debug("Address updated to %s for player %s", ip_address, self.name)
        self._client.session_config.ip = ip_address
        self._attr_device_info.add_identifier(IdentifierType.IP_ADDRESS, ip_address)
        self.mass.players.trigger_player_update(self.player_id)

    async def get_config_entries(self) -> list[ConfigEntry]:
        """Get config entries."""
        base_entries = await super().get_config_entries()

        default_entries = [
            CONF_ENTRY_HTTP_PROFILE_DEFAULT_2,
            CONF_ENTRY_FLOW_MODE,
            create_sample_rates_config_entry(max_sample_rate=192000, max_bit_depth=24),
        ]
        return base_entries + default_entries

    # --- Private helpers ---

    async def _listen(self) -> None:
        """Connect to the speaker's notification websocket and handle push updates."""
        while not self._stop_event.is_set():
            uri = f"ws://{self._client.session_config.ip}:{NOTIFICATION_PORT}"
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
                            await self._handle_update_message(msg.data)
                        elif msg.type == aiohttp.WSMsgType.BINARY:
                            await self._handle_update_message(msg.data.decode())
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

    async def _handle_update_message(self, message: str) -> None:
        """Handle a single websocket notification message."""
        # a physical preset button press maps to the configured Music Assistant media;
        # the bulk "presetsUpdated" notification (which lists all presets) is not a press
        if (
            "presetsUpdated" not in message
            and (preset_id := extract_preset_id(message)) is not None
        ):
            await self._handle_preset(preset_id)
            return
        if self._update_lock.locked():
            # ignore message as we poll regularly too, and polling is the ultimate truth
            return
        async with self._update_lock:
            try:
                if "volumeUpdated" in message:
                    await self._refresh_volume()
                if "nowPlayingUpdated" in message:
                    await self._refresh_now_playing()
                if "zoneUpdated" in message:
                    await self._refresh_zone()
                self.update_state()
            except (aiohttp.ClientError, TimeoutError, OSError) as err:
                self.logger.debug("Failed to refresh state for %s: %s", self.name, err)

    async def _handle_preset(self, preset_id: int) -> None:
        """Play the Music Assistant media configured for the given preset button."""
        if preset_id not in PRESET_IDS:
            return
        # preset buttons are mapped once on the provider, shared by all its speakers
        media_id = cast("BoseSoundTouchProvider", self.provider).get_preset_media(preset_id)
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
        self._attr_volume_level = volume.actual_volume
        self._attr_volume_muted = volume.mute_enabled

    async def _refresh_now_playing(self) -> None:
        """Refresh playback state from the speaker."""
        with contextlib.suppress(ApiError):
            self._update_state_from_now_playing(await self._client.get_now_playing())

    async def _refresh_options(self) -> None:
        """Refresh available player options."""
        if not self._supported_player_options:
            self._supported_player_options.add(PlayerOptionKeys.NETWORK_NAME)
            bass_capability = await self._client.get_bass_capabilities()
            if bass_capability.available is not None and bass_capability.available:
                self._supported_player_options.add(PlayerOptionKeys.BASS)
        _updated_options: list[PlayerOption] = []
        if PlayerOptionKeys.NETWORK_NAME in self._supported_player_options:
            info = await self._client.get_info()
            _updated_options.append(
                PlayerOption(
                    key=PlayerOptionKeys.NETWORK_NAME,
                    translation_key=PlayerOptionKeys.NETWORK_NAME,
                    name=PlayerOptionKeys.NETWORK_NAME,
                    type=PlayerOptionType.STRING,
                    value=info.name,
                    read_only=True,  # TODO: make that non-read-only
                )
            )
        if PlayerOptionKeys.BASS in self._supported_player_options:
            old_option: PlayerOption | None = None
            for option in self._attr_options:
                if option.key == PlayerOptionKeys.BASS:
                    old_option = option
                    break
            if not old_option:
                bass_capability = await self._client.get_bass_capabilities()
                assert bass_capability.available is not None
                assert bass_capability.minimum is not None
                assert bass_capability.maximum is not None
                bass_min = bass_capability.minimum
                bass_max = bass_capability.maximum
            else:
                assert old_option.min_value is not None
                assert old_option.max_value is not None
                bass_min = int(old_option.min_value)
                bass_max = int(old_option.max_value)
            bass = await self._client.get_bass()
            assert bass.actual_bass is not None
            _updated_options.append(
                PlayerOption(
                    key=PlayerOptionKeys.BASS,
                    translation_key=PlayerOptionKeys.BASS,
                    name=PlayerOptionKeys.BASS,
                    type=PlayerOptionType.INTEGER,
                    value=bass.actual_bass,
                    min_value=bass_min,
                    max_value=bass_max,
                    step=1,
                )
            )

        self._attr_options = _updated_options

    async def _refresh_sources(self) -> None:
        """Refresh the list of selectable native sources from the speaker."""
        try:
            sources = await self._client.get_sources()
        except (aiohttp.ClientError, TimeoutError, OSError) as err:
            self.logger.debug("Failed to fetch sources for %s: %s", self.name, err)
            return
        self._attr_source_list = []
        for source in sources.sources:
            if source.source and source.source_name and source.status == SourceStatus.READY:
                self._attr_source_list.append(
                    PlayerSource(
                        id=source_id(source.source, source.source_account),
                        name=source.source_name,
                        passive=False,
                        can_play_pause=True,
                        can_seek=False,
                        can_next_previous=True,
                    )
                )

    async def _refresh_zone(self) -> None:
        """Refresh multiroom zone (group) membership from the speaker."""
        zone = await self._client.get_zone()
        if zone.leader and zone.leader.mac == self._device_id and zone.members:
            members = [self.player_id]
            members.extend(
                f"{PLAYER_ID_PREFIX}{member_id}"
                for member_id in [x.mac for x in zone.members]
                if member_id != self._device_id
            )
            self._attr_group_members = members
        else:
            self._attr_group_members = []

    def _update_state_from_now_playing(self, now_playing: NowPlaying) -> None:
        """Update player state from a now_playing snapshot."""
        if now_playing.content_item:
            self._attr_powered = now_playing.content_item.source != SOURCE_STANDBY
        if not self._attr_powered:
            self._attr_playback_state = PlaybackState.IDLE
            self._attr_active_source = None
            self._attr_current_media = None
            return

        if now_playing.play_status in [PlayStatus.PLAY_STATE, PlayStatus.BUFFERING_STATE]:
            self._attr_playback_state = PlaybackState.PLAYING
        elif now_playing.play_status == PlayStatus.PAUSE_STATE:
            self._attr_playback_state = PlaybackState.PAUSED
        else:
            self._attr_playback_state = PlaybackState.IDLE

        if now_playing.time_information and now_playing.time_information.position is not None:
            self._attr_elapsed_time = float(now_playing.time_information.position)
            self._attr_elapsed_time_last_updated = time.time()

        active_queue = self.mass.player_queues.get(self.player_id)
        if (
            active_queue
            and active_queue.current_item
            and now_playing.content_item
            and now_playing.content_item.source == "UPNP"
        ):
            # Music Assistant is the active source; audio is rendered via the linked
            # protocol and Music Assistant owns the metadata, so don't override it here.
            self._attr_active_source = self.player_id
        elif now_playing.content_item and now_playing.content_item.source:
            # a native source (Bluetooth, AUX, Spotify, ...) is playing on the speaker
            if now_playing.content_item.source != SOURCE_INVALID:
                # SOURCE_INVALID is some API flakiness
                self._attr_active_source = source_id(
                    now_playing.content_item.source, now_playing.content_item.source_account
                )
            if (
                now_playing.track
                or now_playing.artist
                or now_playing.album
                or now_playing.content_item.item_name
            ):
                image_url = now_playing.art.url if now_playing.art is not None else None
                duration = (
                    now_playing.time_information.total
                    if now_playing.time_information is not None
                    else None
                )
                self._attr_current_media = PlayerMedia(
                    uri=f"soundtouch://{now_playing.content_item.source}",
                    media_type=MediaType.UNKNOWN,
                    title=now_playing.track or now_playing.content_item.item_name,
                    artist=now_playing.artist,
                    album=now_playing.album,
                    image_url=image_url,
                    duration=duration,
                    source_id=self._attr_active_source,
                )
            else:
                self._attr_current_media = None
