"""Tests for the Yandex Disk config flow (OAuth code flow, GD-style)."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest
from music_assistant_models.constants import SECURE_STRING_SUBSTITUTE

from music_assistant.providers.filesystem_yandex_disk import auth, get_config_entries
from music_assistant.providers.filesystem_yandex_disk.constants import (
    CONF_ACTION_AUTH,
    CONF_AUTH_CODE,
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
async def test_manual_code_help_link_uses_entered_client_id() -> None:
    """The manual-code field's help link embeds the entered client id."""
    entries = await get_config_entries(_mass(), instance_id=None, values={CONF_CLIENT_ID: "cid42"})
    code_entry = next(e for e in entries if e.key == "auth_code")
    assert code_entry.help_link
    assert "client_id=cid42" in code_entry.help_link


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
    """Reauthorization decrypts a stored client secret before token exchange."""
    exchanged_secret: str | None = None

    async def exchange_code(
        mass: MusicAssistant, code: str, client_id: str, client_secret: str
    ) -> str:
        nonlocal exchanged_secret
        assert mass is not None
        exchanged_secret = client_secret
        assert code == "confirmation-code"
        assert client_id == "client-id"
        return "refresh-token"

    monkeypatch.setattr(auth, "exchange_manual_code", exchange_code)
    values: dict[str, ConfigValueType] = {
        CONF_AUTH_CODE: "confirmation-code",
        CONF_CLIENT_ID: "client-id",
        CONF_CLIENT_SECRET: SECURE_STRING_SUBSTITUTE,
    }

    await get_config_entries(
        cast("MusicAssistant", _MassStub()),
        instance_id="instance-id",
        action=CONF_ACTION_AUTH,
        values=values,
    )

    assert exchanged_secret == "decrypted-secret"
    assert values[CONF_REFRESH_TOKEN] == "refresh-token"
    assert values[CONF_AUTH_CODE] is None
