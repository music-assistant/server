"""Handle Config related endpoints for Music Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from music_assistant.common.models.config_entries import (
    ConfigEntry,
    ConfigValueType,
    CoreConfig,
    PlayerConfig,
    ProviderConfig,
)
from music_assistant.common.models.enums import ProviderType

if TYPE_CHECKING:
    from .client import MusicAssistantClient


class Config:
    """Config related endpoints/data for Music Assistant."""

    def __init__(self, client: MusicAssistantClient) -> None:
        """Handle Initialization."""
        self.client = client

    # Provider Config related commands/functions

    async def get_provider_configs(
        self,
        provider_type: ProviderType | None = None,
        provider_domain: str | None = None,
        include_values: bool = False,
    ) -> list[ProviderConfig]:
        """Return all known provider configurations, optionally filtered by ProviderType."""
        return [
            ProviderConfig.from_dict(item)
            for item in await self.client.send_command(
                "config/providers",
                provider_type=provider_type,
                provider_domain=provider_domain,
                include_values=include_values,
            )
        ]

    async def get_provider_config(self, instance_id: str) -> ProviderConfig:
        """Return (full) configuration for a single provider."""
        return ProviderConfig.from_dict(
            await self.client.send_command("config/providers/get", instance_id=instance_id)
        )

    async def get_provider_config_value(self, instance_id: str, key: str) -> ConfigValueType:
        """Return single configentry value for a provider."""
        return cast(
            ConfigValueType,
            await self.client.send_command(
                "config/providers/get_value", instance_id=instance_id, key=key
            ),
        )

    async def get_provider_config_entries(
        self,
        provider_domain: str,
        instance_id: str | None = None,
        action: str | None = None,
        values: dict[str, ConfigValueType] | None = None,
    ) -> tuple[ConfigEntry, ...]:
        """
        Return Config entries to setup/configure a provider.

        provider_domain: (mandatory) domain of the provider.
        instance_id: id of an existing provider instance (None for new instance setup).
        action: [optional] action key called from config entries UI.
        values: the (intermediate) raw values for config entries sent with the action.
        """
        return tuple(
            ConfigEntry.from_dict(x)
            for x in await self.client.send_command(
                "config/providers/get_entries",
                provider_domain=provider_domain,
                instance_id=instance_id,
                action=action,
                values=values,
            )
        )

    async def save_provider_config(
        self,
        provider_domain: str,
        values: dict[str, ConfigValueType],
        instance_id: str | None = None,
    ) -> ProviderConfig:
        """
        Save Provider(instance) Config.

        provider_domain: (mandatory) domain of the provider.
        values: the raw values for config entries that need to be stored/updated.
        instance_id: id of an existing provider instance (None for new instance setup).
        """
        return ProviderConfig.from_dict(
            await self.client.send_command(
                "config/providers/save",
                provider_domain=provider_domain,
                values=values,
                instance_id=instance_id,
            )
        )

    async def remove_provider_config(self, instance_id: str) -> None:
        """Remove ProviderConfig."""
        await self.client.send_command(
            "config/providers/remove",
            instance_id=instance_id,
        )

    async def reload_provider(self, instance_id: str) -> None:
        """Reload provider."""
        await self.client.send_command(
            "config/providers/reload",
            instance_id=instance_id,
        )

    # Player Config related commands/functions

    async def get_player_configs(
        self, provider: str | None = None, include_values: bool = False
    ) -> list[PlayerConfig]:
        """Return all known player configurations, optionally filtered by provider domain."""
        return [
            PlayerConfig.from_dict(item)
            for item in await self.client.send_command(
                "config/players",
                provider=provider,
                include_values=include_values,
            )
        ]

    async def get_player_config(self, player_id: str) -> PlayerConfig:
        """Return (full) configuration for a single player."""
        return PlayerConfig.from_dict(
            await self.client.send_command("config/players/get", player_id=player_id)
        )

    async def get_player_config_value(
        self,
        player_id: str,
        key: str,
    ) -> ConfigValueType:
        """Return single configentry value for a player."""
        return cast(
            ConfigValueType,
            await self.client.send_command(
                "config/players/get_value", player_id=player_id, key=key
            ),
        )

    async def save_player_config(
        self, player_id: str, values: dict[str, ConfigValueType]
    ) -> PlayerConfig:
        """Save/update PlayerConfig."""
        return PlayerConfig.from_dict(
            await self.client.send_command(
                "config/players/save", player_id=player_id, values=values
            )
        )

    async def remove_player_config(self, player_id: str) -> None:
        """Remove PlayerConfig."""
        await self.client.send_command("config/players/remove", player_id=player_id)

    # Core Controller config commands

    async def get_core_configs(self, include_values: bool = False) -> list[CoreConfig]:
        """Return all core controllers config options."""
        return [
            CoreConfig.from_dict(item)
            for item in await self.client.send_command(
                "config/core",
                include_values=include_values,
            )
        ]

    async def get_core_config(self, domain: str) -> CoreConfig:
        """Return configuration for a single core controller."""
        return CoreConfig.from_dict(
            await self.client.send_command(
                "config/core/get",
                domain=domain,
            )
        )

    async def get_core_config_value(self, domain: str, key: str) -> ConfigValueType:
        """Return single configentry value for a core controller."""
        return cast(
            ConfigValueType,
            await self.client.send_command("config/core/get_value", domain=domain, key=key),
        )

    async def get_core_config_entries(
        self,
        domain: str,
        action: str | None = None,
        values: dict[str, ConfigValueType] | None = None,
    ) -> tuple[ConfigEntry, ...]:
        """
        Return Config entries to configure a core controller.

        core_controller: name of the core controller
        action: [optional] action key called from config entries UI.
        values: the (intermediate) raw values for config entries sent with the action.
        """
        return tuple(
            ConfigEntry.from_dict(x)
            for x in await self.client.send_command(
                "config/core/get_entries",
                domain=domain,
                action=action,
                values=values,
            )
        )

    async def save_core_config(
        self,
        domain: str,
        values: dict[str, ConfigValueType],
    ) -> CoreConfig:
        """Save CoreController Config values."""
        return CoreConfig.from_dict(
            await self.client.send_command(
                "config/core/get_entries",
                domain=domain,
                values=values,
            )
        )
