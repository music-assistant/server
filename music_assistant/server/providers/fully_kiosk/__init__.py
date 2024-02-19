"""FullyKiosk Player provider for Music Assistant."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from fullykiosk import FullyKiosk

from music_assistant.common.models.config_entries import (
    CONF_ENTRY_CROSSFADE,
    CONF_ENTRY_CROSSFADE_DURATION,
    ConfigEntry,
    ConfigValueType,
)
from music_assistant.common.models.enums import (
    ConfigEntryType,
    ContentType,
    PlayerFeature,
    PlayerState,
    PlayerType,
)
from music_assistant.common.models.errors import PlayerUnavailableError, SetupFailedError
from music_assistant.common.models.player import DeviceInfo, Player
from music_assistant.constants import CONF_IP_ADDRESS, CONF_PASSWORD, CONF_PORT
from music_assistant.server.models.player_provider import PlayerProvider

if TYPE_CHECKING:
    from music_assistant.common.models.config_entries import ProviderConfig
    from music_assistant.common.models.provider import ProviderManifest
    from music_assistant.common.models.queue_item import QueueItem
    from music_assistant.server import MusicAssistant
    from music_assistant.server.controllers.streams import MultiClientStreamJob
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
            type=ConfigEntryType.STRING,
            label="Password to use to connect to the Fully Kiosk API.",
            required=True,
        ),
        ConfigEntry(
            key=CONF_PORT,
            type=ConfigEntryType.STRING,
            default_value="2323",
            label="Port to use to connect to the Fully Kiosk API (default is 2323).",
            required=True,
            advanced=True,
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
        player.current_item_id = self._fully.deviceInfo.get("soundUrlPlaying")
        player.available = True
        self.mass.players.update(player_id)

    async def get_player_config_entries(self, player_id: str) -> tuple[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the given player (if any)."""
        base_entries = await super().get_player_config_entries(player_id)
        return (
            *base_entries,
            CONF_ENTRY_CROSSFADE,
            CONF_ENTRY_CROSSFADE_DURATION,
            ConfigEntry(
                key=CONF_ENFORCE_MP3,
                type=ConfigEntryType.BOOLEAN,
                label="Enforce (lossy) mp3 stream",
                default_value=False,
                description="By default, Music Assistant sends lossless, high quality audio "
                "to all players. Some devices can not deal with that and require "
                "the stream to be packed into a lossy mp3 codec. Only enable when needed.",
                advanced=True,
            ),
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
        queue_item: QueueItem,
        seek_position: int,
        fade_in: bool,
    ) -> None:
        """Handle PLAY MEDIA on given player.

        This is called by the Queue controller to start playing a queue item on the given player.
        The provider's own implementation should work out how to handle this request.

            - player_id: player_id of the player to handle the command.
            - queue_item: The QueueItem that needs to be played on the player.
            - seek_position: Optional seek to this position.
            - fade_in: Optionally fade in the item at playback start.
        """
        player = self.mass.players.get(player_id)
        enforce_mp3 = await self.mass.config.get_player_config_value(player_id, CONF_ENFORCE_MP3)
        url = await self.mass.streams.resolve_stream_url(
            queue_item=queue_item,
            output_codec=ContentType.MP3 if enforce_mp3 else ContentType.FLAC,
            seek_position=seek_position,
            fade_in=fade_in,
            flow_mode=True,
        )
        await self._fully.playSound(url, AUDIOMANAGER_STREAM_MUSIC)
        player.current_item_id = queue_item.queue_id
        player.elapsed_time = 0
        player.elapsed_time_last_updated = time.time()
        player.state = PlayerState.PLAYING
        self.mass.players.update(player_id)

    async def play_stream(self, player_id: str, stream_job: MultiClientStreamJob) -> None:
        """Handle PLAY STREAM on given player.

        This is a special feature from the Universal Group provider.
        """
        player = self.mass.players.get(player_id)
        enforce_mp3 = await self.mass.config.get_player_config_value(player_id, CONF_ENFORCE_MP3)
        output_codec = ContentType.MP3 if enforce_mp3 else ContentType.FLAC
        url = stream_job.resolve_stream_url(player_id, output_codec)
        await self._fully.playSound(url, AUDIOMANAGER_STREAM_MUSIC)
        player.current_item_id = player_id
        player.elapsed_time = 0
        player.elapsed_time_last_updated = time.time()
        player.state = PlayerState.PLAYING
        self.mass.players.update(player_id)

    async def poll_player(self, player_id: str) -> None:
        """Poll player for state updates.

        This is called by the Player Manager;
        - every 360 seconds if the player if not powered
        - every 30 seconds if the player is powered
        - every 10 seconds if the player is playing

        Use this method to request any info that is not automatically updated and/or
        to detect if the player is still alive.
        If this method raises the PlayerUnavailable exception,
        the player is marked as unavailable until
        the next successful poll or event where it becomes available again.
        If the player does not need any polling, simply do not override this method.
        """
        try:
            async with asyncio.timeout(15):
                await self._fully.getDeviceInfo()
                self._handle_player_update()
        except Exception as err:
            msg = f"Unable to start the FullyKiosk connection ({err!s}"
            raise PlayerUnavailableError(msg) from err
