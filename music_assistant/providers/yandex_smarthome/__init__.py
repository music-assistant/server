"""
Yandex Smart Home Plugin Provider for Music Assistant.

Exposes Music Assistant players as Yandex Smart Home devices so Alice can
control playback (play / pause / volume / mute / source) via natural-language
commands. The voice-skill (custom dialog) functionality lives in the sister
provider `ma-provider-yandex-alice`.

Connection modes:
- ``cloud`` — public yaha-cloud.ru relay (zero setup, but the public skill can
  only be linked to one MA / Home Assistant instance per Yandex account).
- ``cloud_plus`` — private skill via the yaha-cloud relay (multiple instances
  per account, registered manually in the dev console).
- ``direct`` — Yandex calls the MA webserver directly (requires public HTTPS).

Authentication and cloud/skill provisioning are handled by the setup flow
(see setup_flow.py); only the genuine playback options are configurable here.

Reference: https://github.com/dext0r/yandex_smart_home
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType, ProviderFeature

from .constants import (
    CONF_EXPOSED_PLAYERS,
    CONF_EXPOSED_PLAYLISTS,
    CONF_INSTANCE_NAME,
)
from .playlists import fetch_playlist_options
from .plugin import YandexSmartHomePlugin

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


_LOGGER = logging.getLogger(__name__)

SUPPORTED_FEATURES: set[ProviderFeature] = set()


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return YandexSmartHomePlugin(mass, manifest, config, SUPPORTED_FEATURES)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    Authentication and cloud/skill provisioning are handled by the setup flow (see
    setup_flow.py); only the genuine playback options are configurable here.

    :param mass: The MusicAssistant instance.
    :param instance_id: id of an existing provider instance (None if new instance setup).
    :param action: [optional] action key called from config entries UI.
    :param values: the (intermediate) raw values for config entries sent with the action.
    """
    player_options = await _list_player_options(mass)
    playlist_options: list[ConfigValueOption] = []
    try:
        playlist_options = await fetch_playlist_options(mass)
    except asyncio.CancelledError:
        raise
    except Exception:
        _LOGGER.debug("could not enumerate playlists")

    return (
        ConfigEntry(
            key=CONF_INSTANCE_NAME,
            type=ConfigEntryType.STRING,
            required=False,
            default_value="Music Assistant",
        ),
        ConfigEntry(
            key=CONF_EXPOSED_PLAYERS,
            type=ConfigEntryType.STRING,
            required=False,
            multi_value=True,
            default_value=[],
            options=list(player_options) if player_options else [],
        ),
        ConfigEntry(
            key=CONF_EXPOSED_PLAYLISTS,
            type=ConfigEntryType.STRING,
            required=False,
            multi_value=True,
            default_value=[],
            options=list(playlist_options) if playlist_options else [],
        ),
    )


async def _list_player_options(mass: MusicAssistant) -> list[ConfigValueOption]:
    """Build the player-picker options list."""
    options: list[ConfigValueOption] = []
    try:
        for player in mass.players.all_players():
            state = player.state
            options.append(
                ConfigValueOption(title=state.name or state.player_id, value=state.player_id)
            )
    except Exception:
        _LOGGER.debug("could not enumerate players")
    return options
