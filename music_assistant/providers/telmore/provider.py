"""Telmore Musik musicprovider support for MusicAssistant."""

from __future__ import annotations

from music_assistant.providers.music247e.provider import Music247eProvider
from music_assistant.providers.telmore.api_client import TelmoreAPIClient
from music_assistant.providers.telmore.auth_manager import TelmoreAuthManager
from music_assistant.providers.telmore.constants import CONF_QUALITY


class TelmoreMusikProvider(Music247eProvider):
    """Provider implementation for Telmore Musik."""

    CONF_QUALITY_KEY = CONF_QUALITY
    AUTH_MANAGER_CLASS = TelmoreAuthManager
    API_CLIENT_CLASS = TelmoreAPIClient
