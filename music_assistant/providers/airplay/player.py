"""AirPlay Player implementations."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, cast

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption, ConfigValueType
from music_assistant_models.enums import (
    ConfigEntryType,
    IdentifierType,
    PlaybackState,
    PlayerFeature,
    PlayerType,
)

from music_assistant.constants import CONF_ENTRY_SYNC_ADJUST, create_sample_rates_config_entry
from music_assistant.helpers.util import is_valid_mac_address
from music_assistant.models.player import DeviceInfo, Player, PlayerMedia

from .constants import (
    AIRPLAY_DISCOVERY_TYPE,
    AIRPLAY_FLOW_PCM_FORMAT,
    AIRPLAY_OUTPUT_BUFFER_DURATION_MS,
    AIRPLAY_OUTPUT_BUFFER_MIN_DURATION_MS,
    BASE_PLAYER_FEATURES,
    BROKEN_AIRPLAY_WARN,
    CONF_ACTION_FINISH_PAIRING,
    CONF_ACTION_RESET_PAIRING,
    CONF_ACTION_START_PAIRING,
    CONF_AIRPLAY_CREDENTIALS,
    CONF_AIRPLAY_LATENCY,
    CONF_AIRPLAY_PROTOCOL,
    CONF_ALAC_ENCODE,
    CONF_ENCRYPTION,
    CONF_IGNORE_VOLUME,
    CONF_PAIRING_PIN,
    CONF_PASSWORD,
    CONF_RAOP_CREDENTIALS,
    CONF_STORED_VOLUME,
    FALLBACK_VOLUME,
    LEGACY_PAIRING_BIT,
    PIN_REQUIRED,
    RAOP_DISCOVERY_TYPE,
    StreamingProtocol,
)
from .helpers import (
    get_primary_ip_address_from_zeroconf,
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
        preferred_option = cast("int", self.config.get_value(CONF_AIRPLAY_PROTOCOL))
        return self._get_protocol_for_config_value(preferred_option)

    @property
    def available(self) -> bool:
        """Return if the player is currently available."""
        if self._requires_pairing():
            # check if we have credentials stored for the current protocol
            creds_key = self._get_credentials_key(self.protocol)
            if not self.config.get_value(creds_key):
                return False
        return super().available

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
    def output_buffer_duration_ms(self) -> int:
        """Get the configured output buffer duration in milliseconds."""
        return cast(
            "int",
            self.config.get_value(CONF_AIRPLAY_LATENCY, AIRPLAY_OUTPUT_BUFFER_MIN_DURATION_MS),
        )

    async def get_config_entries(
        self,
        action: str | None = None,
        values: dict[str, ConfigValueType] | None = None,
    ) -> list[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the given player (if any)."""
        base_entries: list[ConfigEntry] = []
        require_pairing = self._requires_pairing()

        # Handle pairing actions
        if action and require_pairing:
            await self._handle_pairing_action(action=action, values=values)

        # Add pairing config entries for Apple TV and macOS devices
        if require_pairing:
            base_entries = [*self._get_pairing_config_entries(values)]

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
                "In most cases the default automatic selection will work fine.",
                options=[
                    ConfigValueOption("Automatically select", 0),
                    ConfigValueOption("Prefer AirPlay 1 (RAOP)", StreamingProtocol.RAOP.value),
                    ConfigValueOption("Prefer AirPlay 2", StreamingProtocol.AIRPLAY2.value),
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
                hidden=self.protocol != StreamingProtocol.RAOP,
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
                hidden=self.protocol != StreamingProtocol.RAOP,
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
                hidden=self.protocol != StreamingProtocol.RAOP,
                category="protocol_generic",
                advanced=True,
            ),
            # airplay has fixed sample rate/bit depth so make this config entry static and hidden
            create_sample_rates_config_entry(
                supported_sample_rates=[44100], supported_bit_depths=[16], hidden=True
            ),
            ConfigEntry(
                key=CONF_AIRPLAY_LATENCY,
                type=ConfigEntryType.INTEGER,
                default_value=AIRPLAY_OUTPUT_BUFFER_MIN_DURATION_MS,
                range=(AIRPLAY_OUTPUT_BUFFER_MIN_DURATION_MS, AIRPLAY_OUTPUT_BUFFER_DURATION_MS),
                label="Milliseconds of data to buffer",
                description=(
                    "The number of milliseconds of data to buffer\n"
                    "NOTE: This adds to the latency experienced for commencement "
                    "of playback. \n"
                    "Try increasing value if playback is unreliable."
                ),
                category="protocol_generic",
                depends_on=CONF_AIRPLAY_PROTOCOL,
                depends_on_value=StreamingProtocol.AIRPLAY2.value,
                hidden=self.protocol != StreamingProtocol.AIRPLAY2,
                advanced=True,
            ),
        ]

        if is_broken_airplay_model(self.device_info.manufacturer, self.device_info.model):
            base_entries.insert(-1, BROKEN_AIRPLAY_WARN)

        return base_entries

    def _get_flags(self) -> int:
        # Flags are either present via "sf" or "flags. Taken from pyatv.protocols.airplay.utils"
        if self.airplay_discovery_info:
            properties = self.airplay_discovery_info.properties
        elif self.raop_discovery_info:
            properties = self.raop_discovery_info.properties
        else:
            return 0

        flags = properties.get(b"sf") or properties.get(b"flags") or "0x0"
        return int(flags, 16)

    def _requires_pairing(self) -> bool:
        """Check if this device requires pairing.

        Adapted from pyatv.protocols.airplay.utils.get_pairing_requirement.
        """
        return bool(self._get_flags() & (LEGACY_PAIRING_BIT | PIN_REQUIRED))

    def _get_credentials_key(self, protocol: StreamingProtocol) -> str:
        """Get the config key for credentials for given protocol."""
        if protocol == StreamingProtocol.RAOP:
            return CONF_RAOP_CREDENTIALS
        return CONF_AIRPLAY_CREDENTIALS

    def _get_protocol_for_config_value(self, config_option: int) -> StreamingProtocol:
        if config_option == StreamingProtocol.AIRPLAY2 and self.airplay_discovery_info:
            return StreamingProtocol.AIRPLAY2
        if config_option == StreamingProtocol.RAOP and self.raop_discovery_info:
            return StreamingProtocol.RAOP
        # automatic selection
        if self.airplay_discovery_info and is_airplay2_preferred_model(
            self.device_info.manufacturer, self.device_info.model
        ):
            return StreamingProtocol.AIRPLAY2
        return StreamingProtocol.RAOP

    def _get_pairing_config_entries(
        self, values: dict[str, ConfigValueType] | None
    ) -> list[ConfigEntry]:
        """
        Return pairing config entries for Apple TV and macOS devices.

        Uses native pairing for both AirPlay 2 (HAP) and RAOP protocols.
        """
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

        if not has_creds_for_current_protocol:
            # If pairing was started, show PIN entry
            if self._active_pairing and self._active_pairing.is_pairing:
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
            else:
                # Show pairing instructions and start button
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
        return entries

    async def _handle_pairing_action(
        self, action: str, values: dict[str, ConfigValueType] | None
    ) -> None:
        """
        Handle pairing actions.

        Uses native pairing for both AirPlay 2 (HAP) and RAOP protocols.
        Both produce credentials compatible with cliap2/cliraop respectively.
        """
        conf_protocol: int = 0
        if values and (val := values.get(CONF_AIRPLAY_PROTOCOL)):
            conf_protocol = cast("int", val)
        else:
            conf_protocol = cast("int", self.config.get_value(CONF_AIRPLAY_PROTOCOL, 0) or 0)
        protocol = self._get_protocol_for_config_value(conf_protocol)
        protocol_name = "RAOP" if protocol == StreamingProtocol.RAOP else "AirPlay"

        if action == CONF_ACTION_START_PAIRING:
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
            await self._active_pairing.start_pairing()

        elif action == CONF_ACTION_FINISH_PAIRING:
            if not values:
                return

            pin = values.get(CONF_PAIRING_PIN)
            if not pin:
                self.logger.warning("No PIN provided for pairing")
                return

            if not self._active_pairing:
                self.logger.warning("No active pairing session for %s", self.display_name)
                return

            credentials = await self._active_pairing.finish_pairing(pin=str(pin))
            self._active_pairing = None

            # Store credentials with the protocol-specific key
            cred_key = self._get_credentials_key(protocol)
            values[cred_key] = credentials

            self.logger.info("Finished %s pairing for %s", protocol_name, self.display_name)

        elif action == CONF_ACTION_RESET_PAIRING:
            cred_key = self._get_credentials_key(protocol)
            self.logger.info("Resetting %s pairing for %s", protocol_name, self.display_name)
            if values is not None:
                values[cred_key] = None

    async def stop(self) -> None:
        """Send STOP command to player."""
        if self.stream and self.stream.session:
            # forward stop to the entire stream session
            await self.stream.session.stop()
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
        audio_source = self.mass.streams.get_stream(media, AIRPLAY_FLOW_PCM_FORMAT, self.player_id)

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
        if self.stream and self.stream.running:
            await self.stream.send_cli_command(f"VOLUME={volume_level}")
        self._attr_volume_level = volume_level
        self.update_state()
        # store last state in playerconfig
        self.mass.config.set_raw_player_config_value(
            self.player_id, CONF_STORED_VOLUME, volume_level
        )

    async def set_members(
        self,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        """Handle SET_MEMBERS command on the player."""
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
                # dissolve the entire sync group
                if stream_session:
                    # stop the stream session if it is running
                    await stream_session.stop()
                self._attr_group_members = []
                self.update_state()
                return

            for child_player in self._get_sync_clients():
                if child_player.player_id in player_ids_to_remove:
                    if stream_session:
                        await stream_session.remove_client(child_player)
                    if child_player.player_id in self._attr_group_members:
                        self._attr_group_members.remove(child_player.player_id)

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
                    await child_player_to_add.stream.session.remove_client(child_player_to_add)

            # add new child to the existing stream (RAOP or AirPlay2) session (if any)
            self._attr_group_members.append(player_id)
            if stream_session and child_player_to_add is not None:
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
        if abs(cur_volume - volume) > 3 or (time.time() - self.last_command_sent) > 3:
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
        new_address = get_primary_ip_address_from_zeroconf(discovery_info)
        if new_address is None:
            # should always be set, but guard against None
            return
        if cur_address != new_address:
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
            if self._attr_volume_level == parent_player.state.volume_level:
                return
            self._attr_volume_level = parent_player.state.volume_level
            self.mass.config.set_raw_player_config_value(
                self.player_id, CONF_STORED_VOLUME, self._attr_volume_level
            )
            self.update_state()

    async def on_unload(self) -> None:
        """Handle logic when the player is unloaded from the Player controller."""
        await super().on_unload()
        if self.stream:
            # stop the stream session if it is running
            if self.stream.running and self.stream.session:
                self.mass.create_task(self.stream.session.stop())
            self.stream = None
        if self._active_pairing:
            await self._active_pairing.close()
            self._active_pairing = None

    def _get_sync_clients(self) -> list[AirPlayPlayer]:
        """Get all sync clients for a player."""
        sync_clients: list[AirPlayPlayer] = []
        # we need to return the player itself too
        group_child_ids = {self.player_id}
        group_child_ids.update(self.group_members)
        for child_id in group_child_ids:
            if client := cast("AirPlayPlayer | None", self.mass.players.get_player(child_id)):
                sync_clients.append(client)
        return sync_clients
