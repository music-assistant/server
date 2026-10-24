"""Sony SongPal player."""

from __future__ import annotations

import asyncio
import ipaddress
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import (
    ConfigEntryType,
    IdentifierType,
    MediaType,
    PlaybackState,
    PlayerFeature,
    PlayerType,
)
from music_assistant_models.player import OutputProtocol, PlayerSoundMode, PlayerSource
from music_assistant_models.unique_list import UniqueList
from songpal import (
    ConnectChange,
    ContentChange,
    Device,
    PowerChange,
    SettingChange,
    SongpalException,
    VolumeChange,
)
from songpal.containers import Input, PlayInfo, Volume

from music_assistant.helpers.util import is_valid_mac_address
from music_assistant.models.player import Player

from .avtransport import (
    AvTransportError,
    pause_body,
    play_body,
    post_avtransport,
    set_av_transport_uri_body,
    stop_body,
)
from .constants import (
    CONF_PLAYBACK_INPUT,
    CONNECT_TIMEOUT,
    INITIAL_RETRY_DELAY,
    MANUFACTURER,
    MAX_RETRY_DELAY,
)
from .util import (
    device_volume_from_level,
    is_main_zone,
    is_power_retry,
    level_from_device_volume,
    normalize_mac,
    playback_state_name,
    replace_url_host,
    select_main_zone,
)

if TYPE_CHECKING:
    from music_assistant_models.player import PlayerMedia

    from .provider import SongpalPlayerProvider


