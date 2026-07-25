"""Control-capable player implementation for the AirPlay provider."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
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
from pyatv.settings import MrpTunnel
from pyatv.storage.memory_storage import MemoryStorage

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
    CONF_NATIVE_MRP_CREDENTIALS,
    CONF_STORED_VOLUME,
    FALLBACK_VOLUME,
)
from .helpers import (
    get_decoded_property,
    supports_companion_pairing,
    supports_mrp_service,
    supports_mrp_tunnel,
    supports_transient_mrp,
)
from .player import AirPlayPlayer

if TYPE_CHECKING:
    from zeroconf.asyncio import AsyncServiceInfo

    from .provider import AirPlayProvider

_CONTROL_RECONNECT_DELAY: Final[float] = 30.0
_WAKE_TIMEOUT: Final[float] = 10.0

# mDNS TXT keys whose values change with transient playback, session or group
# state - most notably `flags`, which a receiver toggles while it is receiving a
# stream. They never affect how the control connection is established, so a
# change must not force a reconnect: doing so made Apple TVs tear down and
# re-establish the control channel on every stream start and stop, each time
# surfacing the on-screen pairing code. Compared case-insensitively (RFC 6763).
_VOLATILE_DISCOVERY_KEYS: Final = frozenset({"flags", "gcgl", "gid", "igl", "gpn", "pgcgl"})

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


class AirPlayControlPlayer(AirPlayPlayer):
    """AirPlay player with independent device monitoring and control."""

    _attr_type = PlayerType.PLAYER

    def __init__(  # noqa: PLR0913
        self,
        provider: AirPlayProvider,
        player_id: str,
        raop_discovery_info: AsyncServiceInfo | None,
        airplay_discovery_info: AsyncServiceInfo | None,
        companion_discovery_info: AsyncServiceInfo | None,
        mrp_discovery_info: AsyncServiceInfo | None,
        address: str,
        display_name: str,
        manufacturer: str,
        model: str,
        initial_volume: int,
    ) -> None:
        """Initialize a control-capable AirPlay player."""
        self.companion_discovery_info = companion_discovery_info
        self.mrp_discovery_info = mrp_discovery_info
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
        self._companion_listener: _AirPlayStateListener | None = None
        self._mrp_state_listener: _AirPlayStateListener | None = None
        self._mrp_push_listener: _AirPlayPushListener | None = None
        self._active_companion_pairing: PairingHandler | None = None
        self._active_mrp_pairing: PairingHandler | None = None
        self._connection_task: asyncio.Task[None] | None = None
        self._connection_lock = asyncio.Lock()
        self._power_on_event = asyncio.Event()
        self._volume_before_mute: int | None = None
        self._disconnecting = False
        self._restart_connections = False
        self._unloading = False
        # invoked (if set) whenever the Companion connection comes up or goes down,
        # so an observer (e.g. the dashboard adapter) can re-evaluate its state
        self.on_companion_state_change: Callable[[], None] | None = None

    @property
    def companion_pairing_supported(self) -> bool:
        """Return whether this device advertises Companion PIN pairing."""
        return supports_companion_pairing(self.companion_discovery_info)

    @property
    def mrp_pairing_supported(self) -> bool:
        """Return whether MRP playback monitoring can be paired."""
        endpoint = self._mrp_endpoint
        if endpoint is None:
            return False
        discovery_info, protocol = endpoint
        if protocol == Protocol.MRP:
            allow_pairing = get_decoded_property(discovery_info, "AllowPairing") or "no"
            return allow_pairing.lower() == "yes"
        return bool(
            not self._uses_transient_mrp
            and protocol == Protocol.AirPlay
            and self._is_airplay2_capable
            and discovery_info.decoded_properties.get("acl", "0") != "1"
        )

    @property
    def supported_features(self) -> set[PlayerFeature]:
        """Return the supported features of this controlled device."""
        features = {*super().supported_features}
        if self._device_for_feature(FeatureName.Next) or self._device_for_feature(
            FeatureName.Previous
        ):
            features.add(PlayerFeature.NEXT_PREVIOUS)
        # POWER is advertised only when it can actually be served: a connected
        # control channel exposing power commands, or stored Companion
        # credentials (so the feature does not flap while (re)connecting).
        if (
            self.config.get_value(CONF_COMPANION_CREDENTIALS)
            or self._device_for_feature(FeatureName.TurnOn)
            or self._device_for_feature(FeatureName.TurnOff)
        ):
            features.add(PlayerFeature.POWER)
        if not self._stream_active and not self._device_for_feature(FeatureName.SetVolume):
            features.discard(PlayerFeature.VOLUME_MUTE)
        return features

    @property
    def companion_connected(self) -> bool:
        """Return whether the Companion control channel is currently connected."""
        return self._companion_device is not None

    async def get_config_entries(
        self,
        action: str | None = None,
        values: dict[str, ConfigValueType] | None = None,
    ) -> list[ConfigEntry]:
        """Return player configuration entries, including control pairing."""
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

        entries.extend(self._get_companion_config_entries(values))
        entries.extend(self._get_mrp_config_entries(values))
        return entries

    async def power(self, powered: bool) -> None:
        """Turn the controlled device on or off."""
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
        """Stop Music Assistant playback, or return the device to its home screen."""
        if self._stream_active:
            await super().stop()
            return
        # For external playback there is no real "stop"; returning to the home
        # screen backgrounds the current app, which is the closest equivalent.
        device = self._device_for_feature(FeatureName.Home)
        if device is not None:
            await self._run_control_command(device.remote_control.home(), "stop")
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
        """Wake the controlled device and start Music Assistant playback."""
        await self._wake_for_playback()
        await super().play_media(media)

    async def volume_set(self, volume_level: int) -> None:
        """Set stream or native device volume."""
        if self._stream_active:
            await super().volume_set(volume_level)
            return
        device = self._device_for_feature(FeatureName.SetVolume)
        if device is None:
            await super().volume_set(volume_level)
            return
        await self._run_volume_command(device.audio.set_volume(volume_level), "set volume")
        self._handle_volume_update("command", volume_level)

    async def volume_mute(self, muted: bool) -> None:
        """Mute an active stream or native device volume."""
        if self._stream_active:
            await super().volume_mute(muted)
            return
        device = self._device_for_feature(FeatureName.SetVolume)
        if device is None:
            raise PlayerCommandFailed(f"Mute control is unavailable for {self.display_name}")
        if muted:
            if self.volume_muted:
                return
            if self.volume_level and self.volume_level > 0:
                self._volume_before_mute = self.volume_level
            await self._run_volume_command(device.audio.set_volume(0), "mute")
            self._handle_volume_update("command", 0)
            return
        if not self.volume_muted:
            return
        volume = self._volume_before_mute or self.volume_level or FALLBACK_VOLUME
        await self._run_volume_command(device.audio.set_volume(volume), "unmute")
        self._handle_volume_update("command", volume)

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

    async def wake(self) -> None:
        """Wake the device from sleep when it exposes power control."""
        await self._wake_for_playback()

    async def async_list_installed_app_ids(self) -> set[str] | None:
        """
        Return the bundle ids of the apps installed on the device.

        Uses the Companion app-listing feature. Returns None when the app list cannot be
        retrieved (Companion channel down or the query failed), so a caller can tell
        "unknown" apart from "installed, but not this app".
        """
        device = self._device_for_feature(FeatureName.AppList)
        if device is None:
            return None
        try:
            apps = await device.apps.app_list()
        except _COMMAND_ERRORS as err:
            self.logger.debug("Unable to list installed apps for %s: %s", self.name, err)
            return None
        return {app.identifier for app in apps}

    async def async_launch_app(self, bundle_id_or_url: str) -> None:
        """
        Launch an app (bundle id) or custom URL on the device over Companion.

        :param bundle_id_or_url: A bundle id or a URL-scheme value to launch.
        :raises PlayerCommandFailed: If app launching is unavailable or the launch fails.
        """
        device = self._device_for_feature(FeatureName.LaunchApp)
        if device is None:
            raise PlayerCommandFailed(f"App launching is unavailable for {self.display_name}")
        await self._run_control_command(device.apps.launch_app(bundle_id_or_url), "launch app")

    def set_discovery_info(self, discovery_info: AsyncServiceInfo, display_name: str) -> None:
        """Update AirPlay discovery data and reconnect device control if needed."""
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

    async def set_mrp_discovery_info(self, discovery_info: AsyncServiceInfo | None) -> None:
        """Update native MRP discovery data and reconnect playback monitoring."""
        if self._service_signature(self.mrp_discovery_info) == self._service_signature(
            discovery_info
        ):
            return
        self.mrp_discovery_info = discovery_info
        self.update_state()
        self._schedule_connection(force=True)

    async def on_config_updated(self) -> None:
        """Reconnect control services when player configuration changes."""
        await super().on_config_updated()
        self._schedule_connection(force=True)

    async def on_unload(self) -> None:
        """Close control connections and pairing resources."""
        self._unloading = True
        if self._connection_task and not self._connection_task.done():
            self._connection_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._connection_task
        await self._disconnect_control_services()
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
        """Connect control services and retry transient failures."""
        retry = True
        while retry and not self._unloading:
            retry = await self._connect_control_services()
            if retry:
                await asyncio.sleep(_CONTROL_RECONNECT_DELAY)

    async def _connect_control_services(self) -> bool:
        """Connect Companion and MRP independently."""
        async with self._connection_lock:
            if self._restart_connections:
                self._restart_connections = False
                await self._disconnect_control_services()
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
                "Stored Companion credentials are no longer valid for %s",
                self.display_name,
            )
            self._clear_stored_credentials(CONF_COMPANION_CREDENTIALS)
            return False
        except _CONNECTION_ERRORS as err:
            self.logger.debug("Unable to connect Companion control for %s: %s", self.name, err)
            return True

        self._companion_device = device
        listener = _AirPlayStateListener(self, device, "companion")
        self._companion_listener = listener
        device.listener = listener
        device.power.listener = listener
        device.audio.listener = listener
        self._apply_initial_device_state(device, "companion")
        self.update_state()
        self.logger.debug("Connected Companion control for %s", self.display_name)
        self._notify_companion_state_change()
        return False

    async def _connect_mrp(self) -> bool:
        """Connect MRP playback monitoring."""
        if self._mrp_device is not None:
            return False
        endpoint = self._mrp_endpoint
        if endpoint is None:
            return False
        discovery_info, protocol = endpoint
        credentials = self._mrp_credentials
        if protocol == Protocol.AirPlay and credentials is None and not self._uses_transient_mrp:
            return False
        config = self._build_config(
            discovery_info,
            protocol,
            str(credentials) if credentials else None,
            PairingRequirement.NotNeeded,
        )
        if config is None:
            return False
        storage: MemoryStorage | None = None
        if protocol == Protocol.AirPlay:
            storage = MemoryStorage()
            settings = await storage.get_settings(config)
            settings.protocols.airplay.mrp_tunnel = MrpTunnel.Force
        try:
            device = await pyatv.connect(config, self.mass.loop, storage=storage)
        except (
            pyatv_exceptions.AuthenticationError,
            pyatv_exceptions.InvalidCredentialsError,
        ) as err:
            self.logger.warning(
                "Unable to authenticate playback monitoring for %s: %s", self.name, err
            )
            if credentials:
                self._clear_stored_credentials(self._mrp_credentials_key)
            return False
        except _CONNECTION_ERRORS as err:
            self.logger.debug("Unable to connect playback monitoring for %s: %s", self.name, err)
            return True

        if not self._feature_available(device, FeatureName.PushUpdates):
            device.close()
            self.logger.debug("Playback monitoring is not supported by %s", self.name)
            return False

        self._mrp_device = device
        state_listener = _AirPlayStateListener(self, device, "mrp")
        push_listener = _AirPlayPushListener(self, device)
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
        self.logger.debug("Connected MRP playback monitoring for %s", self.display_name)
        self.update_state()
        return False

    async def _disconnect_control_services(self) -> None:
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
        if companion_device is not None:
            self._notify_companion_state_change()

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
            self.logger.debug("Device control requires an IPv4 address for %s", self.name)
            return None
        if info.port is None:
            self.logger.debug("Device control service has no port for %s", self.name)
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
    def _stream_active(self) -> bool:
        """Return whether Music Assistant is actively streaming to this device."""
        return bool((stream := getattr(self, "stream", None)) and stream.running)

    @property
    def _mrp_endpoint(self) -> tuple[AsyncServiceInfo, Protocol] | None:
        """Return the preferred MRP endpoint and transport protocol."""
        if supports_mrp_service(self.mrp_discovery_info):
            assert self.mrp_discovery_info is not None
            return self.mrp_discovery_info, Protocol.MRP
        if supports_mrp_tunnel(self.airplay_discovery_info):
            assert self.airplay_discovery_info is not None
            return self.airplay_discovery_info, Protocol.AirPlay
        return None

    @property
    def _mrp_credentials_key(self) -> str:
        """Return the credential key for the active MRP transport."""
        endpoint = self._mrp_endpoint
        if endpoint is not None and endpoint[1] == Protocol.MRP:
            return CONF_NATIVE_MRP_CREDENTIALS
        return CONF_MRP_CREDENTIALS

    @property
    def _mrp_credentials(self) -> str | None:
        """Return credentials for the active MRP transport."""
        credentials = self.config.get_value(self._mrp_credentials_key)
        return str(credentials) if credentials else None

    @property
    def _uses_transient_mrp(self) -> bool:
        """Return whether playback monitoring uses transient AirPlay credentials."""
        # Mirrors pyatv's device rules: Apple TVs only accept real (paired) HAP
        # credentials on the MRP tunnel and answer a transient pair-setup by
        # showing the on-screen AirPlay pairing dialog, so they must never take
        # this path - not even while the Companion record is still undiscovered.
        # HomePods (and tunnel-capable third-party receivers) accept the
        # transient handshake silently.
        if self._is_apple_tv_device:
            return False
        return supports_transient_mrp(self.airplay_discovery_info)

    @property
    def _is_apple_tv_device(self) -> bool:
        """Return whether the underlying device identifies itself as an Apple TV."""
        if self.airplay_discovery_info and (
            model := get_decoded_property(self.airplay_discovery_info, "model")
        ):
            return model.startswith("AppleTV")
        return "apple tv" in self.device_info.model.lower()

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

    async def _run_volume_command(self, command: Awaitable[None], description: str) -> None:
        """Run a native volume command, tolerating a missing confirmation event."""
        # pyatv waits (up to 5s) for a pushed volume confirmation after a Companion
        # volume command. Apple TVs that pass volume through to an HDMI-CEC amplifier
        # apply the change but never emit that event, so the call times out even
        # though it succeeded. Treat the timeout as success and let the caller apply
        # the requested level; genuine command failures still surface.
        try:
            await command
        except TimeoutError:
            self.logger.debug(
                "No volume confirmation from %s; assuming the change was applied",
                self.display_name,
            )
        except _COMMAND_ERRORS as err:
            raise PlayerCommandFailed(
                f"Unable to {description} {self.display_name}: {err}"
            ) from err

    def _get_companion_config_entries(
        self,
        values: dict[str, ConfigValueType] | None,
    ) -> list[ConfigEntry]:
        """Return Companion pairing configuration entries."""
        credentials = self.config.get_value(CONF_COMPANION_CREDENTIALS)
        if values is not None:
            credentials = values.get(CONF_COMPANION_CREDENTIALS, credentials)
        entries: list[ConfigEntry] = []
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
        # Only in-flight values are echoed back; stored secrets must never be
        # included in the config-entry payload.
        entries.append(
            self._get_hidden_credentials_entry(
                CONF_COMPANION_CREDENTIALS,
                values.get(CONF_COMPANION_CREDENTIALS) if values is not None else None,
            )
        )
        return entries

    def _get_mrp_config_entries(
        self,
        values: dict[str, ConfigValueType] | None,
    ) -> list[ConfigEntry]:
        """Return MRP pairing configuration entries."""
        credentials = self._mrp_credentials
        if values is not None:
            credentials_value = values.get(self._mrp_credentials_key, credentials)
            credentials = str(credentials_value) if credentials_value else None
        entries: list[ConfigEntry] = []
        if credentials:
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
        # Only in-flight values are echoed back; stored secrets must never be
        # included in the config-entry payload.
        entries.extend(
            self._get_hidden_credentials_entry(
                credentials_key,
                values.get(credentials_key) if values is not None else None,
            )
            for credentials_key in (CONF_MRP_CREDENTIALS, CONF_NATIVE_MRP_CREDENTIALS)
        )
        return entries

    @staticmethod
    def _get_hidden_credentials_entry(
        credentials_key: str,
        credentials: ConfigValueType,
    ) -> ConfigEntry:
        """Return a hidden secure credentials entry."""
        return ConfigEntry(
            key=credentials_key,
            type=ConfigEntryType.SECURE_STRING,
            default_value=None,
            value=credentials,
            required=False,
            hidden=True,
            category="protocol_generic",
        )

    async def _handle_companion_pairing_action(
        self,
        action: str,
        values: dict[str, ConfigValueType] | None,
    ) -> None:
        """Handle a Companion control pairing action."""
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
        """Handle an MRP playback-monitoring pairing action."""
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
            raise PlayerCommandFailed("Finish playback monitoring pairing first")
        if not self.companion_discovery_info or not self.companion_pairing_supported:
            raise PlayerCommandFailed(f"Companion pairing is unavailable for {self.display_name}")
        config = self._build_config(
            self.companion_discovery_info,
            Protocol.Companion,
            None,
            PairingRequirement.Mandatory,
        )
        if config is None:
            raise PlayerCommandFailed(
                f"Companion pairing requires an IPv4 address for {self.display_name}"
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
                f"Unable to start Companion pairing for {self.display_name}: {err}"
            ) from err
        self._active_companion_pairing = pairing

    async def _finish_companion_pairing(self, values: dict[str, ConfigValueType] | None) -> None:
        """Finish Companion PIN pairing and retain its credentials."""
        if not self._active_companion_pairing or values is None:
            raise PlayerCommandFailed("Companion pairing was not started")
        pin = values.get(CONF_COMPANION_PAIRING_PIN)
        try:
            pin_code = int(str(pin))
        except (TypeError, ValueError) as err:
            raise PlayerCommandFailed("Enter the 4-digit PIN shown on the device") from err
        pairing = self._active_companion_pairing
        try:
            pairing.pin(pin_code)
            await pairing.finish()
            credentials = pairing.service.credentials
            if not pairing.has_paired or not credentials:
                raise PlayerCommandFailed("Companion pairing did not complete")
            values[CONF_COMPANION_CREDENTIALS] = credentials
            # the action flow never persists `values` itself, so save right away
            await self.mass.config.save_player_config(
                self.player_id, {CONF_COMPANION_CREDENTIALS: credentials}
            )
        except (pyatv_exceptions.PairingError, *_CONNECTION_ERRORS) as err:
            raise PlayerCommandFailed(
                f"Unable to finish Companion pairing for {self.display_name}: {err}"
            ) from err
        finally:
            await pairing.close()
            self._active_companion_pairing = None
        self._schedule_connection(force=True)

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
        await self._disconnect_control_services()
        # persist the cleared credentials so a restart cannot resurrect them
        await self.mass.config.save_player_config(
            self.player_id, {CONF_COMPANION_CREDENTIALS: None}
        )
        self._schedule_connection(force=True)

    async def _start_mrp_pairing(self) -> None:
        """Start pairing for MRP playback monitoring."""
        if self._active_companion_pairing:
            raise PlayerCommandFailed("Finish Companion pairing first")
        endpoint = self._mrp_endpoint
        if endpoint is None or not self.mrp_pairing_supported:
            raise PlayerCommandFailed(
                f"Playback monitoring pairing is unavailable for {self.display_name}"
            )
        discovery_info, protocol = endpoint
        config = self._build_config(
            discovery_info,
            protocol,
            None,
            PairingRequirement.Optional
            if protocol == Protocol.MRP
            else PairingRequirement.Mandatory,
        )
        if config is None:
            raise PlayerCommandFailed(
                f"Playback monitoring pairing requires an IPv4 address for {self.display_name}"
            )
        pairing: PairingHandler | None = None
        try:
            pairing = await pyatv.pair(
                config,
                protocol,
                self.mass.loop,
                name="Music Assistant",
            )
            await pairing.begin()
        except (pyatv_exceptions.PairingError, *_CONNECTION_ERRORS) as err:
            if pairing:
                await pairing.close()
            raise PlayerCommandFailed(
                f"Unable to start playback monitoring pairing for {self.display_name}: {err}"
            ) from err
        self._active_mrp_pairing = pairing

    async def _finish_mrp_pairing(self, values: dict[str, ConfigValueType] | None) -> None:
        """Finish MRP playback-monitoring pairing and retain its credentials."""
        if not self._active_mrp_pairing or values is None:
            raise PlayerCommandFailed("Playback monitoring pairing was not started")
        pin = values.get(CONF_MRP_PAIRING_PIN)
        try:
            pin_code = int(str(pin))
        except (TypeError, ValueError) as err:
            raise PlayerCommandFailed("Enter the 4-digit PIN shown on the device") from err
        pairing = self._active_mrp_pairing
        try:
            pairing.pin(pin_code)
            await pairing.finish()
            credentials = pairing.service.credentials
            if not pairing.has_paired or not credentials:
                raise PlayerCommandFailed("Playback monitoring pairing did not complete")
            credentials_key = self._mrp_credentials_key
            values[credentials_key] = credentials
            # the action flow never persists `values` itself, so save right away
            await self.mass.config.save_player_config(
                self.player_id, {credentials_key: credentials}
            )
        except (pyatv_exceptions.PairingError, *_CONNECTION_ERRORS) as err:
            raise PlayerCommandFailed(
                f"Unable to finish playback monitoring pairing for {self.display_name}: {err}"
            ) from err
        finally:
            await pairing.close()
            self._active_mrp_pairing = None
        self._schedule_connection(force=True)

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
            values[CONF_NATIVE_MRP_CREDENTIALS] = None
        await self._disconnect_control_services()
        # persist the cleared credentials so a restart cannot resurrect them
        await self.mass.config.save_player_config(
            self.player_id,
            {
                CONF_MRP_CREDENTIALS: None,
                CONF_NATIVE_MRP_CREDENTIALS: None,
            },
        )
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
        volume_level = max(0, min(100, round(volume)))
        if volume_level == 0:
            if self._volume_before_mute is None and self._attr_volume_level:
                self._volume_before_mute = self._attr_volume_level
            mute_changed = self._attr_volume_muted is not True
            self._attr_volume_muted = True
            if mute_changed:
                self.update_state()
            return
        mute_changed = self._attr_volume_muted is not False
        self._attr_volume_muted = False
        self._volume_before_mute = None
        self._update_native_volume(volume_level, state_changed=mute_changed)

    def _update_native_volume(self, volume: int, *, state_changed: bool = False) -> None:
        """Update and persist a volume reported by device control."""
        volume = max(0, min(100, volume))
        if self._attr_volume_level == volume:
            if state_changed:
                self.update_state()
            return
        self._attr_volume_level = volume
        self.mass.config.set_raw_player_config_value(
            self.player_id,
            CONF_STORED_VOLUME,
            volume,
        )
        self.update_state()

    def _clear_stored_credentials(self, credentials_key: str) -> None:
        """Clear credentials that the receiver rejected."""
        self.config.update({credentials_key: None})
        self.mass.config.set_raw_player_config_value(
            self.player_id,
            credentials_key,
            None,
        )
        self.update_state()

    def _handle_playing_update(self, playing: Playing) -> None:
        """Apply external playback state received over the MRP tunnel."""
        if self._stream_active:
            return
        app = self._mrp_device.metadata.app if self._mrp_device else None
        playback_state = {
            DeviceState.Playing: PlaybackState.PLAYING,
            DeviceState.Loading: PlaybackState.PLAYING,
            DeviceState.Seeking: PlaybackState.PLAYING,
            DeviceState.Paused: PlaybackState.PAUSED,
        }.get(playing.device_state, PlaybackState.IDLE)
        # Many tvOS apps (e.g. Netflix) report Idle rather than Paused when
        # paused. While the same app stays the active source, keep it paused
        # instead of going idle so transport controls resume the app itself
        # rather than falling back to the Music Assistant queue.
        if (
            playback_state == PlaybackState.IDLE
            and app is not None
            and self._attr_active_source == app.identifier
        ):
            playback_state = PlaybackState.PAUSED
        self._attr_playback_state = playback_state
        if playback_state == PlaybackState.IDLE:
            self._attr_active_source = None
            self._attr_current_media = None
            self.update_state()
            return

        source_id = app.identifier if app else "airplay_control"
        source_name = (app.name or app.identifier) if app else "AirPlay device"
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
        """Add a passive source reported by MRP playback monitoring."""
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
        companion_closed = False
        if source == "companion" and self._companion_device is device:
            self._companion_device = None
            self._companion_listener = None
            companion_closed = True
        elif source == "mrp" and self._mrp_device is device:
            self._mrp_device = None
            self._mrp_state_listener = None
            self._mrp_push_listener = None
        else:
            return
        if exception:
            self.logger.debug("Apple %s connection lost for %s: %s", source, self.name, exception)
        if companion_closed:
            self._notify_companion_state_change()
        if not self._disconnecting and not self._unloading:
            self._schedule_connection()

    def _handle_push_error(self, device: AppleTV, exception: Exception) -> None:
        """Restart MRP playback monitoring after a push update error."""
        if self._mrp_device is not device:
            return
        self.logger.debug("MRP playback updates failed for %s: %s", self.name, exception)
        device.push_updater.stop()
        self._mrp_device = None
        self._mrp_state_listener = None
        self._mrp_push_listener = None
        device.close()
        self._schedule_connection()

    def _notify_companion_state_change(self) -> None:
        """Notify a wired-up observer that the Companion connection state changed."""
        if self.on_companion_state_change is not None:
            self.on_companion_state_change()

    @staticmethod
    def _service_signature(info: AsyncServiceInfo | None) -> tuple[object, ...] | None:
        """Return fields that require a pyatv reconnection when changed."""
        if info is None:
            return None
        # TXT keys are case-insensitive (RFC 6763); casefold them so a re-cased
        # key is never mistaken for a connection-relevant change.
        stable_properties = tuple(
            sorted(
                (key.casefold(), value)
                for key, value in info.decoded_properties.items()
                if key.casefold() not in _VOLATILE_DISCOVERY_KEYS
            )
        )
        return (
            info.name,
            info.port,
            tuple(info.addresses),
            stable_properties,
        )


class _AirPlayStateListener(DeviceListener, PowerListener, AudioListener):
    """Forward pyatv device, power, and volume events to a controlled player."""

    def __init__(self, player: AirPlayControlPlayer, device: AppleTV, source: str) -> None:
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


class _AirPlayPushListener(PushListener):
    """Forward MRP now-playing updates to a controlled player."""

    def __init__(self, player: AirPlayControlPlayer, device: AppleTV) -> None:
        """Initialize an MRP push listener."""
        self._player = player
        self._device = device

    def playstatus_update(self, updater: object, playstatus: Playing) -> None:
        """Forward an external playback update."""
        self._player._handle_playing_update(playstatus)

    def playstatus_error(self, updater: object, exception: Exception) -> None:
        """Handle an MRP push update failure."""
        self._player._handle_push_error(self._device, exception)
