"""Server specific/only models."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, overload

from .audio_analysis_provider import AudioAnalysisProvider
from .metadata_provider import MetadataProvider
from .music_provider import MusicProvider
from .player_provider import PlayerProvider
from .plugin import PluginProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry, ConfigValueType, ProviderConfig
    from music_assistant_models.enums import ProviderFeature
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant


ProviderInstanceType = (
    AudioAnalysisProvider | MetadataProvider | MusicProvider | PlayerProvider | PluginProvider
)


class ProviderModuleType(Protocol):
    """Model for a provider module to support type hints."""

    """Return the (base) features supported by this Provider."""
    SUPPORTED_FEATURES: set[ProviderFeature]

    @overload
    @staticmethod
    async def setup(
        mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
    ) -> ProviderInstanceType: ...

    @overload
    @staticmethod
    async def setup(
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
        *,
        auto_setup: bool,
    ) -> ProviderInstanceType: ...

    @staticmethod
    async def setup(
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
        *,
        auto_setup: bool = False,
    ) -> ProviderInstanceType:
        """
        Initialize provider(instance) with given configuration.

        :param mass: The MusicAssistant instance.
        :param manifest: Manifest of the provider domain.
        :param config: Config of the provider instance to create.
        :param auto_setup: First-boot setup of a default provider, only passed to a setup()
            that declares it; raise UnsupportedSystemError to refuse it.
        """
        raise NotImplementedError

    @staticmethod
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
        raise NotImplementedError
