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

from typing import TYPE_CHECKING

from .plugin import YandexSmartHomePlugin

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.enums import ProviderFeature
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


SUPPORTED_FEATURES: set[ProviderFeature] = set()


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return YandexSmartHomePlugin(mass, manifest, config, SUPPORTED_FEATURES)
