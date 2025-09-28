"""Internet Archive music provider for Music Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueType
from music_assistant_models.enums import ConfigEntryType, ProviderFeature

from .provider import InternetArchiveProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant import MusicAssistant
    from music_assistant.models import ProviderInstanceType


SUPPORTED_FEATURES = {
    ProviderFeature.SEARCH,
    ProviderFeature.ARTIST_ALBUMS,
    ProviderFeature.ARTIST_TOPTRACKS,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider instance with given configuration."""
    return InternetArchiveProvider(mass, manifest, config, SUPPORTED_FEATURES)


async def get_config_entries(
    mass: MusicAssistant,  # noqa: ARG001
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    return (
        ConfigEntry(
            key="info",
            type=ConfigEntryType.LABEL,
            label="Internet Archive provides access to millions of free audio recordings "
            "including live concerts from the Live Music Archive (etree), historical recordings, "
            "and audiobooks from LibriVox. No authentication is required as all content is "
            "public domain or Creative Commons licensed.",
        ),
        ConfigEntry(
            key="enable_etree",
            type=ConfigEntryType.BOOLEAN,
            label="Enable Live Music Archive (etree)",
            description="Include live concert recordings from the etree collection. "
            "These are high-quality audience and soundboard recordings from "
            "artists who allow taping and sharing.",
            default_value=True,
        ),
        ConfigEntry(
            key="enable_audiobooks",
            type=ConfigEntryType.BOOLEAN,
            label="Enable LibriVox Audiobooks",
            description="Include free public domain audiobooks from LibriVox, "
            "read by volunteers. These are classic literature works whose "
            "copyright has expired.",
            default_value=False,
        ),
    )
