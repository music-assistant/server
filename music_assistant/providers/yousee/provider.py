"""YouSee Musik musicprovider support for MusicAssistant."""

from __future__ import annotations

from music_assistant.providers.music247e.provider import Music247eProvider
from music_assistant.providers.yousee.api_client import YouSeeAPIClient
from music_assistant.providers.yousee.auth_manager import YouSeeAuthManager
from music_assistant.providers.yousee.constants import CONF_QUALITY


class YouSeeMusikProvider(Music247eProvider):
    """Provider implementation for YouSee Musik."""

    CONF_QUALITY_KEY = CONF_QUALITY
    AUTH_MANAGER_CLASS = YouSeeAuthManager
    API_CLIENT_CLASS = YouSeeAPIClient
