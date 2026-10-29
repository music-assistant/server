"""Auracast Bluetooth LE Audio player provider."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from music_assistant.models.player_provider import PlayerProvider

from .constants import (
    CONF_BAUDRATE,
    CONF_BROADCAST_NAME,
    CONF_MANUFACTURER_DATA,
    CONF_SERIAL_PORT,
    DEFAULT_BAUDRATE,
    DEFAULT_BROADCAST_NAME,
    MAX_BAUDRATE,
    MIN_BAUDRATE,
)
from .player import AuracastPlayer
from .validators import validate_baudrate, validate_manufacturer_data

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


class AuracastProvider(PlayerProvider):
    """Provider that broadcasts one MA player over an Auracast controller."""

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return the controller and broadcast settings."""
        return (
            ConfigEntry(
                key=CONF_SERIAL_PORT,
                type=ConfigEntryType.STRING,
                required=True,
                default_value=str(self.get_setup_value(CONF_SERIAL_PORT, "") or ""),
                requires_reload=True,
            ),
            ConfigEntry(
                key=CONF_BAUDRATE,
                type=ConfigEntryType.INTEGER,
                required=True,
                default_value=int(
                    cast("int | str", self.get_setup_value(CONF_BAUDRATE, DEFAULT_BAUDRATE))
                ),
                range=(MIN_BAUDRATE, MAX_BAUDRATE),
                validate=validate_baudrate,
                requires_reload=True,
            ),
            ConfigEntry(
                key=CONF_BROADCAST_NAME,
                type=ConfigEntryType.STRING,
                required=True,
                default_value=str(
                    self.get_setup_value(CONF_BROADCAST_NAME, DEFAULT_BROADCAST_NAME)
                    or DEFAULT_BROADCAST_NAME
                ),
                requires_reload=True,
            ),
            ConfigEntry(
                key=CONF_MANUFACTURER_DATA,
                type=ConfigEntryType.STRING,
                required=False,
                default_value=str(self.get_setup_value(CONF_MANUFACTURER_DATA, "") or ""),
                validate=validate_manufacturer_data,
                requires_reload=True,
            ),
        )

    async def discover_players(self) -> None:
        """Register the controller as the provider's single player."""
        player = AuracastPlayer(self, f"auracast_{self.instance_id}")
        await self.mass.players.register(player)
        await player.start_broadcast()

    async def unload(self, is_removed: bool = False) -> None:
        """Unregister the player and release any active Bluetooth transport."""
        for player in self.players:
            await self.mass.players.unregister(player.player_id)


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize the Auracast player provider."""
    return AuracastProvider(mass, manifest, config, set())
