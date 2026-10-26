"""Tests for the native Yandex Smart Home setup flow."""

from __future__ import annotations

import base64
import dataclasses
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest
from ya_dialogs_api import SkillCreationState, load_artifacts
from ya_passport_auth import InvalidCredentialsError
from ya_passport_auth.ma import BORROW_SOURCE_OWN

from music_assistant.models.setup_flow import AbortFlow
from music_assistant.providers.yandex_smarthome.constants import (
    CONF_AUTH_X_TOKEN,
    CONF_CLOUD_CONNECTION_TOKEN,
    CONF_CLOUD_INSTANCE_ID,
    CONF_CLOUD_INSTANCE_PASSWORD,
    CONF_CONNECTION_TYPE,
    CONF_DIRECT_CLIENT_SECRET,
    CONF_EXTERNAL_BASE_URL,
    CONF_SKILL_ID,
    CONF_SKILL_TOKEN,
    CONF_YM_INSTANCE,
    CONNECTION_TYPE_CLOUD,
    CONNECTION_TYPE_CLOUD_PLUS,
    CONNECTION_TYPE_DIRECT,
)
from music_assistant.providers.yandex_smarthome.setup_flow import (
    _code_image,
    _collect_skill_token,
    _collect_user,
    _device_image,
    _device_login,
    _provision_skill,
    _run_cloud,
    _run_cloud_plus,
    _run_direct,
    _user_entries,
)

_SETUP_FLOW = "music_assistant.providers.yandex_smarthome.setup_flow"


def _fake_session(*, setup_data: dict[str, Any] | None = None) -> Any:
    """Build a setup-session stand-in whose progress helper awaits inline."""
    session = mock.MagicMock()
    session.mass = mock.MagicMock()
    session.mass.webserver.base_url = "http://ma.local:8095"
    session.flow_id = "flow-id"
    session.context = SimpleNamespace(setup_data=dict(setup_data or {}))
    session.form = mock.AsyncMock(return_value={})
    session.finish = mock.AsyncMock()
    session.progress = mock.MagicMock()

    async def _progress_until(awaitable: Any, **_: Any) -> Any:
        return await awaitable

    session.progress_until = mock.AsyncMock(side_effect=_progress_until)
    return session


def _done_artifacts(skill_id: str) -> Any:
    """Create completed skill artifacts for provisioning tests."""
    return dataclasses.replace(
        load_artifacts(None), state=SkillCreationState.DONE, skill_id=skill_id
    )


def test_user_entries_offer_all_connection_modes() -> None:
    """The first form offers Cloud, Cloud Plus, and Direct modes."""
    entries = _user_entries(CONNECTION_TYPE_CLOUD, BORROW_SOURCE_OWN, "", [])
    connection = {entry.key: entry for entry in entries}[CONF_CONNECTION_TYPE]

    assert [option.value for option in connection.options] == [
        CONNECTION_TYPE_CLOUD,
        CONNECTION_TYPE_CLOUD_PLUS,
        CONNECTION_TYPE_DIRECT,
    ]


async def test_direct_reprompts_when_effective_url_is_not_https() -> None:
    """Direct setup rejects an HTTP public URL before provisioning."""
    session = _fake_session()
    session.form = mock.AsyncMock(
        side_effect=[
            {
                CONF_CONNECTION_TYPE: CONNECTION_TYPE_DIRECT,
                CONF_EXTERNAL_BASE_URL: "http://public.example",
                CONF_YM_INSTANCE: BORROW_SOURCE_OWN,
            },
            {
                CONF_CONNECTION_TYPE: CONNECTION_TYPE_CLOUD,
                CONF_YM_INSTANCE: BORROW_SOURCE_OWN,
            },
        ]
    )

    with mock.patch(f"{_SETUP_FLOW}.list_yandex_music_instances", return_value=[]):
        result = await _collect_user(session, {})

    assert result[0] == CONNECTION_TYPE_CLOUD
    assert session.form.await_args_list[1].kwargs["errors"] == {"base": "direct_requires_https"}


async def test_cloud_registers_shows_otp_and_finishes() -> None:
    """Cloud setup persists relay credentials after displaying its OTP."""
    session = _fake_session()
    collected: dict[str, Any] = {}
    with (
        mock.patch(
            f"{_SETUP_FLOW}.register_cloud_instance", new_callable=mock.AsyncMock
        ) as register,
        mock.patch(f"{_SETUP_FLOW}.get_cloud_otp", new_callable=mock.AsyncMock) as get_otp,
    ):
        register.return_value = {
            "id": "cloud-id",
            "password": "cloud-password",
            "connection_token": "connection-token",
        }
        get_otp.return_value = "1234"

        await _run_cloud(session, collected)

    assert collected == {
        CONF_CLOUD_INSTANCE_ID: "cloud-id",
        CONF_CLOUD_INSTANCE_PASSWORD: "cloud-password",
        CONF_CLOUD_CONNECTION_TOKEN: "connection-token",
    }
    session.progress.assert_called_once()
    session.finish.assert_awaited_once_with(collected)


