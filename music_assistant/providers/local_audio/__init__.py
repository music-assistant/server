"""
Retired Local Audio Out provider.

The implementation is gone: local audio output now runs as a Sendspin add-on
outside the server. Only this tombstone remains, so an existing install still
resolves the manifest and gets a message explaining what to switch to instead of
the provider silently disappearing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.errors import UnsupportedSystemError

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


async def setup(
    mass: MusicAssistant,  # noqa: ARG001
    manifest: ProviderManifest,  # noqa: ARG001
    config: ProviderConfig,  # noqa: ARG001
) -> ProviderInstanceType:
    """Fail the load with the retirement notice."""
    # UnsupportedSystemError maps to ProviderStatus.INCOMPATIBLE, which is never retried
    # and which the frontend renders with the Remove button that resolves this for good.
    raise UnsupportedSystemError(
        "The local audio provider within Music Assistant has been retired in favor of "
        "running a sendspin add-on such as the official Local Audio App.",
        translation_key="provider_retired",
        translation_owner="provider.local_audio",
    )
