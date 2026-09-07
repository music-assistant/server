"""Tests for the Audible setup flow."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from music_assistant_models.enums import FlowStepType

from music_assistant.models.setup_flow import (
    SetupFlowContext,
    SetupFlowError,
    SetupSession,
    StepExpiredError,
)
from music_assistant.providers.audible import CONF_AUTH_FILE, CONF_LOCALE
from music_assistant.providers.audible.setup_flow import CONF_POST_LOGIN_URL, run_setup


def _make_session(
    finish_handler: Any, tmp_path: Path, setup_data: dict[str, Any] | None = None
) -> SetupSession:
    """Build a SetupSession backed by a Mock mass for driving run_setup directly."""
    mass = Mock()
    mass.storage_path = str(tmp_path)
    context = SetupFlowContext(
        kind="reconfigure" if setup_data else "setup",
        reason="user",
        domain="audible",
        setup_data=setup_data or {},
    )
    session = SetupSession(mass, "flow-test", context, finish_handler)
    # skip the 30s "open the login URL" wait; the flow suppresses the expiry
    session.external_until = AsyncMock(side_effect=StepExpiredError)  # type: ignore[method-assign]
    return session


def _mock_registered_auth() -> MagicMock:
    """Return a mock Authenticator whose to_file writes a placeholder file."""
    auth = MagicMock()
    auth.adp_token = "adp_token"
    auth.device_private_key = "private_key"
    auth.to_file.side_effect = lambda path: Path(path).write_text("{}")
    return auth


async def _wait_for(predicate: Any, timeout: float = 5.0) -> Any:
    """Wait until the predicate returns truthy (or fail the test)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result := predicate():
            return result
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met within timeout")


async def _wait_for_form(session: SetupSession, step_id: str, with_errors: bool = False) -> Any:
    """Wait until the flow publishes the FORM step with the given step_id."""
    return await _wait_for(
        lambda: (
            session.current_step
            if session.current_step
            and session.current_step.type == FlowStepType.FORM
            and session.current_step.step_id == step_id
            and (session.current_step.errors if with_errors else True)
            else None
        )
    )


async def _drive_to_finish(session: SetupSession) -> None:
    """Submit the locale and post-login-url forms so the flow reaches finish()."""
    await _wait_for_form(session, "user")
    session.handle_submit({CONF_LOCALE: "de"})
    await _wait_for_form(session, "authenticate")
    session.handle_submit({CONF_POST_LOGIN_URL: "https://example.com/?code=dummy"})


async def test_reauth_retires_previous_registration(tmp_path: Path) -> None:
    """A successful re-auth deregisters the old device and removes its token file."""
    old_file = tmp_path / "audible_auth_old.json"
    old_file.write_text("{}")
    collected: dict[str, Any] = {}

    async def finish_handler(_session: SetupSession, values: dict[str, Any]) -> dict[str, str]:
        collected.update(values)
        return {"instance_id": "audible--test"}

    session = _make_session(
        finish_handler, tmp_path, {CONF_LOCALE: "us", CONF_AUTH_FILE: str(old_file)}
    )
    prefix = "music_assistant.providers.audible.setup_flow"
    with (
        patch(f"{prefix}.audible_get_auth_info", return_value=("verifier", "http://l", "serial")),
        patch(f"{prefix}.audible_custom_login", return_value=_mock_registered_auth()),
        patch(f"{prefix}.deregister_auth_file") as deregister_mock,
    ):
        task = asyncio.create_task(run_setup(session))
        await _drive_to_finish(session)
        await _wait_for(lambda: session.finished)
        await task

    assert collected[CONF_LOCALE] == "de"
    assert collected[CONF_AUTH_FILE] != str(old_file)
    deregister_mock.assert_called_once_with(str(old_file))
    assert not old_file.exists()
    assert Path(collected[CONF_AUTH_FILE]).exists()


async def test_failed_finish_keeps_previous_registration(tmp_path: Path) -> None:
    """A failed finish drops the new token file and leaves the previous one alone."""
    old_file = tmp_path / "audible_auth_old.json"
    old_file.write_text("{}")
    attempts: list[dict[str, Any]] = []

    async def finish_handler(_session: SetupSession, values: dict[str, Any]) -> dict[str, str]:
        attempts.append(dict(values))
        if len(attempts) == 1:
            raise SetupFlowError("Provider did not load")
        return {"instance_id": "audible--test"}

    session = _make_session(
        finish_handler, tmp_path, {CONF_LOCALE: "us", CONF_AUTH_FILE: str(old_file)}
    )
    prefix = "music_assistant.providers.audible.setup_flow"
    with (
        patch(f"{prefix}.audible_get_auth_info", return_value=("verifier", "http://l", "serial")),
        patch(f"{prefix}.audible_custom_login", side_effect=lambda *_: _mock_registered_auth()),
        patch(f"{prefix}.deregister_auth_file") as deregister_mock,
    ):
        task = asyncio.create_task(run_setup(session))
        await _drive_to_finish(session)

        # first finish fails: the rejected token file is gone, the old one is untouched
        await _wait_for_form(session, "authenticate", with_errors=True)
        rejected_file = attempts[0][CONF_AUTH_FILE]
        assert not Path(rejected_file).exists()
        assert old_file.exists()
        deregister_mock.assert_not_called()

        session.handle_submit({CONF_POST_LOGIN_URL: "https://example.com/?code=dummy"})
        await _wait_for(lambda: session.finished)
        await task

    deregister_mock.assert_called_once_with(str(old_file))
    assert not old_file.exists()
