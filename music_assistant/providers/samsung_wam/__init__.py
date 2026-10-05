"""Samsung WAM player provider for Music Assistant."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from music_assistant.constants import VERBOSE_LOG_LEVEL

from .provider import SamsungWamProvider


class SpeakerStatusFilter(logging.Filter):
    """Suppress high-frequency SpeakerStatus events unless verbose logging is enabled."""

    def __init__(self, provider_logger: logging.Logger, name: str = "") -> None:
        """
        Initialize the filter.

        :param provider_logger: The logger instance attached to the provider.
        :param name: Optional name for the filter.
        """
        super().__init__(name)
        self.provider_logger = provider_logger

    def filter(self, record: logging.LogRecord) -> bool:
        """Filter SpeakerStatus logs if the provider is not in verbose mode."""
        if self.provider_logger.isEnabledFor(VERBOSE_LOG_LEVEL):
            return True
        if record.msg == "Event: %s" and record.args and isinstance(record.args, tuple):
            try:
                event_obj = record.args[0]
                if getattr(event_obj, "method", None) == "SpeakerStatus":
                    return False
            except IndexError, AttributeError:
                pass
        return True


if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """
    Set up and return a new Samsung WAM provider instance.

    :param mass: The MusicAssistant instance.
    :param manifest: The provider manifest.
    :param config: The provider configuration.
    :return: The initialized provider instance.
    """
    prov = SamsungWamProvider(mass, manifest, config)

    # Suppress high-frequency SpeakerStatus events from pywam unless verbose logging is enabled
    pywam_events_logger = logging.getLogger("pywam.events")
    if not any(isinstance(f, SpeakerStatusFilter) for f in pywam_events_logger.filters):
        pywam_events_logger.addFilter(SpeakerStatusFilter(prov.logger))

    # Configure dependency log levels to match the provider's configured verbosity
    if prov.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
        logging.getLogger("pywam").setLevel(logging.DEBUG)
    elif prov.logger.isEnabledFor(logging.DEBUG):
        logging.getLogger("pywam").setLevel(logging.DEBUG)
        logging.getLogger("pywam.client").setLevel(logging.INFO)
    else:
        logging.getLogger("pywam").setLevel(logging.CRITICAL)

    return prov


__all__ = ["setup"]
