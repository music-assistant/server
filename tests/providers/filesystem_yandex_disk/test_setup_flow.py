"""Tests for the Yandex Disk guided setup flow."""

from __future__ import annotations

import asyncio
import sys
import time
from typing import Any
from unittest import mock

import pytest
from music_assistant_models.config_entries import ProviderConfig
from music_assistant_models.enums import FlowStepType, ProviderStage, ProviderType
from music_assistant_models.provider import ProviderManifest

from music_assistant.models.setup_flow import AbortFlow, SetupFlowContext, SetupSession
from music_assistant.providers.filesystem_cloud.base import (
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_FOLDER_ID,
    CONF_REFRESH_TOKEN,
)
from music_assistant.providers.filesystem_yandex_disk import setup as setup_provider
from music_assistant.providers.filesystem_yandex_disk import setup_flow
from music_assistant.providers.filesystem_yandex_disk.auth import (
    DeviceCodeDenied,
    DeviceCodeExpired,
    DeviceCodeGrant,
    DevicePollState,
    OAuthProtocolError,
    OAuthTokens,
    OAuthTransportError,
)
from music_assistant.providers.filesystem_yandex_disk.provider import YandexDiskFileSystemProvider

provider_module = sys.modules[YandexDiskFileSystemProvider.__module__]


def _grant(code: str = "CODE-1234") -> DeviceCodeGrant:
    """Return a device grant suitable for setup tests."""
    return DeviceCodeGrant(
        device_code=f"device-{code}",
        user_code=code,
        verification_url="https://yandex.ru/activate",
        expires_in=300,
        interval=5,
    )


def _make_session(
    finish_handler: Any,
    *,
    setup_data: dict[str, Any] | None = None,
) -> tuple[SetupSession, mock.Mock]:
    """Build a real SetupSession backed by a lightweight Music Assistant mock."""
    mass = mock.Mock()
    mass.http_session = object()
    context = SetupFlowContext(
        kind="setup",
        reason="user",
        domain="filesystem_yandex_disk",
        setup_data=setup_data or {},
    )
    return SetupSession(mass, "flow-test", context, finish_handler), mass


