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
    BACKEND_SOLOIST,
    CONF_API_KEY,
    CONF_BACKEND,
    CONF_SOLOIST_CONSENT,
)

if TYPE_CHECKING:
    from music_assistant_models.setup_flow import SetupFlowStep

_VALID_API_KEY = "soloist-api-key-0123456789abcdef"
_ACCESS_TOKEN = "at-test"


def _make_session(
    *,
    kind: str = "setup",
    setup_data: dict[str, Any] | None = None,
) -> SetupSession:
    """Return a real setup session driving the playback-mode steps."""
    mass = mock.Mock()
    mass.get_provider = mock.Mock(return_value=None)
    mass.providers = []

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


async def _wait_for_step(
    session: SetupSession,
    previous: SetupFlowStep | None = None,
    step_type: FlowStepType = FlowStepType.FORM,
) -> SetupFlowStep:
    """Wait until a (new) step of the given type is published and return it."""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        step = session.current_step
        if step is not None and step.type == step_type and step is not previous:
            return step
        await asyncio.sleep(0.01)
    raise AssertionError(f"{step_type} step not published")


async def _submit(session: SetupSession, values: dict[str, Any]) -> SetupFlowStep:
    """Submit form values (which must validate) and return the next published form step."""
    previous = session.current_step
    assert previous is not None
    assert session.handle_submit(values) is None
    return await _wait_for_step(session, previous)


def _entry(step: SetupFlowStep, key: str) -> Any:
    """Return the form entry with the given key from a step."""
    return next(entry for entry in step.entries if entry.key == key)


def _connect_patches(
    *,
    running: bool = False,
    device_found: bool = False,
    provision: mock.AsyncMock | None = None,
) -> list[Any]:
    """Return the patches that stub the Connect branch's provisioning and pairing."""
    return [
        mock.patch.object(spotify_flow, "has_running_system_wide_connect", return_value=running),
        mock.patch.object(
            spotify_flow, "get_system_wide_connect_config_id", mock.AsyncMock(return_value=None)
        ),
        mock.patch.object(
            spotify_flow, "_find_connect_device", mock.AsyncMock(return_value=device_found)
        ),
        mock.patch.object(spotify_flow, "_await_connect_device", mock.AsyncMock()),
        mock.patch.object(spotify_flow, "_stamp_verified_connect_account", mock.AsyncMock()),
        mock.patch.object(
            spotify_flow,
            "_provision_connect_instance",
            provision or mock.AsyncMock(return_value="spotify_connect--new"),
        ),
        mock.patch.object(spotify_flow, "verify_platform_supported"),
    ]


async def test_connect_mode_sets_up_soloist_and_pairs_inline() -> None:
    """Choosing Connect collects consent + API key, provisions Soloist and pairs in-flow."""
    session = _make_session(kind="reconfigure", setup_data={CONF_LIBRESPOT_CREDENTIALS: "secret"})
    setup_data: dict[str, Any] = {CONF_LIBRESPOT_CREDENTIALS: "secret"}
    provision = mock.AsyncMock(return_value="spotify_connect--new")
    patches = _connect_patches(provision=provision)
    for patcher in patches:
        patcher.start()
    try:
        task = asyncio.create_task(spotify_flow._setup_playback(session, setup_data, _ACCESS_TOKEN))
        step = await _wait_for_step(session)

        # an instance predating the choice preselects librespot
        assert step.step_id == "playback_backend"
        assert _entry(step, CONF_PLAYBACK_BACKEND).value == BACKEND_LIBRESPOT

        # the Soloist engine is set up automatically: warning/consent first
        step = await _submit(session, {CONF_PLAYBACK_BACKEND: BACKEND_CONNECT})
        assert step.step_id == "soloist_terms"

        step = await _submit(session, {CONF_SOLOIST_CONSENT: True})
        assert step.step_id == "soloist_api_key"

        step = await _submit(session, {CONF_API_KEY: _VALID_API_KEY})
        # the pairing is announced first, so the user has the Spotify app at hand
        assert step.step_id == "connect_pairing_intro"
        session.handle_submit({})
        # provisioning and the pairing wait are stubbed; the flow completes
        await asyncio.wait_for(task, timeout=5)
    finally:
        for patcher in patches:
            patcher.stop()

    provision.assert_awaited_once_with(
        session,
        None,
        {
            CONF_BACKEND: BACKEND_SOLOIST,
            CONF_SOLOIST_CONSENT: True,
            CONF_API_KEY: _VALID_API_KEY,
        },
    )
    assert setup_data[CONF_PLAYBACK_BACKEND] == BACKEND_CONNECT
    assert setup_data[CONF_LIBRESPOT_CREDENTIALS] is None


async def test_connect_consent_refusal_bounces_to_the_mode_choice() -> None:
    """Refusing the Soloist consent returns to the mode choice with a clear error."""
    session = _make_session()
    setup_data: dict[str, Any] = {CONF_PLAYBACK_BACKEND: BACKEND_CONNECT}
    patches = _connect_patches()
    for patcher in patches:
        patcher.start()
    try:
        task = asyncio.create_task(spotify_flow._setup_playback(session, setup_data, _ACCESS_TOKEN))
        step = await _wait_for_step(session)
        step = await _submit(session, {CONF_PLAYBACK_BACKEND: BACKEND_CONNECT})
        assert step.step_id == "soloist_terms"

        step = await _submit(session, {CONF_SOLOIST_CONSENT: False})
        assert step.step_id == "playback_backend"
        assert step.errors == {"base": "soloist_consent_required"}
        assert not task.done()

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        for patcher in patches:
            patcher.stop()


