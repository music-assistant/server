"""Pandora music provider support for Music Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import ProviderFeature
from music_assistant_models.errors import SetupFailedError

from music_assistant.constants import CONF_PASSWORD, CONF_USERNAME

from .provider import PandoraProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant import MusicAssistant
    from music_assistant.models import ProviderInstanceType

# Supported Features - Pandora is primarily a radio service
SUPPORTED_FEATURES = {
    ProviderFeature.BROWSE,
    ProviderFeature.LIBRARY_RADIOS,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider instance with given configuration."""
    # read the stored values directly: the config's option entries are only resolved
    # (and its values populated) once the instance exists, which is after this call.
    # A non-empty (still-encrypted) password satisfies the presence check below; the
    # provider decrypts it via get_config_value once loaded.
    username = mass.config.get_raw_provider_config_value(config.instance_id, CONF_USERNAME)
    password = mass.config.get_raw_provider_config_value(config.instance_id, CONF_PASSWORD)

    # Type-safe validation
    if (
        not username
        or not password
        or not isinstance(username, str)
        or not isinstance(password, str)
        or not username.strip()
        or not password.strip()
    ):
        raise SetupFailedError("Username and password are required")

    return PandoraProvider(mass, manifest, config, SUPPORTED_FEATURES)
