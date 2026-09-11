"""Tests for the YouTube Music setup flow (run_setup)."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from music_assistant_models.enums import FlowStepType
from music_assistant_models.errors import LoginFailed

from music_assistant.constants import CONF_USERNAME
from music_assistant.models.setup_flow import SetupFlowContext, SetupFlowError, SetupSession
from music_assistant.providers.ytmusic import (
    CONF_COOKIE,
    CONF_PO_TOKEN_SERVER_URL,
    DEFAULT_PO_TOKEN_SERVER_URL,
)
from music_assistant.providers.ytmusic import setup_flow as ytm_flow

VALID_COOKIE = "VISITOR_INFO1_LIVE=abc; __Secure-3PAPISID=secret/value; SID=xyz"
SIGNED_OUT_COOKIE = "VISITOR_INFO1_LIVE=abc; SID=xyz"


def _make_session(finish_handler: Any) -> tuple[SetupSession, Mock]:
    """Build a real SetupSession backed by a Mock mass for driving run_setup directly."""
    mass = Mock()
    mass.http_session = MagicMock()
    context = SetupFlowContext(kind="setup", reason="user", domain="ytmusic")
    return SetupSession(mass, "flow-test", context, finish_handler), mass


async def _wait_for(predicate: Any, timeout: float = 5.0) -> Any:
    """Wait until the predicate returns truthy (or fail the test)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result := predicate():
            return result
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met within timeout")


async def _wait_for_form(session: SetupSession, after_step: Any = None) -> Any:
    """Wait for a (new) form step to be published and return it."""
    return await _wait_for(
        lambda: (
            (step := session.current_step)
            and step is not after_step
            and step.type == FlowStepType.FORM
            and step
        )
    )


def _submission(cookie: str, po_token_url: str = DEFAULT_PO_TOKEN_SERVER_URL) -> dict[str, Any]:
    return {
        CONF_USERNAME: "user@example.com",
        CONF_COOKIE: cookie,
        CONF_PO_TOKEN_SERVER_URL: po_token_url,
    }


@pytest.fixture
def po_token_reachable() -> Any:
    """Make the PO Token server ping succeed."""
    with patch.object(ytm_flow, "ping_po_token_server", AsyncMock(return_value=True)) as mocked:
        yield mocked


@pytest.fixture
def cookie_accepted() -> Any:
    """Make the signed-in verification request succeed."""
    with patch.object(ytm_flow, "verify_cookie", AsyncMock(return_value=None)) as mocked:
        yield mocked


@pytest.mark.usefixtures("po_token_reachable")
async def test_curl_paste_is_normalized_before_finish(cookie_accepted: AsyncMock) -> None:
    """A 'Copy as cURL' paste is stored as the plain cookie string and verified before saving."""
    finish = AsyncMock(return_value={"instance_id": "ytmusic--1"})
    session, _ = _make_session(finish)
    task = asyncio.create_task(ytm_flow.run_setup(session))
    try:
        await _wait_for_form(session)
        session.handle_submit(
            _submission(f"curl 'https://music.youtube.com/' -H 'cookie: {VALID_COOKIE}'")
        )
        await _wait_for(lambda: session.finished)
    finally:
        task.cancel()
    finish.assert_awaited_once()
    assert finish.await_args is not None
    values = finish.await_args.args[1]
    assert values[CONF_COOKIE] == VALID_COOKIE
    assert values[CONF_USERNAME] == "user@example.com"
    # the verification request was signed with the normalized cookie
    assert cookie_accepted.await_args is not None
    headers = cookie_accepted.await_args.args[0]
    assert headers["Cookie"] == VALID_COOKIE
    assert session.current_step is not None
    assert session.current_step.type == FlowStepType.FINISH


async def test_signed_out_cookie_and_unreachable_po_token_are_reported_per_field() -> None:
    """Both a bad cookie and a missing PO Token server are flagged on their own fields at once."""
    finish = AsyncMock()
    session, _ = _make_session(finish)
    with patch.object(ytm_flow, "ping_po_token_server", AsyncMock(return_value=False)):
        task = asyncio.create_task(ytm_flow.run_setup(session))
        try:
            first = await _wait_for_form(session)
            session.handle_submit(_submission(SIGNED_OUT_COOKIE, "http://nowhere:4416"))
            retry = await _wait_for_form(session, after_step=first)
        finally:
            task.cancel()
    assert retry.errors == {
        CONF_COOKIE: "cookie_missing_sapisid",
        CONF_PO_TOKEN_SERVER_URL: "po_token_server_unreachable",
    }
    finish.assert_not_awaited()
    # the previously entered values are offered again
    assert {entry.key: entry.value for entry in retry.entries}[CONF_PO_TOKEN_SERVER_URL] == (
        "http://nowhere:4416"
    )


@pytest.mark.usefixtures("po_token_reachable")
async def test_cookie_refused_by_youtube_is_reported_on_cookie_field() -> None:
    """A cookie YouTube no longer accepts is flagged on the cookie field."""
    finish = AsyncMock()
    session, _ = _make_session(finish)
    refused = LoginFailed("expired", translation_key="cookie_expired")
    with patch.object(ytm_flow, "verify_cookie", AsyncMock(side_effect=refused)):
        task = asyncio.create_task(ytm_flow.run_setup(session))
        try:
            first = await _wait_for_form(session)
            session.handle_submit(_submission(VALID_COOKIE))
            retry = await _wait_for_form(session, after_step=first)
        finally:
            task.cancel()
    assert retry.errors == {CONF_COOKIE: "cookie_expired"}
    finish.assert_not_awaited()


@pytest.mark.usefixtures("po_token_reachable", "cookie_accepted")
async def test_finish_failure_is_shown_as_base_error_and_retried() -> None:
    """A provider load failure re-renders the form with the error and the flow can be retried."""
    finish = AsyncMock(
        side_effect=[
            SetupFlowError("no premium", translation_key="no_premium"),
            {"instance_id": "ytmusic--1"},
        ]
    )
    session, _ = _make_session(finish)
    task = asyncio.create_task(ytm_flow.run_setup(session))
    try:
        first = await _wait_for_form(session)
        session.handle_submit(_submission(VALID_COOKIE))
        retry = await _wait_for_form(session, after_step=first)
        assert retry.errors == {"base": "no_premium"}
        session.handle_submit(_submission(VALID_COOKIE))
        await _wait_for(lambda: session.finished)
    finally:
        task.cancel()
    assert finish.await_count == 2
