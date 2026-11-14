"""Test the setup flow of the ARD Audiothek provider."""

from __future__ import annotations

from typing import Any
from unittest import mock

import pytest
from music_assistant_models.errors import LoginFailed

from music_assistant.constants import CONF_PASSWORD
from music_assistant.models.setup_flow import SetupFlowContext, SetupSession, StepExpiredError
from music_assistant.providers.ard_audiothek import (
    CONF_DISPLAY_NAME,
    CONF_EMAIL,
    CONF_EXPIRY_TIME,
    CONF_REFRESH_TOKEN,
    CONF_TOKEN_BEARER,
    CONF_USERID,
)
from music_assistant.providers.ard_audiothek import setup_flow as ard_flow
from music_assistant.providers.ard_audiothek.auth import ArdTokens

DEVICE = {
    "user_code": "ABCDE",
    "verification_uri": "https://accounts.ard.de/id/ABCDE?clientId=at",
    "token_uri": "https://accounts.ard.de/device/token/secret",
    "interval": 5,
    "expires_in": 180,
}
TOKENS = ArdTokens(
    id_token="id-token",
    refresh_token="refresh-token",
    user_id="uid",
    display_name="Jane",
    email="jane@example.com",
    expires_at=1000.0,
)
ACCOUNT_KEYS = (
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_REFRESH_TOKEN,
    CONF_TOKEN_BEARER,
    CONF_USERID,
    CONF_DISPLAY_NAME,
    CONF_EXPIRY_TIME,
)


def _make_session(
    monkeypatch: pytest.MonkeyPatch,
    answers: list[bool],
    setup_data: dict[str, Any] | None = None,
) -> tuple[SetupSession, mock.AsyncMock, mock.AsyncMock]:
    """Return a session answering the sign-in question in turn, its finish and form mocks."""
    finish = mock.AsyncMock(return_value={"instance_id": "ard_audiothek--test"})
    context = SetupFlowContext(
        kind="reconfigure" if setup_data else "setup",
        reason="user",
        domain="ard_audiothek",
        setup_data=setup_data or {},
    )
    session = SetupSession(mock.Mock(), "flow-test", context, finish)
    form = mock.AsyncMock(side_effect=[{ard_flow.CONF_SIGN_IN: answer} for answer in answers])
    monkeypatch.setattr(session, "form", form)
    return session, finish, form


def _device_login(monkeypatch: pytest.MonkeyPatch, session: SetupSession, *outcomes: Any) -> Any:
    """Serve device codes and let the step end with the given outcomes in turn."""
    request = mock.AsyncMock(return_value=DEVICE)
    monkeypatch.setattr(ard_flow, "request_device_code", request)
    monkeypatch.setattr(ard_flow, "poll_device_token", mock.Mock())
    monkeypatch.setattr(session, "external_until", mock.AsyncMock(side_effect=list(outcomes)))
    return request


async def test_setup_without_account(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without signing in, the provider is created with all account values cleared."""
    session, finish, _ = _make_session(monkeypatch, [False])
    request = _device_login(monkeypatch, session)

    await ard_flow.run_setup(session)

    request.assert_not_awaited()
    assert finish.call_args.args[1] == dict.fromkeys(ACCOUNT_KEYS)


async def test_setup_with_device_login(monkeypatch: pytest.MonkeyPatch) -> None:
    """Signing in shows the device code and stores the tokens ARD hands out."""
    session, finish, _ = _make_session(monkeypatch, [True])
    _device_login(monkeypatch, session, TOKENS)

    await ard_flow.run_setup(session)

    step = session.external_until.call_args.kwargs  # type: ignore[attr-defined]
    assert step["url"] == DEVICE["verification_uri"]
    assert step["translation_params"] == ["ABCDE"]
    assert step["expires_in"] == 180
    assert finish.call_args.args[1] == {
        CONF_EMAIL: "jane@example.com",
        CONF_PASSWORD: None,
        CONF_REFRESH_TOKEN: "refresh-token",
        CONF_TOKEN_BEARER: "id-token",
        CONF_USERID: "uid",
        CONF_DISPLAY_NAME: "Jane",
        CONF_EXPIRY_TIME: "940.0",
    }


async def test_expired_code_is_replaced(monkeypatch: pytest.MonkeyPatch) -> None:
    """A code that expires before it was confirmed is replaced by a fresh one."""
    session, finish, _ = _make_session(monkeypatch, [True])
    request = _device_login(monkeypatch, session, StepExpiredError(), TOKENS)

    await ard_flow.run_setup(session)

    assert request.await_count == 2
    assert finish.call_args.args[1][CONF_REFRESH_TOKEN] == "refresh-token"


async def test_abandoned_login_stops_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fresh codes are only handed out until the overall time for signing in ran out."""
    session, finish, _ = _make_session(monkeypatch, [True])
    clock = iter([0.0, 0.0, 600.0, 600.0, 1200.0])
    monkeypatch.setattr(ard_flow, "time", mock.Mock(monotonic=lambda: next(clock, 2000.0)))
    request = _device_login(monkeypatch, session, StepExpiredError(), StepExpiredError())

    with pytest.raises(StepExpiredError):
        await ard_flow.run_setup(session)

    assert request.await_count == 2
    finish.assert_not_awaited()


async def test_failed_login_shows_the_form_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """A rejected login returns to the form with the reason, and the setup can go on."""
    session, finish, form = _make_session(monkeypatch, [True, False])
    _device_login(monkeypatch, session, LoginFailed("ARD device login failed (HTTP 400)"))

    await ard_flow.run_setup(session)

    assert form.call_args.kwargs["errors"] == {"base": "ARD device login failed (HTTP 400)"}
    assert finish.call_args.args[1] == dict.fromkeys(ACCOUNT_KEYS)


@pytest.mark.parametrize(
    ("setup_data", "expected"),
    [({CONF_REFRESH_TOKEN: "refresh-token"}, True), ({CONF_PASSWORD: "secret"}, False)],
    ids=["signed-in", "password-login"],
)
async def test_reconfigure_prefills_the_sign_in(
    monkeypatch: pytest.MonkeyPatch, setup_data: dict[str, Any], expected: bool
) -> None:
    """A reconfigure preselects signing in only for an account linked by device login."""
    session, _, form = _make_session(monkeypatch, [False], setup_data)

    await ard_flow.run_setup(session)

    assert form.call_args.args[0][0].default_value is expected
