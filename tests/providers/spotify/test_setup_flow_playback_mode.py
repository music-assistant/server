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

if TYPE_CHECKING:
    from music_assistant_models.setup_flow import SetupFlowStep


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


async def _submit(session: SetupSession, values: dict[str, Any]) -> None:
    """Submit form values (which must validate)."""
    assert session.handle_submit(values) is None


async def test_connect_mode_ensures_instance_and_clears_librespot_credential() -> None:
    """Choosing Connect ensures the system-wide instance and wipes the librespot secret."""
    session = _make_session(kind="reconfigure", setup_data={CONF_LIBRESPOT_CREDENTIALS: "secret"})
    setup_data: dict[str, Any] = {CONF_LIBRESPOT_CREDENTIALS: "secret"}
    ensure = mock.AsyncMock(return_value=True)
    with mock.patch.object(spotify_flow, "ensure_connect_instance", ensure):
        task = asyncio.create_task(spotify_flow._setup_playback(session, setup_data))
        step = await _wait_for_form(session)

        # an instance predating the choice preselects librespot
        assert step.step_id == "playback_backend"
        entry = next(entry for entry in step.entries if entry.key == CONF_PLAYBACK_BACKEND)
        assert entry.value == BACKEND_LIBRESPOT

        await _submit(session, {CONF_PLAYBACK_BACKEND: BACKEND_CONNECT})
        steer = await _wait_for_form(session, step)
        # a freshly created instance steers the user to complete its setup
        assert steer.step_id == "connect_mode"
        assert any(entry.key == "connect_setup_needed" for entry in steer.entries)
        await _submit(session, {})
        await task

    ensure.assert_awaited_once()
    assert setup_data[CONF_PLAYBACK_BACKEND] == BACKEND_CONNECT
    assert setup_data[CONF_LIBRESPOT_CREDENTIALS] is None


async def test_connect_mode_reports_a_running_instance_as_ready() -> None:
    """An existing running system-wide instance is reused and reported as ready."""
    session = _make_session(setup_data={CONF_PLAYBACK_BACKEND: BACKEND_CONNECT})
    setup_data: dict[str, Any] = {CONF_PLAYBACK_BACKEND: BACKEND_CONNECT}
    ensure = mock.AsyncMock(return_value=False)
    with (
        mock.patch.object(spotify_flow, "ensure_connect_instance", ensure),
        mock.patch.object(spotify_flow, "has_running_system_wide_connect", return_value=True),
    ):
        task = asyncio.create_task(spotify_flow._setup_playback(session, setup_data))
        step = await _wait_for_form(session)

        # the stored mode preselects
        entry = next(entry for entry in step.entries if entry.key == CONF_PLAYBACK_BACKEND)
        assert entry.value == BACKEND_CONNECT

        await _submit(session, {CONF_PLAYBACK_BACKEND: BACKEND_CONNECT})
        steer = await _wait_for_form(session, step)
        assert any(entry.key == "connect_ready" for entry in steer.entries)
        await _submit(session, {})
        await task

    ensure.assert_awaited_once()
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

    entry = next(entry for entry in step.entries if entry.key == CONF_PLAYBACK_BACKEND)
    assert entry.value == expected

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
