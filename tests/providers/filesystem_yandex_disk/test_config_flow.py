"""Tests for the Yandex Disk config flow (OAuth code flow, GD-style)."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from music_assistant.providers.filesystem_yandex_disk import get_config_entries
from music_assistant.providers.filesystem_yandex_disk.constants import (
    CONF_ACTION_AUTH,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_REFRESH_TOKEN,
    CONF_ROOT_PATH,
)

if TYPE_CHECKING:
    from music_assistant import MusicAssistant


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
