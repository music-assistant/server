"""Loudness Analysis audio analysis provider."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from .provider import (
    CONF_ANALYZE_LOCAL_FILES_BACKGROUND,
    CONF_WRITE_REPLAYGAIN_TAGS,
    LoudnessAnalysisProvider,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.enums import ProviderFeature
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant

SUPPORTED_FEATURES: set[ProviderFeature] = set()


async def setup(
    mass: MusicAssistant,
    manifest: ProviderManifest,
    config: ProviderConfig,
) -> LoudnessAnalysisProvider:
    """Set up the Loudness Analysis provider."""
    return LoudnessAnalysisProvider(mass, manifest, config, SUPPORTED_FEATURES)


async def get_config_entries(
    mass: MusicAssistant,  # noqa: ARG001
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """Return config entries for this provider."""
    return (
        ConfigEntry(
            key=CONF_ANALYZE_LOCAL_FILES_BACKGROUND,
            type=ConfigEntryType.BOOLEAN,
            label="Analyze loudness of local files in the background",
            description=(
                "Run a nightly background job that measures EBU R128 integrated "
                "loudness for tracks from local filesystem providers that do not "
                "yet have a loudness measurement. Processes up to 250 tracks per "
                "run, one by one. Skips automatically if the storage provider is "
                "offline at the time of the run."
            ),
            default_value=True,
            required=False,
        ),
        ConfigEntry(
            key=CONF_WRITE_REPLAYGAIN_TAGS,
            type=ConfigEntryType.BOOLEAN,
            label="Write REPLAYGAIN_TRACK_GAIN tags back to files",
            description=(
                "When the background loudness job produces a measurement, also "
                "write the corresponding REPLAYGAIN_TRACK_GAIN tag back into the "
                "audio file. Useful if other apps on your network read these tags "
                "for their own volume normalization. Requires write access to the "
                "file; read-only files are silently skipped."
            ),
            default_value=False,
            required=False,
        ),
    )
