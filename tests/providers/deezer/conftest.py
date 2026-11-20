"""Shared fixtures for Deezer provider tests."""

from __future__ import annotations

from typing import Any
from unittest.mock import Mock

import pytest

from music_assistant.providers.deezer.browse import DeezerBrowseManager
from music_assistant.providers.deezer.provider import SUPPORTED_FEATURES, DeezerProvider


@pytest.fixture
def provider() -> DeezerProvider:
    """Create a real DeezerProvider with mocked dependencies."""
    mass = Mock()
    manifest = Mock()
    manifest.domain = "deezer"
    config = Mock()
    config.instance_id = "deezer--test123"
    config.name = "Deezer Test"
    config.enabled = True
    config.get_value.side_effect = lambda key, default=None: {
        "log_level": "GLOBAL",
    }.get(key, default)
    provider = DeezerProvider(mass, manifest, config, SUPPORTED_FEATURES)
    provider.user_id = "user123"
    provider.gql_client = Mock()  # replaced with per-test stubs
    provider.browse_manager = DeezerBrowseManager(provider)
    return provider


@pytest.fixture
def gw_user_data() -> dict[str, Any]:
    """Return GW user data for an account with a streaming subscription."""
    return {
        "error": [],
        "results": {
            "checkForm": "csrf-token",
            "COUNTRY": "DE",
            "OFFER_ID": 1,
            "USER": {
                "USER_ID": "123",
                "OPTIONS": {
                    "license_token": "license",
                    "expiration_timestamp": 4102444800,
                    "web_sound_quality": {"high": True, "lossless": True},
                    "mobile_sound_quality": {"high": True, "lossless": True},
                },
            },
        },
    }
