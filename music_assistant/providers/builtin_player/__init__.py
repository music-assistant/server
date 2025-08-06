"""Built-in HTTP-based Player Provider for Music Assistant.

This provider creates a standards HTTP audio streaming endpoint that can be utilized
by the MA web interface, accessed directly as a URL, consumed by Home Assistant media
browser, or integrated with other plugins without requiring third-party protocols.

Usage requires registering a player through the 'builtin_player/register' API command.
The registered player must regularly update its state via 'builtin_player/update_state'
to maintain the connection. Players can be manually disconnected with 'builtin_player/unregister'
when no longer needed.

Communication with the player occurs via events. The provider sends commands (play media url, pause,
stop, volume changes, etc.) through the BUILTIN_PLAYER event type. Client implementations must
listen for these events and respond accordingly to control playback and handle media changes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant.mass import MusicAssistant
from music_assistant.models import ProviderInstanceType

from .provider import BuiltinPlayerProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry, ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return BuiltinPlayerProvider(mass, manifest, config)


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
    return ()
