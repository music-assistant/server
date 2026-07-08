"""End-to-end tests for the CONFIG_READ tool group."""
# ruff: noqa: D103, PLC0415
#   D103: test functions don't need docstrings.
#   PLC0415: mock reconfiguration inside test bodies requires deferred imports.

from __future__ import annotations

from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError


async def _call(mcp: Any, name: str, **kwargs: Any) -> Any:
    async with Client(mcp) as client:
        return await client.call_tool(f"config_{name}", kwargs)


async def test_get_provider_masks_secret(mounted_config: Any, mock_config_targets: Any) -> None:  # noqa: ARG001
    result = await _call(mounted_config, "get_provider", instance_id="yandex_music")
    by_key = {v.key: v for v in result.data.values}
    assert by_key["token"].value == "this_value_is_encrypted"
    assert "real-secret" not in str(result.data)


async def test_get_core_returns_values(mounted_config: Any, mock_config_targets: Any) -> None:  # noqa: ARG001
    result = await _call(mounted_config, "get_core", domain="webserver")
    assert any(v.key == "log_level" for v in result.data.values)


async def test_get_player_returns_values(mounted_config: Any, mock_config_targets: Any) -> None:  # noqa: ARG001
    result = await _call(mounted_config, "get_player", player_id="kitchen")
    assert result.data.player_id == "kitchen"
    assert any(v.key == "http_port" for v in result.data.values)


async def test_get_entries_lists_editable(mounted_config: Any, mock_config_targets: Any) -> None:  # noqa: ARG001
    result = await _call(
        mounted_config, "get_entries", target_type="provider", target_id="yandex_music"
    )
    keys = {e.key for e in result.data.entries}
    assert {"log_level", "http_port", "token"} <= keys


def test_entry_dump_accepts_labelless_entry() -> None:
    """
    A ConfigEntry whose ``label`` is None must dump without choking.

    Upstream ``music_assistant_models`` widened ``ConfigEntry.label`` to
    ``str | None``; ``_entry_dump`` passes the label straight through, so the
    dump must preserve None rather than assume a string is always present.
    Regression for the release-gate mypy failure under the newer models.
    """
    from music_assistant_models.config_entries import ConfigEntry
    from music_assistant_models.enums import ConfigEntryType

    from music_assistant.providers.fastmcp_server.tools.config import _entry_dump

    # Set the label out-of-band so the test type checks whether the installed
    # models type ``label`` as ``str`` or ``str | None`` — ``object.__setattr__``
    # also bypasses the frozen dataclass.
    entry = ConfigEntry(key="k", type=ConfigEntryType.STRING, label="x")
    object.__setattr__(entry, "label", None)
    dump = _entry_dump(entry, "current")

    assert dump.label is None
    assert dump.key == "k"


def test_entry_dump_resolves_localized_label_and_description() -> None:
    """
    `_entry_dump` surfaces the localized label/description, not raw None.

    Server-category entries set label/description to None and rely on the
    strings.json translations resolved at serialization, so the dump must read
    the resolved values rather than the raw (None) attributes — otherwise
    `config_get_entries` returns null text for every server setting (regression
    from the strings.json migration, flagged on the #4486 review).
    """
    from music_assistant_models.config_entries import ConfigEntry
    from music_assistant_models.enums import ConfigEntryType
    from music_assistant_models.translations import TRANSLATION_RESOLVER

    from music_assistant.providers.fastmcp_server.tools.config import _entry_dump

    resolved = {
        "config_entries.require_auth.label": "Require authentication",
        "config_entries.require_auth.description": "Require a bearer token.",
    }

    def _resolver(key: str, **_kwargs: Any) -> str | None:
        return resolved.get(key)

    entry = ConfigEntry(key="require_auth", type=ConfigEntryType.BOOLEAN)
    assert entry.label is None  # raw attribute is unset; text lives in translations
    token = TRANSLATION_RESOLVER.set(_resolver)
    try:
        dump = _entry_dump(entry, True)
    finally:
        TRANSLATION_RESOLVER.reset(token)

    assert dump.label == "Require authentication"
    assert dump.description == "Require a bearer token."


async def test_get_provider_unknown_raises(mounted_config: Any, mock_mass: Any) -> None:
    from unittest.mock import AsyncMock

    mock_mass.config.get_provider_config = AsyncMock(side_effect=KeyError("nope"))
    with pytest.raises(ToolError, match="not found"):
        await _call(mounted_config, "get_provider", instance_id="nope")


async def test_get_dsp_returns_shape(mounted_config: Any, mock_config_targets: Any) -> None:  # noqa: ARG001
    result = await _call(mounted_config, "get_dsp", player_id="kitchen")
    assert result.data.player_id == "kitchen"
    assert result.data.enabled is True
    assert result.data.filters == []


async def test_list_targets_rolls_up(mounted_config: Any, mock_config_targets: Any) -> None:  # noqa: ARG001
    result = await _call(mounted_config, "list_targets")
    assert len(result.data.providers) >= 1
    assert len(result.data.core) >= 1
    assert len(result.data.players) >= 1


async def test_get_dsp_unknown_player_raises(mounted_config: Any, mock_mass: Any) -> None:
    from unittest.mock import AsyncMock

    mock_mass.config.get_player_config = AsyncMock(side_effect=KeyError("nope"))
    with pytest.raises(ToolError, match="not found"):
        await _call(mounted_config, "get_dsp", player_id="nope")


async def test_get_entries_masks_secret_current_value(
    mounted_config: Any,
    mock_config_targets: Any,  # noqa: ARG001
) -> None:
    """
    config_get_entries must mask SECURE_STRING current_value.

    Regression for PR #99 review finding B.
    """
    from music_assistant_models.constants import SECURE_STRING_SUBSTITUTE

    result = await _call(
        mounted_config, "get_entries", target_type="provider", target_id="yandex_music"
    )
    by_key = {e.key: e for e in result.data.entries}
    assert by_key["token"].type == "secure_string"
    assert by_key["token"].current_value == SECURE_STRING_SUBSTITUTE
    # the raw fixture secret value must not leak
    assert "raw-secret-xyz" not in str(result.data)
