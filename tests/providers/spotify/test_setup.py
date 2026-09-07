"""Tests for the Spotify provider setup and legacy-token migration."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from music_assistant_models.errors import LoginFailed

from music_assistant.constants import ENCRYPT_SUFFIX
from music_assistant.providers.spotify import setup
from music_assistant.providers.spotify.constants import (
    CONF_CLIENT_ID,
    CONF_REFRESH_TOKEN_DEPRECATED,
    CONF_REFRESH_TOKEN_DEV,
    CONF_REFRESH_TOKEN_GLOBAL,
)


def _fake_encrypt(value: str) -> str:
    """Mirror ConfigController.encrypt_string: prefix once, idempotent for encrypted values."""
    return value if value.startswith(ENCRYPT_SUFFIX) else ENCRYPT_SUFFIX + value


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


async def test_setup_migrates_legacy_dev_token_with_migrated_client_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A legacy token is recognised as a developer one from a client id already in setup_data.

    The setup_data migration moves the client id out of the config values, so the legacy
    split has to find it there; the stored value is encrypted and must reach setup_data
    unchanged rather than encrypted a second time.
    """
    encrypted_client_id = _fake_encrypt("my-client-id")
    values = {CONF_REFRESH_TOKEN_DEPRECATED: "legacy_tok"}
    config = MagicMock(instance_id="spotify--test")
    config.get_value = MagicMock(side_effect=lambda key, default=None: values.get(key, default))
    config.setup_data = {CONF_CLIENT_ID: encrypted_client_id}
    mass = MagicMock()
    mass.config.encrypt_string = MagicMock(side_effect=_fake_encrypt)
    mass.config.set = MagicMock()
    mass.config.set_raw_provider_config_value = MagicMock()
    # patched so that misreading the client id fails the raises check below rather than
    # crashing on a MagicMock somewhere inside the provider it would then construct
    monkeypatch.setattr(
        "music_assistant.providers.spotify.SpotifyProvider", MagicMock(return_value=object())
    )
    # a developer-only legacy token leaves no global token, so re-auth is still required
    with pytest.raises(LoginFailed):
        await setup(mass, MagicMock(), config)
    base = "providers/spotify--test/setup_data"
    mass.config.set.assert_any_call(f"{base}/{CONF_REFRESH_TOKEN_DEV}", _fake_encrypt("legacy_tok"))
    assert config.setup_data[CONF_REFRESH_TOKEN_DEV] == _fake_encrypt("legacy_tok")
    # the already encrypted client id survives the round trip untouched
    mass.config.set.assert_any_call(f"{base}/{CONF_CLIENT_ID}", encrypted_client_id)
    assert config.setup_data[CONF_CLIENT_ID] == encrypted_client_id
