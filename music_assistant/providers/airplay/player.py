"""AirPlay Player implementations."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import time
from typing import TYPE_CHECKING, cast

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption, ConfigValueType
from music_assistant_models.constants import PLAYER_CONTROL_NATIVE
from music_assistant_models.enums import (
    ConfigEntryType,
    IdentifierType,
    PlaybackState,
    PlayerFeature,
    PlayerType,
)

from music_assistant.constants import CONF_ENTRY_SYNC_ADJUST
from music_assistant.helpers.util import get_primary_ip_address_from_zeroconf, is_valid_mac_address
from music_assistant.models.player import DeviceInfo, Player, PlayerMedia

from .constants import (
    AIRPLAY_DEFAULT_SESSION_DELAY_MS,
    AIRPLAY_DISCOVERY_TYPE,
    AIRPLAY_FLOW_PCM_FORMAT,
    AIRPLAY_OUTPUT_BUFFER_DEFAULT_DURATION_MS,
    AIRPLAY_PCM_FORMAT,
    AIRPLAY_SESSION_ESTABLISHMENT_LATENCY_DEFAULT_MS,
    AIRPLAY_SESSION_ESTABLISHMENT_LATENCY_MAX_MS,
    AIRPLAY_SESSION_ESTABLISHMENT_LATENCY_MIN_MS,
    BASE_PLAYER_FEATURES,
    BROKEN_AIRPLAY_WARN,
    CONF_ACTION_FINISH_PAIRING,
    CONF_ACTION_RESET_PAIRING,
    CONF_ACTION_START_PAIRING,
    CONF_AIRPLAY_CREDENTIALS,
    CONF_AIRPLAY_PROTOCOL,
    CONF_ALAC_ENCODE,
    CONF_AP2PASSWORD,
    CONF_ENCRYPTION,
    CONF_IGNORE_VOLUME,
    CONF_PAIRING_PASSWORD,
    CONF_PAIRING_PIN,
    CONF_PASSWORD,
    CONF_RAOP_CREDENTIALS,
    CONF_RAOP_LATENCY,
    CONF_SESSION_ESTABLISHMENT_LATENCY,
    CONF_STORED_VOLUME,
    FALLBACK_VOLUME,
    LEGACY_PAIRING_BIT,
    PASSWORD_BIT,
    PIN_REQUIRED,
    RAOP_CONNECT_TIME_MS,
    RAOP_DISCOVERY_TYPE,
    RAOP_OUTPUT_BUFFER_MAX_DURATION_MS,
    RAOP_OUTPUT_BUFFER_MIN_DURATION_MS,
    StreamingProtocol,
)
from .helpers import (
    is_airplay2_preferred_model,
    is_apple_device,
    is_broken_airplay_model,
    player_id_to_mac_address,
)
from .stream_session import AirPlayStreamSession

if TYPE_CHECKING:
    from zeroconf.asyncio import AsyncServiceInfo

    from .pairing import AirPlayPairing
    from .protocols._protocol import AirPlayProtocol
    from .protocols.airplay2 import AirPlay2Stream
    from .protocols.raop import RaopStream
    from .provider import AirPlayProvider

# Docker bridge subnet, sometimes wrongly advertised via mDNS by containerized devices.
_DOCKER_SUBNET = ipaddress.ip_network("172.16.0.0/12")


class AirPlayPlayer(Player):
    """AirPlay Player implementation."""

    def __init__(
        self,
        provider: AirPlayProvider,
        player_id: str,
        raop_discovery_info: AsyncServiceInfo | None,
        airplay_discovery_info: AsyncServiceInfo | None,
        address: str,
        display_name: str,
        manufacturer: str,
        model: str,
        initial_volume: int = FALLBACK_VOLUME,
    ) -> None:
        """Initialize AirPlayPlayer."""
        self.raop_discovery_info = raop_discovery_info
        self.airplay_discovery_info = airplay_discovery_info
        super().__init__(provider, player_id)
        self.address = address
        self.stream: RaopStream | AirPlay2Stream | None = None
        self.last_command_sent = 0.0
        self._lock = asyncio.Lock()
        self._active_pairing: AirPlayPairing | None = None
        self._transitioning = False  # Set during stream replacement to ignore stale DACP messages
        # Set (static) player attributes
        self._attr_name = display_name
        self._attr_available = True
        mac_address = player_id_to_mac_address(player_id)
        self._attr_device_info = DeviceInfo(
            model=model,
            manufacturer=manufacturer,
        )
        # Only add MAC address if it's valid (not 00:00:00:00:00:00)
        if is_valid_mac_address(mac_address):
            self._attr_device_info.add_identifier(IdentifierType.MAC_ADDRESS, mac_address)
        self._attr_device_info.add_identifier(IdentifierType.IP_ADDRESS, address)
        self._attr_device_info.add_identifier(IdentifierType.AIRPLAY_ID, player_id)
        self._attr_volume_level = initial_volume
        self._attr_can_group_with = {provider.instance_id}
        self._attr_enabled_by_default = not is_broken_airplay_model(manufacturer, model)
        self._attr_supported_sample_rates = [
            (AIRPLAY_PCM_FORMAT.sample_rate, AIRPLAY_PCM_FORMAT.bit_depth)
        ]

        # Set player type based on manufacturer/model:
        # - Apple devices (HomePod, Apple TV) have native AirPlay support -> PLAYER
        # - Non-Apple devices are generic AirPlay receivers -> PROTOCOL (wrapped in UniversalPlayer)
        if is_apple_device(manufacturer, model):
            self._attr_type = PlayerType.PLAYER
        else:
            self._attr_type = PlayerType.PROTOCOL

    @property
    def protocol(self) -> StreamingProtocol:
        """Get the streaming protocol to use/prefer for this player."""
        preferred_option = cast("int", self.config.get_value(CONF_AIRPLAY_PROTOCOL, 0))
        return self._get_protocol_for_config_value(preferred_option)

    @property
    def needs_setup(self) -> bool:
        """Return if the player needs setup."""
        if self._requires_pin_pairing() or (
            self._requires_password_pairing() and self.protocol == StreamingProtocol.AIRPLAY2
        ):
            # check if we have credentials stored for the current protocol
            creds_key = self._get_credentials_key(self.protocol)
            if not self.config.get_value(creds_key):
                return True
        return False

    @property
    def requires_flow_mode(self) -> bool:
        """Return if the player requires flow mode."""
        return True

    @property
    def supported_features(self) -> set[PlayerFeature]:
        """Return the supported features of this player."""
        features = set(BASE_PLAYER_FEATURES)
        if not (self.group_members or self.synced_to):
            # we only support pause when the player is not synced,
            # because we don't want to deal with the complexity of pausing a group of players
            # so in this case stop will be used to pause the stream instead of pausing it,
            # which is a common approach for AirPlay players
            features.add(PlayerFeature.PAUSE)
        return features

    @property
    def can_group_with(self) -> set[str]:
        """Return player IDs this player can group with.

        RAOP and AP2 players can group with other RAOP and/or AP2 players.
        """
        prov = cast("AirPlayProvider", self.provider)
        return {
            p.player_id for p in prov.get_players() if p.available and p.player_id != self.player_id
        }

    @property
    def output_buffer_duration_ms(self) -> int:
        """Get the output buffer duration in milliseconds."""
        # Only the RAOP (AirPlay 1) path exposes a configurable read-ahead buffer;
        # AirPlay 2 uses the fixed default to avoid interfering with sync.
        if self.protocol == StreamingProtocol.RAOP:
            return cast(
                "int",
                self.config.get_value(CONF_RAOP_LATENCY, AIRPLAY_OUTPUT_BUFFER_DEFAULT_DURATION_MS),
            )
        return AIRPLAY_OUTPUT_BUFFER_DEFAULT_DURATION_MS

    @property
    def session_establishment_latency_ms(self) -> int:
        """Get the configured session establishment latency in milliseconds."""
        if self.protocol == StreamingProtocol.AIRPLAY2:
            return cast(
                "int",
                self.config.get_value(
                    CONF_SESSION_ESTABLISHMENT_LATENCY,
                    AIRPLAY_SESSION_ESTABLISHMENT_LATENCY_DEFAULT_MS,
                ),
            )
        return RAOP_CONNECT_TIME_MS

    @property
    def wait_start(self) -> int:
        """Get the time in ms to allow device to connect before starting stream."""
        if self.protocol == StreamingProtocol.AIRPLAY2:
            return int(self.session_establishment_latency_ms + AIRPLAY_DEFAULT_SESSION_DELAY_MS)
        return int(self.session_establishment_latency_ms + self.output_buffer_duration_ms)

    async def get_config_entries(
        self,
        action: str | None = None,
        values: dict[str, ConfigValueType] | None = None,
    ) -> list[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the given player (if any)."""
        base_entries: list[ConfigEntry] = []
        require_authentication = self._requires_pin_pairing() or self._requires_password_pairing()

        # Handle pairing actions
        if action and require_authentication:
            await self._handle_pairing_action(action=action, values=values)

        # Add pairing config entries for Apple TV and macOS devices
        if require_authentication:
            base_entries = [*self._get_pairing_config_entries(values)]

        # Determine effective protocol from values being saved (if available)
        # or fall back to stored config. This ensures config entries reflect
        # the current form state, not stale stored state.
        if values and (val := values.get(CONF_AIRPLAY_PROTOCOL)) is not None:
            effective_protocol = self._get_protocol_for_config_value(cast("int", val))
        else:
            effective_protocol = self.protocol
        is_raop = effective_protocol == StreamingProtocol.RAOP

        # Regular AirPlay config entries
        base_entries += [
            ConfigEntry(
                key=CONF_AIRPLAY_PROTOCOL,
                type=ConfigEntryType.INTEGER,
                required=False,
                label="AirPlay protocol version to use for streaming",
                description="AirPlay version 1 protocol uses RAOP.\n"
                "AirPlay version 2 is an extension of RAOP.\n"
                "Some newer devices do not fully support RAOP and "
                "will only work with AirPlay version 2, "
                "while older devices may only support RAOP.\n\n"
                "In most cases the default automatic selection will work fine.\n\n"
                "NOTE: AirPlay 2 currently does not support audio synchronization. "
                "Grouping/syncing with other players is only available when "
                "using AirPlay 1 (RAOP).",
                options=[
                    opt
                    for opt in (
                        ConfigValueOption("Automatically select", 0),
                        ConfigValueOption("Prefer AirPlay 1 (RAOP)", StreamingProtocol.RAOP.value)
                        if self.raop_discovery_info
                        else None,
                        ConfigValueOption("Prefer AirPlay 2", StreamingProtocol.AIRPLAY2.value)
                        if self.airplay_discovery_info
                        else None,
                    )
                    if opt is not None
                ],
                default_value=0,
                category="protocol_generic",
            ),
            ConfigEntry(
                key=CONF_ENCRYPTION,
                type=ConfigEntryType.BOOLEAN,
                default_value=True,
                label="Enable encryption",
                description="Enable encrypted communication with the player, "
                "some (3rd party) players require this to be disabled.",
                depends_on=CONF_AIRPLAY_PROTOCOL,
                depends_on_value=StreamingProtocol.RAOP.value,
                hidden=not is_raop,
                category="protocol_generic",
                advanced=True,
            ),
            ConfigEntry(
                key=CONF_ALAC_ENCODE,
                type=ConfigEntryType.BOOLEAN,
                default_value=True,
                label="Enable compression",
                description="Save some network bandwidth by sending the audio as "
                "(lossless) ALAC at the cost of a bit of CPU.",
                depends_on=CONF_AIRPLAY_PROTOCOL,
                depends_on_value=StreamingProtocol.RAOP.value,
                hidden=not is_raop,
                category="protocol_generic",
                advanced=True,
            ),
            CONF_ENTRY_SYNC_ADJUST,
            ConfigEntry(
                key=CONF_PASSWORD,
                type=ConfigEntryType.SECURE_STRING,
                default_value=None,
                required=False,
                label="Device password",
                description="Some devices require a password to connect/play.",
                depends_on=CONF_AIRPLAY_PROTOCOL,
                depends_on_value=StreamingProtocol.RAOP.value,
                hidden=not is_raop,
                category="protocol_generic",
                advanced=True,
            ),
            ConfigEntry(
                key=CONF_IGNORE_VOLUME,
                type=ConfigEntryType.BOOLEAN,
                default_value=False,
                label="Ignore volume reports sent by the device itself",
                description=(
                    "The AirPlay protocol allows devices to report their own volume "
                    "level. \n"
                    "For some devices this is not reliable and can cause unexpected "
                    "volume changes. \n"
                    "Enable this option to ignore these reports."
                ),
                category="protocol_generic",
                advanced=True,
            ),
            ConfigEntry(
                key=CONF_RAOP_LATENCY,
                type=ConfigEntryType.INTEGER,
                default_value=AIRPLAY_OUTPUT_BUFFER_DEFAULT_DURATION_MS,
                range=(
                    RAOP_OUTPUT_BUFFER_MIN_DURATION_MS,
                    RAOP_OUTPUT_BUFFER_MAX_DURATION_MS,
                ),
                label="Milliseconds of data to buffer",
                description=(
                    "The number of milliseconds of data to buffer\n"
                    "NOTE: This adds to the latency experienced for commencement "
                    "of playback. \n"
                    "Try increasing value if playback is unreliable."
                ),
                depends_on=CONF_AIRPLAY_PROTOCOL,
                depends_on_value=StreamingProtocol.RAOP.value,
                hidden=not is_raop,
                category="protocol_generic",
                advanced=True,
            ),
            ConfigEntry(
                key=CONF_SESSION_ESTABLISHMENT_LATENCY,
                type=ConfigEntryType.INTEGER,
                default_value=AIRPLAY_SESSION_ESTABLISHMENT_LATENCY_DEFAULT_MS,
                range=(
                    AIRPLAY_SESSION_ESTABLISHMENT_LATENCY_MIN_MS,
                    AIRPLAY_SESSION_ESTABLISHMENT_LATENCY_MAX_MS,
                ),
                label="Expected milliseconds to establish streaming session with the AirPlay device.",
                description="Adjust this value only if playback is out of sync or does not work.\n"
                "The log will contain a WARNING entry showing a recommendation.",
                hidden=is_raop,
                category="protocol_generic",
                advanced=True,
            ),
        ]

        if is_broken_airplay_model(self.device_info.manufacturer, self.device_info.model):
            base_entries.insert(-1, BROKEN_AIRPLAY_WARN)

        if effective_protocol == StreamingProtocol.AIRPLAY2:
            # Insert the warning right after the protocol choice entry
            for i, entry in enumerate(base_entries):
                if entry.key == CONF_AIRPLAY_PROTOCOL:
                    base_entries.insert(
                        i + 1,
                        ConfigEntry(
                            key="AIRPLAY2_SYNC_WARN",
                            type=ConfigEntryType.ALERT,
                            default_value=None,
                            required=False,
                            label="Music Assistant support for the AirPlay2 protocol "
                            "does support audio synchronisation, but it is fragile. "
                            "If playback or synchronisation does not work, try adjusting the "
                            "session establishment latency. This is an interim advanced configuration "
                            "setting. It will be removed when a robust synchronisation method is implemented.",
                        ),
                    )
                    break

        return base_entries

    def _get_flags(self) -> int:
        # Flags are either present via "sf" or "flags". Taken from pyatv.protocols.airplay.utils.
        # We combine flags from both RAOP and AirPlay discovery services because
        # LEGACY_PAIRING_BIT (0x200) is typically only in the RAOP service sf field
        # (e.g. Apple TV HD), while PIN_REQUIRED (0x8) may only appear in the AirPlay
        # service sf/flags field. Using only one source misses the pairing requirement.
        flags = 0
        for discovery_info in filter(None, [self.raop_discovery_info, self.airplay_discovery_info]):
            raw = (
                discovery_info.properties.get(b"sf")
                or discovery_info.properties.get(b"flags")
                or b"0x0"
            )
            with contextlib.suppress(ValueError, TypeError):
                flags |= int(raw, 16)
        return flags

    def _requires_pin_pairing(self) -> bool:
        """Check if this device requires pairing.

        Adapted from pyatv.protocols.airplay.utils.get_pairing_requirement.
        """
        return bool(self._get_flags() & (LEGACY_PAIRING_BIT | PIN_REQUIRED))

    def _requires_password_pairing(self) -> bool:
        """Check if this device requires password authentication.

        Password can be used for pairing instead of interactive PIN entry.
        """
        return bool(self._get_flags() & PASSWORD_BIT)

    def _get_credentials_key(self, protocol: StreamingProtocol) -> str:
        """Get the config key for credentials for given protocol."""
        if protocol == StreamingProtocol.RAOP:
            return CONF_RAOP_CREDENTIALS
        return CONF_AIRPLAY_CREDENTIALS

    def _get_protocol_for_config_value(self, config_option: int) -> StreamingProtocol:
        if config_option == StreamingProtocol.AIRPLAY2:
            return StreamingProtocol.AIRPLAY2
        if config_option == StreamingProtocol.RAOP:
            return StreamingProtocol.RAOP
        # automatic selection
        if self.airplay_discovery_info and is_airplay2_preferred_model(
            self.device_info.manufacturer, self.device_info.model
        ):
            return StreamingProtocol.AIRPLAY2
        # Fall back to AirPlay 2 if RAOP service was not discovered
        if not self.raop_discovery_info and self.airplay_discovery_info:
            return StreamingProtocol.AIRPLAY2
        return StreamingProtocol.RAOP

    def _get_pairing_config_entries(
        self, values: dict[str, ConfigValueType] | None
    ) -> list[ConfigEntry]:
        """
        Return pairing config entries for Apple TV and macOS devices.

        Uses native pairing for both AirPlay 2 (HAP) and RAOP protocols.
        """
        self.logger.debug(f"_get_pairing_config_entries with values: {values}")
        entries: list[ConfigEntry] = []

        # Determine protocol name for UI
        conf_protocol: int = 0
        if values and (val := values.get(CONF_AIRPLAY_PROTOCOL)):
            conf_protocol = cast("int", val)
        else:
            conf_protocol = cast("int", self.config.get_value(CONF_AIRPLAY_PROTOCOL, 0) or 0)
        protocol = self._get_protocol_for_config_value(conf_protocol)
        protocol_name = "RAOP" if protocol == StreamingProtocol.RAOP else "AirPlay"
        protocol_key = (
            CONF_RAOP_CREDENTIALS
            if protocol == StreamingProtocol.RAOP
            else CONF_AIRPLAY_CREDENTIALS
        )
        has_creds_for_current_protocol = (
            values.get(protocol_key) if values else self.config.get_value(protocol_key)
        )
        self.logger.debug(
            f"Has credentials for {protocol_name}: {has_creds_for_current_protocol!s}"
        )

        if not has_creds_for_current_protocol:
            # If pairing was started, show PIN or password entry (depending on device configuration)
            if self._active_pairing and self._active_pairing.is_pairing:
                if self._requires_pin_pairing():
                    self.logger.debug(f"Device requires PIN pairing for {protocol_name}")
                    entries.append(
                        ConfigEntry(
                            key=CONF_PAIRING_PIN,
                            type=ConfigEntryType.STRING,
                            label="Enter the 4-digit PIN shown on the device",
                            required=True,
                            category="protocol_generic",
                        )
                    )
                    entries.append(
                        ConfigEntry(
                            key=CONF_ACTION_FINISH_PAIRING,
                            type=ConfigEntryType.ACTION,
                            label=f"Complete {protocol_name} pairing with the PIN",
                            action=CONF_ACTION_FINISH_PAIRING,
                            category="protocol_generic",
                        )
                    )
                elif self._requires_password_pairing():
                    self.logger.debug(f"Device requires password pairing for {protocol_name}")
                    entries.append(
                        ConfigEntry(
                            key=CONF_PAIRING_PASSWORD,
                            type=ConfigEntryType.SECURE_STRING,
                            required=True,
                            label="Enter the device password",
                            category="protocol_generic",
                        )
                    )
                    entries.append(
                        ConfigEntry(
                            key=CONF_ACTION_FINISH_PAIRING,
                            type=ConfigEntryType.ACTION,
                            label=f"Complete {protocol_name} pairing with the password",
                            action=CONF_ACTION_FINISH_PAIRING,
                            category="protocol_generic",
                        )
                    )
            else:
                # Show pairing instructions and start button
                self.logger.debug(
                    f"Device requires pairing for {protocol_name}, but no active pairing session"
                )
                entries.append(
                    ConfigEntry(
                        key="pairing_instructions",
                        type=ConfigEntryType.LABEL,
                        label=(
                            f"This device requires {protocol_name} pairing before it can be used. "
                            "Click the button below to start the pairing process."
                        ),
                        category="protocol_generic",
                    )
                )
                entries.append(
                    ConfigEntry(
                        key=CONF_ACTION_START_PAIRING,
                        type=ConfigEntryType.ACTION,
                        label=f"Start {protocol_name} pairing",
                        action=CONF_ACTION_START_PAIRING,
                        category="protocol_generic",
                    )
                )
        else:
            self.logger.debug(f"Device is already paired for {protocol_name}, showing reset option")
            # Show paired status
            entries.append(
                ConfigEntry(
                    key="pairing_status",
                    type=ConfigEntryType.LABEL,
                    label=f"Device is paired ({protocol_name}) and ready to use.",
                    category="protocol_generic",
                )
            )
            # Add reset pairing button
            entries.append(
                ConfigEntry(
                    key=CONF_ACTION_RESET_PAIRING,
                    type=ConfigEntryType.ACTION,
                    label=f"Reset {protocol_name} pairing",
                    action=CONF_ACTION_RESET_PAIRING,
                    category="protocol_generic",
                )
            )

        # Store credentials (hidden from UI)
        for protocol in (StreamingProtocol.RAOP, StreamingProtocol.AIRPLAY2):
            conf_key = self._get_credentials_key(protocol)
            entries.append(
                ConfigEntry(
                    key=conf_key,
                    type=ConfigEntryType.SECURE_STRING,
                    label=conf_key,
                    default_value=None,
                    value=values.get(conf_key) if values else None,
                    required=False,
                    hidden=True,
                    category="protocol_generic",
                )
            )
            if protocol is StreamingProtocol.AIRPLAY2:
                entries.append(
                    ConfigEntry(
                        key=CONF_AP2PASSWORD,
                        type=ConfigEntryType.SECURE_STRING,
                        label=CONF_AP2PASSWORD,
                        default_value=None,
                        value=values.get(CONF_PAIRING_PASSWORD) if values else None,
                        required=False,
                        hidden=True,
                        category="protocol_generic",
                    )
                )
        return entries

    async def _handle_pairing_action(
        self, action: str, values: dict[str, ConfigValueType] | None
    ) -> None:
        """
        Handle pairing actions.

        Uses native pairing for both AirPlay 2 (HAP) and RAOP protocols.
        Both produce credentials compatible with cliap2/cliraop respectively.
        """
        self.logger.debug(f"_handle_pairing_action with action: {action} and values: {values}")
        conf_protocol: int = 0
        if values and (val := values.get(CONF_AIRPLAY_PROTOCOL)):
            conf_protocol = cast("int", val)
        else:
            conf_protocol = cast("int", self.config.get_value(CONF_AIRPLAY_PROTOCOL, 0) or 0)
        protocol = self._get_protocol_for_config_value(conf_protocol)
        protocol_name = "RAOP" if protocol == StreamingProtocol.RAOP else "AirPlay"

        if action == CONF_ACTION_START_PAIRING:
            await self._reset_pairing(values, protocol, protocol_name)
            await self._start_pairing(protocol, protocol_name)
        elif action == CONF_ACTION_FINISH_PAIRING:
            await self._finish_pairing(values, protocol, protocol_name)
        elif action == CONF_ACTION_RESET_PAIRING:
            await self._reset_pairing(values, protocol, protocol_name)

    async def _start_pairing(self, protocol: StreamingProtocol, protocol_name: str) -> None:
        """Begin a new pairing session for the given protocol."""
        self.logger.debug(f"_start_pairing for protocol: {protocol_name}")
        if self._active_pairing and self._active_pairing.is_pairing:
            self.logger.warning("Pairing process already in progress for %s", self.display_name)
            return

        self.logger.info("Starting %s pairing for %s", protocol_name, self.display_name)

        from .pairing import AirPlayPairing  # noqa: PLC0415

        # Determine port based on protocol
        # Note: For Apple devices, pairing always happens on the AirPlay port (7000)
        # even when streaming will use RAOP. The RAOP port (5000) is only for streaming.
        port: int | None = None
        if self.airplay_discovery_info:
            port = self.airplay_discovery_info.port or 7000
        elif self.raop_discovery_info:
            # Fallback for devices without AirPlay service
            port = self.raop_discovery_info.port or 5000
        # Get the DACP ID from the provider - must match what cliap2 uses
        provider = cast("AirPlayProvider", self.provider)
        device_id = provider.dacp_id

        self._active_pairing = AirPlayPairing(
            address=self.address,
            name=self.display_name,
            protocol=protocol,
            logger=self.logger,
            port=port,
            device_id=device_id,
        )
        await self._active_pairing.start_pairing_session()

        if self._requires_pin_pairing():
            await self._active_pairing.start_pin_pairing()

    async def _finish_pairing(
        self,
        values: dict[str, ConfigValueType] | None,
        protocol: StreamingProtocol,
        protocol_name: str,
    ) -> None:
        """Complete an in-progress pairing session.

        ``values`` may contain a PIN or a password supplied by the user when required.
        """
        self.logger.debug(f"_finish_pairing for protocol: {protocol_name} with values: {values}")
        if not values:
            return
        pin = None
        if self._requires_pin_pairing():
            pin = values.get(CONF_PAIRING_PIN)
            if not pin:
                self.logger.warning("No PIN provided for pairing")
                return
        elif self._requires_password_pairing():
            pin = values.get(CONF_PAIRING_PASSWORD)
            if not pin:
                self.logger.warning("No password configured for pairing")
                return

        if not self._active_pairing:
            self.logger.warning(f"No active pairing session for {self.display_name}")
            return
        if not pin:
            self.logger.warning("No authentication method provided (PIN or password)")
            return
        credentials = await self._active_pairing.finish_pairing(pin=str(pin))
        self._active_pairing = None

        # Store credentials with the protocol-specific key
        cred_key = self._get_credentials_key(protocol)
        values[cred_key] = credentials

        self.logger.info(f"Finished {protocol_name} pairing for {self.display_name}")

    async def _reset_pairing(
        self,
        values: dict[str, ConfigValueType] | None,
        protocol: StreamingProtocol,
        protocol_name: str,
    ) -> None:
        """Clear stored credentials for the given protocol."""
        cred_key = self._get_credentials_key(protocol)
        self.logger.info(f"Resetting {protocol_name} pairing for {self.display_name}")
        if values is not None:
            values[cred_key] = None
            values[CONF_AP2PASSWORD] = None
        self.config.update({cred_key: None, CONF_AP2PASSWORD: None})

    async def stop(self) -> None:
        """Send STOP command to player."""
        async with self._lock:
            if self.stream and self.stream.session:
                # forward stop to the entire stream session
                await self.stream.session.stop()
            elif cast("AirPlayProvider", self.provider).bridge_manager.stop_streaming(
                self.player_id
            ):
                # Sendspin bridge active: trigger full bridge cleanup
                # which stops streaming, kills the CLI, and cancels writer tasks
                pass
            elif self.stream and self.stream.running:
                # Fallback: stop protocol directly
                await self.stream.stop(force=True)
                self.stream = None
            self._attr_current_media = None
            self.update_state()

    async def play(self) -> None:
        """Send PLAY (unpause) command to player."""
        async with self._lock:
            if self.stream and self.stream.running:
                await self.stream.send_cli_command("ACTION=PLAY")

    async def pause(self) -> None:
        """Send PAUSE command to player."""
        if self.group_members:
            # pause is not supported while synced, use stop instead
            self.logger.debug("Player is synced, using STOP instead of PAUSE")
            await self.stop()
            return

        async with self._lock:
            if not self.stream or not self.stream.running:
                return
            await self.stream.send_cli_command("ACTION=PAUSE")

    async def play_media(self, media: PlayerMedia) -> None:
        """Handle PLAY MEDIA on given player."""
        async with self._lock:
            if self.synced_to:
                # this should not happen, but guard anyways
                raise RuntimeError("Player is synced")
            self._attr_current_media = media

            # Always stop any existing stream
            if self.stream and self.stream.running and self.stream.session:
                # Set transitioning flag to ignore stale DACP messages (like prevent-playback)
                self._transitioning = True
                await self.stream.session.stop()
                self.stream = None

            # select audio source
            audio_source = self.mass.streams.get_stream(
                media, AIRPLAY_FLOW_PCM_FORMAT, self.player_id, use_flow_stream_buffering=True
            )

            # setup StreamSession for player (and its sync childs if any)
            sync_clients = self._get_sync_clients()
            provider = cast("AirPlayProvider", self.provider)
            stream_session = AirPlayStreamSession(provider, sync_clients, AIRPLAY_FLOW_PCM_FORMAT)
            await stream_session.start(audio_source)
            self._attr_elapsed_time = time.time() - stream_session.start_time
            self._attr_elapsed_time_last_updated = time.time()
            self._transitioning = False

    async def volume_set(self, volume_level: int) -> None:
        """Send VOLUME_SET command to given player."""
        if self.stream and self.stream.running and self.volume_muted is not True:
            await self.stream.send_cli_command(f"VOLUME={volume_level}")
        self._attr_volume_level = volume_level
        self.update_state()
        # store last state in playerconfig
        self.mass.config.set_raw_player_config_value(
            self.player_id, CONF_STORED_VOLUME, volume_level
        )

    async def volume_mute(self, muted: bool) -> None:
        """Handle VOLUME_MUTE command on the player."""
        self._attr_volume_muted = muted
        if self.stream and self.stream.running:
            volume = 0 if muted else (self.volume_level or 0)
            await self.stream.send_cli_command(f"VOLUME={volume}")
        self.update_state()

    async def set_members(
        self,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        """Handle SET_MEMBERS command on the player."""
        async with self._lock:
            if self.synced_to:
                # this should not happen, but guard anyways
                raise RuntimeError("Player is synced, cannot set members")
            if not player_ids_to_add and not player_ids_to_remove:
                # nothing to do
                return

            stream_session = (
                self.stream.session
                if self.stream and self.stream.running and self.stream.session
                else None
            )
            # handle removals first
            if player_ids_to_remove:
                if self.player_id in player_ids_to_remove:
                    if stream_session and len(stream_session.sync_clients) > 1:
                        # Other clients remain: remove only this leader client,
                        # session continues for remaining players (dynamic leader switch)
                        await stream_session.remove_client(self, reason="leader removed from group")
                    elif stream_session:
                        # Last client, stop the whole session
                        await stream_session.stop()
                    self._attr_group_members = []
                    self.update_state()
                    return

                for child_player in self._get_sync_clients():
                    if child_player.player_id in player_ids_to_remove:
                        # update group_members first to prevent race conditions
                        # where a concurrent play_media could re-include this player
                        if child_player.player_id in self._attr_group_members:
                            self._attr_group_members.remove(child_player.player_id)
                        if stream_session:
                            await stream_session.remove_client(
                                child_player, reason="child removed from group"
                            )
                        elif child_player.stream and child_player.stream.running:
                            # leader's stream is no longer running but child still has
                            # an active stream - stop it directly
                            await child_player.stream.stop(force=True)

                # If group leader is left alone after removals, clear the group_members list
                if (
                    self._attr_group_members
                    and len(self._attr_group_members) == 1
                    and self.player_id in self._attr_group_members
                ):
                    self._attr_group_members = []

            # handle additions
            for player_id in player_ids_to_add or []:
                if player_id == self.player_id or player_id in self.group_members:
                    # nothing to do: player is already part of the group
                    continue
                child_player_to_add: AirPlayPlayer | None = cast(
                    "AirPlayPlayer | None", self.mass.players.get_player(player_id)
                )
                if not child_player_to_add:
                    # should not happen, but guard against it
                    continue

                # ensure the child does not have an existing stream session active
                if child_player_to_add := cast(
                    "AirPlayPlayer | None", self.mass.players.get_player(player_id)
                ):
                    if (
                        child_player_to_add.playback_state == PlaybackState.PAUSED
                        and child_player_to_add.stream
                    ):
                        # Stop the paused stream to avoid a deadlock situation
                        await child_player_to_add.stream.stop()
                    if (
                        child_player_to_add.stream
                        and child_player_to_add.stream.running
                        and child_player_to_add.stream.session
                        and child_player_to_add.stream.session != stream_session
                    ):
                        await child_player_to_add.stream.session.remove_client(
                            child_player_to_add, reason="moving to different session"
                        )

                # add new child to the existing stream (RAOP or AirPlay2) session (if any)
                self._attr_group_members.append(player_id)
                if stream_session and child_player_to_add is not None:
                    # Skip add_client if the player is already streaming in this session
                    # (e.g. after a dynamic leader switch where the stream continues)
                    if child_player_to_add not in stream_session.sync_clients:
                        await stream_session.add_client(child_player_to_add)

            # Ensure group leader includes itself in group_members when it has members
            # This is required for the synced_to property to work correctly
            if self._attr_group_members and self.player_id not in self._attr_group_members:
                self._attr_group_members.insert(0, self.player_id)

            # always update the state after modifying group members
            self.update_state()

    def _on_player_media_updated(self) -> None:
        """Handle callback when the current media of the player is updated."""
        if not self.stream or not self.stream.running:
            return
        metadata = self.state.current_media
        if not metadata:
            return
        progress = int(metadata.corrected_elapsed_time or 0)
        self.mass.create_task(self.stream.send_metadata(progress, metadata))

    def update_volume_from_device(self, volume: int) -> None:
        """Update volume from device feedback."""
        ignore_volume_report = (
            self.config.get_value(CONF_IGNORE_VOLUME)
            or self.device_info.manufacturer.lower() == "apple"
        )

        if ignore_volume_report:
            return

        cur_volume = self.volume_level or 0
        if abs(cur_volume - volume) > 1 or (time.time() - self.last_command_sent) > 3:
            self.mass.create_task(self.volume_set(volume))
        else:
            self._attr_volume_level = volume
            self.mass.config.set_raw_player_config_value(self.player_id, CONF_STORED_VOLUME, volume)
            self.update_state()

    def set_discovery_info(self, discovery_info: AsyncServiceInfo, display_name: str) -> None:
        """Set/update the discovery info for the player."""
        self._attr_name = display_name
        if discovery_info.type == AIRPLAY_DISCOVERY_TYPE:
            self.airplay_discovery_info = discovery_info
        elif discovery_info.type == RAOP_DISCOVERY_TYPE:
            self.raop_discovery_info = discovery_info
        else:  # guard
            return
        cur_address = self.address
        prefer_ipv6 = ":" in str(self.mass.streams.publish_ip)
        new_address = get_primary_ip_address_from_zeroconf(discovery_info, prefer_ipv6=prefer_ipv6)
        if new_address is None:
            # should always be set, but guard against None
            return
        if cur_address != new_address:
            # Ignore mDNS updates that replace a routable address with a Docker bridge one.
            try:
                if (
                    cur_address
                    and ipaddress.ip_address(new_address) in _DOCKER_SUBNET
                    and ipaddress.ip_address(cur_address) not in _DOCKER_SUBNET
                ):
                    self.logger.warning(
                        "Ignoring mDNS update from %s to Docker address %s",
                        cur_address,
                        new_address,
                    )
                    self.update_state()
                    return
            except ValueError:
                pass
            self.logger.debug("Address updated from %s to %s", cur_address, new_address)
            self._attr_device_info.add_identifier(IdentifierType.IP_ADDRESS, new_address)
            self.address = new_address
        self.update_state()

    def set_state_from_stream(
        self,
        state: PlaybackState | None = None,
        elapsed_time: float | None = None,
        stream: AirPlayProtocol | None = None,
    ) -> None:
        """Set the playback state from stream (RAOP or AirPlay2).

        :param state: New playback state (or None to keep current).
        :param elapsed_time: New elapsed time (or None to keep current).
        :param stream: The stream instance sending this update (for validation).
        """
        # Ignore state updates from old/stale streams
        if stream is not None and stream != self.stream:
            return
        if state is not None:
            self._attr_playback_state = state
        if elapsed_time is not None:
            self._attr_elapsed_time = elapsed_time
            self._attr_elapsed_time_last_updated = time.time()
        self.update_state()

    def sync_volume_level(self) -> None:
        """
        Sync volume from parent player if needed.

        AirPlay players only report their volume level when we are actually streaming to them
        and we remember the last used/reported volume level in the player config by default
        but if we have a parent player, that may know better about the current volume level,
        so we try to sync from that parent player if possible
        """
        if (
            self.protocol_parent_id
            and (parent_player := self.mass.players.get_player(self.protocol_parent_id))
            and parent_player.state.volume_level is not None
        ):
            if self._has_native_protocol_parent:
                # Native parent volume is on the receiver/amplifier scale.
                # Keep the AirPlay child volume learned from DACP feedback instead.
                return
            if parent_player.state.volume_level == 0:
                # A parent volume of 0 usually means the (idle) sibling interface
                # feeding the parent doesn't know the real device volume, e.g. the
                # cast side of the same device reports 0 while in standby. Adopting
                # it would start the stream hard muted, so keep our own last known
                # volume instead.
                return
            if self._attr_volume_level == parent_player.state.volume_level:
                return
            self._attr_volume_level = parent_player.state.volume_level
            self.mass.config.set_raw_player_config_value(
                self.player_id, CONF_STORED_VOLUME, self._attr_volume_level
            )
            self.update_state()

    async def on_config_updated(self) -> None:
        """Handle logic when the player config is updated."""
        await super().on_config_updated()
        prov = cast("AirPlayProvider", self.provider)
        bridge_manager = prov.bridge_manager
        if bridge_manager.get_bridge(self.player_id) is None:
            await bridge_manager.setup_bridge(self)

    async def on_unload(self) -> None:
        """Handle logic when the player is unloaded from the Player controller."""
        await super().on_unload()
        if self.stream:
            # remove this player from the stream session if it is running
            if self.stream.running and self.stream.session:
                await self.stream.session.remove_client(self, reason="player unloaded")
            self.stream = None
        if self._active_pairing:
            await self._active_pairing.close()
            self._active_pairing = None

    @property
    def _has_native_protocol_parent(self) -> bool:
        """Return True if this AirPlay protocol player is linked to a native parent."""
        if not self.protocol_parent_id:
            return False
        parent_player = self.mass.players.get_player(self.protocol_parent_id)
        return bool(parent_player and parent_player.volume_control == PLAYER_CONTROL_NATIVE)

    def _get_sync_clients(self) -> list[AirPlayPlayer]:
        """Get all sync clients for a player."""
        sync_clients: list[AirPlayPlayer] = []
        # we need to return the player itself too
        group_child_ids = {self.player_id}
        group_child_ids.update(self.group_members)
        for child_id in group_child_ids:
            if client := cast("AirPlayPlayer | None", self.mass.players.get_player(child_id)):
                sync_clients.append(client)
        return sync_clients  # base don
