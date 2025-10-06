"""Chromecast Player provider for Music Assistant, utilizing the pychromecast library."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pychromecast.controllers.media import MediaController

from music_assistant.constants import CONF_ENTRY_MANUAL_DISCOVERY_IPS

from .provider import ChromecastProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry, ConfigValueType, ProviderConfig
    from music_assistant_models.enums import ProviderFeature
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES: set[ProviderFeature] = (
    set()
)  # we don't have any special supported features (yet)

# Monkey patch the Media controller here to store the queue items
_patched_process_media_status_org = MediaController._process_media_status


def _patched_process_media_status(self: MediaController, data: dict) -> None:
    """Process STATUS message(s) of the media controller."""
    _patched_process_media_status_org(self, data)
    for status_msg in data.get("status", []):
        if items := status_msg.get("items"):
            self.status.current_item_id = status_msg.get("currentItemId", 0)
            self.status.items = items


# Apply the monkey patch
MediaController._process_media_status = _patched_process_media_status


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return ChromecastProvider(mass, manifest, config, SUPPORTED_FEATURES)


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
    return (CONF_ENTRY_MANUAL_DISCOVERY_IPS,)
