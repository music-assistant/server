"""Tests for the Spotify provider setup and legacy-token migration."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from music_assistant.providers.spotify import setup
from music_assistant.providers.spotify.constants import (
    CONF_REFRESH_TOKEN_DEPRECATED,
    CONF_REFRESH_TOKEN_GLOBAL,
)

if TYPE_CHECKING:
    import pytest


async def test_setup_migrates_legacy_global_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    A legacy token (no custom client id) is migrated into setup_data under the global key.

    The provider isn't registered during setup, so the migration writes straight to the
    setup_data store; the presence check then passes without forcing re-auth.
    """
    values = {CONF_REFRESH_TOKEN_DEPRECATED: "legacy_tok"}
    config = MagicMock(instance_id="spotify--test")
    config.get_value = MagicMock(side_effect=lambda key, default=None: values.get(key, default))
    config.setup_data = {}
    mass = MagicMock()
    mass.config.encrypt_string = MagicMock(side_effect=lambda value: value)
    mass.config.set = MagicMock()
    mass.config.set_raw_provider_config_value = MagicMock()
    sentinel = object()
    monkeypatch.setattr(
        "music_assistant.providers.spotify.SpotifyProvider", MagicMock(return_value=sentinel)
    )
    result = await setup(mass, MagicMock(), config)
    assert result is sentinel
    # legacy token migrated into setup_data under the global key (encrypt is identity here)
    mass.config.set.assert_any_call(
        f"providers/spotify--test/setup_data/{CONF_REFRESH_TOKEN_GLOBAL}", "legacy_tok"
    )
    assert config.setup_data[CONF_REFRESH_TOKEN_GLOBAL] == "legacy_tok"
    # the deprecated legacy key is cleared once migrated
    mass.config.set_raw_provider_config_value.assert_any_call(
        "spotify--test", CONF_REFRESH_TOKEN_DEPRECATED, None
    )
