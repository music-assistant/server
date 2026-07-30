"""Tests for the Pandora setup flow and the options surface it took over from."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import Mock

from music_assistant_models.enums import ConfigEntryType, FlowStepType

from music_assistant.constants import CONF_PASSWORD, CONF_USERNAME
from music_assistant.models.setup_flow import SetupFlowContext, SetupFlowError, SetupSession
from music_assistant.providers.pandora.constants import (
    CONF_QUALITY,
    CONF_TAKEOVER_ACTION,
    QUALITY_STANDARD,
)
from music_assistant.providers.pandora.provider import PandoraProvider
from music_assistant.providers.pandora.setup_flow import run_setup


def _make_session(
    finish_handler: Any, setup_data: dict[str, Any] | None = None
) -> tuple[SetupSession, Mock]:
    """Build a SetupSession backed by a Mock mass for driving run_setup directly."""
    mass = Mock()
    context = SetupFlowContext(
        kind="setup", reason="user", domain="pandora", setup_data=setup_data or {}
    )
    session = SetupSession(mass, "flow-test", context, finish_handler)
    return session, mass


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


async def test_get_config_entries_excludes_credentials() -> None:
    """The credentials are flow-owned now and must not appear on the options surface."""
    provider = PandoraProvider.__new__(PandoraProvider)
    entries = await provider.get_config_entries()
    keys = {entry.key for entry in entries}
    assert CONF_USERNAME not in keys
    assert CONF_PASSWORD not in keys
    # the genuine options (and the takeover action) stay
    assert {CONF_QUALITY, CONF_TAKEOVER_ACTION} <= keys
    # every remaining options entry must resolve without user input, since the instance
    # is created with empty values and its config is validated right after construction
    assert all(
        entry.default_value is not None or not entry.required
        for entry in entries
        if entry.type != ConfigEntryType.ACTION
    )


async def test_run_setup_collects_credentials() -> None:
    """The flow shows a username/password form and persists both as setup data."""
    collected: dict[str, Any] = {}

    async def finish_handler(_session: SetupSession, values: dict[str, Any]) -> dict[str, str]:
        collected.update(values)
        return {"instance_id": "pandora--test"}

    session, _mass = _make_session(finish_handler)
    task = asyncio.create_task(run_setup(session))
    step = await _wait_for_form(session)

    assert step.step_id == "user"
    assert step.last_step is True
    entries = {entry.key: entry for entry in step.entries}
    assert set(entries) == {CONF_USERNAME, CONF_PASSWORD}
    assert entries[CONF_USERNAME].type == ConfigEntryType.STRING
    assert entries[CONF_PASSWORD].type == ConfigEntryType.SECURE_STRING
    assert all(entry.required for entry in entries.values())

    session.handle_submit({CONF_USERNAME: "listener@example.com", CONF_PASSWORD: "hunter2"})
    await _wait_for(lambda: session.finished)
    await task

    assert collected == {CONF_USERNAME: "listener@example.com", CONF_PASSWORD: "hunter2"}


async def test_run_setup_prefills_username_but_not_password() -> None:
    """A reconfigure re-shows the stored username; the secret is never echoed back."""

    async def finish_handler(_session: SetupSession, _values: dict[str, Any]) -> dict[str, str]:
        return {"instance_id": "pandora--test"}

    session, _mass = _make_session(
        finish_handler,
        setup_data={CONF_USERNAME: "listener@example.com", CONF_PASSWORD: "old-secret"},
    )
    task = asyncio.create_task(run_setup(session))
    step = await _wait_for_form(session)

    entries = {entry.key: entry for entry in step.entries}
    assert entries[CONF_USERNAME].value == "listener@example.com"
    assert entries[CONF_PASSWORD].value is None

    session.handle_submit({CONF_USERNAME: "listener@example.com", CONF_PASSWORD: "new-secret"})
    await _wait_for(lambda: session.finished)
    await task


async def test_run_setup_retries_form_on_login_failure() -> None:
    """A failed login re-renders the form with the error, then succeeds on retry."""
    attempts: list[dict[str, Any]] = []

    async def finish_handler(_session: SetupSession, values: dict[str, Any]) -> dict[str, str]:
        # snapshot: the flow carries (and mutates) one setup_data dict across retries
        attempts.append(dict(values))
        if len(attempts) == 1:
            raise SetupFlowError("Authentication failed", translation_key="login_failed")
        return {"instance_id": "pandora--test"}

    session, _mass = _make_session(finish_handler)
    task = asyncio.create_task(run_setup(session))
    await _wait_for_form(session)
    session.handle_submit({CONF_USERNAME: "listener", CONF_PASSWORD: "wrong"})

    error_form = await _wait_for_form(session, with_errors=True)
    assert error_form.errors == {"base": "login_failed"}
    # the rejected username is prefilled again so only the password has to be retyped
    entries = {entry.key: entry for entry in error_form.entries}
    assert entries[CONF_USERNAME].value == "listener"
    assert entries[CONF_PASSWORD].value is None

    session.handle_submit({CONF_USERNAME: "listener", CONF_PASSWORD: "right"})
    await _wait_for(lambda: session.finished)
    await task

    assert [attempt[CONF_PASSWORD] for attempt in attempts] == ["wrong", "right"]


async def test_default_quality_is_standard() -> None:
    """The quality option keeps a default so the instance loads without user input."""
    provider = PandoraProvider.__new__(PandoraProvider)
    entries = {entry.key: entry for entry in await provider.get_config_entries()}
    assert entries[CONF_QUALITY].default_value == QUALITY_STANDARD