async def test_connect_mode_reports_a_paired_running_instance_as_ready() -> None:
    """A running instance already paired to this account is reused as-is."""
    session = _make_session(setup_data={CONF_PLAYBACK_BACKEND: BACKEND_CONNECT})
    setup_data: dict[str, Any] = {CONF_PLAYBACK_BACKEND: BACKEND_CONNECT}
    patches = _connect_patches(running=True, device_found=True)
    for patcher in patches:
        patcher.start()
    try:
        task = asyncio.create_task(spotify_flow._setup_playback(session, setup_data, _ACCESS_TOKEN))
        step = await _wait_for_step(session)

        # the stored mode preselects
        assert _entry(step, CONF_PLAYBACK_BACKEND).value == BACKEND_CONNECT

        step = await _submit(session, {CONF_PLAYBACK_BACKEND: BACKEND_CONNECT})
        assert step.step_id == "connect_mode"
        assert any(entry.key == "connect_ready" for entry in step.entries)
        session.handle_submit({})
        await asyncio.wait_for(task, timeout=5)
    finally:
        for patcher in patches:
            patcher.stop()

    assert setup_data[CONF_PLAYBACK_BACKEND] == BACKEND_CONNECT


async def test_connect_running_but_unpaired_instance_goes_to_pairing() -> None:
    """A running instance not visible in this account's device list must be paired."""
    session = _make_session(setup_data={CONF_PLAYBACK_BACKEND: BACKEND_CONNECT})
    setup_data: dict[str, Any] = {CONF_PLAYBACK_BACKEND: BACKEND_CONNECT}
    patches = _connect_patches(running=True, device_found=False)
    for patcher in patches:
        patcher.start()
    try:
        task = asyncio.create_task(spotify_flow._setup_playback(session, setup_data, _ACCESS_TOKEN))
        await _wait_for_step(session)
        step = await _submit(session, {CONF_PLAYBACK_BACKEND: BACKEND_CONNECT})
        # no consent/key steps: straight to the pairing intro + progress step
        assert step.step_id == "connect_pairing_intro"
        session.handle_submit({})
        step = await _wait_for_step(session, step_type=FlowStepType.PROGRESS)
        assert step.step_id == "connect_pairing"
        await asyncio.wait_for(task, timeout=5)
    finally:
        for patcher in patches:
            patcher.stop()


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
    task = asyncio.create_task(spotify_flow._setup_playback(session, setup_data, _ACCESS_TOKEN))
    step = await _wait_for_step(session)

    assert _entry(step, CONF_PLAYBACK_BACKEND).value == expected

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize(
    ("product", "aborts"),
    [("premium", False), ("free", True), ("open", True), ("", False), (None, False)],
)
async def test_account_check_turns_away_non_premium_accounts(
    product: str | None, aborts: bool
) -> None:
    """A non-Premium account is turned away right after the sign-in."""
    from music_assistant.models.setup_flow import AbortFlow  # noqa: PLC0415

    session = _make_session()
    response = mock.MagicMock()
    response.status = 200
    response.json = mock.AsyncMock(
        return_value={"id": "u1"} if product is None else {"id": "u1", "product": product}
    )
    session.mass.http_session.get = mock.MagicMock(  # type: ignore[method-assign]
        return_value=mock.MagicMock(
            __aenter__=mock.AsyncMock(return_value=response), __aexit__=mock.AsyncMock()
        )
    )

    if aborts:
        with pytest.raises(AbortFlow, match="premium_required"):
            await spotify_flow._verify_account(session, "at-test")
    else:
        await spotify_flow._verify_account(session, "at-test")


async def test_account_check_tolerates_a_failing_lookup() -> None:
    """A lookup Spotify does not answer must not block the setup."""
    session = _make_session()
    response = mock.MagicMock()
    response.status = 503
    session.mass.http_session.get = mock.MagicMock(  # type: ignore[method-assign]
        return_value=mock.MagicMock(
            __aenter__=mock.AsyncMock(return_value=response), __aexit__=mock.AsyncMock()
        )
    )

    await spotify_flow._verify_account(session, "at-test")


@pytest.mark.parametrize(
    ("other_instance_id", "aborts"),
    [("spotify--other", True), ("spotify--test", False)],
)
async def test_account_check_turns_away_an_already_configured_account(
    other_instance_id: str, aborts: bool
) -> None:
    """An account already served by another instance is refused; a reconfigure is not."""
    from music_assistant.models.setup_flow import AbortFlow  # noqa: PLC0415
    from music_assistant.providers.spotify.provider import SpotifyProvider  # noqa: PLC0415

    session = _make_session(kind="reconfigure")
    existing = mock.MagicMock(spec=SpotifyProvider)
    existing.instance_id = other_instance_id
    existing.account_id = "u1"
    session.mass.providers = [existing]  # type: ignore[misc]
    response = mock.MagicMock()
    response.status = 200
    response.json = mock.AsyncMock(return_value={"id": "u1", "product": "premium"})
    session.mass.http_session.get = mock.MagicMock(  # type: ignore[method-assign]
        return_value=mock.MagicMock(
            __aenter__=mock.AsyncMock(return_value=response), __aexit__=mock.AsyncMock()
        )
    )

    if aborts:
        with pytest.raises(AbortFlow, match="account_already_configured"):
            await spotify_flow._verify_account(session, "at-test")
    else:
        # the instance being reconfigured keeps its own account
        await spotify_flow._verify_account(session, "at-test")
