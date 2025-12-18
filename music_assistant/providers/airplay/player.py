"""AirPlay Player implementations."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, cast

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption, ConfigValueType
from music_assistant_models.enums import ConfigEntryType, PlaybackState, PlayerFeature, PlayerType
from propcache import under_cached_property as cached_property

from music_assistant.constants import (
    CONF_ENTRY_DEPRECATED_EQ_BASS,
    CONF_ENTRY_DEPRECATED_EQ_MID,
    CONF_ENTRY_DEPRECATED_EQ_TREBLE,
    CONF_ENTRY_FLOW_MODE_ENFORCED,
    CONF_ENTRY_OUTPUT_CODEC_HIDDEN,
    CONF_ENTRY_SYNC_ADJUST,
    create_sample_rates_config_entry,
)
from music_assistant.models.player import DeviceInfo, Player, PlayerMedia

from .constants import (
    AIRPLAY_DISCOVERY_TYPE,
    AIRPLAY_FLOW_PCM_FORMAT,
    CACHE_CATEGORY_PREV_VOLUME,
    CONF_ACTION_FINISH_PAIRING,
    CONF_ACTION_START_PAIRING,
    CONF_AIRPLAY_PROTOCOL,
    CONF_ALAC_ENCODE,
    CONF_AP_CREDENTIALS,
    CONF_ENCRYPTION,
    CONF_IGNORE_VOLUME,
    CONF_PAIRING_PIN,
    CONF_PASSWORD,
    FALLBACK_VOLUME,
    RAOP_DISCOVERY_TYPE,
    StreamingProtocol,
)
from .helpers import (
    get_primary_ip_address_from_zeroconf,
    is_airplay2_preferred_model,
    is_broken_airplay_model,
)
from .stream_session import AirPlayStreamSession

if TYPE_CHECKING:
    from zeroconf.asyncio import AsyncServiceInfo

    from .protocols.airplay2 import AirPlay2Stream
    from .protocols.raop import RaopStream
    from .provider import AirPlayProvider


BROKEN_AIRPLAY_WARN = ConfigEntry(
    key="BROKEN_AIRPLAY",
    type=ConfigEntryType.ALERT,
    default_value=None,
    required=False,
    label="This player is known to have broken AirPlay support. "
    "Playback may fail or simply be silent. "
    "There is no workaround for this issue at the moment. \n"
    "If you already enforced AirPlay 2 on the player and it remains silent, "
    "this is one of the known broken models. Only remedy is to nag the manufacturer for a fix.",
)


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
        super().__init__(provider, player_id)
        self.raop_discovery_info = raop_discovery_info
        self.airplay_discovery_info = airplay_discovery_info
        self.address = address
        self.stream: RaopStream | AirPlay2Stream | None = None
        self.last_command_sent = 0.0
        self._lock = asyncio.Lock()
        # Set (static) player attributes
        self._attr_type = PlayerType.PLAYER
        self._attr_name = display_name
        self._attr_available = True
        self._attr_device_info = DeviceInfo(
            model=model,
            manufacturer=manufacturer,
            ip_address=address,
        )
        self._attr_supported_features = {
            PlayerFeature.PAUSE,
            PlayerFeature.SET_MEMBERS,
            PlayerFeature.MULTI_DEVICE_DSP,
            PlayerFeature.VOLUME_SET,
        }
        self._attr_volume_level = initial_volume
        self._attr_can_group_with = {provider.instance_id}
        self._attr_enabled_by_default = not is_broken_airplay_model(manufacturer, model)

    @cached_property
    def protocol(self) -> StreamingProtocol:
        """Get the streaming protocol to use for this player."""
        protocol_value = self.config.get_value(CONF_AIRPLAY_PROTOCOL)
        # Convert integer value to StreamingProtocol enum
        if protocol_value == 2:
            return StreamingProtocol.AIRPLAY2
        return StreamingProtocol.RAOP

    @property
    def available(self) -> bool:
        """Return if the player is currently available."""
        if self._requires_pairing():
            # check if we have credentials stored
            credentials = self.config.get_value(CONF_AP_CREDENTIALS)
            if not credentials:
                return False
        return super().available

    @property
    def corrected_elapsed_time(self) -> float:
        """Return the corrected elapsed time accounting for stream session restarts."""
        if not self.stream or not self.stream.session:
            return super().corrected_elapsed_time or 0.0
        return time.time() - self.stream.session.last_stream_started

    async def get_config_entries(
        self,
        action: str | None = None,
        values: dict[str, ConfigValueType] | None = None,
    ) -> list[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the given player (if any)."""
        base_entries = await super().get_config_entries()

        require_pairing = self._requires_pairing()

        # Handle pairing actions
        if action and require_pairing:
            await self._handle_pairing_action(action=action, values=values)

        # Add pairing config entries for Apple TV and macOS devices
        if require_pairing:
            base_entries = [*self._get_pairing_config_entries(values), *base_entries]

        # Regular AirPlay config entries
        base_entries += [
            CONF_ENTRY_FLOW_MODE_ENFORCED,
            CONF_ENTRY_DEPRECATED_EQ_BASS,
            CONF_ENTRY_DEPRECATED_EQ_MID,
            CONF_ENTRY_DEPRECATED_EQ_TREBLE,
            CONF_ENTRY_OUTPUT_CODEC_HIDDEN,
            ConfigEntry(
                key=CONF_AIRPLAY_PROTOCOL,
                type=ConfigEntryType.INTEGER,
                required=False,
                label="AirPlay version to use for streaming",
                description="AirPlay version 1 protocol uses RAOP.\n"
                "AirPlay version 2 is an extension of RAOP.\n"
                "Some newer devices do not fully support RAOP and "
                "will only work with AirPlay version 2.",
                category="airplay",
                options=[
                    ConfigValueOption("AirPlay 1 (RAOP)", StreamingProtocol.RAOP.value),
                    ConfigValueOption("AirPlay 2", StreamingProtocol.AIRPLAY2.value),
                ],
                default_value=StreamingProtocol.AIRPLAY2.value
                if is_airplay2_preferred_model(
                    self.device_info.manufacturer, self.device_info.model
                )
                else StreamingProtocol.RAOP.value,
            ),
            ConfigEntry(
                key=CONF_ENCRYPTION,
                type=ConfigEntryType.BOOLEAN,
                default_value=True,
                label="Enable encryption",
                description="Enable encrypted communication with the player, "
                "some (3rd party) players require this to be disabled.",
                category="airplay",
                depends_on=CONF_AIRPLAY_PROTOCOL,
                depends_on_value=StreamingProtocol.RAOP.value,
            ),
            ConfigEntry(
                key=CONF_ALAC_ENCODE,
                type=ConfigEntryType.BOOLEAN,
                default_value=True,
                label="Enable compression",
                description="Save some network bandwidth by sending the audio as "
                "(lossless) ALAC at the cost of a bit of CPU.",
                category="airplay",
                depends_on=CONF_AIRPLAY_PROTOCOL,
                depends_on_value=StreamingProtocol.RAOP.value,
            ),
            CONF_ENTRY_SYNC_ADJUST,
            ConfigEntry(
                key=CONF_PASSWORD,
                type=ConfigEntryType.SECURE_STRING,
                default_value=None,
                required=False,
                label="Device password",
                description="Some devices require a password to connect/play.",
                category="airplay",
            ),
            # airplay has fixed sample rate/bit depth so make this config entry static and hidden
            create_sample_rates_config_entry(
                supported_sample_rates=[44100], supported_bit_depths=[16], hidden=True
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
                category="airplay",
                depends_on=CONF_AIRPLAY_PROTOCOL,
                depends_on_value=StreamingProtocol.RAOP.value,
            ),
        ]

        if is_broken_airplay_model(self.device_info.manufacturer, self.device_info.model):
            base_entries.insert(-1, BROKEN_AIRPLAY_WARN)

        return base_entries

    def _requires_pairing(self) -> bool:
        """Check if this device requires pairing (Apple TV or macOS)."""
        if self.device_info.manufacturer.lower() != "apple":
            return False

        model = self.device_info.model
        # Apple TV devices
        if "appletv" in model.lower() or "apple tv" in model.lower():
            return True
        # Mac devices (including iMac, MacBook, Mac mini, Mac Pro, Mac Studio)
        return model.startswith(("Mac", "iMac"))

    def _get_pairing_config_entries(
        self, values: dict[str, ConfigValueType] | None
    ) -> list[ConfigEntry]:
        """Return pairing config entries for Apple TV and macOS devices.

        Uses cliraop for AirPlay/RAOP pairing.
        """
        entries: list[ConfigEntry] = []

        # Check if we have credentials stored
        if values and (creds := values.get(CONF_AP_CREDENTIALS)):
            credentials = str(creds)
        else:
            credentials = str(self.config.get_value(CONF_AP_CREDENTIALS) or "")
        has_credentials = bool(credentials)

        if not has_credentials:
            # Show pairing instructions and start button
            if not self.stream and self.protocol == StreamingProtocol.RAOP:
                # ensure we have a stream instance to track pairing state
                from .protocols.raop import RaopStream  # noqa: PLC0415

                self.stream = RaopStream(self)
            elif not self.stream and self.protocol == StreamingProtocol.AIRPLAY2:
                # ensure we have a stream instance to track pairing state
                from .protocols.airplay2 import AirPlay2Stream  # noqa: PLC0415

                self.stream = AirPlay2Stream(self)
            if self.stream and not self.stream.supports_pairing:
                # TEMP until ap2 pairing is implemented
                return [
                    ConfigEntry(
                        key="pairing_unsupported",
                        type=ConfigEntryType.ALERT,
                        label=(
                            "This device requires pairing but it is not supported "
                            "by the current Music Assistant AirPlay implementation."
                        ),
                    )
                ]

            # If pairing was started, show PIN entry
            if self.stream and self.stream.is_pairing:
                entries.append(
                    ConfigEntry(
                        key=CONF_PAIRING_PIN,
                        type=ConfigEntryType.STRING,
                        label="Enter the 4-digit PIN shown on the device",
                        required=True,
                    )
                )
                entries.append(
                    ConfigEntry(
                        key=CONF_ACTION_FINISH_PAIRING,
                        type=ConfigEntryType.ACTION,
                        label="Complete the pairing process with the PIN",
                        action=CONF_ACTION_FINISH_PAIRING,
                    )
                )
            else:
                entries.append(
                    ConfigEntry(
                        key="pairing_instructions",
                        type=ConfigEntryType.LABEL,
                        label=(
                            "This device requires pairing before it can be used. "
                            "Click the button below to start the pairing process."
                        ),
                    )
                )
                entries.append(
                    ConfigEntry(
                        key=CONF_ACTION_START_PAIRING,
                        type=ConfigEntryType.ACTION,
                        label="Start the AirPlay pairing process",
                        action=CONF_ACTION_START_PAIRING,
                    )
                )
        else:
            # Show paired status
            entries.append(
                ConfigEntry(
                    key="pairing_status",
                    type=ConfigEntryType.LABEL,
                    label="Device is paired and ready to use.",
                )
            )

        # Store credentials (hidden from UI)
        entries.append(
            ConfigEntry(
                key=CONF_AP_CREDENTIALS,
                type=ConfigEntryType.SECURE_STRING,
                label="AirPlay Credentials",
                default_value=credentials,
                value=credentials,
                required=False,
                hidden=True,
            )
        )

        return entries

    async def _handle_pairing_action(
        self, action: str, values: dict[str, ConfigValueType] | None
    ) -> None:
        """Handle pairing actions using the configured protocol."""
        if not self.stream and self.protocol == StreamingProtocol.RAOP:
            # ensure we have a stream instance to track pairing state
            from .protocols.raop import RaopStream  # noqa: PLC0415

            self.stream = RaopStream(self)
        elif not self.stream and self.protocol == StreamingProtocol.AIRPLAY2:
            # ensure we have a stream instance to track pairing state
            from .protocols.airplay2 import AirPlay2Stream  # noqa: PLC0415

            self.stream = AirPlay2Stream(self)
        if action == CONF_ACTION_START_PAIRING:
            if self.stream and self.stream.is_pairing:
                self.logger.warning("Pairing process already in progress for %s", self.display_name)
                return
            self.logger.info("Started AirPlay pairing for %s", self.display_name)
            if self.stream:
                await self.stream.start_pairing()

        elif action == CONF_ACTION_FINISH_PAIRING:
            if not values:
                # guard
                return

            pin = values.get(CONF_PAIRING_PIN)
            if not pin:
                self.logger.warning("No PIN provided for pairing")
                return

            if self.stream:
                credentials = await self.stream.finish_pairing(pin=str(pin))
            else:
                return

            values[CONF_AP_CREDENTIALS] = credentials

            self.logger.info(
                "Finished AirPlay pairing for %s",
                self.display_name,
            )

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
                await self.stream.send_cli_command("ACTION=PLAY\n")

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
            await self.stream.send_cli_command("ACTION=PAUSE\n")

    async def play_media(self, media: PlayerMedia) -> None:
        """Handle PLAY MEDIA on given player."""
        if self.synced_to:
            # this should not happen, but guard anyways
            raise RuntimeError("Player is synced")
        self._attr_current_media = media

        # select audio source
        audio_source = self.mass.streams.get_stream(media, AIRPLAY_FLOW_PCM_FORMAT)

        # if an existing stream session is running, we could replace it with the new stream
        if self.stream and self.stream.running:
            # check if we need to replace the stream
            if self.stream.prevent_playback:
                # player is in prevent playback mode, we need to stop the stream
                await self.stop()
            elif self.stream.session:
                await self.stream.session.replace_stream(audio_source)
                return

        # setup StreamSession for player (and its sync childs if any)
        sync_clients = self._get_sync_clients()
        provider = cast("AirPlayProvider", self.provider)
        stream_session = AirPlayStreamSession(provider, sync_clients, AIRPLAY_FLOW_PCM_FORMAT)
        await stream_session.start(audio_source)

    async def volume_set(self, volume_level: int) -> None:
        """Send VOLUME_SET command to given player."""
        if self.stream and self.stream.running:
            await self.stream.send_cli_command(f"VOLUME={volume_level}\n")
        self._attr_volume_level = volume_level
        self.update_state()
        # store last state in cache
        await self.mass.cache.set(
            key=self.player_id,
            data=volume_level,
            provider=self.provider.instance_id,
            category=CACHE_CATEGORY_PREV_VOLUME,
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

        # handle additions
        for player_id in player_ids_to_add or []:
            if player_id == self.player_id or player_id in self.group_members:
                # nothing to do: player is already part of the group
                continue
            child_player_to_add: AirPlayPlayer | None = cast(
                "AirPlayPlayer | None", self.mass.players.get(player_id)
            )
            if not child_player_to_add:
                # should not happen, but guard against it
                continue
            if child_player_to_add.synced_to and child_player_to_add.synced_to != self.player_id:
                raise RuntimeError("Player is already synced to another player")

            # ensure the child does not have an existing stream session active
            if child_player_to_add := cast(
                "AirPlayPlayer | None", self.mass.players.get(player_id)
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
            if stream_session:
                await stream_session.add_client(child_player_to_add)

        # always update the state after modifying group members
        self.update_state()

    def _on_player_media_updated(self) -> None:
        """Handle callback when the current media of the player is updated."""
        if not self.stream or not self.stream.running or not self.stream.session:
            return
        metadata = self.current_media
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
            self.address = cur_address
            self._attr_device_info.ip_address = new_address
        self.update_state()

    def set_state_from_stream(
        self, state: PlaybackState | None = None, elapsed_time: float | None = None
    ) -> None:
        """Set the playback state from stream (RAOP or AirPlay2)."""
        if state is not None:
            self._attr_playback_state = state
        if elapsed_time is not None:
            self._attr_elapsed_time = elapsed_time
            self._attr_elapsed_time_last_updated = time.time()
        self.update_state()

    async def on_unload(self) -> None:
        """Handle logic when the player is unloaded from the Player controller."""
        await super().on_unload()
        if self.stream:
            # stop the stream session if it is running
            if self.stream.running and self.stream.session:
                self.mass.create_task(self.stream.session.stop())
            self.stream = None

    def _get_sync_clients(self) -> list[AirPlayPlayer]:
        """Get all sync clients for a player."""
        sync_clients: list[AirPlayPlayer] = []
        # we need to return the player itself too
        group_child_ids = {self.player_id}
        group_child_ids.update(self.group_members)
        for child_id in group_child_ids:
            if client := cast("AirPlayPlayer | None", self.mass.players.get(child_id)):
                sync_clients.append(client)
        return sync_clients
