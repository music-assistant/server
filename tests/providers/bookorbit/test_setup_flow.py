"""Tests for the BookOrbit setup flow."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest import mock

import pytest
from music_assistant_models.enums import FlowStepType

from music_assistant.constants import CONF_PASSWORD, CONF_USERNAME, CONF_VERIFY_SSL
from music_assistant.models.setup_flow import SetupFlowContext, SetupFlowError, SetupSession
from music_assistant.providers.bookorbit.constants import CONF_URL, DOMAIN
from music_assistant.providers.bookorbit.setup_flow import run_setup


def _make_session(
    finish_handler: Any,
    mass: Any,
    *,
    setup_data: dict[str, Any] | None = None,
) -> SetupSession:
    context = SetupFlowContext(
        kind="reconfigure" if setup_data else "setup",
        reason="user",
        domain=DOMAIN,
        setup_data=setup_data or {},
    )
    return SetupSession(mass, "flow-test", context, finish_handler)


async def _wait_for(predicate: Any, timeout: float = 5.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result := predicate():
            return result
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met within timeout")


async def _wait_for_form(session: SetupSession, *, with_errors: bool = False) -> Any:
    return await _wait_for(
        lambda: (
            session.current_step
            if session.current_step
            and session.current_step.type == FlowStepType.FORM
            and (session.current_step.errors if with_errors else True)
            else None
        )
    )


@pytest.mark.asyncio
async def test_run_setup_finishes_and_persists_values() -> None:
    """A successful finish persists the submitted setup data."""
    collected: dict[str, Any] = {}

    async def finish(_session: SetupSession, values: dict[str, Any]) -> dict[str, str]:
        collected.update(values)
        return {"instance_id": "bookorbit--test"}

    session = _make_session(finish, mock.Mock())
    task = asyncio.create_task(run_setup(session))
    await _wait_for_form(session)
    session.handle_submit(
        {
            CONF_URL: "http://bo",
            CONF_USERNAME: "u",
            CONF_PASSWORD: "p",
            CONF_VERIFY_SSL: True,
        }
    )
    await _wait_for(lambda: session.finished)
    await task

    assert collected[CONF_URL] == "http://bo"
    assert collected[CONF_USERNAME] == "u"
    assert collected[CONF_PASSWORD] == "p"


@pytest.mark.asyncio
async def test_run_setup_reconfigure_blank_password_keeps_stored() -> None:
    """Reconfigure with unchanged username and blank password reuses the saved password."""
    collected: dict[str, Any] = {}

    async def finish(_session: SetupSession, values: dict[str, Any]) -> dict[str, str]:
        collected.update(values)
        return {"instance_id": "bookorbit--test"}

    session = _make_session(
        finish,
        mock.Mock(),
        setup_data={
            CONF_URL: "http://bo",
            CONF_USERNAME: "u",
            CONF_PASSWORD: "stored-secret",
            CONF_VERIFY_SSL: True,
        },
    )
    task = asyncio.create_task(run_setup(session))
    await _wait_for_form(session)
    session.handle_submit(
        {
            CONF_URL: "http://bo",
            CONF_USERNAME: "u",
            CONF_PASSWORD: None,
            CONF_VERIFY_SSL: True,
        }
    )
    await _wait_for(lambda: session.finished)
    await task

    assert collected[CONF_PASSWORD] == "stored-secret"


@pytest.mark.asyncio
async def test_run_setup_keeps_replacement_password_after_finish_error() -> None:
    """After a failed finish with a new password, a blank-password retry keeps it."""
    collected: dict[str, Any] = {}
    attempts = 0

    async def finish(_session: SetupSession, values: dict[str, Any]) -> dict[str, str]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise SetupFlowError(
                "Login failed.",
                translation_key="login_failed",
                translation_owner=DOMAIN,
            )
        collected.update(values)
        return {"instance_id": "bookorbit--test"}

    session = _make_session(
        finish,
        mock.Mock(),
        setup_data={
            CONF_URL: "http://bo",
            CONF_USERNAME: "u",
            CONF_PASSWORD: "stored-secret",
            CONF_VERIFY_SSL: True,
        },
    )
    task = asyncio.create_task(run_setup(session))
    await _wait_for_form(session)
    session.handle_submit(
        {
            CONF_URL: "http://bo",
            CONF_USERNAME: "u",
            CONF_PASSWORD: "replacement-secret",
            CONF_VERIFY_SSL: True,
        }
    )
    await _wait_for_form(session, with_errors=True)
    session.handle_submit(
        {
            CONF_URL: "http://bo",
            CONF_USERNAME: "u",
            CONF_PASSWORD: None,
            CONF_VERIFY_SSL: True,
        }
    )
    await _wait_for(lambda: session.finished)
    await task

    assert attempts == 2
    assert collected[CONF_PASSWORD] == "replacement-secret"
