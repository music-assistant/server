"""Tests for the iHeartRadio setup flow."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import Mock

from music_assistant_models.enums import FlowStepType

from music_assistant.constants import CONF_PASSWORD, CONF_USERNAME
from music_assistant.models.setup_flow import SetupFlowContext, SetupFlowError, SetupSession
from music_assistant.providers.iheartradio.constants import CONF_COUNTRY, DEFAULT_COUNTRY
from music_assistant.providers.iheartradio.setup_flow import run_setup

from .conftest import DOMAIN


def _make_session(finish_handler: Any) -> SetupSession:
    """Build a SetupSession backed by a Mock mass for driving run_setup directly."""
    context = SetupFlowContext(kind="setup", reason="user", domain=DOMAIN, setup_data={})
    return SetupSession(Mock(), "flow-test", context, finish_handler)


async def _wait_for(predicate: Any, timeout: float = 5.0) -> Any:
    """Wait until the predicate returns truthy (or fail the test)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result := predicate():
            return result
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met within timeout")


async def _wait_for_form(session: SetupSession, with_errors: bool = False) -> Any:
    """Wait until the flow publishes a FORM step (optionally one carrying errors)."""
    return await _wait_for(
        lambda: (
            session.current_step
            if session.current_step
            and session.current_step.type == FlowStepType.FORM
            and (session.current_step.errors if with_errors else True)
            else None
        )
    )


async def test_run_setup_persists_country_and_credentials() -> None:
    """The form defaults to Australia and persists the chosen country and credentials."""
    collected: dict[str, Any] = {}

    async def finish_handler(_session: SetupSession, values: dict[str, Any]) -> dict[str, str]:
        collected.update(values)
        return {"instance_id": "iheartradio--test"}

    session = _make_session(finish_handler)
    task = asyncio.create_task(run_setup(session))
    step = await _wait_for_form(session)
    entries = {entry.key: entry for entry in step.entries}
    assert entries[CONF_COUNTRY].default_value == DEFAULT_COUNTRY
    assert not entries[CONF_USERNAME].required
    assert not entries[CONF_PASSWORD].required

    session.handle_submit(
        {CONF_COUNTRY: "ca", CONF_USERNAME: "gav@example.com", CONF_PASSWORD: "secret"}
    )
    await _wait_for(lambda: session.finished)
    await task
    assert collected == {
        CONF_COUNTRY: "ca",
        CONF_USERNAME: "gav@example.com",
        CONF_PASSWORD: "secret",
    }


async def test_run_setup_retries_form_on_failure() -> None:
    """A failed finish re-renders the form with the error and the entered values, then succeeds."""
    attempts: list[dict[str, Any]] = []

    async def finish_handler(_session: SetupSession, values: dict[str, Any]) -> dict[str, str]:
        attempts.append(dict(values))
        if len(attempts) == 1:
            raise SetupFlowError("Authentication failed", translation_key="login_failed")
        return {"instance_id": "iheartradio--test"}

    session = _make_session(finish_handler)
    task = asyncio.create_task(run_setup(session))
    await _wait_for_form(session)
    session.handle_submit(
        {CONF_COUNTRY: "nz", CONF_USERNAME: "gav@example.com", CONF_PASSWORD: "wrong"}
    )

    error_form = await _wait_for_form(session, with_errors=True)
    assert error_form.errors == {"base": "login_failed"}
    entries = {entry.key: entry for entry in error_form.entries}
    assert entries[CONF_COUNTRY].value == "nz"
    assert entries[CONF_USERNAME].value == "gav@example.com"
    assert entries[CONF_PASSWORD].value is None

    session.handle_submit(
        {CONF_COUNTRY: "nz", CONF_USERNAME: "gav@example.com", CONF_PASSWORD: "right"}
    )
    await _wait_for(lambda: session.finished)
    await task
    assert [attempt[CONF_PASSWORD] for attempt in attempts] == ["wrong", "right"]
