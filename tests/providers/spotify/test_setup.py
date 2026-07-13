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
    A legacy token (no custom client id) is migrated to the global key without forcing re-auth.

    The provider isn't registered during setup, so the raw store write can't sync the in-memory
    config snapshot; setup must use the migrated value rather than re-reading the stale snapshot.
    """
    values = {CONF_REFRESH_TOKEN_DEPRECATED: "legacy_tok"}
    config = MagicMock(instance_id="spotify--test")
    config.get_value = MagicMock(side_effect=lambda key: values.get(key))
    mass = MagicMock()
    mass.config.set_raw_provider_config_value = MagicMock()
    sentinel = object()
    monkeypatch.setattr(
        "music_assistant.providers.spotify.SpotifyProvider", MagicMock(return_value=sentinel)
    )
    result = await setup(mass, MagicMock(), config)
    assert result is sentinel
    mass.config.set_raw_provider_config_value.assert_any_call(
        "spotify--test", CONF_REFRESH_TOKEN_GLOBAL, "legacy_tok", encrypted=True
    )
