"""Tests for the Spotify Connect setup flow (backend choice, soloist branch, reconfigure)."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any
from unittest import mock

import pytest
from music_assistant_models.enums import ConfigEntryType, FlowStepType

from music_assistant.models.setup_flow import SetupFlowContext, SetupSession
from music_assistant.providers.spotify_connect import (
    BACKEND_GO_LIBRESPOT,
    BACKEND_SOLOIST,
    CONF_API_KEY,
    CONF_BACKEND,
    CONF_MASS_PLAYER_ID,
    CONF_PUBLISH_NAME,
    CONF_SOLOIST_CONSENT,
)
from music_assistant.providers.spotify_connect import setup_flow as spotify_flow
from music_assistant.providers.spotify_connect.soloist.runtime import UnsupportedPlatformError

if TYPE_CHECKING:
    from music_assistant_models.setup_flow import SetupFlowStep

_VALID_API_KEY = "soloist-api-key-0123456789abcdef"

_SOLOIST_SETUP_DATA = {
    CONF_BACKEND: BACKEND_SOLOIST,
    CONF_API_KEY: _VALID_API_KEY,
    CONF_SOLOIST_CONSENT: True,
    CONF_MASS_PLAYER_ID: "living-room",
    CONF_PUBLISH_NAME: "Living Room Spotify",
}


def _player(player_id: str, display_name: str) -> mock.Mock:
    """Return a minimal player for setup-flow option generation."""
    player = mock.Mock()
    player.player_id = player_id
    player.display_name = display_name
    return player


def _make_session(
    *,
    kind: str = "setup",
    setup_data: dict[str, Any] | None = None,
    values: dict[str, Any] | None = None,
) -> tuple[SetupSession, dict[str, Any]]:
    """Return a real setup session and the values collected by its finish handler."""
    mass = mock.Mock()
    mass.players.all_players.return_value = [
        _player("living-room", "Living Room"),
        _player("kitchen", "Kitchen"),
    ]
    collected: dict[str, Any] = {}

    async def finish(_session: SetupSession, submitted: dict[str, Any]) -> dict[str, str]:
        collected.update(submitted)
        return {"instance_id": "spotify_connect--test"}

    context = SetupFlowContext(
        kind="reconfigure" if kind == "reconfigure" else "setup",
        reason="user",
        domain="spotify_connect",
        instance_id="spotify_connect--test" if kind == "reconfigure" else None,
        setup_data=setup_data or {},
        values=values or {},
    )
    return SetupSession(mass, "flow-test", context, finish), collected


async def _start_flow(session: SetupSession) -> tuple[asyncio.Task[None], SetupFlowStep]:
    """Start the setup flow and wait for its first form step."""
    task = asyncio.create_task(spotify_flow.run_setup(session))
    return task, await _wait_for_form(session)


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


async def _wait_finished(session: SetupSession) -> None:
    """Wait for a setup session to finish."""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if session.finished:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("setup flow did not finish")


async def _cancel(task: asyncio.Task[None]) -> None:
    """Cancel a still-running flow task."""
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def _entry(step: SetupFlowStep, key: str) -> Any:
    """Return the form entry with the given key from a step."""
    return next(entry for entry in step.entries if entry.key == key)


async def test_new_setup_go_librespot_path() -> None:
    """The go-librespot branch keeps the original player/name step and its values."""
    session, collected = _make_session()
    task, step = await _start_flow(session)

    # the flow opens with an expanded backend choice, defaulting to soloist
    assert step.step_id == "backend"
    assert [entry.key for entry in step.entries] == [CONF_BACKEND]
    backend_entry = _entry(step, CONF_BACKEND)
    assert [option.value for option in backend_entry.options] == [
        BACKEND_SOLOIST,
        BACKEND_GO_LIBRESPOT,
    ]
    assert backend_entry.expanded_options is True
    assert backend_entry.default_value == BACKEND_SOLOIST
    assert backend_entry.value == BACKEND_SOLOIST

    step = await _submit(session, {CONF_BACKEND: BACKEND_GO_LIBRESPOT})
    assert step.step_id == "user"
    player_entry = _entry(step, CONF_MASS_PLAYER_ID)
    assert [option.value for option in player_entry.options] == [
        "__auto__",
        "kitchen",
        "living-room",
    ]

    session.handle_submit(
        {CONF_MASS_PLAYER_ID: "living-room", CONF_PUBLISH_NAME: "Living Room Spotify"}
    )
    await _wait_finished(session)
    await task

    assert collected == {
        CONF_BACKEND: BACKEND_GO_LIBRESPOT,
        CONF_MASS_PLAYER_ID: "living-room",
        CONF_PUBLISH_NAME: "Living Room Spotify",
    }


async def test_new_setup_soloist_path() -> None:
    """The soloist branch collects consent, the API key and the volume mode."""
    session, collected = _make_session()
    with mock.patch.object(spotify_flow, "verify_platform_supported"):
        task, step = await _start_flow(session)

        # choosing soloist leads to the warning/consent step
        step = await _submit(session, {CONF_BACKEND: BACKEND_SOLOIST})
        assert step.step_id == "soloist_terms"

        # refusing consent blocks the branch: back to the choice with an error
        step = await _submit(session, {CONF_SOLOIST_CONSENT: False})
        assert step.step_id == "backend"
        assert step.errors == {"base": "soloist_consent_required"}
        assert not session.finished

        # pick soloist again and give consent this time
        step = await _submit(session, {CONF_BACKEND: BACKEND_SOLOIST})
        assert step.step_id == "soloist_terms"
        step = await _submit(session, {CONF_SOLOIST_CONSENT: True})
        assert step.step_id == "soloist_api_key"
        key_entry = _entry(step, CONF_API_KEY)
        assert key_entry.type == ConfigEntryType.SECURE_STRING

        # an empty and a too-short key are both rejected
        step = await _submit(session, {CONF_API_KEY: ""})
        assert step.step_id == "soloist_api_key"
        assert step.errors == {CONF_API_KEY: "soloist_api_key_invalid"}
        step = await _submit(session, {CONF_API_KEY: "too-short"})
        assert step.step_id == "soloist_api_key"
        assert step.errors == {CONF_API_KEY: "soloist_api_key_invalid"}

        # a valid key advances to the player/name step
        step = await _submit(session, {CONF_API_KEY: _VALID_API_KEY})
        assert step.step_id == "user"

        session.handle_submit(
            {CONF_MASS_PLAYER_ID: "kitchen", CONF_PUBLISH_NAME: "Kitchen Spotify"}
        )
        await _wait_finished(session)
        await task

    assert collected == {
        CONF_BACKEND: BACKEND_SOLOIST,
        CONF_SOLOIST_CONSENT: True,
        CONF_API_KEY: _VALID_API_KEY,
        CONF_MASS_PLAYER_ID: "kitchen",
        CONF_PUBLISH_NAME: "Kitchen Spotify",
    }


async def test_api_key_never_echoed_on_published_step() -> None:
    """The published API key step never carries a (typed) secret value."""
    session, _collected = _make_session()
    with mock.patch.object(spotify_flow, "verify_platform_supported"):
        task, _step = await _start_flow(session)
        await _submit(session, {CONF_BACKEND: BACKEND_SOLOIST})
        step = await _submit(session, {CONF_SOLOIST_CONSENT: True})

        # after a (failed) submit carrying the secret, the stored step is clean
        step = await _submit(session, {CONF_API_KEY: "too-short"})
        assert _entry(step, CONF_API_KEY).value is None

        await _cancel(task)


async def test_reconfigure_preselects_current_backend() -> None:
    """Reconfiguring an existing soloist config preselects soloist on the choice step."""
    session, _collected = _make_session(kind="reconfigure", setup_data=dict(_SOLOIST_SETUP_DATA))
    task, step = await _start_flow(session)

    assert step.step_id == "backend"
    assert _entry(step, CONF_BACKEND).value == BACKEND_SOLOIST

    await _cancel(task)


async def test_reconfigure_soloist_keeps_stored_key_on_empty_input() -> None:
    """An existing API key survives the key step when the field is left empty."""
    session, collected = _make_session(kind="reconfigure", setup_data=dict(_SOLOIST_SETUP_DATA))
    with mock.patch.object(spotify_flow, "verify_platform_supported"):
        task, _step = await _start_flow(session)
        step = await _submit(session, {CONF_BACKEND: BACKEND_SOLOIST})

        # consent given earlier is prefilled
        assert step.step_id == "soloist_terms"
        assert _entry(step, CONF_SOLOIST_CONSENT).value is True
        step = await _submit(session, {CONF_SOLOIST_CONSENT: True})

        # with a stored key the field is optional and a hint label is shown
        assert step.step_id == "soloist_api_key"
        assert _entry(step, CONF_API_KEY).required is False
        assert any(entry.key == "soloist_api_key_hint" for entry in step.entries)
        step = await _submit(session, {CONF_API_KEY: ""})
        assert step.step_id == "user"

        session.handle_submit(
            {CONF_MASS_PLAYER_ID: "living-room", CONF_PUBLISH_NAME: "Living Room Spotify"}
        )
        await _wait_finished(session)
        await task

    assert collected[CONF_API_KEY] == _VALID_API_KEY


async def test_switch_soloist_to_go_librespot_clears_secrets_on_finish() -> None:
    """Switching to go-librespot wipes the soloist secrets, but only when finish succeeds."""
    session, collected = _make_session(kind="reconfigure", setup_data=dict(_SOLOIST_SETUP_DATA))
    task, _step = await _start_flow(session)

    step = await _submit(session, {CONF_BACKEND: BACKEND_GO_LIBRESPOT})
    assert step.step_id == "user"

    session.handle_submit(
        {CONF_MASS_PLAYER_ID: "living-room", CONF_PUBLISH_NAME: "Living Room Spotify"}
    )
    await _wait_finished(session)
    await task

    assert collected[CONF_BACKEND] == BACKEND_GO_LIBRESPOT
    assert collected[CONF_API_KEY] == ""
    assert collected[CONF_SOLOIST_CONSENT] is False


async def test_switch_aborted_before_finish_keeps_soloist_secrets() -> None:
    """Aborting a backend switch before finish leaves the stored soloist secrets alone."""
    session, collected = _make_session(kind="reconfigure", setup_data=dict(_SOLOIST_SETUP_DATA))
    task, _step = await _start_flow(session)

    step = await _submit(session, {CONF_BACKEND: BACKEND_GO_LIBRESPOT})
    assert step.step_id == "user"
    await _cancel(task)

    # finish never ran, so nothing was persisted (the stored setup_data is untouched)
    assert collected == {}
    assert not session.finished


async def test_switch_go_librespot_to_soloist_keeps_existing_values() -> None:
    """Switching to soloist adds the soloist values without touching the go-librespot ones."""
    session, collected = _make_session(
        kind="reconfigure",
        setup_data={
            CONF_BACKEND: BACKEND_GO_LIBRESPOT,
            CONF_MASS_PLAYER_ID: "living-room",
            CONF_PUBLISH_NAME: "Legacy Speaker",
        },
    )
    with mock.patch.object(spotify_flow, "verify_platform_supported"):
        task, _step = await _start_flow(session)
        step = await _submit(session, {CONF_BACKEND: BACKEND_SOLOIST})
        assert step.step_id == "soloist_terms"
        step = await _submit(session, {CONF_SOLOIST_CONSENT: True})
        step = await _submit(session, {CONF_API_KEY: _VALID_API_KEY})
        assert step.step_id == "user"

        # the previously configured player and name are prefilled
        assert _entry(step, CONF_MASS_PLAYER_ID).value == "living-room"
        assert _entry(step, CONF_PUBLISH_NAME).value == "Legacy Speaker"

        session.handle_submit(
            {CONF_MASS_PLAYER_ID: "living-room", CONF_PUBLISH_NAME: "Legacy Speaker"}
        )
        await _wait_finished(session)
        await task

    assert collected == {
        CONF_BACKEND: BACKEND_SOLOIST,
        CONF_SOLOIST_CONSENT: True,
        CONF_API_KEY: _VALID_API_KEY,
        CONF_MASS_PLAYER_ID: "living-room",
        CONF_PUBLISH_NAME: "Legacy Speaker",
    }


async def test_unsupported_platform_bounces_back_to_choice() -> None:
    """Choosing soloist on an unsupported platform re-renders the choice with an error."""
    session, _collected = _make_session()
    task, _step = await _start_flow(session)

    # no platform patch: the real check refuses on non-Linux; force it for Linux CI
    with mock.patch.object(
        spotify_flow,
        "verify_platform_supported",
        side_effect=UnsupportedPlatformError("unsupported"),
    ):
        step = await _submit(session, {CONF_BACKEND: BACKEND_SOLOIST})

    assert step.step_id == "backend"
    assert step.errors == {"base": "soloist_unsupported_platform"}
    assert _entry(step, CONF_BACKEND).value == BACKEND_GO_LIBRESPOT

    await _cancel(task)
