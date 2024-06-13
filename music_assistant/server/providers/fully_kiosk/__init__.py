"""FullyKiosk Player provider for Music Assistant."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from fullykiosk import FullyKiosk

from music_assistant.common.models.config_entries import (
    CONF_ENTRY_CROSSFADE,
    CONF_ENTRY_CROSSFADE_DURATION,
    CONF_ENTRY_ENFORCE_MP3,
    CONF_ENTRY_FLOW_MODE_ENFORCED,
    ConfigEntry,
    ConfigValueType,
)
from music_assistant.common.models.enums import (
    ConfigEntryType,
    PlayerFeature,
    PlayerState,
    PlayerType,
)
from music_assistant.common.models.errors import PlayerUnavailableError, SetupFailedError
from music_assistant.common.models.player import DeviceInfo, Player, PlayerMedia
from music_assistant.constants import CONF_IP_ADDRESS, CONF_PASSWORD, CONF_PORT, VERBOSE_LOG_LEVEL
from music_assistant.server.models.player_provider import PlayerProvider

if TYPE_CHECKING:
    from music_assistant.common.models.config_entries import ProviderConfig
    from music_assistant.common.models.provider import ProviderManifest
    from music_assistant.server import MusicAssistant
    from music_assistant.server.models import ProviderInstanceType

AUDIOMANAGER_STREAM_MUSIC = 3
CONF_ENFORCE_MP3 = "enforce_mp3"


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    prov = FullyKioskProvider(mass, manifest, config)
    await prov.handle_async_init()
    return prov


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    instance_id: id of an existing provider instance (None if new instance setup).
    action: [optional] action key called from config entries UI.
    values: the (intermediate) raw values for config entries sent with the action.
    """
    # ruff: noqa: ARG001
    return (
        ConfigEntry(
            key=CONF_IP_ADDRESS,
            type=ConfigEntryType.STRING,
            label="IP-Address (or hostname) of the device running Fully Kiosk/app.",
            required=True,
        ),
        ConfigEntry(
            key=CONF_PASSWORD,
            type=ConfigEntryType.SECURE_STRING,
            label="Password to use to connect to the Fully Kiosk API.",
            required=True,
        ),
        ConfigEntry(
            key=CONF_PORT,
            type=ConfigEntryType.STRING,
            default_value="2323",
            label="Port to use to connect to the Fully Kiosk API (default is 2323).",
            required=True,
            category="advanced",
        ),
    )


class FullyKioskProvider(PlayerProvider):
    """Player provider for FullyKiosk based players."""

    _fully: FullyKiosk

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self._fully = FullyKiosk(
            self.mass.http_session,
            self.config.get_value(CONF_IP_ADDRESS),
            self.config.get_value(CONF_PORT),
            self.config.get_value(CONF_PASSWORD),
        )
        try:
            async with asyncio.timeout(15):
                await self._fully.getDeviceInfo()
        except Exception as err:
            msg = f"Unable to start the FullyKiosk connection ({err!s}"
            raise SetupFailedError(msg) from err
        # set-up fullykiosk logging
        if self.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
            logging.getLogger("fullykiosk").setLevel(logging.DEBUG)
        else:
            logging.getLogger("fullykiosk").setLevel(self.logger.level + 10)

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        # Add FullyKiosk device to Player controller.
        player_id = self._fully.deviceInfo["deviceID"]
        player = self.mass.players.get(player_id, raise_unavailable=False)
        address = (
            f"http://{self.config.get_value(CONF_IP_ADDRESS)}:{self.config.get_value(CONF_PORT)}"
        )
        if not player:
            player = Player(
                player_id=player_id,
                provider=self.instance_id,
                type=PlayerType.PLAYER,
                name=self._fully.deviceInfo["deviceName"],
                available=True,
                powered=False,
                device_info=DeviceInfo(
                    model=self._fully.deviceInfo["deviceModel"],
                    manufacturer=self._fully.deviceInfo["deviceManufacturer"],
                    address=address,
                ),
                supported_features=(PlayerFeature.VOLUME_SET,),
                needs_poll=True,
                poll_interval=10,
            )
        self.mass.players.register_or_update(player)
        self._handle_player_update()

    def _handle_player_update(self) -> None:
        """Update FullyKiosk player attributes."""
        player_id = self._fully.deviceInfo["deviceID"]
        player = self.mass.players.get(player_id)
        player.name = self._fully.deviceInfo["deviceName"]
        # player.volume_level = snap_client.volume
        for volume_dict in self._fully.deviceInfo.get("audioVolumes", []):
            if str(AUDIOMANAGER_STREAM_MUSIC) in volume_dict:
                volume = volume_dict[str(AUDIOMANAGER_STREAM_MUSIC)]
                player.volume_level = volume
                break
        current_url = self._fully.deviceInfo.get("soundUrlPlaying")
        player.current_item_id = current_url
        if not current_url:
            player.state = PlayerState.IDLE
        player.available = True
        self.mass.players.update(player_id)

    async def get_player_config_entries(self, player_id: str) -> tuple[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the given player (if any)."""
        base_entries = await super().get_player_config_entries(player_id)
        return (
            *base_entries,
            CONF_ENTRY_FLOW_MODE_ENFORCED,
            CONF_ENTRY_CROSSFADE,
            CONF_ENTRY_CROSSFADE_DURATION,
            CONF_ENTRY_ENFORCE_MP3,
        )

    async def cmd_volume_set(self, player_id: str, volume_level: int) -> None:
        """Send VOLUME_SET command to given player."""
        player = self.mass.players.get(player_id, raise_unavailable=False)
        await self._fully.setAudioVolume(volume_level, AUDIOMANAGER_STREAM_MUSIC)
        player.volume_level = volume_level
        self.mass.players.update(player_id)

    async def cmd_stop(self, player_id: str) -> None:
        """Send STOP command to given player."""
        player = self.mass.players.get(player_id, raise_unavailable=False)
        await self._fully.stopSound()
        player.state = PlayerState.IDLE
        self.mass.players.update(player_id)

    async def play_media(
        self,
        player_id: str,
        media: PlayerMedia,
    ) -> None:
        """Handle PLAY MEDIA on given player."""
        player = self.mass.players.get(player_id)
        if self.mass.config.get_raw_player_config_value(player_id, CONF_ENFORCE_MP3, False):
            media.uri = media.uri.replace(".flac", ".mp3")
        await self._fully.playSound(media.uri, AUDIOMANAGER_STREAM_MUSIC)
        player.current_media = media
        player.elapsed_time = 0
        player.elapsed_time_last_updated = time.time()
        player.state = PlayerState.PLAYING
        self.mass.players.update(player_id)

    async def poll_player(self, player_id: str) -> None:
        """Poll player for state updates."""
        try:
            async with asyncio.timeout(15):
                await self._fully.getDeviceInfo()
                self._handle_player_update()
        except Exception as err:
            msg = f"Unable to start the FullyKiosk connection ({err!s}"
            raise PlayerUnavailableError(msg) from err
