"""Tests for the Spotify setup flow's playback mode choice (librespot | Spotify Connect)."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any
from unittest import mock

import pytest
from music_assistant_models.enums import FlowStepType

from music_assistant.models.setup_flow import SetupFlowContext, SetupSession
from music_assistant.providers.spotify import setup_flow as spotify_flow
from music_assistant.providers.spotify.constants import (
    BACKEND_CONNECT,
    BACKEND_LIBRESPOT,
    CONF_LIBRESPOT_CREDENTIALS,
    CONF_PLAYBACK_BACKEND,
)
from music_assistant.providers.spotify_connect import (
    BACKEND_GO_LIBRESPOT,
    BACKEND_SOLOIST,
    CONF_API_KEY,
    CONF_BACKEND,
    CONF_SOLOIST_CONSENT,
)

if TYPE_CHECKING:
    from music_assistant_models.setup_flow import SetupFlowStep

_VALID_API_KEY = "soloist-api-key-0123456789abcdef"


def _make_session(
    *,
    kind: str = "setup",
    setup_data: dict[str, Any] | None = None,
) -> SetupSession:
    """Return a real setup session driving the playback-mode steps."""
    mass = mock.Mock()
    mass.get_provider = mock.Mock(return_value=None)

    async def finish(_session: SetupSession, _submitted: dict[str, Any]) -> dict[str, str]:
        return {"instance_id": "spotify--test"}

    context = SetupFlowContext(
        kind="reconfigure" if kind == "reconfigure" else "setup",
        reason="user",
        domain="spotify",
        instance_id="spotify--test" if kind == "reconfigure" else None,
        setup_data=setup_data or {},
    )
    return SetupSession(mass, "flow-test", context, finish)


async def _wait_for_form(
    session: SetupSession, previous: SetupFlowStep | None = None
) -> SetupFlowStep:
    """Wait until a (new) form step is published and return it."""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        step = session.current_step
        if step is not None and step.type == FlowStepType.FORM and step is not previous:
            return step
        await asyncio.sleep(0.01)
    raise AssertionError("form step not published")


async def _submit(session: SetupSession, values: dict[str, Any]) -> SetupFlowStep:
    """Submit form values (which must validate) and return the next published step."""
    previous = session.current_step
    assert previous is not None
    assert session.handle_submit(values) is None
    return await _wait_for_form(session, previous)


def _entry(step: SetupFlowStep, key: str) -> Any:
    """Return the form entry with the given key from a step."""
    return next(entry for entry in step.entries if entry.key == key)


async def test_connect_mode_sets_up_the_instance_inline() -> None:
    """Choosing Connect runs the engine steps inline and provisions the instance."""
    session = _make_session(kind="reconfigure", setup_data={CONF_LIBRESPOT_CREDENTIALS: "secret"})
    setup_data: dict[str, Any] = {CONF_LIBRESPOT_CREDENTIALS: "secret"}
    provision = mock.AsyncMock(return_value="spotify_connect--new")
    with (
        mock.patch.object(spotify_flow, "has_running_system_wide_connect", return_value=False),
        mock.patch.object(
            spotify_flow, "get_system_wide_connect_config_id", mock.AsyncMock(return_value=None)
        ),
        mock.patch.object(spotify_flow, "_provision_connect_instance", provision),
        mock.patch.object(spotify_flow, "verify_platform_supported"),
    ):
        task = asyncio.create_task(spotify_flow._setup_playback(session, setup_data))
        step = await _wait_for_form(session)

        # an instance predating the choice preselects librespot
        assert step.step_id == "playback_backend"
        assert _entry(step, CONF_PLAYBACK_BACKEND).value == BACKEND_LIBRESPOT

        step = await _submit(session, {CONF_PLAYBACK_BACKEND: BACKEND_CONNECT})
        assert step.step_id == "connect_engine"
        # supported platform: the official engine is the default for a fresh instance
        assert _entry(step, CONF_BACKEND).value == BACKEND_SOLOIST

        session.handle_submit({CONF_BACKEND: BACKEND_GO_LIBRESPOT})
        await task

    provision.assert_awaited_once_with(
        session,
        None,
        {CONF_BACKEND: BACKEND_GO_LIBRESPOT, CONF_API_KEY: "", CONF_SOLOIST_CONSENT: False},
    )
    assert setup_data[CONF_PLAYBACK_BACKEND] == BACKEND_CONNECT
    assert setup_data[CONF_LIBRESPOT_CREDENTIALS] is None


async def test_connect_soloist_engine_collects_consent_and_api_key() -> None:
    """The Soloist engine branch collects consent (with refusal bounce) and the API key."""
    session = _make_session()
    setup_data: dict[str, Any] = {CONF_PLAYBACK_BACKEND: BACKEND_CONNECT}
    provision = mock.AsyncMock(return_value="spotify_connect--new")
    with (
        mock.patch.object(spotify_flow, "has_running_system_wide_connect", return_value=False),
        mock.patch.object(
            spotify_flow, "get_system_wide_connect_config_id", mock.AsyncMock(return_value=None)
        ),
        mock.patch.object(spotify_flow, "_provision_connect_instance", provision),
        mock.patch.object(spotify_flow, "verify_platform_supported"),
    ):
        task = asyncio.create_task(spotify_flow._setup_playback(session, setup_data))
        step = await _wait_for_form(session)
        step = await _submit(session, {CONF_PLAYBACK_BACKEND: BACKEND_CONNECT})
        assert step.step_id == "connect_engine"

        step = await _submit(session, {CONF_BACKEND: BACKEND_SOLOIST})
        assert step.step_id == "soloist_terms"

        # refusing consent bounces back to the engine choice with an error
        step = await _submit(session, {CONF_SOLOIST_CONSENT: False})
        assert step.step_id == "connect_engine"
        assert step.errors == {"base": "soloist_consent_required"}

        step = await _submit(session, {CONF_BACKEND: BACKEND_SOLOIST})
        step = await _submit(session, {CONF_SOLOIST_CONSENT: True})
        assert step.step_id == "soloist_api_key"

        # a too-short key is rejected
        step = await _submit(session, {CONF_API_KEY: "too-short"})
        assert step.step_id == "soloist_api_key"
        assert step.errors == {CONF_API_KEY: "soloist_api_key_invalid"}

        session.handle_submit({CONF_API_KEY: _VALID_API_KEY})
        await task

    provision.assert_awaited_once_with(
        session,
        None,
        {
            CONF_BACKEND: BACKEND_SOLOIST,
            CONF_SOLOIST_CONSENT: True,
            CONF_API_KEY: _VALID_API_KEY,
        },
    )


async def test_connect_mode_reports_a_running_instance_as_ready() -> None:
    """An existing running system-wide instance is reused and reported as ready."""
    session = _make_session(setup_data={CONF_PLAYBACK_BACKEND: BACKEND_CONNECT})
    setup_data: dict[str, Any] = {CONF_PLAYBACK_BACKEND: BACKEND_CONNECT}
    with mock.patch.object(spotify_flow, "has_running_system_wide_connect", return_value=True):
        task = asyncio.create_task(spotify_flow._setup_playback(session, setup_data))
        step = await _wait_for_form(session)

        # the stored mode preselects
        assert _entry(step, CONF_PLAYBACK_BACKEND).value == BACKEND_CONNECT

        step = await _submit(session, {CONF_PLAYBACK_BACKEND: BACKEND_CONNECT})
        assert step.step_id == "connect_mode"
        assert any(entry.key == "connect_ready" for entry in step.entries)
        session.handle_submit({})
        await task

    assert setup_data[CONF_PLAYBACK_BACKEND] == BACKEND_CONNECT


@pytest.mark.parametrize(
    ("running_plugin", "expected"),
    [(None, BACKEND_LIBRESPOT), (object(), BACKEND_CONNECT)],
)
async def test_fresh_setup_preselects_connect_only_with_a_running_plugin(
    running_plugin: object | None, expected: str
) -> None:
    """A fresh setup preselects Connect only when a Connect instance already runs."""
    session = _make_session()
    session.mass.get_provider = mock.Mock(  # type: ignore[method-assign]
        return_value=running_plugin
    )
    setup_data: dict[str, Any] = {}
    task = asyncio.create_task(spotify_flow._setup_playback(session, setup_data))
    step = await _wait_for_form(session)

    assert _entry(step, CONF_PLAYBACK_BACKEND).value == expected

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
