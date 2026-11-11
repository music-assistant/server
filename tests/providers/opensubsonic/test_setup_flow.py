"""Tests for Open Subsonic connection setup."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import FlowStepType

from music_assistant.constants import CONF_PASSWORD, CONF_USERNAME
from music_assistant.models.setup_flow import SetupFlowContext, SetupFlowError, SetupSession
from music_assistant.providers.opensubsonic.setup_flow import run_setup
from music_assistant.providers.opensubsonic.sonic_provider import CONF_API_KEY, CONF_BASE_URL


@pytest.mark.parametrize("secret_key", [CONF_PASSWORD, CONF_API_KEY])
@pytest.mark.parametrize("submitted_secret", [None, "", "replacement"])
@pytest.mark.parametrize("has_saved_secret", [False, True])
async def test_reconfigure_preserves_or_replaces_secret(
    secret_key: str, submitted_secret: str | None, has_saved_secret: bool
) -> None:
    """Blank secret fields retain credentials without exposing them to the client."""
    original = {
        CONF_BASE_URL: "https://navidrome.example",
        CONF_USERNAME: "listener",
    }
    if has_saved_secret:
        original[secret_key] = "saved-secret"
    finish = AsyncMock(return_value={"instance_id": "opensubsonic--test"})
    session = SetupSession(
        mass=MagicMock(),
        flow_id="test",
        context=SetupFlowContext(
            kind="reconfigure",
            reason="user",
            domain="opensubsonic",
            instance_id="opensubsonic--test",
            setup_data=dict(original),
        ),
        finish_handler=finish,
    )
    task = asyncio.create_task(run_setup(session))
    try:
        await asyncio.wait_for(session._step_changed.wait(), timeout=5)
        step = session.current_step
        assert step is not None
        assert step.type == FlowStepType.FORM
        assert all(
            entry.value is None
            for entry in step.entries
            if entry.key in (CONF_API_KEY, CONF_PASSWORD)
        )
        assert session.handle_submit({secret_key: submitted_secret}) is None
        await asyncio.wait_for(task, timeout=5)
        finish.assert_awaited_once()
        saved = finish.call_args.args[1]
        assert saved.get(secret_key) == (
            submitted_secret or ("saved-secret" if has_saved_secret else None)
        )
        assert saved[CONF_BASE_URL] == original[CONF_BASE_URL]
        assert saved[CONF_USERNAME] == original[CONF_USERNAME]
        assert session.context.setup_data == original
        assert session.current_step is not None
        assert session.current_step.type == FlowStepType.FINISH
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("api_key", [None, "", "new-api-key"])
@pytest.mark.parametrize("password", [None, "", "new-password"])
async def test_submission_can_switch_authentication(
    api_key: str | None, password: str | None
) -> None:
    """New credentials replace the saved authentication, with API keys winning ties."""
    session = MagicMock(spec=SetupSession)
    session.context = SimpleNamespace(
        setup_data={CONF_API_KEY: "old-api-key", CONF_PASSWORD: "old-password"}
    )
    session.form = AsyncMock(
        return_value={
            CONF_BASE_URL: "https://navidrome.example",
            CONF_USERNAME: "listener",
            CONF_PASSWORD: password,
            CONF_API_KEY: api_key,
        }
    )
    session.finish = AsyncMock()
    await run_setup(session)
    saved = session.finish.call_args.args[0]
    if api_key:
        assert saved[CONF_API_KEY] == api_key
        assert saved[CONF_PASSWORD] is None
    elif password:
        assert saved[CONF_API_KEY] is None
        assert saved[CONF_PASSWORD] == password
    else:
        assert saved[CONF_API_KEY] == "old-api-key"
        assert saved[CONF_PASSWORD] == "old-password"


async def test_retry_keeps_newly_entered_password() -> None:
    """Retrying a failed connection must not discard the password from the previous submit."""
    session = MagicMock(spec=SetupSession)
    session.context = SimpleNamespace(setup_data={CONF_PASSWORD: "old-password"})
    session.form = AsyncMock(
        side_effect=[
            {CONF_PASSWORD: "new-password"},
            {CONF_PASSWORD: None},
        ]
    )
    session.finish = AsyncMock(side_effect=[SetupFlowError("Connection failed"), None])
    await run_setup(session)
    assert session.finish.await_count == 2
    assert session.finish.call_args.args[0][CONF_PASSWORD] == "new-password"
