"""Samsung Wireless Audio (WAM) Player provider for Music Assistant."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from music_assistant.constants import VERBOSE_LOG_LEVEL

from .provider import SamsungWamProvider, get_config_entries


class SpeakerStatusFilter(logging.Filter):
    """Filter out spammy SpeakerStatus logs unless VERBOSE is enabled."""

    def __init__(self, provider_logger: logging.Logger, name: str = "") -> None:
        """Initialize the filter.

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
            except (IndexError, AttributeError):
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
    """Initialize the Samsung WAM provider instance with the given configuration.

    :param mass: The MusicAssistant instance.
    :param manifest: The provider manifest.
    :param config: The user configuration for this provider.
    :return: An initialized ProviderInstanceType.
    """
    prov = SamsungWamProvider(mass, manifest, config)

    # Add a filter to suppress spammy SpeakerStatus logs from pywam.
    pywam_events_logger = logging.getLogger("pywam.events")
    if not any(isinstance(f, SpeakerStatusFilter) for f in pywam_events_logger.filters):
        pywam_events_logger.addFilter(SpeakerStatusFilter(prov.logger))

    # Configure dependency log levels to match the provider's configured verbosity.
    if prov.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
        logging.getLogger("pywam").setLevel(logging.DEBUG)
        logging.getLogger("async_upnp_client").setLevel(logging.DEBUG)
    elif prov.logger.isEnabledFor(logging.DEBUG):
        logging.getLogger("pywam").setLevel(logging.DEBUG)
        logging.getLogger("pywam.client").setLevel(logging.INFO)
        logging.getLogger("async_upnp_client").setLevel(logging.DEBUG)
        logging.getLogger("async_upnp_client.traffic").setLevel(logging.INFO)
        logging.getLogger("async_upnp_client.advertisement").setLevel(logging.INFO)
        logging.getLogger("async_upnp_client.search").setLevel(logging.INFO)
    else:
        logging.getLogger("pywam").setLevel(logging.CRITICAL)
        logging.getLogger("async_upnp_client").setLevel(logging.CRITICAL)

    return prov


__all__ = ["get_config_entries", "setup"]
