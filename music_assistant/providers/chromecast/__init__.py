"""Chromecast Player provider for Music Assistant, utilizing the pychromecast library."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from pychromecast.controllers.media import MediaController

from .provider import ChromecastProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.enums import ProviderFeature
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES: set[ProviderFeature] = (
    set()
)  # we don't have any special supported features (yet)

# Monkey patch the Media controller here to store the queue items
_patched_process_media_status_org = MediaController._process_media_status


def _patched_process_media_status(self: MediaController, data: dict[str, object]) -> None:
    """Process STATUS message(s) of the media controller."""
    _patched_process_media_status_org(self, data)
    for status_msg in cast("list[dict[str, Any]]", data.get("status", [])):
        if items := status_msg.get("items"):
            self.status.current_item_id = status_msg.get("currentItemId", 0)  # type: ignore[attr-defined]
            self.status.items = items  # type: ignore[attr-defined]


# Apply the monkey patch
MediaController._process_media_status = _patched_process_media_status  # type: ignore[method-assign]


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return ChromecastProvider(mass, manifest, config, SUPPORTED_FEATURES)