async def test_own_rejected_cache_runs_one_fresh_device_login() -> None:
    """A rejected own cache triggers one native Device Flow and retry."""
    session = _fake_session(setup_data={CONF_AUTH_X_TOKEN: "expired"})
    collected = {
        **session.context.setup_data,
        CONF_CLOUD_INSTANCE_ID: "cloud-id",
    }
    with (
        mock.patch(f"{_SETUP_FLOW}.make_authenticator") as make_auth,
        mock.patch(f"{_SETUP_FLOW}.auto_create_skill", new_callable=mock.AsyncMock) as create,
        mock.patch(f"{_SETUP_FLOW}.load_default_logo_bytes", return_value=b"logo"),
        mock.patch(f"{_SETUP_FLOW}._device_login", new_callable=mock.AsyncMock) as device_login,
    ):
        create.side_effect = [
            InvalidCredentialsError("expired"),
            _done_artifacts("skill-id"),
        ]
        device_login.return_value = "fresh-token"

        result = await _provision_skill(
            session,
            collected,
            connection_type=CONNECTION_TYPE_CLOUD_PLUS,
            skill_name="Test",
            ym_instance=BORROW_SOURCE_OWN,
        )

    assert result == "skill-id"
    assert collected[CONF_AUTH_X_TOKEN] == "fresh-token"
    assert [call.kwargs for call in make_auth.call_args_list] == [
        {"cached_x_token": "expired"},
        {"cached_x_token": "fresh-token"},
    ]
    device_login.assert_awaited_once_with(session)


async def test_device_login_denial_aborts_with_translation_key() -> None:
    """A denied native Device Flow aborts with its localized reason."""
    session = _fake_session()
    device = SimpleNamespace(
        user_code="ABCD-1234",
        verification_url="https://ya.ru/device",
        expires_in=300,
    )
    client = mock.MagicMock()
    client.start_device_login = mock.AsyncMock(return_value=device)
    client.poll_device_until_confirmed = mock.MagicMock(
        return_value=mock.AsyncMock(side_effect=InvalidCredentialsError("denied"))()
    )
    client_cm = mock.MagicMock()
    client_cm.__aenter__ = mock.AsyncMock(return_value=client)
    client_cm.__aexit__ = mock.AsyncMock(return_value=False)

    with (
        mock.patch("ya_passport_auth.PassportClient.create", return_value=client_cm),
        pytest.raises(AbortFlow) as err,
    ):
        await _device_login(session)

    assert err.value.args == ("device_login_denied",)


async def test_direct_generates_client_secret_before_provisioning() -> None:
    """Direct setup creates its OAuth secret before deriving skill URLs."""
    session = _fake_session()
    collected: dict[str, Any] = {}
    with (
        mock.patch(f"{_SETUP_FLOW}._provision_skill", new_callable=mock.AsyncMock) as provision,
        mock.patch(
            f"{_SETUP_FLOW}._collect_skill_token", new_callable=mock.AsyncMock
        ) as collect_token,
    ):
        provision.return_value = "skill-id"
        collect_token.return_value = None
        await _run_direct(session, collected, "Test", BORROW_SOURCE_OWN)

    assert collected[CONF_DIRECT_CLIENT_SECRET]
    assert provision.await_args is not None
    assert provision.await_args.kwargs["connection_type"] == CONNECTION_TYPE_DIRECT
    session.finish.assert_awaited_once_with(collected)


async def test_cloud_plus_auto_provisions_then_links() -> None:
    """Cloud Plus automatic setup follows registration, provisioning, and linking order."""
    session = _fake_session()
    session.form = mock.AsyncMock(return_value={"skill_method": "auto"})
    collected: dict[str, Any] = {}
    with (
        mock.patch(
            f"{_SETUP_FLOW}.register_cloud_instance", new_callable=mock.AsyncMock
        ) as register,
        mock.patch(f"{_SETUP_FLOW}._provision_skill", new_callable=mock.AsyncMock) as provision,
        mock.patch(
            f"{_SETUP_FLOW}._collect_skill_token", new_callable=mock.AsyncMock
        ) as collect_token,
        mock.patch(f"{_SETUP_FLOW}._show_linking_code", new_callable=mock.AsyncMock) as show_code,
    ):
        register.return_value = {
            "id": "cloud-id",
            "password": "cloud-password",
            "connection_token": "connection-token",
        }
        provision.return_value = "skill-id"
        collect_token.return_value = None

        await _run_cloud_plus(session, collected, "Test", BORROW_SOURCE_OWN)

    assert register.await_args is not None
    assert register.await_args.kwargs["platform"] == "yandex"
    assert collected[CONF_SKILL_ID] == "skill-id"
    provision.assert_awaited_once()
    show_code.assert_awaited_once()
    session.finish.assert_awaited_once_with(collected)


