"""Tests for get_config_entries source-selection behavior."""

from __future__ import annotations

from typing import Any
from unittest import mock
from unittest.mock import MagicMock

import pytest
from music_assistant_models.errors import LoginFailed

from music_assistant.providers.yandex_ynison import get_config_entries
from music_assistant.providers.yandex_ynison.constants import (
    CONF_ACCOUNT_LOGIN,
    CONF_ACTION_AUTH_QR,
    CONF_ACTION_CLEAR_AUTH,
    CONF_REMEMBER_SESSION,
    CONF_TOKEN,
    CONF_X_TOKEN,
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


async def test_own_mode_surfaces_qr_login_button() -> None:
    """Own mode unauthenticated → QR action visible, reset action hidden."""
    mass = _make_mock_mass({})
    entries = await get_config_entries(mass, values={CONF_YM_INSTANCE: YM_INSTANCE_OWN})
    by_key = _entries_by_key(entries)

    qr = by_key[CONF_ACTION_AUTH_QR]
    reset = by_key[CONF_ACTION_CLEAR_AUTH]
    remember = by_key[CONF_REMEMBER_SESSION]
    assert qr.hidden is False
    assert qr.action == CONF_ACTION_AUTH_QR
    assert remember.hidden is False
    assert remember.default_value is True
    assert reset.hidden is True


async def test_borrow_mode_hides_own_mode_actions() -> None:
    """Borrow mode → QR / reset / remember-session entries are all hidden."""
    mass = _make_mock_mass({"ym-a": {"domain": "yandex_music", "name": "Primary"}})
    entries = await get_config_entries(mass, values={CONF_YM_INSTANCE: "ym-a"})
    by_key = _entries_by_key(entries)

    assert by_key[CONF_ACTION_AUTH_QR].hidden is True
    assert by_key[CONF_ACTION_CLEAR_AUTH].hidden is True
    assert by_key[CONF_REMEMBER_SESSION].hidden is True


async def test_authenticated_own_mode_shows_reset_hides_qr() -> None:
    """Once a token is stored, hide the QR/remember entries and show reset."""
    mass = _make_mock_mass({})
    entries = await get_config_entries(
        mass,
        values={
            CONF_YM_INSTANCE: YM_INSTANCE_OWN,
            CONF_TOKEN: "music-tok",
            CONF_X_TOKEN: "x-tok",
            CONF_ACCOUNT_LOGIN: "alice",
        },
    )
    by_key = _entries_by_key(entries)

    assert by_key[CONF_ACTION_AUTH_QR].hidden is True
    assert by_key[CONF_REMEMBER_SESSION].hidden is True
    assert by_key[CONF_ACTION_CLEAR_AUTH].hidden is False


async def test_qr_action_persists_tokens_into_values() -> None:
    """CONF_ACTION_AUTH_QR action calls perform_qr_auth and stores both tokens."""
    mass = _make_mock_mass({})
    values: dict[str, Any] = {
        CONF_YM_INSTANCE: YM_INSTANCE_OWN,
        "session_id": "sess-1",
    }

    with mock.patch(
        "music_assistant.providers.yandex_ynison.perform_qr_auth",
        new=mock.AsyncMock(return_value=("x-tok", "music-tok", "alice")),
    ) as mocked:
        await get_config_entries(mass, action=CONF_ACTION_AUTH_QR, values=values)

    mocked.assert_awaited_once_with(mass, "sess-1")
    assert values[CONF_TOKEN] == "music-tok"
    assert values[CONF_X_TOKEN] == "x-tok"
    assert values[CONF_ACCOUNT_LOGIN] == "alice"


async def test_qr_action_without_remember_session_skips_x_token() -> None:
    """remember_session=False → music token stored, x_token cleared."""
    mass = _make_mock_mass({})
    values: dict[str, Any] = {
        CONF_YM_INSTANCE: YM_INSTANCE_OWN,
        CONF_REMEMBER_SESSION: False,
        "session_id": "sess-1",
    }

    with mock.patch(
        "music_assistant.providers.yandex_ynison.perform_qr_auth",
        new=mock.AsyncMock(return_value=("x-tok", "music-tok", "alice")),
    ):
        await get_config_entries(mass, action=CONF_ACTION_AUTH_QR, values=values)

    assert values[CONF_TOKEN] == "music-tok"
    assert values[CONF_X_TOKEN] is None
    assert values[CONF_ACCOUNT_LOGIN] == "alice"


async def test_qr_action_in_borrow_mode_is_refused() -> None:
    """A stray QR action while the dropdown is on borrow must not mutate values."""
    mass = _make_mock_mass({"ym-a": {"domain": "yandex_music", "name": "Primary"}})
    values: dict[str, Any] = {CONF_YM_INSTANCE: "ym-a", "session_id": "sess-1"}

    with (
        mock.patch(
            "music_assistant.providers.yandex_ynison.perform_qr_auth", new=mock.AsyncMock()
        ) as mocked,
        pytest.raises(LoginFailed, match="own-mode action"),
    ):
        await get_config_entries(mass, action=CONF_ACTION_AUTH_QR, values=values)

    mocked.assert_not_awaited()
    assert CONF_TOKEN not in values
    assert CONF_X_TOKEN not in values


async def test_clear_action_in_borrow_mode_is_refused() -> None:
    """Clear-auth must also be refused outside own mode."""
    mass = _make_mock_mass({"ym-a": {"domain": "yandex_music", "name": "Primary"}})
    values: dict[str, Any] = {
        CONF_YM_INSTANCE: "ym-a",
        CONF_TOKEN: "leftover",
        CONF_X_TOKEN: "leftover-x",
    }

    with pytest.raises(LoginFailed, match="own-mode action"):
        await get_config_entries(mass, action=CONF_ACTION_CLEAR_AUTH, values=values)

    # Borrow-mode token fields must be untouched on refusal.
    assert values[CONF_TOKEN] == "leftover"
    assert values[CONF_X_TOKEN] == "leftover-x"


async def test_qr_action_without_session_id_raises() -> None:
    """Missing session_id is a programmer error from the MA frontend."""
    mass = _make_mock_mass({})
    with pytest.raises(LoginFailed, match="session_id"):
        await get_config_entries(
            mass,
            action=CONF_ACTION_AUTH_QR,
            values={CONF_YM_INSTANCE: YM_INSTANCE_OWN},
        )


async def test_clear_auth_action_zeroes_token_x_token_login() -> None:
    """CONF_ACTION_CLEAR_AUTH wipes all three persisted auth fields."""
    mass = _make_mock_mass({})
    values: dict[str, Any] = {
        CONF_YM_INSTANCE: YM_INSTANCE_OWN,
        CONF_TOKEN: "music-tok",
        CONF_X_TOKEN: "x-tok",
        CONF_ACCOUNT_LOGIN: "alice",
    }

    await get_config_entries(mass, action=CONF_ACTION_CLEAR_AUTH, values=values)

    assert values[CONF_TOKEN] is None
    assert values[CONF_X_TOKEN] is None
    assert values[CONF_ACCOUNT_LOGIN] is None


async def test_own_mode_with_only_x_token_marks_token_optional() -> None:
    """Stored x_token alone is enough — the token field becomes optional."""
    mass = _make_mock_mass({})
    entries = await get_config_entries(
        mass,
        values={CONF_YM_INSTANCE: YM_INSTANCE_OWN, CONF_X_TOKEN: "x-tok"},
    )
    by_key = _entries_by_key(entries)
    token = by_key[CONF_TOKEN]
    assert token.required is False


async def test_stale_ym_selection_normalizes_to_own() -> None:
    """A saved selection pointing at a removed YM instance is normalized to OWN.

    Guards against the dropdown rendering with a default_value that is not in
    its options, AND ensures the in-memory `values` dict is rewritten so a
    no-touch Save persists the correction (otherwise the stored config stays
    stale and the provider keeps trying borrow-mode against a missing instance
    until the user manually re-saves).
    """
    mass = _make_mock_mass({"ym-b": {"domain": "yandex_music", "name": "B"}})
    values: dict[str, object] = {CONF_YM_INSTANCE: "ym-removed"}
    # `arg-type`: upstream (music-assistant-models ≥ 1.1.117) flags
    # `dict[str, object]` vs `dict[str, ConfigValueType] | None`; the
    # local pin (1.1.111) accepts it, so the local mypy gate sees the
    # ignore as unused. Combine both codes so the comment is correct
    # under either dependency version.
    entries = await get_config_entries(mass, values=values)  # type: ignore[arg-type, unused-ignore]
    by_key = _entries_by_key(entries)

    ym_source = by_key[CONF_YM_INSTANCE]
    option_values = {opt.value for opt in ym_source.options}
    assert ym_source.default_value == YM_INSTANCE_OWN
    assert ym_source.default_value in option_values
    # The stale id must be rewritten in `values` so a Save without touching the
    # dropdown persists the corrected selection.
    assert values[CONF_YM_INSTANCE] == YM_INSTANCE_OWN

    token = by_key[CONF_TOKEN]
    assert token.hidden is False
    assert token.required is True