async def _wait_for(predicate: Any, timeout: float = 5.0) -> Any:
    """Wait for a setup-flow state transition."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result := predicate():
            return result
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met within timeout")


async def _submit_user_form(
    session: SetupSession,
    secret: str | None = None,
    folder_id: str = "root",
) -> None:
    """Wait for and submit the common cloud setup form."""
    await _wait_for(lambda: session.current_step and session.current_step.type == FlowStepType.FORM)
    session.handle_submit(
        {
            "content_type": "music",
            CONF_CLIENT_ID: "client-id",
            CONF_CLIENT_SECRET: "client-secret" if secret is None else secret,
            CONF_FOLDER_ID: folder_id,
        }
    )


async def test_setup_finishes_with_refresh_token_and_device_progress() -> None:
    """Successful confirmation persists setup data and shows the user code."""
    collected: dict[str, Any] = {}

    async def finish(_session: SetupSession, values: dict[str, Any]) -> dict[str, str]:
        collected.update(values)
        return {"instance_id": "filesystem_yandex_disk--1"}

    session, mass = _make_session(finish)
    tokens = OAuthTokens("access-token", "refresh-token", 3600)
    with (
        mock.patch.object(setup_flow, "request_device_code", mock.AsyncMock(return_value=_grant())),
        mock.patch.object(setup_flow, "poll_device_token", mock.AsyncMock(return_value=tokens)),
    ):
        task = asyncio.create_task(setup_flow.run_setup(session))
        await _submit_user_form(session)
        await _wait_for(lambda: session.finished)
        await task

    assert collected == {
        "content_type": "music",
        CONF_CLIENT_ID: "client-id",
        CONF_CLIENT_SECRET: "client-secret",
        CONF_FOLDER_ID: "root",
        CONF_REFRESH_TOKEN: "refresh-token",
    }
    steps = [call.kwargs["data"] for call in mass.signal_event.call_args_list]
    progress = [step for step in steps if step.type == FlowStepType.PROGRESS]
    assert progress[0].step_id == "device_login"
    assert progress[0].image.startswith("data:image/svg+xml;base64,")


async def test_setup_data_initializes_provider_and_registers_stream_route() -> None:
    """Setup output initializes the provider and registers its stream route."""
    provider_instance: Any = None
    api = mock.Mock()
    api.validate = mock.AsyncMock()
    api.exists_dir = mock.AsyncMock(return_value=True)
    api.close = mock.AsyncMock()

    mass = mock.Mock()
    mass.http_session = object()
    mass.cache = mock.Mock()
    mass.streams.base_url = "http://ma.local:8095"
    mass.streams.register_dynamic_route.return_value = mock.Mock()
    mass.config.decrypt_string.side_effect = lambda value: value

    manifest = ProviderManifest(
        type=ProviderType.MUSIC,
        domain="filesystem_yandex_disk",
        name="Yandex Disk",
        description="Yandex Disk test provider",
        codeowners=["@TrudenBoy"],
        stage=ProviderStage.BETA,
        requirements=["yadisk==3.4.1"],
        multi_instance=True,
    )

    async def finish(_session: SetupSession, values: dict[str, Any]) -> dict[str, str]:
        nonlocal provider_instance
        config = ProviderConfig(
            values={},
            type=ProviderType.MUSIC,
            domain=manifest.domain,
            instance_id="filesystem_yandex_disk--lifecycle",
            setup_data=dict(values),
        )
        mass.config.get.side_effect = lambda path: (
            config.setup_data if path.endswith("/setup_data") else {}
        )
        provider_instance = await setup_provider(mass, manifest, config)
        await provider_instance.handle_async_init()
        return {"instance_id": config.instance_id}

    session = SetupSession(
        mass,
        "flow-lifecycle",
        SetupFlowContext(
            kind="setup",
            reason="user",
            domain=manifest.domain,
            setup_data={},
        ),
        finish,
    )
    with (
        mock.patch.object(setup_flow, "request_device_code", mock.AsyncMock(return_value=_grant())),
        mock.patch.object(
            setup_flow,
            "poll_device_token",
            mock.AsyncMock(return_value=OAuthTokens("access", "refresh", 3600)),
        ),
        mock.patch.object(provider_module, "YandexDiskApi", return_value=api),
    ):
        task = asyncio.create_task(setup_flow.run_setup(session))
        await _submit_user_form(session, folder_id="disk:/Music")
        await _wait_for(lambda: session.finished)
        await task

    assert provider_instance.root_folder_id == "disk:/Music"
    api.validate.assert_awaited_once_with()
    api.exists_dir.assert_awaited_once_with("disk:/Music")
    mass.streams.register_dynamic_route.assert_called_once()
    assert (
        mass.streams.register_dynamic_route.call_args.args[0]
        == "/filesystem_yandex_disk--lifecycle_stream"
    )


async def test_reconfigure_reuses_stored_client_secret() -> None:
    """A blank secret on reconfigure reuses the decrypted stored setup value."""

    async def finish(_session: SetupSession, _values: dict[str, Any]) -> dict[str, str]:
        return {"instance_id": "filesystem_yandex_disk--1"}

    session, _mass = _make_session(
        finish,
        setup_data={
            CONF_CLIENT_ID: "client-id",
            CONF_CLIENT_SECRET: "stored-secret",
            CONF_FOLDER_ID: "root",
        },
    )
    request = mock.AsyncMock(return_value=_grant())
    poll = mock.AsyncMock(return_value=OAuthTokens("access", "refresh", 3600))
    with (
        mock.patch.object(setup_flow, "request_device_code", request),
        mock.patch.object(setup_flow, "poll_device_token", poll),
    ):
        task = asyncio.create_task(setup_flow.run_setup(session))
        await _submit_user_form(session, secret="")
        await _wait_for(lambda: session.finished)
        await task

    poll.assert_awaited_once()
    assert poll.await_args is not None
    assert poll.await_args.args[3] == "stored-secret"


async def test_expired_device_code_is_replaced() -> None:
    """An expired device grant is replaced without returning to the user form."""

    async def finish(_session: SetupSession, _values: dict[str, Any]) -> dict[str, str]:
        return {"instance_id": "filesystem_yandex_disk--1"}

    session, _mass = _make_session(finish)
    request = mock.AsyncMock(side_effect=[_grant("OLD"), _grant("NEW")])
    poll = mock.AsyncMock(
        side_effect=[DeviceCodeExpired("expired"), OAuthTokens("access", "refresh", 3600)]
    )
    with (
        mock.patch.object(setup_flow, "request_device_code", request),
        mock.patch.object(setup_flow, "poll_device_token", poll),
    ):
        task = asyncio.create_task(setup_flow.run_setup(session))
        await _submit_user_form(session)
        await _wait_for(lambda: session.finished)
        await task

    assert request.await_count == 2


async def test_poll_slow_down_increases_interval() -> None:
    """Yandex slow_down increases subsequent polling intervals by five seconds."""
    session = mock.Mock()
    session.mass.http_session = object()
    sleeps: list[int] = []

    async def record_sleep(delay: int) -> None:
        sleeps.append(delay)

    poll = mock.AsyncMock(
        side_effect=[
            DevicePollState.SLOW_DOWN,
            DevicePollState.PENDING,
            OAuthTokens("access", "refresh", 3600),
        ]
    )
    with (
        mock.patch.object(setup_flow, "poll_device_token", poll),
        mock.patch.object(asyncio, "sleep", record_sleep),
    ):
        tokens = await setup_flow._poll_until_confirmed(
            session, _grant(), "client-id", "client-secret"
        )

    assert tokens.refresh_token == "refresh"
    assert sleeps == [10, 10]


async def test_denied_device_login_aborts_flow() -> None:
    """An explicit denial becomes a localized clean flow abort."""

    async def finish(_session: SetupSession, _values: dict[str, Any]) -> dict[str, str]:
        raise AssertionError("finish must not be called")

    session, _mass = _make_session(finish)
    with (
        mock.patch.object(setup_flow, "request_device_code", mock.AsyncMock(return_value=_grant())),
        mock.patch.object(
            setup_flow,
            "poll_device_token",
            mock.AsyncMock(side_effect=DeviceCodeDenied("denied")),
        ),
    ):
        task = asyncio.create_task(setup_flow.run_setup(session))
        await _submit_user_form(session)
        with pytest.raises(AbortFlow, match="login_denied"):
            await task


@pytest.mark.parametrize(
    ("failure", "translation_key"),
    [
        (OAuthProtocolError("invalid client"), "oauth_error"),
        (OAuthTransportError("offline"), "connection_error"),
    ],
)
async def test_oauth_error_returns_to_form_with_base_error(
    failure: Exception, translation_key: str
) -> None:
    """A non-terminal OAuth setup failure lets the user correct credentials."""

    async def finish(_session: SetupSession, _values: dict[str, Any]) -> dict[str, str]:
        raise AssertionError("finish must not be called")

    session, mass = _make_session(finish)
    request = mock.AsyncMock(side_effect=failure)
    with mock.patch.object(setup_flow, "request_device_code", request):
        task = asyncio.create_task(setup_flow.run_setup(session))
        await _submit_user_form(session)
        await _wait_for(lambda: mass.signal_event.call_count >= 2)
        assert session.current_step is not None
        assert session.current_step.type == FlowStepType.FORM
        assert session.current_step.errors == {"base": translation_key}
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