async def test_cloud_plus_manual_accepts_existing_skill_id() -> None:
    """Cloud Plus manual setup bypasses automatic provisioning."""
    session = _fake_session()
    session.form = mock.AsyncMock(
        side_effect=[{"skill_method": "manual"}, {CONF_SKILL_ID: "existing-skill"}]
    )
    collected: dict[str, Any] = {}
    with (
        mock.patch(
            f"{_SETUP_FLOW}.register_cloud_instance", new_callable=mock.AsyncMock
        ) as register,
        mock.patch(f"{_SETUP_FLOW}._provision_skill", new_callable=mock.AsyncMock) as provision,
        mock.patch(
            f"{_SETUP_FLOW}._collect_skill_token", new_callable=mock.AsyncMock
        ) as collect_token,
        mock.patch(f"{_SETUP_FLOW}._show_linking_code", new_callable=mock.AsyncMock),
    ):
        register.return_value = {
            "id": "cloud-id",
            "password": "cloud-password",
            "connection_token": "connection-token",
        }
        collect_token.return_value = None

        await _run_cloud_plus(session, collected, "Test", BORROW_SOURCE_OWN)

    assert collected[CONF_SKILL_ID] == "existing-skill"
    provision.assert_not_awaited()
    session.finish.assert_awaited_once_with(collected)


async def test_skill_token_reprompts_after_empty_submission() -> None:
    """An empty skill token produces a localized error before accepting a retry."""
    session = _fake_session()
    session.form = mock.AsyncMock(side_effect=[{}, {CONF_SKILL_TOKEN: "oauth-token"}])
    collected: dict[str, Any] = {}

    errors = await _collect_skill_token(session, collected, None)
    assert errors == {"base": "skill_token_required"}

    errors = await _collect_skill_token(session, collected, errors)
    assert errors is None
    assert collected[CONF_SKILL_TOKEN] == "oauth-token"
    assert session.form.await_args_list[1].kwargs["errors"] == {"base": "skill_token_required"}


async def test_provisioning_tracks_intermediate_artifacts() -> None:
    """Provisioning stores progress artifacts before persisting the final result."""
    session = _fake_session()
    collected: dict[str, Any] = {CONF_CLOUD_INSTANCE_ID: "cloud-id"}
    observed_states: list[SkillCreationState] = []

    async def _create(**kwargs: Any) -> Any:
        intermediate = dataclasses.replace(
            load_artifacts(None), state=SkillCreationState.APP_CREATED
        )
        await kwargs["progress_cb"](intermediate)
        observed_states.append(load_artifacts(str(collected["auto_create_artifacts"])).state)
        return _done_artifacts("skill-id")

    with (
        mock.patch(f"{_SETUP_FLOW}.make_authenticator"),
        mock.patch(f"{_SETUP_FLOW}.auto_create_skill", side_effect=_create),
        mock.patch(f"{_SETUP_FLOW}.load_default_logo_bytes", return_value=b"logo"),
        mock.patch(f"{_SETUP_FLOW}._borrowed_x_token", return_value="borrowed-token"),
    ):
        result = await _provision_skill(
            session,
            collected,
            connection_type=CONNECTION_TYPE_CLOUD_PLUS,
            skill_name="Test",
            ym_instance="linked-account",
        )

    assert result == "skill-id"
    assert observed_states == [SkillCreationState.APP_CREATED]


async def test_device_login_timeout_propagates_to_setup_engine() -> None:
    """Setup-session expiry remains responsible for rendering timeout state."""
    session = _fake_session()
    session.progress_until = mock.AsyncMock(side_effect=TimeoutError("expired"))
    device = SimpleNamespace(
        user_code="ABCD-1234",
        verification_url="https://ya.ru/device",
        expires_in=300,
    )
    client = mock.MagicMock()
    client.start_device_login = mock.AsyncMock(return_value=device)
    client.poll_device_until_confirmed.return_value = mock.AsyncMock()()
    client_cm = mock.MagicMock()
    client_cm.__aenter__ = mock.AsyncMock(return_value=client)
    client_cm.__aexit__ = mock.AsyncMock(return_value=False)

    with (
        mock.patch("ya_passport_auth.PassportClient.create", return_value=client_cm),
        pytest.raises(TimeoutError, match="expired"),
    ):
        await _device_login(session)


def test_setup_images_escape_dynamic_values() -> None:
    """Device and linking-code SVGs escape values before embedding them."""
    device_svg = base64.b64decode(
        _device_image("</text><script>", "https://x/<tag>").split(",", 1)[1]
    ).decode()
    code_svg = base64.b64decode(_code_image("</text><script>").split(",", 1)[1]).decode()

    assert "</text><script>" not in device_svg
    assert "</text><script>" not in code_svg
    assert "&lt;script&gt;" in device_svg
    assert "&lt;script&gt;" in code_svg
