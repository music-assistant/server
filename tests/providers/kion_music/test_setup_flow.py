"""Tests for the KION Music interactive setup flow."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from music_assistant.models.setup_flow import SetupFlowError
from music_assistant.providers.kion_music.constants import CONF_TOKEN
from music_assistant.providers.kion_music.setup_flow import run_setup


@pytest.fixture
def session() -> Mock:
    """Return a setup session with no previously collected values."""
    result = Mock()
    result.context = SimpleNamespace(setup_data={})
    result.form = AsyncMock()
    result.finish = AsyncMock()
    return result


async def test_setup_flow_collects_token(session: Mock) -> None:
    """A submitted token is passed to the setup finish handler."""
    session.form.return_value = {CONF_TOKEN: "secret-token"}

    await run_setup(session)

    entries = session.form.await_args.args[0]
    assert len(entries) == 1
    assert entries[0].key == CONF_TOKEN
    assert entries[0].required is True
    assert session.form.await_args.kwargs == {
        "step_id": "user",
        "errors": None,
        "last_step": True,
    }
    session.finish.assert_awaited_once_with({CONF_TOKEN: "secret-token"})


async def test_setup_flow_retries_with_translated_error(session: Mock) -> None:
    """A failed login re-renders the token form with a translated error key."""
    session.form.side_effect = [
        {CONF_TOKEN: "expired-token"},
        {CONF_TOKEN: "fresh-token"},
    ]
    session.finish.side_effect = [
        SetupFlowError("invalid credentials", translation_key="login_failed"),
        None,
    ]

    await run_setup(session)

    assert session.form.await_count == 2
    retry_call = session.form.await_args_list[1]
    assert retry_call.kwargs["errors"] == {"base": "login_failed"}
    assert retry_call.args[0][0].value == "expired-token"
    assert session.finish.await_args_list[-1].args[0] == {CONF_TOKEN: "fresh-token"}
    assert "expired-token" not in str(retry_call.kwargs["errors"])
    assert "fresh-token" not in str(retry_call.kwargs["errors"])
