"""Tests for the Alexa Devices setup flow."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Any
from unittest import mock

import pytest
from aioamazondevices.exceptions import CannotAuthenticate, CannotConnect, CannotRetrieveData
from music_assistant_models.enums import FlowStepType

from music_assistant.constants import CONF_PASSWORD, CONF_USERNAME
from music_assistant.models.setup_flow import SetupFlowContext, SetupSession
from music_assistant.providers.alexa_devices import setup_flow
from music_assistant.providers.alexa_devices.constants import (
    CONF_API_BASIC_AUTH_PASSWORD,
    CONF_API_BASIC_AUTH_USERNAME,
    CONF_API_URL,
    CONF_LOGIN_DATA,
    CONF_OTP_CODE,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType

LOGIN_DATA = {"site": "https://www.amazon.it", "customer_info": {"user_id": "AMZN1"}}
CREDENTIALS: dict[str, ConfigValueType] = {
    CONF_USERNAME: "user@example.com",
    CONF_PASSWORD: "secret",
    CONF_OTP_CODE: "123456",
    CONF_API_URL: "http://skill:5000",
    CONF_API_BASIC_AUTH_USERNAME: "skill_user",
    CONF_API_BASIC_AUTH_PASSWORD: "skill_pass",
}


@pytest.fixture
def amazon_api() -> Iterator[mock.MagicMock]:
    """Mock the aioamazondevices API class."""
    with mock.patch(
        "music_assistant.providers.alexa_devices.provider.AmazonEchoApi", autospec=True
    ) as api_class:
        api_class.return_value.login.login_mode_interactive = mock.AsyncMock(
            return_value=LOGIN_DATA
        )
        yield api_class


def _make_session(
    collected: dict[str, Any], setup_data: dict[str, Any] | None = None
) -> SetupSession:
    """Build a real SetupSession that records the finished values."""

    async def finish(_session: SetupSession, values: dict[str, Any]) -> dict[str, str]:
        collected.update(values)
        return {"instance_id": "alexa_devices--1"}

    mass = mock.Mock()
    mass.storage_path = "/tmp/ma"  # noqa: S108 - never written to, the library is mocked
    context = SetupFlowContext(
        kind="setup", reason="user", domain="alexa_devices", setup_data=setup_data or {}
    )
    return SetupSession(mass, "flow-test", context, finish)


async def _wait_for(predicate: Callable[[], Any], timeout: float = 5.0) -> Any:
    """Wait until the predicate returns truthy (or fail the test)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result := predicate():
            return result
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met within timeout")


async def _wait_form(session: SetupSession, errors: dict[str, str] | None = None) -> None:
    """Wait until the credentials form is presented (with the given errors)."""
    await _wait_for(
        lambda: (
            session.current_step
            and session.current_step.type == FlowStepType.FORM
            and session.current_step.step_id == "credentials"
            and session.current_step.errors == (errors or {})
        )
    )


async def test_login_stores_login_data_not_password(amazon_api: mock.MagicMock) -> None:
    """A successful login stores the login data, never the password or one-time code."""
    collected: dict[str, Any] = {}
    session = _make_session(collected)
    task = asyncio.create_task(setup_flow.run_setup(session))
    await _wait_form(session)

    session.handle_submit(CREDENTIALS)
    await _wait_for(lambda: session.finished)
    await task

    amazon_api.return_value.login.login_mode_interactive.assert_awaited_once_with("123456")
    assert collected == {
        CONF_USERNAME: "user@example.com",
        CONF_LOGIN_DATA: json.dumps(LOGIN_DATA),
        CONF_API_URL: "http://skill:5000",
        CONF_API_BASIC_AUTH_USERNAME: "skill_user",
        CONF_API_BASIC_AUTH_PASSWORD: "skill_pass",
    }


@pytest.mark.parametrize(
    ("error", "error_key"),
    [
        pytest.param(CannotAuthenticate("wrong otp"), "invalid_auth", id="invalid_auth"),
        pytest.param(CannotConnect("offline"), "cannot_connect", id="cannot_connect"),
        pytest.param(CannotRetrieveData("bad json"), "cannot_retrieve_data", id="bad_data"),
    ],
)
async def test_login_error_shows_form_again(
    amazon_api: mock.MagicMock, error: Exception, error_key: str
) -> None:
    """A failed login shows the form again with the error, a retry can still succeed."""
    amazon_api.return_value.login.login_mode_interactive.side_effect = [error, LOGIN_DATA]
    collected: dict[str, Any] = {}
    session = _make_session(collected)
    task = asyncio.create_task(setup_flow.run_setup(session))
    await _wait_form(session)

    session.handle_submit(CREDENTIALS)
    await _wait_form(session, {"base": error_key})
    session.handle_submit(CREDENTIALS)
    await _wait_for(lambda: session.finished)
    await task

    assert collected[CONF_LOGIN_DATA] == json.dumps(LOGIN_DATA)


@pytest.mark.usefixtures("amazon_api")
async def test_sign_in_again_keeps_skill_api_password() -> None:
    """Signing in again prefills the form and keeps the skill API password if left empty."""
    collected: dict[str, Any] = {}
    session = _make_session(
        collected,
        setup_data={
            CONF_USERNAME: "user@example.com",
            CONF_LOGIN_DATA: "{}",
            CONF_API_URL: "http://skill:5000",
            CONF_API_BASIC_AUTH_USERNAME: "skill_user",
            CONF_API_BASIC_AUTH_PASSWORD: "skill_pass",
        },
    )
    task = asyncio.create_task(setup_flow.run_setup(session))
    await _wait_form(session)
    assert session.current_step is not None
    prefilled = {entry.key: entry.value for entry in session.current_step.entries}
    assert prefilled[CONF_USERNAME] == "user@example.com"
    assert prefilled[CONF_API_BASIC_AUTH_PASSWORD] is None

    session.handle_submit({**CREDENTIALS, CONF_API_BASIC_AUTH_PASSWORD: None})
    await _wait_for(lambda: session.finished)
    await task

    assert collected[CONF_API_BASIC_AUTH_PASSWORD] == "skill_pass"
    assert collected[CONF_LOGIN_DATA] == json.dumps(LOGIN_DATA)
