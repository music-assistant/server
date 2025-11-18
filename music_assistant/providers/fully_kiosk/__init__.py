"""FullyKiosk Player provider for Music Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueType
from music_assistant_models.enums import ConfigEntryType, ProviderFeature

from music_assistant.constants import (
    CONF_IP_ADDRESS,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_SSL_FINGERPRINT,
    CONF_USE_SSL,
    CONF_VERIFY_SSL,
)

from .provider import FullyKioskProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES: set[ProviderFeature] = (
    set()
)  # we don't have any special supported features (yet)


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return FullyKioskProvider(mass, manifest, config, SUPPORTED_FEATURES)


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
        ConfigEntry(
            key=CONF_USE_SSL,
            type=ConfigEntryType.BOOLEAN,
            label="Use HTTPS when connecting to the Fully Kiosk API.",
            default_value=False,
            category="advanced",
        ),
        ConfigEntry(
            key=CONF_VERIFY_SSL,
            type=ConfigEntryType.BOOLEAN,
            label="Verify HTTPS certificates (recommended).",
            default_value=True,
            description="Disabling verification trusts any certificate (no validation).",
            category="advanced",
        ),
        ConfigEntry(
            key=CONF_SSL_FINGERPRINT,
            type=ConfigEntryType.STRING,
            label="TLS certificate fingerprint",
            description=(
                "Optional SHA-256 hex fingerprint. When provided it must "
                "match the device certificate and overrides the verify setting."
            ),
            required=False,
            category="advanced",
        ),
    )
