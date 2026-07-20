"""
Borrow mode: skill auto-create uses a linked Yandex Music account.

Covers spec 0001 — account-source dropdown, no-Device-Flow semantics for a
borrowed token, read-only use, and actionable failure reporting.
"""

from __future__ import annotations

import json
from typing import Any
from unittest import mock

import pytest
from music_assistant_models.enums import ProviderType
from music_assistant_models.errors import LoginFailed
from ya_passport_auth.ma import BORROW_SOURCE_OWN

from music_assistant.providers.yandex_smarthome import _run_auto_create_action, get_config_entries
from music_assistant.providers.yandex_smarthome.constants import (
    CONF_AUTH_X_TOKEN,
    CONF_AUTO_CREATE_ARTIFACTS,
    CONF_YM_INSTANCE,
)
from music_assistant.providers.yandex_smarthome.ma_authenticator import make_authenticator


def _make_mass(instances: dict[str, str] | None = None) -> mock.MagicMock:
    mass = mock.MagicMock()
    mass.config.get.return_value = {
        inst_id: {"domain": "yandex_music", "name": name}
        for inst_id, name in (instances or {}).items()
    }
    owner = mock.MagicMock()
    owner.domain = "yandex_music"
    owner.type = ProviderType.MUSIC
    owner.config.get_value = lambda key: {"x_token": "test-x-ym"}.get(key)
    mass.get_provider.return_value = owner
    mass.webserver.base_url = "http://ma.local:8095"
    return mass


def _by_key(entries: tuple[Any, ...]) -> dict[str, Any]:
    return {e.key: e for e in entries}


class TestDropdown:
    """Account-source dropdown in the config form."""

    async def test_dropdown_lists_instances_and_own(self) -> None:
        """List every Yandex Music instance plus the own-credentials option."""
        mass = _make_mass({"ym-a": "Main", "ym-b": "Second"})
        entries = await get_config_entries(mass, values={})
        source = _by_key(entries)[CONF_YM_INSTANCE]
        option_values = [opt.value for opt in source.options]
        assert BORROW_SOURCE_OWN in option_values
        assert "ym-a" in option_values
        assert "ym-b" in option_values

    async def test_stale_selection_normalizes_to_own(self) -> None:
        """Reset a selection pointing at a removed instance back to own credentials."""
        mass = _make_mass({"ym-a": "Main"})
        values: dict[str, Any] = {CONF_YM_INSTANCE: "removed"}
        await get_config_entries(mass, values=values)
        assert values[CONF_YM_INSTANCE] == BORROW_SOURCE_OWN


class TestAuthenticatorNoDeviceFlow:
    """Borrowed-token authenticator must never fall back to Device Flow."""

    async def test_no_device_flow_when_disallowed(self) -> None:
        """Fail with guidance when the borrowed token is rejected."""
        mass = mock.MagicMock()
        authenticator = make_authenticator(
            mass=mass,
            session_id="s1",
            cached_x_token="test-x-borrowed",
            allow_device_flow=False,
        )
        fake_client = mock.MagicMock()
        fake_client.refresh_passport_cookies = mock.AsyncMock(side_effect=RuntimeError("rejected"))
        fake_client.start_device_login = mock.AsyncMock()
        cm = mock.MagicMock()
        cm.__aenter__ = mock.AsyncMock(return_value=fake_client)
        cm.__aexit__ = mock.AsyncMock(return_value=False)
        with (
            mock.patch("ya_passport_auth.PassportClient.create", return_value=cm),
            pytest.raises(LoginFailed, match="Yandex Music"),
        ):
            async with authenticator():
                pass
        fake_client.start_device_login.assert_not_awaited()

    async def test_missing_borrowed_token_fails_fast(self) -> None:
        """Fail with guidance when the linked account has no session token."""
        mass = mock.MagicMock()
        authenticator = make_authenticator(
            mass=mass,
            session_id="s1",
            cached_x_token=None,
            allow_device_flow=False,
        )
        fake_client = mock.MagicMock()
        fake_client.start_device_login = mock.AsyncMock()
        cm = mock.MagicMock()
        cm.__aenter__ = mock.AsyncMock(return_value=fake_client)
        cm.__aexit__ = mock.AsyncMock(return_value=False)
        with (
            mock.patch("ya_passport_auth.PassportClient.create", return_value=cm),
            pytest.raises(LoginFailed, match="Yandex Music"),
        ):
            async with authenticator():
                pass
        fake_client.start_device_login.assert_not_awaited()


class TestAutoCreateBorrow:
    """Auto-create action wiring in borrow mode."""

    async def test_borrow_uses_ym_token_without_persistence(self) -> None:
        """Pass the borrowed token through without storing it in config."""
        mass = _make_mass({"ym-a": "Main"})
        values: dict[str, Any] = {
            CONF_YM_INSTANCE: "ym-a",
            "session_id": "s1",
            "instance_name": "Test",
            "external_base_url": "https://ma.example.com",
            "cloud_instance_id": "ci-1",
        }
        with (
            mock.patch(
                "music_assistant.providers.yandex_smarthome.make_authenticator"
            ) as make_auth,
            mock.patch(
                "music_assistant.providers.yandex_smarthome.auto_create_skill",
                new_callable=mock.AsyncMock,
            ) as create,
        ):
            create.side_effect = RuntimeError("stop after auth wiring")
            await _run_auto_create_action(mass, values, "cloud_plus", None)
        kwargs = make_auth.call_args.kwargs
        assert kwargs["cached_x_token"] == "test-x-ym"
        assert kwargs["allow_device_flow"] is False
        assert kwargs["on_token_obtained"] is None
        assert not values.get(CONF_AUTH_X_TOKEN)

    async def test_borrow_source_error_lands_in_artifacts(self) -> None:
        """Report a missing linked instance in the artifacts blob, not via Device Flow."""
        mass = _make_mass({"ym-a": "Main"})
        mass.get_provider.return_value = None  # instance not loaded
        values: dict[str, Any] = {
            CONF_YM_INSTANCE: "ym-a",
            "session_id": "s1",
            "external_base_url": "https://ma.example.com",
            "cloud_instance_id": "ci-1",
        }
        with mock.patch(
            "music_assistant.providers.yandex_smarthome.make_authenticator"
        ) as make_auth:
            await _run_auto_create_action(mass, values, "cloud_plus", None)
        make_auth.assert_not_called()
        artifacts = json.loads(str(values[CONF_AUTO_CREATE_ARTIFACTS]))
        assert artifacts["state"] == "failed"
        assert "not loaded" in artifacts["last_error"]
