"""
DEVELOPMENT-ONLY provider that runs fake Sendspin clients.

Sendspin pairing is driven entirely by what a *client* advertises in its hello: which pairing
methods it offers, whether it admits unpaired access, how a PIN reaches the operator, and where
a static secret is found. None of that can be faked with a Music Assistant player object, so this
provider connects real ``aiosendspin`` clients to this server's own Sendspin endpoint, one per
scenario, each with a different pairing profile.

The Sendspin provider then treats them as ordinary devices and renders the real approval,
pairing, verification and device-management screens against them. Audio is decoded and dropped,
so the resulting players are usable playback targets as well.

Only loaded when Music Assistant runs in dev mode, like the other ``_``-prefixed providers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .provider import DemoSendspinClientsProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return DemoSendspinClientsProvider(mass, manifest, config)