class SongpalPlayer(Player):
    """Sony soundbar or receiver controlled with python-songpal and AVTransport."""

    def __init__(
        self,
        provider: SongpalPlayerProvider,
        player_id: str,
        device: Device,
        name: str,
        *,
        endpoint: str,
        control_url: str | None,
        ip_address: str | None,
    ) -> None:
        """Initialize the player."""
        super().__init__(provider, player_id)
        self._device = device
        self._endpoint = endpoint
        self._control_url = control_url
        self._ip_address = ip_address
        self._attr_name = name
        self._attr_type = PlayerType.PLAYER
        self._volume_control: Volume | None = None
        self._volume_max = 1
        self._inputs: dict[str, Input] = {}
        self._sound_modes: dict[str, Any] = {}
        self._active_sound_value: str | None = None
        self._sysinfo: Any = None
        self._model: str | None = None
        self._socket_up = False
        self._closing = False
        self._listen_task: asyncio.Task[None] | None = None
        self._listen_tasks: set[asyncio.Task[None]] = set()
        self.logger = provider.logger.getChild(player_id)
        self._apply_features()

    @property
    def needs_poll(self) -> bool:
        """Poll only to refresh state after the notification socket drops."""
        return not self._socket_up

    @property
    def poll_interval(self) -> int:
        """Slow poll used while the WebSocket is down."""
        return 30

    @property
    def sound_mode_list(self) -> UniqueList[PlayerSoundMode]:
        """Return sound-field modes. Empty when the active mode is missing."""
        if not self._active_sound_value:
            return UniqueList()
        return UniqueList(self._attr_sound_mode_list)

    async def get_config_entries(self) -> list[ConfigEntry]:
        """Return the playback-input option used when a linked protocol plays."""
        options = [
            ConfigValueOption(source.uri, title=source.title or source.uri)
            for source in self._inputs.values()
        ]
        return [
            ConfigEntry(
                key=CONF_PLAYBACK_INPUT,
                type=ConfigEntryType.STRING,
                label="Playback input",
                required=False,
                default_value="",
                options=options,
                description=(
                    "Input to activate when AirPlay, Chromecast, or DLNA playback starts. "
                    "Music Assistant's own queue uses DLNA AVTransport instead."
                ),
            ),
        ]

    async def power(self, powered: bool) -> None:
        """Power the device on or off."""
        try:
            await self._device.set_power(powered)
        except SongpalException as err:
            if is_power_retry(err):
                self.logger.debug(
                    "Ignoring power error %s; the device is already %s",
                    err.code,
                    "on" if powered else "off",
                )
            else:
                raise
        self._attr_powered = powered
        self.update_state()

    async def volume_set(self, volume_level: int) -> None:
        """Set the first volume controller, scaled with maxVolume."""
        if self._volume_control is None:
            return
        device_volume = device_volume_from_level(volume_level, self._volume_max)
        self.logger.debug("Setting volume to %s (level %s)", device_volume, volume_level)
        await self._volume_control.set_volume(device_volume)
        self._attr_volume_level = volume_level
        self.update_state()

    async def volume_mute(self, muted: bool) -> None:
        """Mute or unmute the first volume controller."""
        if self._volume_control is None:
            return
        await self._volume_control.set_mute(muted)
        self._attr_volume_muted = muted
        self.update_state()

    async def select_source(self, source: str) -> None:
        """Activate an input by URI, or by title when that is what was stored."""
        chosen = self._inputs.get(source)
        if chosen is None:
            chosen = next((item for item in self._inputs.values() if item.title == source), None)
        if chosen is None:
            self.logger.error("Unable to find source %s", source)
            return
        await chosen.activate()
        self._attr_active_source = chosen.uri
        self.update_state()

    async def select_sound_mode(self, sound_mode: str) -> None:
        """Set the soundField value."""
        candidate = self._sound_modes.get(sound_mode)
        value = sound_mode
        if candidate is None:
            for mode in self._sound_modes.values():
                if getattr(mode, "title", None) == sound_mode:
                    value = mode.value
                    break
            else:
                self.logger.error("Unable to find sound mode %s", sound_mode)
                return
        else:
            value = candidate.value
        await self._device.set_sound_settings("soundField", value)
        self._active_sound_value = value
        self._attr_active_sound_mode = value
        self.update_state()

    async def play(self) -> None:
        """Resume playback. AVTransport Play is also how a pause is resumed."""
        if not await self._transport("Play", play_body()):
            return
        self._attr_playback_state = PlaybackState.PLAYING
        self.update_state()

    async def pause(self) -> None:
        """Pause AVTransport playback."""
        if not await self._transport("Pause", pause_body()):
            return
        self._attr_playback_state = PlaybackState.PAUSED
        self.update_state()

    async def stop(self) -> None:
        """Stop AVTransport playback."""
        if not await self._transport("Stop", stop_body()):
            return
        self._attr_playback_state = PlaybackState.IDLE
        self._attr_elapsed_time = 0
        self._attr_elapsed_time_last_updated = time.time()
        self.update_state()

    async def play_media(self, media: PlayerMedia) -> None:
        """Power on, then play the Music Assistant stream over AVTransport."""
        if not self._control_url:
            self.logger.error("Player %s has no AVTransport control URL", self.display_name)
            return
        if self._attr_powered is not True:
            await self.power(True)
        stream_url = await self._stream_url(media)
        title = media.title or "Music Assistant"
        self.logger.debug("Playing %s on %s", stream_url, self._control_url)
        if not await self._transport(
            "SetAVTransportURI", set_av_transport_uri_body(stream_url, title)
        ):
            return
        if not await self._transport("Play", play_body()):
            return
        self.set_current_media(
            uri=media.uri,
            media_type=media.media_type or MediaType.TRACK,
            title=title,
            artist=media.artist,
            album=media.album,
            image_url=media.image_url,
            duration=media.duration,
            queue_item_id=media.queue_item_id,
        )
        self._attr_playback_state = PlaybackState.PLAYING
        self._attr_elapsed_time = 0
        self._attr_elapsed_time_last_updated = time.time()
        self.update_state()

    async def on_protocol_playback(self, output_protocol: OutputProtocol) -> None:
        """Power on and select the configured input when a linked protocol plays."""
        self.logger.debug("Protocol playback started: %s", output_protocol)
        if self._attr_powered is not True:
            await self.power(True)
        source = self.config.get_value(CONF_PLAYBACK_INPUT)
        if source:
            await self.select_source(str(source))

    async def poll(self) -> None:
        """Refresh state, and restart notifications if the listener has exited."""
        if self._socket_up or self._closing:
            return
        if self._listen_task is None or self._listen_task.done():
            await self.start_listening()
            return
        await self.refresh()
        self.update_state()

    async def prepare(self) -> None:
        """Load device state before the player is registered."""
        await self.refresh()

    async def refresh(self) -> None:
        """Load power, volume, inputs, sound field, and playback state."""
        try:
            if self._sysinfo is None:
                self._sysinfo = await self._device.get_system_info()
            if self._model is None:
                interface_info = await self._device.get_interface_information()
                self._model = interface_info.modelName
            volumes = await self._device.get_volume_information()
            if not volumes:
                self.logger.error("Got no volume controls from %s", self._endpoint)
                self._attr_available = False
                return
            volume = select_main_zone(volumes) or volumes[0]
            if len(volumes) > 1:
                self.logger.debug("Got %s volume controls, using %s", len(volumes), volume)
            self._volume_max = int(volume.maxVolume or 0)
            self._volume_control = volume
            self._attr_volume_level = level_from_device_volume(
                int(volume.volume or 0), self._volume_max
            )
            self._attr_volume_muted = volume.is_muted

            status = await self._device.get_power()
            self._attr_powered = bool(status)

            inputs = await self._device.get_inputs()
            self._inputs = {}
            active_source = None
            for item in inputs:
                if not item.uri:
                    continue
                self._inputs[item.uri] = item
                if item.active:
                    active_source = item.uri
            self._attr_source_list = [
                PlayerSource(id=item.uri, name=item.title or item.uri, passive=False)
                for item in self._inputs.values()
            ]
            self._attr_active_source = active_source
            await self._load_sound_modes()
            try:
                play_infos = await self._device.get_play_info()
            except SongpalException as err:
                self.logger.debug("Playing content is unavailable: %s", err)
            else:
                if play_info := select_main_zone(play_infos):
                    self._apply_play_info(play_info)
            self._apply_device_info()
            self._attr_available = True
        except SongpalException as err:
            self.logger.error("Unable to update %s: %s", self._endpoint, err)
            self._attr_available = False
        finally:
            self._apply_features()

    async def start_listening(self) -> None:
        """Subscribe to the avContent WebSocket and the other SongPal notifications."""
        if self._closing:
            return
        if self._listen_task and not self._listen_task.done():
            return
        self._spawn_listener()

    def _spawn_listener(self) -> None:
        task = self.mass.create_task(self._listen())
        self._listen_task = task
        self._listen_tasks.add(task)
        task.add_done_callback(self._listen_tasks.discard)

    async def close(self) -> None:
        """Stop the notification listener."""
        self._closing = True
        self._socket_up = False
        tasks = [task for task in self._listen_tasks if not task.done()]
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._listen_task = None
        try:
            await self._device.stop_listen_notifications()
        except Exception as err:  # noqa: BLE001 - unload must finish
            self.logger.debug("Stopping notifications failed: %s", err)
        self._device.clear_notification_callbacks()

    async def update_connection(
        self,
        *,
        endpoint: str,
        control_url: str | None,
        name: str | None,
        ip_address: str | None,
    ) -> None:
        """Apply a new endpoint or AVTransport URL from a later discovery.

        An unchanged endpoint is not fully refreshed. ``get_play_info()`` often
        has no title while AVTransport is playing, and that report would mark
        the player idle.
        """
        endpoint_changed = endpoint != self._endpoint
        self._control_url = control_url
        self._ip_address = ip_address
        if name:
            self._attr_name = name
        if not endpoint_changed:
            self._apply_device_info()
            self._apply_features()
            self.update_state()
            return
        await self.close()
        self._closing = False
        self._device = Device(endpoint)
        self._endpoint = endpoint
        self._sysinfo = None
        self._model = None
        try:
            async with asyncio.timeout(CONNECT_TIMEOUT):
                await self._device.get_supported_methods()
        except (SongpalException, TimeoutError) as err:
            self.logger.warning("Cannot connect to %s: %s", endpoint, err)
            self._attr_available = False
            self.update_state()
            return
        await self.refresh()
        self.update_state()
        await self.start_listening()

    async def _listen(self) -> None:
        self._bind_notifications()
        self._socket_up = True
        try:
            await self._device.listen_notifications()
        finally:
            # A reconnect starts a new listener before this one returns.
            if asyncio.current_task() is self._listen_task:
                self._socket_up = False

    def _bind_notifications(self) -> None:
        self._device.clear_notification_callbacks()
        self._device.on_notification(VolumeChange, self._on_volume)
        self._device.on_notification(PowerChange, self._on_power)
        self._device.on_notification(ContentChange, self._on_content)
        self._device.on_notification(SettingChange, self._on_setting)
        self._device.on_notification(ConnectChange, self._on_disconnect)

    async def _on_volume(self, volume: VolumeChange) -> None:
        if self._volume_control is not None and volume.output != self._volume_control.output:
            return
        self._attr_volume_level = level_from_device_volume(
            int(volume.volume or 0), self._volume_max
        )
        self._attr_volume_muted = bool(volume.mute)
        self.update_state()

    async def _on_power(self, power: PowerChange) -> None:
        self._attr_powered = bool(power.status)
        self.update_state()

    async def _on_content(self, content: ContentChange) -> None:
        if not is_main_zone(content.output):
            return
        if content.is_input:
            if content.uri:
                self._attr_active_source = content.uri
            self.update_state()
            return
        self._apply_play_info(content)
        self.update_state()

    async def _on_setting(self, setting: SettingChange) -> None:
        if setting.target != "soundField":
            return
        self._active_sound_value = setting.currentValue or None
        self._attr_active_sound_mode = self._active_sound_value
        self._apply_features()
        self.update_state()

    async def _on_disconnect(self, connect: ConnectChange) -> None:
        """Mark the player unavailable and retry until the device answers."""
        if self._closing:
            return
        self._socket_up = False
        self._attr_available = False
        self.update_state()
        self.logger.warning(
            "SongPal disconnected from %s: %s", self._endpoint, connect.exception
        )
        delay = INITIAL_RETRY_DELAY
        while not self._closing:
            await asyncio.sleep(delay)
            if self._closing:
                return
            try:
                async with asyncio.timeout(CONNECT_TIMEOUT):
                    await self._device.get_supported_methods()
                    await self.refresh()
                    if not self._attr_available:
                        raise SongpalException("device is still unavailable")
            except (SongpalException, TimeoutError) as err:
                self.logger.debug("Reconnect to %s failed: %s", self._endpoint, err)
                delay = min(delay * 2, MAX_RETRY_DELAY)
                continue
            self.update_state()
            break
        if self._closing:
            return
        self.logger.warning("Connection reestablished to %s", self._endpoint)
        self._spawn_listener()

    async def _load_sound_modes(self) -> None:
        settings = await self._device.get_sound_settings()
        sound_field = next((item for item in settings if item.target == "soundField"), None)
        self._sound_modes = {}
        self._active_sound_value = None
        modes: list[PlayerSoundMode] = []
        if sound_field is None:
            self._attr_sound_mode_list = []
            self._attr_active_sound_mode = None
            return
        current = sound_field.currentValue
        for candidate in sound_field.candidate or []:
            if not candidate.isAvailable:
                continue
            self._sound_modes[candidate.value] = candidate
            modes.append(
                PlayerSoundMode(
                    id=candidate.value, name=candidate.title or candidate.value
                )
            )
            if candidate.value == current:
                self._active_sound_value = candidate.value
        self._attr_sound_mode_list = modes
        self._attr_active_sound_mode = self._active_sound_value

    def _apply_play_info(self, info: PlayInfo) -> None:
        state_name = playback_state_name(info.state, is_idle=bool(info.is_idle))
        if state_name == "playing":
            self._attr_playback_state = PlaybackState.PLAYING
        elif state_name == "paused":
            self._attr_playback_state = PlaybackState.PAUSED
        elif state_name == "idle":
            self._attr_playback_state = PlaybackState.IDLE
        uri = info.uri or self._endpoint
        self.set_current_media(
            uri=uri,
            media_type=MediaType.TRACK,
            title=info.title,
            artist=info.artist,
            album=info.albumName,
        )
        if self._attr_current_media is not None:
            self._attr_current_media.artist = info.artist
            self._attr_current_media.album = info.albumName
            if info.durationMsec:
                self._attr_current_media.duration = int(info.durationMsec / 1000)
        if info.positionMsec is not None:
            self._attr_elapsed_time = info.positionMsec / 1000
            self._attr_elapsed_time_last_updated = time.time()

    def _apply_device_info(self) -> None:
        """Publish the MAC and IP so protocol players can link to this device.

        ``DeviceInfo`` keeps a single MAC address. The player id is ``macAddr``
        when it is valid, otherwise ``wirelessMacAddr``, matching Home
        Assistant's unique id.
        """
        info = self._attr_device_info
        info.manufacturer = MANUFACTURER
        info.model = self._model
        info.software_version = getattr(self._sysinfo, "version", None)
        wired = normalize_mac(getattr(self._sysinfo, "macAddr", None))
        wireless = normalize_mac(getattr(self._sysinfo, "wirelessMacAddr", None))
        valid_macs = [mac for mac in (wired, wireless) if mac and is_valid_mac_address(mac)]
        # DeviceInfo keeps one MAC. The player id is macAddr, then wirelessMacAddr.
        chosen = next((mac for mac in valid_macs if mac == self.player_id), None)
        if chosen:
            info.add_identifier(IdentifierType.MAC_ADDRESS, chosen)
        if self._ip_address:
            info.add_identifier(IdentifierType.IP_ADDRESS, self._ip_address)

    def _apply_features(self) -> None:
        features = {
            PlayerFeature.POWER,
            PlayerFeature.VOLUME_SET,
            PlayerFeature.VOLUME_MUTE,
            PlayerFeature.SELECT_SOURCE,
        }
        if self._control_url:
            features.add(PlayerFeature.PLAY_MEDIA)
            features.add(PlayerFeature.PAUSE)
        if self._active_sound_value:
            features.add(PlayerFeature.SELECT_SOUND_MODE)
        self._attr_supported_features = features

    async def _stream_url(self, media: PlayerMedia) -> str:
        url = await self.mass.streams.resolve_stream_url(self.player_id, media)
        host = _hostname(url)
        if not host or _is_ip(host):
            return url
        try:
            infos = await asyncio.getaddrinfo(host, None)
        except OSError as err:
            raise AvTransportError(f"Cannot resolve stream host {host}") from err
        if not infos:
            raise AvTransportError(f"Cannot resolve stream host {host}")
        return replace_url_host(url, infos[0][4][0])

    async def _transport(self, action: str, body: str) -> bool:
        if not self._control_url:
            self.logger.debug("Skipping %s; AVTransport is unavailable", action)
            return False
        try:
            await post_avtransport(
                self.mass.http_session_no_ssl, self._control_url, action, body
            )
        except AvTransportError:
            self.logger.exception("AVTransport %s failed on %s", action, self.display_name)
            raise
        return True


def _hostname(url: str) -> str | None:
    return urlsplit(url).hostname


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True
