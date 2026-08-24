"""
Apple Music musicprovider support for MusicAssistant.

TODO MUSIC_APP_TOKEN expires after 6 months so should have a distribution mechanism outside
  compulsory application updates. It is only a semi-private key in JWT format so could be
  refreshed daily by a GitHub action and downloaded by the provider each initialise.
TODO Widevine keys can be obtained dynamically from Apple Music API rather than copied into Docker
  build. This is undocumented but @maxlyth has a working example.
TODO MUSIC_USER_TOKEN must be refreshed (~min 180 days) and needs mechanism to prompt user to
  re-authenticate in browser.
TODO Current provider ignores private tracks that are not available in the storefront catalog as
  streamable url is derived from the catalog id. It is undocumented but @maxlyth has a working
  example to get a streamable url from the library id.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .provider import AppleMusicProvider

__all__ = ["AppleMusicProvider", "setup"]

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant import MusicAssistant
    from music_assistant.models import ProviderInstanceType


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return AppleMusicProvider(mass, manifest, config)
