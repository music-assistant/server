"""Tests for the Yoto provider setup flow."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Self
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlsplit

from music_assistant_models.enums import ConfigEntryType, FlowStepType

from music_assistant.models.setup_flow import SetupFlowContext, SetupSession
from music_assistant.providers.yoto import setup_flow


class _TokenResponse:
    """Minimal async context manager for a successful OAuth token response."""

    ok = True

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def json(self, *, content_type: None = None) -> dict[str, Any]:
        return {
            "access_token": "fixture-access-token",
            "refresh_token": "fixture-refresh-token",
            "expires_in": 3600,
            "token_type": "Bearer",
        }


async def _wait_for_form(session: SetupSession, step_id: str) -> Any:
    """Wait until the setup flow publishes the requested form."""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        step = session.current_step
        if step is not None and step.type == FlowStepType.FORM and step.step_id == step_id:
            return step
        await asyncio.sleep(0.01)
    raise AssertionError(f"setup flow did not publish form {step_id}")


async def test_setup_exchanges_pkce_callback_and_persists_only_credentials() -> None:
    """The two-step flow exchanges the callback without exposing its PKCE verifier."""
    collected: dict[str, Any] = {}
    mass = MagicMock()
    mass.http_session.post.return_value = _TokenResponse()

    async def finish(_session: SetupSession, values: dict[str, Any]) -> dict[str, str]:
        collected.update(values)
        return {"instance_id": "yoto--1"}

    session = SetupSession(
        mass,
        "flow1",
        SetupFlowContext(kind="setup", reason="user", domain="yoto"),
        finish,
    )
    task = asyncio.create_task(setup_flow.run_setup(session))

    client_form = await _wait_for_form(session, "client")
    assert client_form.entries[0].type == ConfigEntryType.LABEL
    assert "unofficial" in (client_form.entries[0].label or "").lower()
    assert "not affiliated" in (client_form.entries[0].label or "").lower()
    client_id_entry = client_form.entries[1]
    assert client_id_entry.key == "client_id"
    assert client_id_entry.help_link == "https://yoto.dev/get-started/start-here/"
    session.handle_submit({"client_id": "fixture-client"})

    callback_form = await _wait_for_form(session, "authorize")
    authorize_label, callback_entry = callback_form.entries
    assert authorize_label.type == ConfigEntryType.LABEL
    assert authorize_label.help_link is not None
    query = parse_qs(urlsplit(authorize_label.help_link).query)
    assert query["client_id"] == ["fixture-client"]
    assert query["redirect_uri"] == ["http://localhost:8095/callback"]
    assert query["scope"] == ["family:library:view offline_access"]
    assert query["code_challenge_method"] == ["S256"]
    assert callback_entry.key == "callback_url"
    session.handle_submit({"callback_url": "http://localhost:8095/callback?code=fixture-code"})
    await task

    assert collected == {
        "client_id": "fixture-client",
        "refresh_token": "fixture-refresh-token",
    }
    post_data = mass.http_session.post.call_args.kwargs["data"]
    assert post_data["code"] == "fixture-code"
    assert post_data["code_verifier"]
    assert "code_verifier" not in collected
    assert post_data["code_verifier"] not in repr(session)
    assert post_data["code_verifier"] not in repr(client_form)
    assert post_data["code_verifier"] not in repr(callback_form)
