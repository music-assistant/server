"""
Borrow mode: skill auto-create uses a linked Yandex Music account.

Covers the account-source dropdown, no-Device-Flow semantics for a borrowed
token, read-only use (nothing persisted by this provider), and actionable
failure reporting — now expressed through the setup flow's borrow sub-flow.
"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest
from music_assistant_models.enums import ProviderType
from music_assistant_models.errors import LoginFailed
from ya_dialogs_api import SkillCreationState, load_artifacts
from ya_passport_auth.ma import BORROW_SOURCE_OWN

from music_assistant.models.setup_flow import SetupFlowError
from music_assistant.providers.yandex_smarthome.constants import (
    CONF_AUTH_X_TOKEN,
    CONF_CLOUD_INSTANCE_ID,
    CONF_CONNECTION_TYPE,
    CONF_YM_INSTANCE,
)
from music_assistant.providers.yandex_smarthome.ma_authenticator import make_authenticator
from music_assistant.providers.yandex_smarthome.setup_flow import (
    _collect_user,
    _provision_skill,
    _user_entries,
)

_SETUP_FLOW = "music_assistant.providers.yandex_smarthome.setup_flow"


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


def _fake_session(mass: mock.MagicMock, *, setup_data: dict[str, Any] | None = None) -> Any:
    """Build a lightweight SetupSession stand-in whose progress_until awaits inline."""
    session = mock.MagicMock()
    session.mass = mass
    session.flow_id = "flowid1234"
    session.context = SimpleNamespace(setup_data=dict(setup_data or {}))

    async def _progress_until(awaitable: Any, **_: Any) -> Any:
        return await awaitable

    session.progress_until = _progress_until
    session.progress = mock.MagicMock()
    return session


def _done_artifacts(skill_id: str = "skill-xyz") -> Any:
    return dataclasses.replace(
        load_artifacts(None), state=SkillCreationState.DONE, skill_id=skill_id
    )


class TestAccountSourceDropdown:
    """Account-source dropdown in the first setup form."""

    def test_dropdown_lists_instances_and_own(self) -> None:
        """List every Yandex Music instance plus the own-credentials option."""
        entries = _user_entries(
            "cloud", BORROW_SOURCE_OWN, "", [("ym-a", "Main"), ("ym-b", "Second")]
        )
        source = {e.key: e for e in entries}[CONF_YM_INSTANCE]
        option_values = [opt.value for opt in source.options]
        assert BORROW_SOURCE_OWN in option_values
        assert "ym-a" in option_values
        assert "ym-b" in option_values

    async def test_stale_selection_normalizes_to_own(self) -> None:
        """A prefilled selection pointing at a removed instance resets to own credentials."""
        mass = _make_mass({"ym-a": "Main"})
        session = _fake_session(mass, setup_data={CONF_YM_INSTANCE: "removed"})
        captured: dict[str, Any] = {}

        async def _form(entries: Any, **_: Any) -> dict[str, Any]:
            captured["entries"] = entries
            return {CONF_CONNECTION_TYPE: "cloud", CONF_YM_INSTANCE: BORROW_SOURCE_OWN}

        session.form = _form
        collected = dict(session.context.setup_data)
        await _collect_user(session, collected)
        source = {e.key: e for e in captured["entries"]}[CONF_YM_INSTANCE]
        assert source.value == BORROW_SOURCE_OWN


class TestAuthenticatorNoDeviceFlow:
    """Borrowed-token authenticator must never fall back to Device Flow."""

    async def test_no_device_flow_when_disallowed(self) -> None:
        """Fail with guidance when the borrowed token is rejected."""
        authenticator = make_authenticator(cached_x_token="test-x-borrowed")
        fake_client = mock.MagicMock()
        fake_client.refresh_passport_cookies = mock.AsyncMock(side_effect=RuntimeError("rejected"))
        cm = mock.MagicMock()
        cm.__aenter__ = mock.AsyncMock(return_value=fake_client)
        cm.__aexit__ = mock.AsyncMock(return_value=False)
        with (
            mock.patch("ya_passport_auth.PassportClient.create", return_value=cm),
            pytest.raises(LoginFailed, match="Yandex Music"),
        ):
            async with authenticator():
                pass

    async def test_missing_borrowed_token_fails_fast(self) -> None:
        """Fail with guidance when the linked account has no session token."""
        authenticator = make_authenticator(cached_x_token=None)
        fake_client = mock.MagicMock()
        cm = mock.MagicMock()
        cm.__aenter__ = mock.AsyncMock(return_value=fake_client)
        cm.__aexit__ = mock.AsyncMock(return_value=False)
        with (
            mock.patch("ya_passport_auth.PassportClient.create", return_value=cm),
            pytest.raises(LoginFailed, match="Yandex Music"),
        ):
            async with authenticator():
                pass


class TestProvisionSkillBorrow:
    """The auto-create sub-flow in borrow mode."""

    async def test_borrow_uses_ym_token_without_persistence(self) -> None:
        """Pass the borrowed token to the authenticator without storing it in setup data."""
        mass = _make_mass({"ym-a": "Main"})
        session = _fake_session(mass)
        collected: dict[str, Any] = {CONF_CLOUD_INSTANCE_ID: "ci-1"}
        with (
            mock.patch(f"{_SETUP_FLOW}.make_authenticator") as make_auth,
            mock.patch(f"{_SETUP_FLOW}.auto_create_skill", new_callable=mock.AsyncMock) as create,
            mock.patch(f"{_SETUP_FLOW}.load_default_logo_bytes", return_value=b"logo"),
        ):
            create.return_value = _done_artifacts("skill-xyz")
            skill_id = await _provision_skill(
                session,
                collected,
                connection_type="cloud_plus",
                skill_name="Test",
                ym_instance="ym-a",
            )
        assert skill_id == "skill-xyz"
        kwargs = make_auth.call_args.kwargs
        assert kwargs["cached_x_token"] == "test-x-ym"
        # borrow mode never persists the borrowed account's token into this provider
        assert not collected.get(CONF_AUTH_X_TOKEN)

    async def test_borrow_source_error_raises_setup_flow_error(self) -> None:
        """A missing linked instance aborts the flow with a clear error, not a Device Flow."""
        mass = _make_mass({"ym-a": "Main"})
        mass.get_provider.return_value = None  # instance not loaded
        session = _fake_session(mass)
        with (
            mock.patch(f"{_SETUP_FLOW}.make_authenticator") as make_auth,
            pytest.raises(SetupFlowError, match="not loaded"),
        ):
            await _provision_skill(
                session,
                {CONF_CLOUD_INSTANCE_ID: "ci-1"},
                connection_type="cloud_plus",
                skill_name="Test",
                ym_instance="ym-a",
            )
        make_auth.assert_not_called()

    async def test_failed_pipeline_raises_setup_flow_error(self) -> None:
        """A pipeline that does not reach DONE surfaces its last_error as a SetupFlowError."""
        mass = _make_mass({"ym-a": "Main"})
        session = _fake_session(mass)
        failed = dataclasses.replace(
            load_artifacts(None),
            state=SkillCreationState.FAILED,
            last_error="dialogs.yandex.ru rejected the request",
        )
        with (
            mock.patch(f"{_SETUP_FLOW}.make_authenticator"),
            mock.patch(f"{_SETUP_FLOW}.auto_create_skill", new_callable=mock.AsyncMock) as create,
            mock.patch(f"{_SETUP_FLOW}.load_default_logo_bytes", return_value=b"logo"),
        ):
            create.return_value = failed
            with pytest.raises(SetupFlowError, match="rejected the request"):
                await _provision_skill(
                    session,
                    {CONF_CLOUD_INSTANCE_ID: "ci-1"},
                    connection_type="cloud_plus",
                    skill_name="Test",
                    ym_instance="ym-a",
                )
