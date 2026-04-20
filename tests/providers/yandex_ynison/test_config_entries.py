"""Tests for get_config_entries source-selection behavior."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from music_assistant.providers.yandex_ynison import get_config_entries
from music_assistant.providers.yandex_ynison.constants import (
    CONF_TOKEN,
    CONF_YM_INSTANCE,
    YM_INSTANCE_OWN,
)


def _make_mock_mass(providers_config: dict[str, Any] | None = None) -> MagicMock:
    """Build a MusicAssistant stub with a configurable providers registry."""
    mass = MagicMock()
    mass.config.get = MagicMock(return_value=providers_config or {})
    mass.players.all_players = MagicMock(return_value=[])
    return mass


def _entries_by_key(entries: tuple[Any, ...]) -> dict[str, Any]:
    return {entry.key: entry for entry in entries}


async def test_no_ym_instances_defaults_to_own_and_shows_token_field() -> None:
    """With 0 YM instances, dropdown has only Own and token field is visible."""
    mass = _make_mock_mass({})
    entries = await get_config_entries(mass)
    by_key = _entries_by_key(entries)

    ym_source = by_key[CONF_YM_INSTANCE]
    option_values = [opt.value for opt in ym_source.options]
    assert option_values == [YM_INSTANCE_OWN]
    assert ym_source.default_value == YM_INSTANCE_OWN

    token = by_key[CONF_TOKEN]
    assert token.hidden is False
    assert token.required is True


async def test_single_ym_instance_defaults_to_borrow_and_hides_token() -> None:
    """With exactly 1 YM instance, default borrows and token field is hidden."""
    mass = _make_mock_mass({"ym-a": {"domain": "yandex_music", "name": "Primary"}})
    entries = await get_config_entries(mass)
    by_key = _entries_by_key(entries)

    ym_source = by_key[CONF_YM_INSTANCE]
    assert ym_source.default_value == "ym-a"

    token = by_key[CONF_TOKEN]
    assert token.hidden is True
    assert token.required is False


async def test_multiple_ym_instances_default_to_own_requiring_explicit_choice() -> None:
    """With 2+ YM instances, default is OWN (user must pick explicitly)."""
    mass = _make_mock_mass(
        {
            "ym-a": {"domain": "yandex_music", "name": "A"},
            "ym-b": {"domain": "yandex_music", "name": "B"},
        }
    )
    entries = await get_config_entries(mass)
    by_key = _entries_by_key(entries)

    ym_source = by_key[CONF_YM_INSTANCE]
    option_values = {opt.value for opt in ym_source.options}
    assert {"ym-a", "ym-b", YM_INSTANCE_OWN} == option_values
    assert ym_source.default_value == YM_INSTANCE_OWN

    token = by_key[CONF_TOKEN]
    assert token.hidden is False
    assert token.required is True


async def test_selected_ym_instance_hides_token() -> None:
    """When values selects a real YM instance, token field is hidden/optional."""
    mass = _make_mock_mass({"ym-a": {"domain": "yandex_music", "name": "Primary"}})
    entries = await get_config_entries(mass, values={CONF_YM_INSTANCE: "ym-a"})
    by_key = _entries_by_key(entries)

    token = by_key[CONF_TOKEN]
    assert token.hidden is True
    assert token.required is False


async def test_selected_own_shows_token() -> None:
    """When values selects OWN, token field is visible and required."""
    mass = _make_mock_mass({"ym-a": {"domain": "yandex_music", "name": "Primary"}})
    entries = await get_config_entries(mass, values={CONF_YM_INSTANCE: YM_INSTANCE_OWN})
    by_key = _entries_by_key(entries)

    token = by_key[CONF_TOKEN]
    assert token.hidden is False
    assert token.required is True


async def test_upgrade_with_existing_token_preserves_own_mode() -> None:
    """Upgrade from own-mode (CONF_TOKEN set, CONF_YM_INSTANCE absent) stays OWN.

    Even if exactly one yandex_music instance exists, we must not silently
    switch the user's auth source on a no-op Save after upgrade.
    """
    mass = _make_mock_mass({"ym-a": {"domain": "yandex_music", "name": "Primary"}})
    entries = await get_config_entries(mass, values={CONF_TOKEN: "legacy-token"})
    by_key = _entries_by_key(entries)

    ym_source = by_key[CONF_YM_INSTANCE]
    assert ym_source.default_value == YM_INSTANCE_OWN

    token = by_key[CONF_TOKEN]
    assert token.hidden is False
    assert token.required is True


async def test_stale_ym_selection_clamps_default_to_own() -> None:
    """A saved selection pointing at a removed YM instance clamps to OWN.

    Guards against the dropdown rendering with a default_value that is not
    in its options (which would show as an invalid/empty selection).
    """
    mass = _make_mock_mass({"ym-b": {"domain": "yandex_music", "name": "B"}})
    entries = await get_config_entries(mass, values={CONF_YM_INSTANCE: "ym-removed"})
    by_key = _entries_by_key(entries)

    ym_source = by_key[CONF_YM_INSTANCE]
    option_values = {opt.value for opt in ym_source.options}
    assert ym_source.default_value == YM_INSTANCE_OWN
    assert ym_source.default_value in option_values

    token = by_key[CONF_TOKEN]
    assert token.hidden is False
    assert token.required is True
