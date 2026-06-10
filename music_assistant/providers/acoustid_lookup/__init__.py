"""AcoustID Lookup audio analysis provider."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from .provider import (
    CONF_ANALYSE_STREAMING,
    CONF_API_KEY,
    CONF_MIN_SCORE,
    CONF_WRITE_TAGS_BACK,
    DEFAULT_MIN_SCORE,
    AcoustidLookupProvider,
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
) -> AcoustidLookupProvider:
    """Set up the AcoustID Lookup provider."""
    return AcoustidLookupProvider(mass, manifest, config, SUPPORTED_FEATURES)


async def get_config_entries(
    mass: MusicAssistant,  # noqa: ARG001
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """Return config entries for this provider."""
    return (
        ConfigEntry(
            key=CONF_API_KEY,
            type=ConfigEntryType.SECURE_STRING,
            label="AcoustID API key",
            description=(
                "Optional. Music Assistant ships with a shared AcoustID API key, so "
                "this can be left empty. Provide your own free key from "
                "https://acoustid.org/api-key if you prefer to use a dedicated one. "
                "AcoustID requests that high-traffic deployments notify the project "
                "in advance — see https://acoustid.org/webservice for details."
            ),
            required=False,
            default_value=None,
            advanced=True,
        ),
        ConfigEntry(
            key=CONF_MIN_SCORE,
            type=ConfigEntryType.FLOAT,
            label="Minimum match score",
            description=(
                "Confidence threshold (0.5 - 1.0) below which AcoustID matches are "
                "discarded. The default of 0.85 is a balance between recall and "
                "false-positive rate; raise it for stricter matching."
            ),
            default_value=DEFAULT_MIN_SCORE,
            range=(0, 1),
            required=False,
            advanced=True,
        ),
        ConfigEntry(
            key=CONF_ANALYSE_STREAMING,
            type=ConfigEntryType.BOOLEAN,
            label="Analyse tracks from streaming providers",
            description=(
                "When enabled, also fingerprint and identify tracks played from "
                "streaming providers (Spotify, Tidal, Qobuz, …) that are present "
                "in your library but lack a MusicBrainz Recording Id."
            ),
            default_value=True,
            required=False,
        ),
        ConfigEntry(
            key=CONF_WRITE_TAGS_BACK,
            type=ConfigEntryType.BOOLEAN,
            label="Write AcoustID/MusicBrainz tags back to files",
            description=(
                "When the audio-analysis scan identifies a track via AcoustID, also "
                "write the Acoustid Id, MusicBrainz Recording Id, ISRC, and (where "
                "resolvable) MusicBrainz Artist Id tags back into the source audio "
                "file. Useful if other apps on your network rely on these tags for "
                "their own metadata. Requires a Filesystem music source with write "
                "access to the file; read-only files are silently skipped."
            ),
            default_value=False,
            required=False,
        ),
    )
