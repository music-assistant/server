"""Tests for the Yandex Disk config flow (official OAuth Device Flow)."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest
from music_assistant_models.constants import SECURE_STRING_SUBSTITUTE
from music_assistant_models.errors import LoginFailed
from ya_passport_auth import OAuthTokens, SecretStr

import music_assistant.providers.filesystem_yandex_disk as provider_module
from music_assistant.providers.filesystem_yandex_disk import get_config_entries
from music_assistant.providers.filesystem_yandex_disk.constants import (
    CONF_ACTION_AUTH,
    CONF_AUTH_STATUS_CONNECTED,
    CONF_AUTH_STATUS_NOT_CONNECTED,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_REFRESH_TOKEN,
    CONF_ROOT_PATH,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType

    from music_assistant import MusicAssistant


class _ConfigStub:
    """Stand-in for the config controller's encrypted secret handling."""

    def get_raw_provider_config_value(self, instance_id: str, key: str) -> str:
        assert instance_id == "instance-id"
        assert key == CONF_CLIENT_SECRET
        return "encrypted-secret"

    def decrypt_string(self, value: str) -> str:
        assert value == "encrypted-secret"
        return "decrypted-secret"


class _MassStub:
    """Music Assistant stand-in exposing the config controller."""

    def __init__(self) -> None:
        self.config = _ConfigStub()


def _mass() -> MusicAssistant:
    """Return a stand-in MusicAssistant; plain config renders never touch it."""
    return cast("MusicAssistant", object())


@pytest.mark.asyncio
async def test_config_entries_first_setup_include_oauth_fields_and_content_type() -> None:
    """First-setup entries include the OAuth fields, root and content type."""
    entries = await get_config_entries(_mass(), instance_id=None)
    keys = {e.key for e in entries}
    assert {CONF_CLIENT_ID, CONF_CLIENT_SECRET, CONF_ACTION_AUTH, CONF_ROOT_PATH} <= keys
    assert "content_type" in keys


@pytest.mark.asyncio
async def test_refresh_token_entry_is_hidden_and_required() -> None:
    """The refresh token is stored hidden and required (filled by the action)."""
    entries = await get_config_entries(_mass(), instance_id=None)
    rt = next(e for e in entries if e.key == CONF_REFRESH_TOKEN)
    assert rt.hidden is True
    assert rt.required is True


@pytest.mark.asyncio
async def test_config_has_no_manual_code_and_shows_auth_status() -> None:
    """Device Flow removes manual-code input and exposes explicit status."""
    entries = await get_config_entries(_mass(), instance_id=None)
    keys = {entry.key for entry in entries}
    assert "auth_code" not in keys
    assert CONF_AUTH_STATUS_NOT_CONNECTED in keys

    entries = await get_config_entries(
        _mass(), instance_id=None, values={CONF_REFRESH_TOKEN: "refresh-token"}
    )
    keys = {entry.key for entry in entries}
    assert CONF_AUTH_STATUS_CONNECTED in keys
    assert CONF_AUTH_STATUS_NOT_CONNECTED not in keys


@pytest.mark.asyncio
async def test_config_entries_reconfigure_content_type_read_only() -> None:
    """On reconfigure the content type is present but read-only."""
    entries = await get_config_entries(_mass(), instance_id="abc")
    keys = {e.key for e in entries}
    assert CONF_CLIENT_ID in keys
    assert "content_type" in keys  # the read-only variant keeps the same key


@pytest.mark.asyncio
async def test_reauthorize_decrypts_stored_client_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reauthorization decrypts a stored secret before Device Flow."""
    exchanged_secret: str | None = None

    async def run_device_flow(
        mass: MusicAssistant,
        session_id: str,
        page: object,
        *,
        client_id: str,
        client_secret: str,
        scope: str | None = None,
        device_name: str | None = None,
    ) -> OAuthTokens:
        nonlocal exchanged_secret
        assert mass is not None
        exchanged_secret = client_secret
        assert session_id == "config-session"
        assert page is not None
        assert client_id == "client-id"
        assert scope == "cloud_api:disk.read"
        assert device_name == "Music Assistant - Yandex Disk"
        return OAuthTokens(
            access_token=SecretStr("access-token"),
            refresh_token=SecretStr("refresh-token"),
            expires_in=3600,
        )

    monkeypatch.setattr(provider_module, "run_oauth_device_flow", run_device_flow)
    values: dict[str, ConfigValueType] = {
        CONF_CLIENT_ID: "client-id",
        CONF_CLIENT_SECRET: SECURE_STRING_SUBSTITUTE,
        "session_id": "config-session",
    }

    await get_config_entries(
        cast("MusicAssistant", _MassStub()),
        instance_id="instance-id",
        action=CONF_ACTION_AUTH,
        values=values,
    )

    assert exchanged_secret == "decrypted-secret"
    assert values[CONF_REFRESH_TOKEN] == "refresh-token"


@pytest.mark.asyncio
async def test_auth_action_requires_frontend_session_id() -> None:
    """The hosted auth page cannot start without MA's action session id."""
    values: dict[str, ConfigValueType] = {
        CONF_CLIENT_ID: "client-id",
        CONF_CLIENT_SECRET: "client-secret",
    }
    with pytest.raises(LoginFailed, match="session is missing"):
        await get_config_entries(_mass(), instance_id=None, action=CONF_ACTION_AUTH, values=values)
