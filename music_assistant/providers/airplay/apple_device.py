"""Apple device implementation for the AirPlay provider."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable
from ipaddress import AddressValueError, IPv4Address
from typing import TYPE_CHECKING, Final

import pyatv
from music_assistant_models.config_entries import ConfigEntry, ConfigValueType
from music_assistant_models.enums import (
    ConfigEntryType,
    MediaType,
    PlaybackState,
    PlayerFeature,
    PlayerType,
)
from music_assistant_models.errors import PlayerCommandFailed
from music_assistant_models.player import PlayerSource
from pyatv import exceptions as pyatv_exceptions
from pyatv.conf import AppleTV as AppleTVConfig
from pyatv.conf import ManualService
from pyatv.const import (
    DeviceState,
    FeatureName,
    FeatureState,
    PairingRequirement,
    PowerState,
    Protocol,
)
from pyatv.const import (
    MediaType as PyatvMediaType,
)
from pyatv.interface import (
    AppleTV,
    AudioListener,
    DeviceListener,
    OutputDevice,
    PairingHandler,
    Playing,
    PowerListener,
    PushListener,
)

from music_assistant.models.player import PlayerMedia

from .constants import (
    CONF_ACTION_FINISH_COMPANION_PAIRING,
    CONF_ACTION_FINISH_MRP_PAIRING,
    CONF_ACTION_RESET_COMPANION_PAIRING,
    CONF_ACTION_RESET_MRP_PAIRING,
    CONF_ACTION_START_COMPANION_PAIRING,
    CONF_ACTION_START_MRP_PAIRING,
    CONF_COMPANION_CREDENTIALS,
    CONF_COMPANION_PAIRING_PIN,
    CONF_MRP_CREDENTIALS,
    CONF_MRP_PAIRING_PIN,
    CONF_STORED_VOLUME,
)
from .player import AirPlayPlayer

if TYPE_CHECKING:
    from zeroconf.asyncio import AsyncServiceInfo

    from .provider import AirPlayProvider

_COMPANION_PAIRING_DISABLED = 0x04
_COMPANION_PAIRING_WITH_PIN = 0x4000
_CONTROL_RECONNECT_DELAY: Final[float] = 30.0
_WAKE_TIMEOUT: Final[float] = 10.0

_CONNECTION_ERRORS = (
    pyatv_exceptions.AuthenticationError,
    pyatv_exceptions.BackOffError,
    pyatv_exceptions.ConnectionFailedError,
    pyatv_exceptions.DeviceIdMissingError,
    pyatv_exceptions.InvalidConfigError,
    pyatv_exceptions.InvalidCredentialsError,
    pyatv_exceptions.InvalidResponseError,
    pyatv_exceptions.NoCredentialsError,
    pyatv_exceptions.NoServiceError,
    pyatv_exceptions.OperationTimeoutError,
    pyatv_exceptions.ProtocolError,
    OSError,
    TimeoutError,
    ValueError,
)
_COMMAND_ERRORS = (
    pyatv_exceptions.AuthenticationError,
    pyatv_exceptions.BlockedStateError,
    pyatv_exceptions.CommandError,
    pyatv_exceptions.ConnectionLostError,
    pyatv_exceptions.InvalidStateError,
    pyatv_exceptions.NotSupportedError,
    pyatv_exceptions.OperationTimeoutError,
    pyatv_exceptions.ProtocolError,
    OSError,
    TimeoutError,
)


class AppleDevicePlayer(AirPlayPlayer):
    """AirPlay player with native Apple device monitoring and control."""

    _attr_type = PlayerType.PLAYER

    def __init__(
        self,
        provider: AirPlayProvider,
        player_id: str,
        raop_discovery_info: AsyncServiceInfo | None,
        airplay_discovery_info: AsyncServiceInfo | None,
        companion_discovery_info: AsyncServiceInfo | None,
        address: str,
        display_name: str,
        manufacturer: str,
        model: str,
        initial_volume: int,
    ) -> None:
        """Initialize an Apple device player."""
        self.companion_discovery_info = companion_discovery_info
        self._companion_device: AppleTV | None = None
        self._mrp_device: AppleTV | None = None
        super().__init__(
            provider=provider,
            player_id=player_id,
            raop_discovery_info=raop_discovery_info,
            airplay_discovery_info=airplay_discovery_info,
            address=address,
            display_name=display_name,
            manufacturer=manufacturer,
            model=model,
            initial_volume=initial_volume,
        )
        self._companion_listener: _AppleStateListener | None = None
        self._mrp_state_listener: _AppleStateListener | None = None
        self._mrp_push_listener: _ApplePushListener | None = None
        self._active_companion_pairing: PairingHandler | None = None
        self._active_mrp_pairing: PairingHandler | None = None
        self._connection_task: asyncio.Task[None] | None = None
        self._connection_lock = asyncio.Lock()
        self._power_on_event = asyncio.Event()
        self._disconnecting = False
        self._restart_connections = False
        self._unloading = False

    @property
    def companion_pairing_supported(self) -> bool:
        """Return whether this device advertises Companion PIN pairing."""
        info = self.companion_discovery_info
        if info is None:
            return False
        raw_flags = info.decoded_properties.get("rpfl", "0x0")
        if raw_flags is None:
            return False
        try:
            flags = int(raw_flags, 16)
        except TypeError, ValueError:
            return False
        return bool(flags & _COMPANION_PAIRING_WITH_PIN) and not bool(
            flags & _COMPANION_PAIRING_DISABLED
        )

    @property
    def needs_setup(self) -> bool:
        """Return whether streaming or Apple device control still requires pairing."""
        return (
            super().needs_setup
            or (
                self.companion_pairing_supported
                and not self.config.get_value(CONF_COMPANION_CREDENTIALS)
            )
            or (self.mrp_pairing_supported and not self.config.get_value(CONF_MRP_CREDENTIALS))
        )

    @property
    def mrp_pairing_supported(self) -> bool:
        """Return whether AirPlay playback monitoring can be paired."""
        return bool(
            not self._is_homepod
            and self.airplay_discovery_info
            and self._is_airplay2_capable
            and self.airplay_discovery_info.decoded_properties.get("acl", "0") != "1"
        )

    @property
    def supported_features(self) -> set[PlayerFeature]:
        """Return the supported features of this Apple device."""
        features = {*super().supported_features}
        if self._device_for_feature(FeatureName.Next) or self._device_for_feature(
            FeatureName.Previous
        ):
            features.add(PlayerFeature.NEXT_PREVIOUS)
        if self.companion_pairing_supported or self.config.get_value(CONF_COMPANION_CREDENTIALS):
            features.add(PlayerFeature.POWER)
        return features

    async def get_config_entries(
        self,
        action: str | None = None,
        values: dict[str, ConfigValueType] | None = None,
    ) -> list[ConfigEntry]:
        """Return player configuration entries, including Apple device pairing."""
        entries = await super().get_config_entries(action, values)
        if action in {
            CONF_ACTION_START_COMPANION_PAIRING,
            CONF_ACTION_FINISH_COMPANION_PAIRING,
            CONF_ACTION_RESET_COMPANION_PAIRING,
        }:
            await self._handle_companion_pairing_action(action, values)
        elif action in {
            CONF_ACTION_START_MRP_PAIRING,
            CONF_ACTION_FINISH_MRP_PAIRING,
            CONF_ACTION_RESET_MRP_PAIRING,
        }:
            await self._handle_mrp_pairing_action(action, values)

        credentials = self.config.get_value(CONF_COMPANION_CREDENTIALS)
        if values is not None:
            credentials = values.get(CONF_COMPANION_CREDENTIALS, credentials)
        if credentials:
            entries.extend(
                [
                    ConfigEntry(
                        key="companion_pairing_status",
                        type=ConfigEntryType.LABEL,
                        category="protocol_generic",
                    ),
                    ConfigEntry(
                        key=CONF_ACTION_RESET_COMPANION_PAIRING,
                        type=ConfigEntryType.ACTION,
                        action=CONF_ACTION_RESET_COMPANION_PAIRING,
                        category="protocol_generic",
                    ),
                ]
            )
        elif self.companion_pairing_supported:
            if self._active_companion_pairing:
                entries.extend(
                    [
                        ConfigEntry(
                            key=CONF_COMPANION_PAIRING_PIN,
                            type=ConfigEntryType.STRING,
                            required=True,
                            category="protocol_generic",
                        ),
                        ConfigEntry(
                            key=CONF_ACTION_FINISH_COMPANION_PAIRING,
                            type=ConfigEntryType.ACTION,
                            action=CONF_ACTION_FINISH_COMPANION_PAIRING,
                            category="protocol_generic",
                        ),
                    ]
                )
            else:
                entries.extend(
                    [
                        ConfigEntry(
                            key="companion_pairing_instructions",
                            type=ConfigEntryType.LABEL,
                            category="protocol_generic",
                        ),
                        ConfigEntry(
                            key=CONF_ACTION_START_COMPANION_PAIRING,
                            type=ConfigEntryType.ACTION,
                            action=CONF_ACTION_START_COMPANION_PAIRING,
                            category="protocol_generic",
                        ),
                    ]
                )

        entries.append(
            ConfigEntry(
                key=CONF_COMPANION_CREDENTIALS,
                type=ConfigEntryType.SECURE_STRING,
                default_value=None,
                value=credentials,
                required=False,
                hidden=True,
                category="protocol_generic",
            )
        )

        mrp_credentials = self.config.get_value(CONF_MRP_CREDENTIALS)
        if values is not None:
            mrp_credentials = values.get(CONF_MRP_CREDENTIALS, mrp_credentials)
        if mrp_credentials:
            entries.extend(
                [
                    ConfigEntry(
                        key="mrp_pairing_status",
                        type=ConfigEntryType.LABEL,
                        category="protocol_generic",
                    ),
                    ConfigEntry(
                        key=CONF_ACTION_RESET_MRP_PAIRING,
                        type=ConfigEntryType.ACTION,
                        action=CONF_ACTION_RESET_MRP_PAIRING,
                        category="protocol_generic",
                    ),
                ]
            )
        elif self.mrp_pairing_supported:
            if self._active_mrp_pairing:
                entries.extend(
                    [
                        ConfigEntry(
                            key=CONF_MRP_PAIRING_PIN,
                            type=ConfigEntryType.STRING,
                            required=True,
                            category="protocol_generic",
                        ),
                        ConfigEntry(
                            key=CONF_ACTION_FINISH_MRP_PAIRING,
                            type=ConfigEntryType.ACTION,
                            action=CONF_ACTION_FINISH_MRP_PAIRING,
                            category="protocol_generic",
                        ),
                    ]
                )
            else:
                entries.extend(
                    [
                        ConfigEntry(
                            key="mrp_pairing_instructions",
                            type=ConfigEntryType.LABEL,
                            category="protocol_generic",
                        ),
                        ConfigEntry(
                            key=CONF_ACTION_START_MRP_PAIRING,
                            type=ConfigEntryType.ACTION,
                            action=CONF_ACTION_START_MRP_PAIRING,
                            category="protocol_generic",
                        ),
                    ]
                )
        entries.append(
            ConfigEntry(
                key=CONF_MRP_CREDENTIALS,
                type=ConfigEntryType.SECURE_STRING,
                default_value=None,
                value=mrp_credentials,
                required=False,
                hidden=True,
                category="protocol_generic",
            )
        )
        return entries

    async def power(self, powered: bool) -> None:
        """Turn the Apple device on or off."""
        feature = FeatureName.TurnOn if powered else FeatureName.TurnOff
        device = self._device_for_feature(feature)
        if device is None:
            raise PlayerCommandFailed(f"Power control is unavailable for {self.display_name}")
        if powered:
            self._power_on_event.clear()
            await self._run_control_command(device.power.turn_on(), "turn on")
            await self._wait_for_wake()
        else:
            await self._run_control_command(device.power.turn_off(), "turn off")

    async def play(self) -> None:
        """Resume Music Assistant or external playback."""
        await self._wake_for_playback()
        if self._stream_active:
            await super().play()
            return
        device = self._device_for_feature(FeatureName.Play)
        if device is None:
            device = self._device_for_feature(FeatureName.PlayPause)
            if device is None:
                raise PlayerCommandFailed(f"Play control is unavailable for {self.display_name}")
            await self._run_control_command(device.remote_control.play_pause(), "play")
            return
        await self._run_control_command(device.remote_control.play(), "play")

    async def pause(self) -> None:
        """Pause Music Assistant or external playback."""
        if self._stream_active:
            await super().pause()
            return
        device = self._device_for_feature(FeatureName.Pause)
        if device is None:
            device = self._device_for_feature(FeatureName.PlayPause)
            if device is None:
                raise PlayerCommandFailed(f"Pause control is unavailable for {self.display_name}")
            await self._run_control_command(device.remote_control.play_pause(), "pause")
            return
        await self._run_control_command(device.remote_control.pause(), "pause")

    async def stop(self) -> None:
        """Stop Music Assistant or external playback."""
        if self._stream_active:
            await super().stop()
            return
        device = self._device_for_feature(FeatureName.Stop)
        if device is not None:
            await self._run_control_command(device.remote_control.stop(), "stop")
            return
        device = self._device_for_feature(FeatureName.Pause)
        if device is not None:
            await self._run_control_command(device.remote_control.pause(), "stop")
            return
        raise PlayerCommandFailed(f"Stop control is unavailable for {self.display_name}")

    async def play_media(self, media: PlayerMedia) -> None:
        """Wake the Apple device and start Music Assistant playback."""
        await self._wake_for_playback()
        await super().play_media(media)

    async def volume_set(self, volume_level: int) -> None:
        """Set stream or native Apple device volume."""
        if self._stream_active:
            await super().volume_set(volume_level)
            return
        device = self._device_for_feature(FeatureName.SetVolume)
        if device is None:
            await super().volume_set(volume_level)
            return
        await self._run_control_command(device.audio.set_volume(volume_level), "set volume")
        self._update_native_volume(volume_level)

    async def volume_mute(self, muted: bool) -> None:
        """Mute the active Music Assistant stream."""
        await super().volume_mute(muted)

    async def next_track(self) -> None:
        """Skip to the next item in external playback."""
        device = self._device_for_feature(FeatureName.Next)
        if device is None:
            raise PlayerCommandFailed(f"Next control is unavailable for {self.display_name}")
        await self._run_control_command(device.remote_control.next(), "skip to next")

    async def previous_track(self) -> None:
        """Return to the previous item in external playback."""
        device = self._device_for_feature(FeatureName.Previous)
        if device is None:
            raise PlayerCommandFailed(f"Previous control is unavailable for {self.display_name}")
        await self._run_control_command(device.remote_control.previous(), "skip to previous")

    def set_discovery_info(self, discovery_info: AsyncServiceInfo, display_name: str) -> None:
        """Update AirPlay discovery data and reconnect Apple control if needed."""
        previous_signature = self._service_signature(self.airplay_discovery_info)
        previous_address = self.address
        super().set_discovery_info(discovery_info, display_name)
        if (
            previous_signature != self._service_signature(self.airplay_discovery_info)
            or previous_address != self.address
        ):
            self._schedule_connection(force=True)

    async def set_companion_discovery_info(self, discovery_info: AsyncServiceInfo | None) -> None:
        """Update Companion discovery data and reconnect the control channel."""
        if self._service_signature(self.companion_discovery_info) == self._service_signature(
            discovery_info
        ):
            return
        self.companion_discovery_info = discovery_info
        self.update_state()
        self._schedule_connection(force=True)

    async def on_config_updated(self) -> None:
        """Reconnect Apple services when player configuration changes."""
        await super().on_config_updated()
        self._schedule_connection(force=True)

    async def on_unload(self) -> None:
        """Close Apple control connections and pairing resources."""
        self._unloading = True
        if self._connection_task and not self._connection_task.done():
            self._connection_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._connection_task
        await self._disconnect_apple_services()
        if self._active_companion_pairing:
            await self._active_companion_pairing.close()
            self._active_companion_pairing = None
        if self._active_mrp_pairing:
            await self._active_mrp_pairing.close()
            self._active_mrp_pairing = None
        await super().on_unload()

    def _schedule_connection(self, *, force: bool = False) -> None:
        """Start or restart the Apple service connection loop."""
        if self._unloading:
            return
        if self._connection_task and not self._connection_task.done():
            if not force:
                return
            self._connection_task.cancel()
        self._restart_connections = self._restart_connections or force
        self._connection_task = self.mass.create_task(
            self._connection_loop,
            task_id=f"airplay_apple_control_{self.player_id}",
            abort_existing=force,
        )

    async def _connection_loop(self) -> None:
        """Connect Apple control services and retry transient failures."""
        retry = True
        while retry and not self._unloading:
            retry = await self._connect_apple_services()
            if retry:
                await asyncio.sleep(_CONTROL_RECONNECT_DELAY)

    async def _connect_apple_services(self) -> bool:
        """Connect Companion and MRP independently."""
        async with self._connection_lock:
            if self._restart_connections:
                self._restart_connections = False
                await self._disconnect_apple_services()
            companion_retry, mrp_retry = await asyncio.gather(
                self._connect_companion(),
                self._connect_mrp(),
            )
            return companion_retry or mrp_retry

    async def _connect_companion(self) -> bool:
        """Connect the Companion control channel."""
        if self._companion_device is not None:
            return False
        credentials = self.config.get_value(CONF_COMPANION_CREDENTIALS)
        if not credentials or not self.companion_discovery_info:
            return False
        config = self._build_config(
            self.companion_discovery_info,
            Protocol.Companion,
            str(credentials),
            PairingRequirement.Mandatory,
        )
        if config is None:
            return False
        try:
            device = await pyatv.connect(config, self.mass.loop)
        except pyatv_exceptions.AuthenticationError, pyatv_exceptions.InvalidCredentialsError:
            self.logger.warning(
                "Stored Apple device control credentials are no longer valid for %s",
                self.display_name,
            )
            return False
        except _CONNECTION_ERRORS as err:
            self.logger.debug("Unable to connect Apple device control for %s: %s", self.name, err)
            return True

        self._companion_device = device
        listener = _AppleStateListener(self, device, "companion")
        self._companion_listener = listener
        device.listener = listener
        device.power.listener = listener
        device.audio.listener = listener
        self._apply_initial_device_state(device, "companion")
        self.update_state()
        self.logger.debug("Connected Companion control for %s", self.display_name)
        return False

    async def _connect_mrp(self) -> bool:
        """Connect the AirPlay MRP tunnel for external playback state."""
        if self._mrp_device is not None:
            return False
        if not self.airplay_discovery_info:
            return False
        credentials = self.config.get_value(CONF_MRP_CREDENTIALS)
        if credentials is None and not self._is_homepod:
            return False
        config = self._build_config(
            self.airplay_discovery_info,
            Protocol.AirPlay,
            str(credentials) if credentials else None,
            PairingRequirement.NotNeeded,
        )
        if config is None:
            return False
        try:
            device = await pyatv.connect(config, self.mass.loop)
        except (
            pyatv_exceptions.AuthenticationError,
            pyatv_exceptions.InvalidCredentialsError,
        ) as err:
            self.logger.warning(
                "Unable to authenticate Apple playback monitoring for %s: %s", self.name, err
            )
            return False
        except _CONNECTION_ERRORS as err:
            self.logger.debug(
                "Unable to connect Apple playback monitoring for %s: %s", self.name, err
            )
            return True

        if not self._feature_available(device, FeatureName.PushUpdates):
            device.close()
            self.logger.debug("Apple playback monitoring is not supported by %s", self.name)
            return False

        self._mrp_device = device
        state_listener = _AppleStateListener(self, device, "mrp")
        push_listener = _ApplePushListener(self, device)
        self._mrp_state_listener = state_listener
        self._mrp_push_listener = push_listener
        device.listener = state_listener
        device.power.listener = state_listener
        device.audio.listener = state_listener
        device.push_updater.listener = push_listener
        device.push_updater.start()
        self._apply_initial_device_state(device, "mrp")
        try:
            self._handle_playing_update(await device.metadata.playing())
        except _CONNECTION_ERRORS as err:
            self.logger.debug("Unable to read initial playback state for %s: %s", self.name, err)
        self.logger.debug("Connected AirPlay playback monitoring for %s", self.display_name)
        self.update_state()
        return False

    async def _disconnect_apple_services(self) -> None:
        """Close all active pyatv connections."""
        self._disconnecting = True
        companion_device = self._companion_device
        mrp_device = self._mrp_device
        self._companion_device = None
        self._mrp_device = None
        self._companion_listener = None
        self._mrp_state_listener = None
        self._mrp_push_listener = None
        if mrp_device:
            with contextlib.suppress(pyatv_exceptions.NotSupportedError):
                if mrp_device.push_updater.active:
                    mrp_device.push_updater.stop()
            mrp_device.close()
        if companion_device:
            companion_device.close()
        self._disconnecting = False

    def _build_config(
        self,
        info: AsyncServiceInfo,
        protocol: Protocol,
        credentials: str | None,
        pairing_requirement: PairingRequirement,
    ) -> AppleTVConfig | None:
        """Build a pyatv configuration from an existing mDNS record."""
        address = self._control_address(info)
        if address is None:
            self.logger.debug("Apple device control requires an IPv4 address for %s", self.name)
            return None
        if info.port is None:
            self.logger.debug("Apple device control service has no port for %s", self.name)
            return None
        properties = {
            key: value for key, value in info.decoded_properties.items() if value is not None
        }
        config = AppleTVConfig(address, self.display_name)
        config.add_service(
            ManualService(
                self.player_id,
                protocol,
                info.port,
                properties,
                credentials=credentials,
                pairing_requirement=pairing_requirement,
            )
        )
        return config

    def _control_address(self, info: AsyncServiceInfo) -> IPv4Address | None:
        """Return an IPv4 address suitable for pyatv."""
        for address in info.parsed_addresses():
            try:
                return IPv4Address(address)
            except AddressValueError:
                continue
        try:
            return IPv4Address(self.address)
        except AddressValueError:
            return None

    @property
    def _is_homepod(self) -> bool:
        """Return whether this Apple player is a HomePod."""
        return "homepod" in self.device_info.model.lower()

    @property
    def _stream_active(self) -> bool:
        """Return whether Music Assistant is actively streaming to this device."""
        return bool(self.stream and self.stream.running)

    def _device_for_feature(self, feature: FeatureName) -> AppleTV | None:
        """Return the preferred connected device facade for a feature."""
        for device in (self._companion_device, self._mrp_device):
            if device and self._feature_available(device, feature):
                return device
        return None

    @staticmethod
    def _feature_available(device: AppleTV, feature: FeatureName) -> bool:
        """Return whether pyatv currently exposes a feature."""
        return device.features.in_state(FeatureState.Available, feature)

    async def _wake_for_playback(self) -> None:
        """Wake the device before starting or resuming playback."""
        if self.powered is True:
            return
        device = self._device_for_feature(FeatureName.TurnOn)
        if device is None:
            return
        self._power_on_event.clear()
        await self._run_control_command(device.power.turn_on(), "wake")
        await self._wait_for_wake()

    async def _wait_for_wake(self) -> None:
        """Wait briefly for a pushed powered-on state."""
        if self.powered is True:
            return
        try:
            await asyncio.wait_for(self._power_on_event.wait(), _WAKE_TIMEOUT)
        except TimeoutError:
            self.logger.debug("No power-state confirmation received from %s", self.display_name)

    async def _run_control_command(self, command: Awaitable[None], description: str) -> None:
        """Run a pyatv command and expose failures as player command errors."""
        try:
            await command
        except _COMMAND_ERRORS as err:
            raise PlayerCommandFailed(
                f"Unable to {description} {self.display_name}: {err}"
            ) from err

    async def _handle_companion_pairing_action(
        self,
        action: str,
        values: dict[str, ConfigValueType] | None,
    ) -> None:
        """Handle an Apple device control pairing action."""
        if action == CONF_ACTION_START_COMPANION_PAIRING:
            await self._reset_companion_pairing(values)
            await self._start_companion_pairing()
        elif action == CONF_ACTION_FINISH_COMPANION_PAIRING:
            await self._finish_companion_pairing(values)
        elif action == CONF_ACTION_RESET_COMPANION_PAIRING:
            await self._reset_companion_pairing(values)

    async def _handle_mrp_pairing_action(
        self,
        action: str,
        values: dict[str, ConfigValueType] | None,
    ) -> None:
        """Handle an Apple playback monitoring pairing action."""
        if action == CONF_ACTION_START_MRP_PAIRING:
            await self._reset_mrp_pairing(values)
            await self._start_mrp_pairing()
        elif action == CONF_ACTION_FINISH_MRP_PAIRING:
            await self._finish_mrp_pairing(values)
        elif action == CONF_ACTION_RESET_MRP_PAIRING:
            await self._reset_mrp_pairing(values)

    async def _start_companion_pairing(self) -> None:
        """Start Companion PIN pairing."""
        if self._active_mrp_pairing:
            raise PlayerCommandFailed("Finish Apple playback monitoring pairing first")
        if not self.companion_discovery_info or not self.companion_pairing_supported:
            raise PlayerCommandFailed(
                f"Apple device control pairing is unavailable for {self.display_name}"
            )
        config = self._build_config(
            self.companion_discovery_info,
            Protocol.Companion,
            None,
            PairingRequirement.Mandatory,
        )
        if config is None:
            raise PlayerCommandFailed(
                f"Apple device control pairing requires an IPv4 address for {self.display_name}"
            )
        pairing: PairingHandler | None = None
        try:
            pairing = await pyatv.pair(
                config,
                Protocol.Companion,
                self.mass.loop,
                name="Music Assistant",
            )
            await pairing.begin()
        except (pyatv_exceptions.PairingError, *_CONNECTION_ERRORS) as err:
            if pairing:
                await pairing.close()
            raise PlayerCommandFailed(
                f"Unable to start Apple device control pairing for {self.display_name}: {err}"
            ) from err
        self._active_companion_pairing = pairing

    async def _finish_companion_pairing(self, values: dict[str, ConfigValueType] | None) -> None:
        """Finish Companion PIN pairing and retain its credentials."""
        if not self._active_companion_pairing or values is None:
            raise PlayerCommandFailed("Apple device control pairing was not started")
        pin = values.get(CONF_COMPANION_PAIRING_PIN)
        try:
            pin_code = int(str(pin))
        except (TypeError, ValueError) as err:
            raise PlayerCommandFailed("Enter the 4-digit PIN shown on the Apple device") from err
        pairing = self._active_companion_pairing
        try:
            pairing.pin(pin_code)
            await pairing.finish()
            credentials = pairing.service.credentials
            if not pairing.has_paired or not credentials:
                raise PlayerCommandFailed("Apple device control pairing did not complete")
            values[CONF_COMPANION_CREDENTIALS] = credentials
        except (pyatv_exceptions.PairingError, *_CONNECTION_ERRORS) as err:
            raise PlayerCommandFailed(
                f"Unable to finish Apple device control pairing for {self.display_name}: {err}"
            ) from err
        finally:
            await pairing.close()
            self._active_companion_pairing = None

    async def _reset_companion_pairing(self, values: dict[str, ConfigValueType] | None) -> None:
        """Clear stored Companion credentials and active connections."""
        if self._connection_task and not self._connection_task.done():
            self._connection_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._connection_task
        if self._active_companion_pairing:
            await self._active_companion_pairing.close()
            self._active_companion_pairing = None
        if values is not None:
            values[CONF_COMPANION_CREDENTIALS] = None
        self.config.update({CONF_COMPANION_CREDENTIALS: None})
        await self._disconnect_apple_services()
        self._schedule_connection(force=True)

    async def _start_mrp_pairing(self) -> None:
        """Start AirPlay remote-control pairing for playback monitoring."""
        if self._active_companion_pairing:
            raise PlayerCommandFailed("Finish Apple device control pairing first")
        if not self.airplay_discovery_info or not self.mrp_pairing_supported:
            raise PlayerCommandFailed(
                f"Apple playback monitoring pairing is unavailable for {self.display_name}"
            )
        config = self._build_config(
            self.airplay_discovery_info,
            Protocol.AirPlay,
            None,
            PairingRequirement.Mandatory,
        )
        if config is None:
            raise PlayerCommandFailed(
                f"Apple playback monitoring pairing requires an IPv4 address for "
                f"{self.display_name}"
            )
        pairing: PairingHandler | None = None
        try:
            pairing = await pyatv.pair(
                config,
                Protocol.AirPlay,
                self.mass.loop,
                name="Music Assistant",
            )
            await pairing.begin()
        except (pyatv_exceptions.PairingError, *_CONNECTION_ERRORS) as err:
            if pairing:
                await pairing.close()
            raise PlayerCommandFailed(
                f"Unable to start Apple playback monitoring pairing for {self.display_name}: {err}"
            ) from err
        self._active_mrp_pairing = pairing

    async def _finish_mrp_pairing(self, values: dict[str, ConfigValueType] | None) -> None:
        """Finish AirPlay remote-control pairing and retain its credentials."""
        if not self._active_mrp_pairing or values is None:
            raise PlayerCommandFailed("Apple playback monitoring pairing was not started")
        pin = values.get(CONF_MRP_PAIRING_PIN)
        try:
            pin_code = int(str(pin))
        except (TypeError, ValueError) as err:
            raise PlayerCommandFailed("Enter the 4-digit PIN shown on the Apple device") from err
        pairing = self._active_mrp_pairing
        try:
            pairing.pin(pin_code)
            await pairing.finish()
            credentials = pairing.service.credentials
            if not pairing.has_paired or not credentials:
                raise PlayerCommandFailed("Apple playback monitoring pairing did not complete")
            values[CONF_MRP_CREDENTIALS] = credentials
        except (pyatv_exceptions.PairingError, *_CONNECTION_ERRORS) as err:
            raise PlayerCommandFailed(
                f"Unable to finish Apple playback monitoring pairing for {self.display_name}: {err}"
            ) from err
        finally:
            await pairing.close()
            self._active_mrp_pairing = None

    async def _reset_mrp_pairing(self, values: dict[str, ConfigValueType] | None) -> None:
        """Clear stored playback-monitoring credentials and reconnect Companion."""
        if self._connection_task and not self._connection_task.done():
            self._connection_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._connection_task
        if self._active_mrp_pairing:
            await self._active_mrp_pairing.close()
            self._active_mrp_pairing = None
        if values is not None:
            values[CONF_MRP_CREDENTIALS] = None
        self.config.update({CONF_MRP_CREDENTIALS: None})
        await self._disconnect_apple_services()
        self._schedule_connection(force=True)

    def _apply_initial_device_state(self, device: AppleTV, source: str) -> None:
        """Apply power and volume snapshots exposed after connection."""
        if self._feature_available(device, FeatureName.PowerState):
            self._handle_power_update(source, device.power.power_state)
        if self._feature_available(device, FeatureName.Volume):
            self._handle_volume_update(source, device.audio.volume)

    def _handle_power_update(self, source: str, power_state: PowerState) -> None:
        """Apply a pushed pyatv power state."""
        if source == "mrp" and self._companion_device is not None:
            return
        if power_state == PowerState.On:
            self._attr_powered = True
            self._power_on_event.set()
        elif power_state == PowerState.Off:
            self._attr_powered = False
            self._power_on_event.clear()
            if not self._stream_active:
                self._attr_playback_state = PlaybackState.IDLE
                self._attr_active_source = None
                self._attr_current_media = None
        else:
            self._attr_powered = None
        self.update_state()

    def _handle_volume_update(self, source: str, volume: float) -> None:
        """Apply a pushed pyatv volume level."""
        if source == "mrp" and self._companion_device is not None:
            return
        self._update_native_volume(round(volume))

    def _update_native_volume(self, volume: int) -> None:
        """Update and persist a volume reported by Apple device control."""
        volume = max(0, min(100, volume))
        self._attr_volume_level = volume
        self.mass.config.set_raw_player_config_value(
            self.player_id,
            CONF_STORED_VOLUME,
            volume,
        )
        self.update_state()

    def _handle_playing_update(self, playing: Playing) -> None:
        """Apply external playback state received over the MRP tunnel."""
        if self._stream_active:
            return
        playback_state = {
            DeviceState.Playing: PlaybackState.PLAYING,
            DeviceState.Loading: PlaybackState.PLAYING,
            DeviceState.Seeking: PlaybackState.PLAYING,
            DeviceState.Paused: PlaybackState.PAUSED,
        }.get(playing.device_state, PlaybackState.IDLE)
        self._attr_playback_state = playback_state
        if playback_state == PlaybackState.IDLE:
            self._attr_active_source = None
            self._attr_current_media = None
            self.update_state()
            return

        app = self._mrp_device.metadata.app if self._mrp_device else None
        source_id = app.identifier if app else "apple_device"
        source_name = (app.name or app.identifier) if app else "Apple device"
        self._attr_active_source = source_id
        self._ensure_source(source_id, source_name)
        self._attr_elapsed_time = float(playing.position or 0)
        self._attr_elapsed_time_last_updated = time.time()
        media_type = (
            MediaType.TRACK if playing.media_type == PyatvMediaType.Music else MediaType.UNKNOWN
        )
        self._attr_current_media = PlayerMedia(
            uri=playing.content_identifier or f"apple-device://{self.player_id}/{playing.hash}",
            media_type=media_type,
            title=playing.title or source_name,
            artist=playing.artist,
            album=playing.album,
            duration=playing.total_time,
            source_id=source_id,
            elapsed_time=playing.position,
            elapsed_time_last_updated=self._attr_elapsed_time_last_updated,
        )
        self.update_state()

    def _ensure_source(self, source_id: str, source_name: str) -> None:
        """Add a passive source reported by Apple playback monitoring."""
        if any(source.id == source_id for source in self._attr_source_list):
            return
        self._attr_source_list.append(
            PlayerSource(
                id=source_id,
                name=source_name,
                passive=True,
                can_play_pause=bool(
                    self._device_for_feature(FeatureName.Play)
                    or self._device_for_feature(FeatureName.Pause)
                ),
                can_seek=False,
                can_next_previous=bool(
                    self._device_for_feature(FeatureName.Next)
                    or self._device_for_feature(FeatureName.Previous)
                ),
            )
        )

    def _handle_connection_closed(
        self,
        source: str,
        device: AppleTV,
        exception: Exception | None = None,
    ) -> None:
        """Handle a pyatv connection closing."""
        if source == "companion" and self._companion_device is device:
            self._companion_device = None
            self._companion_listener = None
        elif source == "mrp" and self._mrp_device is device:
            self._mrp_device = None
            self._mrp_state_listener = None
            self._mrp_push_listener = None
        else:
            return
        if exception:
            self.logger.debug("Apple %s connection lost for %s: %s", source, self.name, exception)
        if not self._disconnecting and not self._unloading:
            self._schedule_connection()

    def _handle_push_error(self, device: AppleTV, exception: Exception) -> None:
        """Restart Apple playback monitoring after a push update error."""
        if self._mrp_device is not device:
            return
        self.logger.debug("Apple playback updates failed for %s: %s", self.name, exception)
        device.push_updater.stop()
        self._mrp_device = None
        self._mrp_state_listener = None
        self._mrp_push_listener = None
        device.close()
        self._schedule_connection()

    @staticmethod
    def _service_signature(info: AsyncServiceInfo | None) -> tuple[object, ...] | None:
        """Return fields that require a pyatv reconnection when changed."""
        if info is None:
            return None
        return (
            info.name,
            info.port,
            tuple(info.addresses),
            tuple(sorted(info.decoded_properties.items())),
        )


class _AppleStateListener(DeviceListener, PowerListener, AudioListener):
    """Forward pyatv device, power, and volume events to an Apple player."""

    def __init__(self, player: AppleDevicePlayer, device: AppleTV, source: str) -> None:
        """Initialize a listener for one pyatv connection."""
        self._player = player
        self._device = device
        self._source = source

    def connection_lost(self, exception: Exception) -> None:
        """Handle an unexpected pyatv disconnect."""
        self._player._handle_connection_closed(self._source, self._device, exception)

    def connection_closed(self) -> None:
        """Handle a closed pyatv connection."""
        self._player._handle_connection_closed(self._source, self._device)

    def powerstate_update(self, old_state: PowerState, new_state: PowerState) -> None:
        """Forward a power-state update."""
        self._player._handle_power_update(self._source, new_state)

    def volume_update(self, old_level: float, new_level: float) -> None:
        """Forward a volume update."""
        self._player._handle_volume_update(self._source, new_level)

    def volume_device_update(
        self,
        output_device: OutputDevice,
        old_level: float,
        new_level: float,
    ) -> None:
        """Ignore volume updates for secondary output devices."""

    def outputdevices_update(
        self,
        old_devices: list[OutputDevice],
        new_devices: list[OutputDevice],
    ) -> None:
        """Ignore output-device membership updates."""


class _ApplePushListener(PushListener):
    """Forward MRP now-playing updates to an Apple player."""

    def __init__(self, player: AppleDevicePlayer, device: AppleTV) -> None:
        """Initialize an MRP push listener."""
        self._player = player
        self._device = device

    def playstatus_update(self, updater: object, playstatus: Playing) -> None:
        """Forward an external playback update."""
        self._player._handle_playing_update(playstatus)

    def playstatus_error(self, updater: object, exception: Exception) -> None:
        """Handle an MRP push update failure."""
        self._player._handle_push_error(self._device, exception)
