"""Tests for the setup flow engine (SetupFlowMixin + SetupSession)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlsplit

import pytest
from aiohttp.test_utils import make_mocked_request
from music_assistant_models.auth import Scope
from music_assistant_models.config_entries import ConfigEntry, PlayerConfig
from music_assistant_models.enums import (
    ConfigEntryType,
    EventType,
    FlowStepType,
    PlayerType,
    ProviderType,
)
from music_assistant_models.errors import ActionUnavailable, LoginFailed, PlayerUnavailableError
from music_assistant_models.player import OutputProtocol
from music_assistant_models.provider import ProviderManifest

from music_assistant.constants import CONF_PLAYERS, CONF_PROVIDERS, ENCRYPT_SUFFIX
from music_assistant.mass import MusicAssistant
from music_assistant.models.player import Player, _state_fingerprint
from music_assistant.models.setup_flow import AbortFlow, SetupSession, StepExpiredError
from music_assistant.providers.filesystem_local.setup_flow import (
    run_setup as filesystem_local_run_setup,
)
from music_assistant.providers.qobuz.setup_flow import run_setup as qobuz_run_setup
from tests.common import MockPlayer, MockProvider

if TYPE_CHECKING:
    from music_assistant_models.event import MassEvent
    from music_assistant_models.setup_flow import SetupFlowStep

FAKE_DOMAIN = "_setup_flow_test"

USERNAME_ENTRY = ConfigEntry(key="username", type=ConfigEntryType.STRING, required=True)
PORT_ENTRY = ConfigEntry(key="port", type=ConfigEntryType.INTEGER, required=False, default_value=80)
PASSWORD_ENTRY = ConfigEntry(key="password", type=ConfigEntryType.SECURE_STRING, required=True)


@pytest.fixture
async def flow_mass(mass_minimal: MusicAssistant) -> AsyncGenerator[MusicAssistant]:
    """
    Provide a minimal server with a fake (flow-capable) provider manifest injected.

    Builds on mass_minimal (no webserver/ports bound) and stubs the narrow surface
    the flow engine touches: the dynamic-route webserver API and the players/music
    controllers.
    """
    manifest = ProviderManifest(
        type=ProviderType.MUSIC,
        domain=FAKE_DOMAIN,
        name="Setup Flow Test Provider",
        description="Fake provider for setup flow tests",
        codeowners=[],
    )
    mass_minimal._provider_manifests[FAKE_DOMAIN] = manifest
    # stub the dynamic-route surface so external-step tests need no bound port
    routes: dict[str, Any] = {}

    def register_dynamic_route(path: str, handler: Any, _method: str = "*") -> Any:
        routes[path] = handler
        return lambda: routes.pop(path, None)

    mass_minimal.webserver = SimpleNamespace(  # type: ignore[assignment]
        base_url="http://test.local:8095",
        register_dynamic_route=register_dynamic_route,
        unregister_dynamic_route=lambda path, _method="*": routes.pop(path, None),
        routes=routes,
    )
    mass_minimal.music = MagicMock()
    mass_minimal.players = MagicMock()
    mass_minimal.players.on_player_config_change = AsyncMock()
    try:
        yield mass_minimal
    finally:
        # abort any flow a test left behind so no tasks outlive the loop
        for flow in list(mass_minimal.config._setup_flows.values()):
            await mass_minimal.config._abort_flow(flow, reason="aborted")
        if (sweep_handle := mass_minimal.config._flow_sweep_handle) is not None:
            sweep_handle.cancel()
        mass_minimal._provider_manifests.pop(FAKE_DOMAIN, None)


@pytest.fixture
def flow_events(flow_mass: MusicAssistant) -> list[MassEvent]:
    """Capture all SETUP_FLOW_UPDATED events emitted during the test."""
    events: list[MassEvent] = []
    flow_mass.subscribe(events.append, EventType.SETUP_FLOW_UPDATED)
    return events


def _use_flow(mass: MusicAssistant, flow: Callable[[SetupSession], Awaitable[Any]]) -> Any:
    """Patch the engine's flow module loader to serve the given run_setup coroutine."""
    return patch.object(
        mass.config,
        "_get_setup_flow_module",
        AsyncMock(return_value=SimpleNamespace(run_setup=flow)),
    )


async def _wait_for(predicate: Callable[[], Any], timeout: float = 5.0) -> Any:
    """Wait until the predicate returns a truthy value (or fail the test)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result := predicate():
            return result
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met within timeout")


def _abort_events(events: list[MassEvent]) -> list[SetupFlowStep]:
    return [event.data for event in events if event.data.type == FlowStepType.ABORT]


def _fake_json_session(payload: dict[str, Any]) -> Any:
    """Return a stub http_session whose .post() yields a 200 JSON response (token exchange)."""
    response = SimpleNamespace(
        status=200,
        json=AsyncMock(return_value=payload),
        text=AsyncMock(return_value=""),
    )
    post_cm = MagicMock()
    post_cm.__aenter__ = AsyncMock(return_value=response)
    post_cm.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.post = MagicMock(return_value=post_cm)
    return session


async def _fire_callback(flow_mass: MusicAssistant, flow_id: str, query: str) -> None:
    """Hit a running flow's external-step callback route with the given query string."""
    callback_path = f"/setup_flow/callback/{flow_id}"
    handler = cast("Any", flow_mass.webserver).routes[callback_path]
    response = await handler(make_mocked_request("GET", f"{callback_path}?{query}"))
    assert response.status == 200


async def test_zero_input_provider_immediate_finish(flow_mass: MusicAssistant) -> None:
    """A provider without a setup_flow module is created right away with a FINISH step."""
    no_module = AsyncMock(return_value=None)
    with (
        patch.object(flow_mass.config, "_get_setup_flow_module", no_module),
        patch.object(flow_mass, "load_provider_config", AsyncMock()) as mock_load,
    ):
        step = await flow_mass.config.setup_provider(FAKE_DOMAIN)
    assert step.type == FlowStepType.FINISH
    assert step.result == {"instance_id": FAKE_DOMAIN}
    mock_load.assert_awaited_once()
    # the config was created and persisted (with empty values/setup_data)
    raw_conf = flow_mass.config.get(f"{CONF_PROVIDERS}/{FAKE_DOMAIN}")
    assert raw_conf["domain"] == FAKE_DOMAIN
    assert raw_conf["setup_data"] == {}
    # no flow session was registered for the synthesized step
    assert not flow_mass.config._setup_flows


async def test_form_flow_finish_success(
    flow_mass: MusicAssistant, flow_events: list[MassEvent]
) -> None:
    """A form flow persists its (encrypted) values as setup_data and loads the provider."""

    async def run_setup(session: SetupSession) -> None:
        values = await session.form([USERNAME_ENTRY, PASSWORD_ENTRY], step_id="credentials")
        await session.finish(values)

    with (
        _use_flow(flow_mass, run_setup),
        patch.object(flow_mass, "load_provider_config", AsyncMock()) as mock_load,
    ):
        step = await flow_mass.config.setup_provider(FAKE_DOMAIN)
        assert step.type == FlowStepType.FORM
        assert step.step_id == "credentials"
        # entries are stamped with the provider's translation owner
        assert all(x.translation_owner == f"provider.{FAKE_DOMAIN}" for x in step.entries)
        finish_step = await flow_mass.config.submit_setup_flow(
            step.flow_id, {"username": "marcel", "password": "secret"}
        )
    assert finish_step.type == FlowStepType.FINISH
    assert finish_step.result == {"instance_id": FAKE_DOMAIN}
    mock_load.assert_awaited_once()
    # values are stored in setup_data with strings encrypted at rest
    raw_conf = flow_mass.config.get(f"{CONF_PROVIDERS}/{FAKE_DOMAIN}")
    assert raw_conf["setup_data"]["username"].startswith(ENCRYPT_SUFFIX)
    assert flow_mass.config.decrypt_string(raw_conf["setup_data"]["username"]) == "marcel"
    assert flow_mass.config.decrypt_string(raw_conf["setup_data"]["password"]) == "secret"
    # the flow is cleaned up after finishing
    assert step.flow_id not in flow_mass.config._setup_flows
    # both steps were pushed as events
    await _wait_for(lambda: len(flow_events) >= 2)
    assert [event.data.type for event in flow_events[:2]] == [
        FlowStepType.FORM,
        FlowStepType.FINISH,
    ]
    assert all(event.object_id == step.flow_id for event in flow_events)


async def test_form_flow_translation_params(flow_mass: MusicAssistant) -> None:
    """A form step exposes translation parameters for its title and description."""

    async def run_setup(session: SetupSession) -> None:
        await session.form(
            [USERNAME_ENTRY],
            step_id="credentials",
            translation_params=["https://example.com/callback"],
        )

    with _use_flow(flow_mass, run_setup):
        step = await flow_mass.config.setup_provider(FAKE_DOMAIN)
        assert step.translation_params == ["https://example.com/callback"]
        await flow_mass.config.abort_setup_flow(step.flow_id)


async def test_form_validation_errors(flow_mass: MusicAssistant) -> None:
    """Invalid submitted values return the FORM step with errors, without advancing the flow."""
    advanced = asyncio.Event()

    async def run_setup(session: SetupSession) -> None:
        values = await session.form([USERNAME_ENTRY, PORT_ENTRY])
        advanced.set()
        await session.finish(values)

    with (
        _use_flow(flow_mass, run_setup),
        patch.object(flow_mass, "load_provider_config", AsyncMock()),
    ):
        step = await flow_mass.config.setup_provider(FAKE_DOMAIN)
        # missing required value + unparsable integer
        error_step = await flow_mass.config.submit_setup_flow(
            step.flow_id, {"port": "not-a-number"}
        )
        assert error_step.type == FlowStepType.FORM
        assert error_step.errors == {"username": "required", "port": "invalid_value"}
        assert not advanced.is_set()
        assert step.flow_id in flow_mass.config._setup_flows
        # a valid re-submit picks up where the form left off
        finish_step = await flow_mass.config.submit_setup_flow(
            step.flow_id, {"username": "marcel", "port": 8095}
        )
    assert advanced.is_set()
    assert finish_step.type == FlowStepType.FINISH
    raw_conf = flow_mass.config.get(f"{CONF_PROVIDERS}/{FAKE_DOMAIN}")
    assert raw_conf["setup_data"]["port"] == 8095


async def test_submit_returns_next_form_step(flow_mass: MusicAssistant) -> None:
    """Submitting a multi-step flow returns the next FORM step."""

    async def run_setup(session: SetupSession) -> None:
        await session.form([USERNAME_ENTRY], step_id="first")
        values = await session.form([PORT_ENTRY], step_id="second", last_step=True)
        await session.finish(values)

    with (
        _use_flow(flow_mass, run_setup),
        patch.object(flow_mass, "load_provider_config", AsyncMock()),
    ):
        step = await flow_mass.config.setup_provider(FAKE_DOMAIN)
        assert step.step_id == "first"
        second_step = await flow_mass.config.submit_setup_flow(step.flow_id, {"username": "marcel"})
        assert second_step.type == FlowStepType.FORM
        assert second_step.step_id == "second"
        assert second_step.last_step is True
        finish_step = await flow_mass.config.submit_setup_flow(step.flow_id, {"port": 1234})
        assert finish_step.type == FlowStepType.FINISH


@pytest.mark.usefixtures("flow_events")
async def test_finish_failure_rolls_back_provider_config(flow_mass: MusicAssistant) -> None:
    """A failing provider load on finish removes the created config again."""

    async def run_setup(session: SetupSession) -> None:
        values = await session.form([USERNAME_ENTRY])
        # SetupFlowError deliberately not caught: flow ends with the failure message
        await session.finish(values)

    with (
        _use_flow(flow_mass, run_setup),
        patch.object(
            flow_mass, "load_provider_config", AsyncMock(side_effect=LoginFailed("bad creds"))
        ),
    ):
        step = await flow_mass.config.setup_provider(FAKE_DOMAIN)
        abort_step = await flow_mass.config.submit_setup_flow(step.flow_id, {"username": "x"})
    assert abort_step.type == FlowStepType.ABORT
    assert abort_step.reason == "bad creds"
    # the config created during finish was removed again
    assert flow_mass.config.get(f"{CONF_PROVIDERS}/{FAKE_DOMAIN}") is None
    assert step.flow_id not in flow_mass.config._setup_flows


async def test_finish_failure_author_retry_loop(flow_mass: MusicAssistant) -> None:
    """An author can catch SetupFlowError and re-render the form with the error."""

    async def run_setup(session: SetupSession) -> None:
        values = await session.form([USERNAME_ENTRY])
        while True:
            try:
                await session.finish(values)
                return
            except Exception as err:
                values = await session.form([USERNAME_ENTRY], errors={"base": str(err)})

    load_mock = AsyncMock(side_effect=[LoginFailed("bad creds"), None])
    with (
        _use_flow(flow_mass, run_setup),
        patch.object(flow_mass, "load_provider_config", load_mock),
    ):
        step = await flow_mass.config.setup_provider(FAKE_DOMAIN)
        retry_step = await flow_mass.config.submit_setup_flow(step.flow_id, {"username": "x"})
        assert retry_step.type == FlowStepType.FORM
        assert retry_step.errors == {"base": "bad creds"}
        finish_step = await flow_mass.config.submit_setup_flow(step.flow_id, {"username": "y"})
    assert finish_step.type == FlowStepType.FINISH
    assert flow_mass.config.get(f"{CONF_PROVIDERS}/{FAKE_DOMAIN}") is not None


async def test_external_step_callback_roundtrip(flow_mass: MusicAssistant) -> None:
    """An external step resumes the flow with the merged callback (query) params."""
    received: dict[str, str] = {}

    async def run_setup(session: SetupSession) -> None:
        assert session.callback_url.endswith(f"/setup_flow/callback/{session.flow_id}")
        received.update(await session.external("https://example.com/authorize"))
        await session.finish({"token": received["code"]})

    with (
        _use_flow(flow_mass, run_setup),
        patch.object(flow_mass, "load_provider_config", AsyncMock()),
    ):
        step = await flow_mass.config.setup_provider(FAKE_DOMAIN)
        assert step.type == FlowStepType.EXTERNAL
        assert step.url == "https://example.com/authorize"
        session = flow_mass.config._setup_flows[step.flow_id].session
        # the callback route was registered on the webserver (stub) for this flow
        registered_routes = cast("Any", flow_mass.webserver).routes
        callback_path = f"/setup_flow/callback/{step.flow_id}"
        handler = registered_routes[callback_path]
        request = make_mocked_request("GET", f"{callback_path}?code=abc&state=xyz")
        response = await handler(request)
        assert response.status == 200
        await _wait_for(lambda: session.finished)
    assert received == {"code": "abc", "state": "xyz"}
    raw_conf = flow_mass.config.get(f"{CONF_PROVIDERS}/{FAKE_DOMAIN}")
    assert flow_mass.config.decrypt_string(raw_conf["setup_data"]["token"]) == "abc"
    # the route is released again when the flow ends
    await _wait_for(lambda: callback_path not in registered_routes)


async def test_external_callback_json_body_coerced(flow_mass: MusicAssistant) -> None:
    """A JSON callback body with non-string values is coerced to the str params contract."""
    received: dict[str, str] = {}

    async def run_setup(session: SetupSession) -> None:
        received.update(await session.external("https://example.com/authorize"))
        await session.finish({})

    with (
        _use_flow(flow_mass, run_setup),
        patch.object(flow_mass, "load_provider_config", AsyncMock()),
    ):
        step = await flow_mass.config.setup_provider(FAKE_DOMAIN)
        session = flow_mass.config._setup_flows[step.flow_id].session
        request = SimpleNamespace(
            query={"state": "xyz"},
            method="POST",
            can_read_body=True,
            content_type="application/json",
            read=AsyncMock(return_value=b'{"code": 123, "granted": true}'),
        )
        response = await session.handle_callback(cast("Any", request))
        assert response.status == 200
        await _wait_for(lambda: session.finished)
    assert received == {"state": "xyz", "code": "123", "granted": "True"}


async def test_form_expiry_aborts_flow(
    flow_mass: MusicAssistant, flow_events: list[MassEvent]
) -> None:
    """An uncaught form deadline converts into a timed_out ABORT."""

    async def run_setup(session: SetupSession) -> None:
        await session.form([USERNAME_ENTRY], expires_in=0.05)
        await session.finish({})

    with _use_flow(flow_mass, run_setup):
        step = await flow_mass.config.setup_provider(FAKE_DOMAIN)
        assert step.expires_at is not None
        abort_step = await _wait_for(
            lambda: next(iter(_abort_events(flow_events)), None), timeout=2
        )
    assert abort_step.reason == "timed_out"
    assert step.flow_id not in flow_mass.config._setup_flows


async def test_form_expiry_author_refresh(flow_mass: MusicAssistant) -> None:
    """An author can catch StepExpiredError and refresh the step in place."""

    async def run_setup(session: SetupSession) -> None:
        try:
            await session.form([USERNAME_ENTRY], step_id="short_lived", expires_in=0.05)
        except StepExpiredError:
            await session.form([USERNAME_ENTRY], step_id="refreshed")
        await session.finish({})

    with (
        _use_flow(flow_mass, run_setup),
        patch.object(flow_mass, "load_provider_config", AsyncMock()),
    ):
        step = await flow_mass.config.setup_provider(FAKE_DOMAIN)
        assert step.step_id == "short_lived"
        flow = flow_mass.config._setup_flows[step.flow_id]
        await _wait_for(
            lambda: flow.session.current_step and flow.session.current_step.step_id == "refreshed",
            timeout=2,
        )
        # the refreshed step accepts input as usual
        finish_step = await flow_mass.config.submit_setup_flow(step.flow_id, {"username": "x"})
        assert finish_step.type == FlowStepType.FINISH


async def test_progress_until_expiry(
    flow_mass: MusicAssistant, flow_events: list[MassEvent]
) -> None:
    """progress_until enforces its deadline by raising StepExpiredError into the flow."""

    async def run_setup(session: SetupSession) -> None:
        try:
            await session.progress_until(
                asyncio.sleep(30), "waiting_for_device", text="press_button", expires_in=0.05
            )
        except StepExpiredError:
            raise AbortFlow("pairing_window_closed") from None

    with _use_flow(flow_mass, run_setup):
        step = await flow_mass.config.setup_provider(FAKE_DOMAIN)
        assert step.type == FlowStepType.PROGRESS
        assert step.progress_text == "press_button"
        assert step.expires_at is not None
        abort_step = await _wait_for(
            lambda: next(iter(_abort_events(flow_events)), None), timeout=2
        )
    assert abort_step.reason == "pairing_window_closed"


async def test_abort_runs_author_cleanup(
    flow_mass: MusicAssistant, flow_events: list[MassEvent]
) -> None:
    """Aborting a flow cancels the coroutine so the author's finally cleanup runs."""
    cleanup_ran = asyncio.Event()

    async def run_setup(session: SetupSession) -> None:
        try:
            await session.form([USERNAME_ENTRY])
        finally:
            cleanup_ran.set()

    with _use_flow(flow_mass, run_setup):
        step = await flow_mass.config.setup_provider(FAKE_DOMAIN)
        await flow_mass.config.abort_setup_flow(step.flow_id)
    assert cleanup_ran.is_set()
    assert step.flow_id not in flow_mass.config._setup_flows
    abort_step = await _wait_for(lambda: next(iter(_abort_events(flow_events)), None))
    assert abort_step.reason == "aborted"
    # continuing an aborted flow is rejected
    with pytest.raises(KeyError):
        await flow_mass.config.get_setup_flow(step.flow_id)


async def test_idle_flow_ttl_sweep(flow_mass: MusicAssistant, flow_events: list[MassEvent]) -> None:
    """The periodic sweeper aborts flows that have been idle for longer than the TTL."""

    async def run_setup(session: SetupSession) -> None:
        await session.form([USERNAME_ENTRY])
        await session.finish({})

    with _use_flow(flow_mass, run_setup):
        step = await flow_mass.config.setup_provider(FAKE_DOMAIN)
        flow = flow_mass.config._setup_flows[step.flow_id]
        flow.session.last_activity = time.monotonic() - 16 * 60
        flow_mass.config._sweep_idle_flows()
        await _wait_for(lambda: step.flow_id not in flow_mass.config._setup_flows)
    abort_step = await _wait_for(lambda: next(iter(_abort_events(flow_events)), None))
    assert abort_step.reason == "timed_out"


async def test_idle_sweep_respects_live_step_deadline(
    flow_mass: MusicAssistant, flow_events: list[MassEvent]
) -> None:
    """The sweeper must not abort a flow whose current step advertises a longer deadline."""

    async def run_setup(session: SetupSession) -> None:
        await session.form([USERNAME_ENTRY], expires_in=3600)
        await session.finish({})

    with _use_flow(flow_mass, run_setup):
        step = await flow_mass.config.setup_provider(FAKE_DOMAIN)
        flow = flow_mass.config._setup_flows[step.flow_id]
        flow.session.last_activity = time.monotonic() - 16 * 60
        flow_mass.config._sweep_idle_flows()
        await asyncio.sleep(0.05)
        # still alive: the step's own countdown is the authoritative deadline
        assert step.flow_id in flow_mass.config._setup_flows
        assert not _abort_events(flow_events)


async def test_concurrent_starts_keep_single_flow(flow_mass: MusicAssistant) -> None:
    """Two concurrent starts for the same target never leave two live flows behind."""

    async def run_setup(session: SetupSession) -> None:
        try:
            await session.form([USERNAME_ENTRY])
        finally:
            # slow author cleanup: widens the abort window the re-scan protects against
            await asyncio.sleep(0.05)
        await session.finish({})

    with _use_flow(flow_mass, run_setup):
        await flow_mass.config.setup_provider(FAKE_DOMAIN)
        await asyncio.gather(
            flow_mass.config.setup_provider(FAKE_DOMAIN),
            flow_mass.config.setup_provider(FAKE_DOMAIN),
        )
        target_flows = [
            flow
            for flow in flow_mass.config._setup_flows.values()
            if flow.target_key == f"provider_setup:{FAKE_DOMAIN}"
        ]
        assert len(target_flows) <= 1


async def test_finish_twice_raises(flow_mass: MusicAssistant, flow_events: list[MassEvent]) -> None:
    """A second finish() call is a flow-author bug and must not create a second target."""

    async def run_setup(session: SetupSession) -> None:
        await session.finish({})
        await session.finish({})

    with (
        _use_flow(flow_mass, run_setup),
        patch.object(flow_mass, "load_provider_config", AsyncMock()),
    ):
        await flow_mass.config.setup_provider(FAKE_DOMAIN)
        abort_step = await _wait_for(lambda: next(iter(_abort_events(flow_events)), None))
    assert abort_step.reason == "internal_error"
    # the first finish still created (exactly one) provider config
    assert flow_mass.config.get(f"{CONF_PROVIDERS}/{FAKE_DOMAIN}")["domain"] == FAKE_DOMAIN


async def test_bare_callback_does_not_extend_ttl(flow_mass: MusicAssistant) -> None:
    """The unauthenticated callback route must not keep an idle flow alive (no pending external)."""

    async def run_setup(session: SetupSession) -> None:
        await session.form([USERNAME_ENTRY])
        await session.finish({})

    with _use_flow(flow_mass, run_setup):
        step = await flow_mass.config.setup_provider(FAKE_DOMAIN)
        flow = flow_mass.config._setup_flows[step.flow_id]
        activity_before = flow.session.last_activity
        # the callback route is only registered while an external step is pending,
        # but the session handler itself must also not count a bare hit as activity
        request = make_mocked_request("GET", f"/setup_flow/callback/{step.flow_id}?foo=bar")
        await flow.session.handle_callback(request)
        assert flow.session.last_activity == activity_before


async def test_submit_timeout_returns_transient_progress(flow_mass: MusicAssistant) -> None:
    """A submit whose next step is slow returns a progress step, not the consumed form."""

    async def run_setup(session: SetupSession) -> None:
        await session.form([USERNAME_ENTRY])
        await asyncio.sleep(0.3)
        await session.form([PORT_ENTRY], step_id="second")
        await session.finish({})

    with (
        _use_flow(flow_mass, run_setup),
        patch("music_assistant.controllers.config.flows.NEXT_STEP_TIMEOUT", 0.05),
    ):
        step = await flow_mass.config.setup_provider(FAKE_DOMAIN)
        result = await flow_mass.config.submit_setup_flow(step.flow_id, {"username": "x"})
        assert result.type == FlowStepType.PROGRESS
        assert result.flow_id == step.flow_id

        # the real next step still arrives (idempotent get picks it up)
        def _second_form() -> SetupFlowStep | None:
            current = flow_mass.config._setup_flows[step.flow_id].session.current_step
            if current is not None and current.step_id == "second":
                return current
            return None

        next_step = await _wait_for(_second_form)
        assert next_step.type == FlowStepType.FORM


async def test_aborted_flow_scope_still_resolvable(flow_mass: MusicAssistant) -> None:
    """A cancel-driven ABORT publishes after the pop; its scope must still resolve."""

    async def run_setup(session: SetupSession) -> None:
        await session.form([USERNAME_ENTRY])
        await session.finish({})

    resolvable_at_publish: list[bool] = []

    def on_event(event: MassEvent) -> None:
        if event.data.type == FlowStepType.ABORT and event.object_id:
            resolvable_at_publish.append(
                flow_mass.config.get_setup_flow_required_scope(event.object_id) is not None
            )

    flow_mass.subscribe(on_event, EventType.SETUP_FLOW_UPDATED)
    with _use_flow(flow_mass, run_setup):
        step = await flow_mass.config.setup_provider(FAKE_DOMAIN)
        await flow_mass.config.abort_setup_flow(step.flow_id)
    # event delivery is async (call_soon); wait for the callback to run
    await _wait_for(lambda: resolvable_at_publish)
    assert resolvable_at_publish == [True]
    assert (
        flow_mass.config.get_setup_flow_required_scope(step.flow_id) == Scope.CONFIG_PROVIDERS_WRITE
    )


async def test_setup_flow_required_scope_accessor(flow_mass: MusicAssistant) -> None:
    """The event filter's scope accessor reports the flow's scope while it runs."""

    async def run_setup(session: SetupSession) -> None:
        await session.form([USERNAME_ENTRY])
        await session.finish({})

    with _use_flow(flow_mass, run_setup):
        step = await flow_mass.config.setup_provider(FAKE_DOMAIN)
        assert (
            flow_mass.config.get_setup_flow_required_scope(step.flow_id)
            == Scope.CONFIG_PROVIDERS_WRITE
        )
    assert flow_mass.config.get_setup_flow_required_scope("nonexistent") is None


async def test_one_flow_per_target_replaces(
    flow_mass: MusicAssistant, flow_events: list[MassEvent]
) -> None:
    """Starting a flow for a target that already has one aborts the old flow."""

    async def run_setup(session: SetupSession) -> None:
        await session.form([USERNAME_ENTRY])
        await session.finish({})

    with _use_flow(flow_mass, run_setup):
        first_step = await flow_mass.config.setup_provider(FAKE_DOMAIN)
        second_step = await flow_mass.config.setup_provider(FAKE_DOMAIN)
    assert first_step.flow_id != second_step.flow_id
    assert first_step.flow_id not in flow_mass.config._setup_flows
    assert second_step.flow_id in flow_mass.config._setup_flows
    replaced = await _wait_for(lambda: next(iter(_abort_events(flow_events)), None))
    assert replaced.flow_id == first_step.flow_id
    assert replaced.reason == "replaced"


async def test_get_flow_is_idempotent(flow_mass: MusicAssistant) -> None:
    """config/flows/get re-renders the current step without ever advancing the flow."""

    async def run_setup(session: SetupSession) -> None:
        values = await session.form([USERNAME_ENTRY])
        await session.finish(values)

    with (
        _use_flow(flow_mass, run_setup),
        patch.object(flow_mass, "load_provider_config", AsyncMock()),
    ):
        step = await flow_mass.config.setup_provider(FAKE_DOMAIN)
        for _ in range(3):
            fetched = await flow_mass.config.get_setup_flow(step.flow_id)
            assert fetched.type == FlowStepType.FORM
            assert fetched.step_id == step.step_id
            assert fetched.flow_id == step.flow_id
        # the flow still accepts input afterwards
        finish_step = await flow_mass.config.submit_setup_flow(step.flow_id, {"username": "x"})
        assert finish_step.type == FlowStepType.FINISH


async def test_submit_without_pending_form(flow_mass: MusicAssistant) -> None:
    """Submitting while no FORM step is pending is rejected."""

    async def run_setup(session: SetupSession) -> None:
        await session.external("https://example.com/authorize")
        await session.finish({})

    with _use_flow(flow_mass, run_setup):
        step = await flow_mass.config.setup_provider(FAKE_DOMAIN)
        assert step.type == FlowStepType.EXTERNAL
        with pytest.raises(ActionUnavailable):
            await flow_mass.config.submit_setup_flow(step.flow_id, {"username": "x"})
        await flow_mass.config.abort_setup_flow(step.flow_id)


@pytest.mark.usefixtures("flow_events")
async def test_form_entries_with_action_rejected(flow_mass: MusicAssistant) -> None:
    """The engine rejects FORM entries that carry an action (banned inside flows)."""
    action_entry = ConfigEntry(
        key="authenticate", type=ConfigEntryType.STRING, action="auth", required=False
    )

    async def run_setup(session: SetupSession) -> None:
        await session.form([action_entry])
        await session.finish({})

    with _use_flow(flow_mass, run_setup):
        step = await flow_mass.config.setup_provider(FAKE_DOMAIN)
    # the ValueError ends the flow as an internal error
    assert step.type == FlowStepType.ABORT
    assert step.reason == "internal_error"


@pytest.mark.usefixtures("flow_events")
async def test_form_entries_with_action_type_rejected(flow_mass: MusicAssistant) -> None:
    """The engine rejects FORM entries of type ACTION (they render dead in the dialog)."""
    action_entry = ConfigEntry(key="authenticate", type=ConfigEntryType.ACTION, required=False)

    async def run_setup(session: SetupSession) -> None:
        await session.form([action_entry])
        await session.finish({})

    with _use_flow(flow_mass, run_setup):
        step = await flow_mass.config.setup_provider(FAKE_DOMAIN)
    # the ValueError ends the flow as an internal error
    assert step.type == FlowStepType.ABORT
    assert step.reason == "internal_error"


async def test_reconfigure_prefill_and_success(flow_mass: MusicAssistant) -> None:
    """Reconfigure decrypts setup_data for prefill and merges the new values on finish."""
    instance_id = FAKE_DOMAIN
    flow_mass.config.set(
        f"{CONF_PROVIDERS}/{instance_id}",
        {
            "type": "music",
            "domain": FAKE_DOMAIN,
            "instance_id": instance_id,
            "enabled": True,
            "values": {"region": "eu"},
            "setup_data": {
                "token": flow_mass.config.encrypt_string("old-secret"),
                "device_id": flow_mass.config.encrypt_string("device-1"),
            },
            "last_error": {"error_code": LoginFailed.error_code, "message": "expired"},
        },
    )
    contexts: list[Any] = []

    async def run_setup(session: SetupSession) -> None:
        contexts.append(session.context)
        values = await session.form([ConfigEntry(key="token", type=ConfigEntryType.STRING)])
        await session.finish(values)

    with (
        _use_flow(flow_mass, run_setup),
        patch.object(flow_mass, "load_provider_config", AsyncMock()) as mock_load,
    ):
        step = await flow_mass.config.reconfigure_provider(instance_id)
        assert step.type == FlowStepType.FORM
        context = contexts[0]
        assert context.kind == "reconfigure"
        assert context.reason == "auth"
        assert context.instance_id == instance_id
        assert context.setup_data == {"token": "old-secret", "device_id": "device-1"}
        assert context.values == {"region": "eu"}
        finish_step = await flow_mass.config.submit_setup_flow(
            step.flow_id, {"token": "new-secret"}
        )
    assert finish_step.type == FlowStepType.FINISH
    assert finish_step.result == {"instance_id": instance_id}
    mock_load.assert_awaited_once()
    raw_conf = flow_mass.config.get(f"{CONF_PROVIDERS}/{instance_id}")
    # new value merged in (encrypted), untouched keys preserved, last_error cleared
    assert flow_mass.config.decrypt_string(raw_conf["setup_data"]["token"]) == "new-secret"
    assert flow_mass.config.decrypt_string(raw_conf["setup_data"]["device_id"]) == "device-1"
    assert raw_conf["last_error"] is None


async def test_reconfigure_failure_restores_setup_data(flow_mass: MusicAssistant) -> None:
    """A failing reload on reconfigure finish restores the previous setup_data."""
    instance_id = FAKE_DOMAIN
    original_setup_data = {"token": flow_mass.config.encrypt_string("old-secret")}
    flow_mass.config.set(
        f"{CONF_PROVIDERS}/{instance_id}",
        {
            "type": "music",
            "domain": FAKE_DOMAIN,
            "instance_id": instance_id,
            "enabled": True,
            "values": {},
            "setup_data": dict(original_setup_data),
        },
    )

    async def run_setup(session: SetupSession) -> None:
        values = await session.form([ConfigEntry(key="token", type=ConfigEntryType.STRING)])
        await session.finish(values)

    with (
        _use_flow(flow_mass, run_setup),
        patch.object(
            flow_mass, "load_provider_config", AsyncMock(side_effect=LoginFailed("still bad"))
        ),
    ):
        step = await flow_mass.config.reconfigure_provider(instance_id)
        abort_step = await flow_mass.config.submit_setup_flow(step.flow_id, {"token": "bogus"})
    assert abort_step.type == FlowStepType.ABORT
    assert abort_step.reason == "still bad"
    raw_conf = flow_mass.config.get(f"{CONF_PROVIDERS}/{instance_id}")
    assert raw_conf["setup_data"] == original_setup_data


async def test_reconfigure_without_flow_module_aborts(flow_mass: MusicAssistant) -> None:
    """Reconfigure on a flow-less provider returns a nothing_to_configure ABORT."""
    instance_id = FAKE_DOMAIN
    flow_mass.config.set(
        f"{CONF_PROVIDERS}/{instance_id}",
        {"type": "music", "domain": FAKE_DOMAIN, "instance_id": instance_id, "enabled": True},
    )
    with patch.object(flow_mass.config, "_get_setup_flow_module", AsyncMock(return_value=None)):
        step = await flow_mass.config.reconfigure_provider(instance_id)
    assert step.type == FlowStepType.ABORT
    assert step.reason == "nothing_to_configure"
    assert not flow_mass.config._setup_flows


async def test_setup_provider_single_instance_guard(flow_mass: MusicAssistant) -> None:
    """Setting up a non-multi-instance provider that already exists aborts fast."""
    flow_mass.config.set(
        f"{CONF_PROVIDERS}/{FAKE_DOMAIN}",
        {"type": "music", "domain": FAKE_DOMAIN, "instance_id": FAKE_DOMAIN, "enabled": True},
    )
    step = await flow_mass.config.setup_provider(FAKE_DOMAIN)
    assert step.type == FlowStepType.ABORT
    assert step.reason == "already_configured"


async def test_setup_unknown_provider_domain(flow_mass: MusicAssistant) -> None:
    """Setting up an unknown provider domain raises."""
    with pytest.raises(KeyError):
        await flow_mass.config.setup_provider("_no_such_provider")


class _FlowPlayer(MockPlayer):
    """Mock player that implements a setup (pairing) flow."""

    async def run_setup_flow(self, session: SetupSession) -> None:
        """Run a minimal pairing flow."""
        values = await session.form([ConfigEntry(key="pin", type=ConfigEntryType.STRING)])
        await session.finish(values)


async def test_player_setup_without_flow_aborts(flow_mass: MusicAssistant) -> None:
    """Player setup for a player without a run_setup_flow override aborts."""
    provider = MockProvider("test_players", instance_id="test_players--1")
    player = MockPlayer(provider, "test_player_1", "Player One")
    assert type(player).run_setup_flow is Player.run_setup_flow
    with patch.object(flow_mass.players, "get_player", return_value=player):
        step = await flow_mass.config.setup_player("test_player_1")
    assert step.type == FlowStepType.ABORT
    assert step.reason == "nothing_to_configure"


async def test_player_setup_abort_mid_finish_restores_setup_data(
    flow_mass: MusicAssistant,
) -> None:
    """Aborting a player flow while finish() is in flight restores the previous setup_data."""
    player_id = "test_player_1"
    provider = MockProvider("test_players", instance_id="test_players--1")
    player = _FlowPlayer(provider, player_id, "Player One")
    original_setup_data = {"pin": flow_mass.config.encrypt_string("0000")}
    flow_mass.config.set(
        f"{CONF_PLAYERS}/{player_id}",
        {
            "player_id": player_id,
            "provider": provider.instance_id,
            "enabled": True,
            "setup_data": dict(original_setup_data),
        },
    )
    finish_reached = asyncio.Event()

    async def hanging_get_player_config(_player_id: str) -> Any:
        finish_reached.set()
        await asyncio.sleep(3600)

    with (
        patch.object(flow_mass.players, "get_player", return_value=player),
        patch.object(flow_mass.config, "get_player_config", hanging_get_player_config),
    ):
        step = await flow_mass.config.setup_player(player_id)
        assert step.type == FlowStepType.FORM
        submit_task = asyncio.create_task(
            flow_mass.config.submit_setup_flow(step.flow_id, {"pin": "1234"})
        )
        await asyncio.wait_for(finish_reached.wait(), timeout=5)
        await flow_mass.config.abort_setup_flow(step.flow_id)
        submit_step = await submit_task
    # the abort interrupted finish(): the previous setup_data was restored
    raw_conf = flow_mass.config.get(f"{CONF_PLAYERS}/{player_id}")
    assert raw_conf["setup_data"] == original_setup_data
    assert submit_step.type == FlowStepType.ABORT


async def test_player_setup_reaches_unavailable_player(flow_mass: MusicAssistant) -> None:
    """
    Player setup must not gate on player availability.

    A player that needs setup is serialized as unavailable (available folds in
    needs_setup), so requesting availability here would lock out exactly the
    players this command exists for.
    """
    provider = MockProvider("test_players", instance_id="test_players--1")
    player = MockPlayer(provider, "test_player_1", "Player One")

    def get_player_needs_setup(player_id: str, raise_unavailable: bool = False) -> MockPlayer:
        # mimic the real controller for a needs_setup player: state.available is
        # False, so raise_unavailable=True would raise PlayerUnavailableError
        if raise_unavailable:
            raise PlayerUnavailableError(f"Player {player_id} is not available")
        return player

    with patch.object(flow_mass.players, "get_player", side_effect=get_player_needs_setup):
        step = await flow_mass.config.setup_player("test_player_1")
    assert step.type == FlowStepType.ABORT
    assert step.reason == "nothing_to_configure"


async def test_player_setup_flow_finish(flow_mass: MusicAssistant) -> None:
    """A player flow persists and applies encrypted setup_data to the active player."""
    events: list[MassEvent] = []
    flow_mass.subscribe(events.append, EventType.PLAYER_CONFIG_UPDATED)
    player_id = "test_player_1"
    provider = MockProvider("test_players", instance_id="test_players--1")
    player = _FlowPlayer(provider, player_id, "Player One")
    flow_mass.config.set(
        f"{CONF_PLAYERS}/{player_id}",
        {"player_id": player_id, "provider": provider.instance_id, "enabled": True},
    )
    player_config = PlayerConfig(values={}, provider=provider.instance_id, player_id=player_id)
    with (
        patch.object(flow_mass.players, "get_player", return_value=player),
        patch.object(flow_mass.config, "get_player_config", AsyncMock(return_value=player_config)),
    ):
        step = await flow_mass.config.setup_player(player_id)
        assert step.type == FlowStepType.FORM
        finish_step = await flow_mass.config.submit_setup_flow(step.flow_id, {"pin": "1234"})
    assert finish_step.type == FlowStepType.FINISH
    assert finish_step.result == {"player_id": player_id}
    raw_conf = flow_mass.config.get(f"{CONF_PLAYERS}/{player_id}")
    assert flow_mass.config.decrypt_string(raw_conf["setup_data"]["pin"]) == "1234"
    cast("AsyncMock", flow_mass.players.on_player_config_change).assert_awaited_once_with(
        player_config, {"setup_data/pin"}
    )
    config_event = await _wait_for(lambda: next(iter(events), None))
    assert config_event.object_id == player_id


async def test_player_setup_apply_failure_restores_setup_data(
    flow_mass: MusicAssistant,
) -> None:
    """A player setup failure restores the previous setup_data."""
    player_id = "test_player_1"
    provider = MockProvider("test_players", instance_id="test_players--1")
    player = _FlowPlayer(provider, player_id, "Player One")
    original_setup_data = {"pin": flow_mass.config.encrypt_string("0000")}
    flow_mass.config.set(
        f"{CONF_PLAYERS}/{player_id}",
        {
            "player_id": player_id,
            "provider": provider.instance_id,
            "enabled": True,
            "setup_data": dict(original_setup_data),
        },
    )
    player_config = PlayerConfig(values={}, provider=provider.instance_id, player_id=player_id)
    cast("AsyncMock", flow_mass.players.on_player_config_change).side_effect = RuntimeError(
        "apply failed"
    )
    with (
        patch.object(flow_mass.players, "get_player", return_value=player),
        patch.object(flow_mass.config, "get_player_config", AsyncMock(return_value=player_config)),
    ):
        step = await flow_mass.config.setup_player(player_id)
        abort_step = await flow_mass.config.submit_setup_flow(step.flow_id, {"pin": "1234"})
    assert abort_step.type == FlowStepType.ABORT
    assert abort_step.reason == "apply failed"
    raw_conf = flow_mass.config.get(f"{CONF_PLAYERS}/{player_id}")
    assert raw_conf["setup_data"] == original_setup_data


class _ProtocolChildPlayer(MockPlayer):
    """A protocol child player that needs setup and implements a pairing flow."""

    def __init__(self, provider: MockProvider, player_id: str, name: str) -> None:
        super().__init__(provider, player_id, name, player_type=PlayerType.PROTOCOL)
        self._attr_needs_setup = True
        self._attr_setup_reason = "pairing_required"

    async def run_setup_flow(self, session: SetupSession) -> None:
        values = await session.form(
            [ConfigEntry(key="pin", type=ConfigEntryType.STRING)], step_id="user"
        )
        await session.finish(values)


def _child_output_protocol(child: MockPlayer) -> OutputProtocol:
    """Build a (non-native) output protocol entry pointing at the given child player."""
    return OutputProtocol(
        output_protocol_id=child.player_id,
        name=child.display_name,
        protocol_domain=child.provider.domain,
        is_native=False,
        priority=10,
        available=False,
    )


def _set_player_conf(flow_mass: MusicAssistant, player: MockPlayer) -> None:
    """Persist a minimal player config for the given player."""
    flow_mass.config.set(
        f"{CONF_PLAYERS}/{player.player_id}",
        {
            "player_id": player.player_id,
            "provider": player.provider.instance_id,
            "enabled": True,
        },
    )


async def test_player_setup_delegates_to_single_child(flow_mass: MusicAssistant) -> None:
    """A wrapper player with one protocol child needing setup runs that child's flow."""
    parent_provider = MockProvider("universal_player", instance_id="universal_player")
    child_provider = MockProvider("airplay", instance_id="airplay")
    parent = MockPlayer(parent_provider, "up_parent", "Living Room")
    child = _ProtocolChildPlayer(child_provider, "ap_child", "Living Room AirPlay")
    # native option (skipped) + the protocol child that needs setup
    parent._cache["output_protocols"] = [
        OutputProtocol(
            output_protocol_id="native", name="", protocol_domain="", is_native=True, available=True
        ),
        _child_output_protocol(child),
    ]
    _set_player_conf(flow_mass, parent)
    _set_player_conf(flow_mass, child)
    players = {parent.player_id: parent, child.player_id: child}
    child_config = PlayerConfig(
        values={}, provider=child_provider.instance_id, player_id=child.player_id
    )
    with (
        patch.object(
            flow_mass.players, "get_player", side_effect=lambda pid, *_a, **_k: players.get(pid)
        ),
        patch.object(flow_mass.config, "get_player_config", AsyncMock(return_value=child_config)),
    ):
        step = await flow_mass.config.setup_player(parent.player_id)
        assert step.type == FlowStepType.FORM
        assert step.step_id == "user"
        # the flow runs as the CHILD: its steps localize under the child provider
        assert step.translation_owner == "provider.airplay"
        finish_step = await flow_mass.config.submit_setup_flow(step.flow_id, {"pin": "4321"})
    assert finish_step.type == FlowStepType.FINISH
    # persisted to the CHILD's config, not the parent's
    assert finish_step.result == {"player_id": child.player_id}
    child_raw = flow_mass.config.get(f"{CONF_PLAYERS}/{child.player_id}")
    assert flow_mass.config.decrypt_string(child_raw["setup_data"]["pin"]) == "4321"
    assert "setup_data" not in flow_mass.config.get(f"{CONF_PLAYERS}/{parent.player_id}")


async def test_player_setup_multi_child_selection(flow_mass: MusicAssistant) -> None:
    """A wrapper player with several children needing setup first asks which to set up."""
    parent_provider = MockProvider("universal_player", instance_id="universal_player")
    ap_provider = MockProvider("airplay", instance_id="airplay")
    ss_provider = MockProvider("sendspin", instance_id="sendspin")
    parent = MockPlayer(parent_provider, "up_parent", "Kitchen")
    child_a = _ProtocolChildPlayer(ap_provider, "ap_child", "Kitchen AirPlay")
    child_b = _ProtocolChildPlayer(ss_provider, "ss_child", "Kitchen Sendspin")
    parent._cache["output_protocols"] = [
        _child_output_protocol(child_a),
        _child_output_protocol(child_b),
    ]
    for player in (parent, child_a, child_b):
        _set_player_conf(flow_mass, player)
    players = {p.player_id: p for p in (parent, child_a, child_b)}

    def _get_config(pid: str) -> PlayerConfig:
        return PlayerConfig(values={}, provider=players[pid].provider.instance_id, player_id=pid)

    with (
        patch.object(
            flow_mass.players, "get_player", side_effect=lambda pid, *_a, **_k: players.get(pid)
        ),
        patch.object(flow_mass.config, "get_player_config", AsyncMock(side_effect=_get_config)),
    ):
        step = await flow_mass.config.setup_player(parent.player_id)
        # first a parent-owned selection form listing both children
        assert step.type == FlowStepType.FORM
        assert step.step_id == "select_child"
        assert step.translation_owner == "provider.universal_player"
        child_entry = next(entry for entry in step.entries if entry.key == "child")
        assert {option.value for option in child_entry.options} == {"ap_child", "ss_child"}
        # pick the sendspin child -> the flow retargets and runs the child's flow
        next_step = await flow_mass.config.submit_setup_flow(step.flow_id, {"child": "ss_child"})
        assert next_step.type == FlowStepType.FORM
        assert next_step.step_id == "user"
        assert next_step.translation_owner == "provider.sendspin"
        finish_step = await flow_mass.config.submit_setup_flow(step.flow_id, {"pin": "9999"})
    assert finish_step.type == FlowStepType.FINISH
    assert finish_step.result == {"player_id": "ss_child"}
    child_raw = flow_mass.config.get(f"{CONF_PLAYERS}/ss_child")
    assert flow_mass.config.decrypt_string(child_raw["setup_data"]["pin"]) == "9999"


async def test_player_setup_reruns_child_flow_when_nothing_needs_setup(
    flow_mass: MusicAssistant,
) -> None:
    """A wrapper player whose children are all set up still reaches the child's flow."""
    parent_provider = MockProvider("universal_player", instance_id="universal_player")
    child_provider = MockProvider("airplay", instance_id="airplay")
    parent = MockPlayer(parent_provider, "up_parent", "Bedroom")
    child = _ProtocolChildPlayer(child_provider, "ap_child", "Bedroom AirPlay")
    child._attr_needs_setup = False  # already set up: this is an on-demand re-run
    parent._cache["output_protocols"] = [_child_output_protocol(child)]
    _set_player_conf(flow_mass, parent)
    _set_player_conf(flow_mass, child)
    players = {parent.player_id: parent, child.player_id: child}
    child_config = PlayerConfig(
        values={}, provider=child_provider.instance_id, player_id=child.player_id
    )
    with (
        patch.object(
            flow_mass.players, "get_player", side_effect=lambda pid, *_a, **_k: players.get(pid)
        ),
        patch.object(flow_mass.config, "get_player_config", AsyncMock(return_value=child_config)),
    ):
        step = await flow_mass.config.setup_player(parent.player_id)
        assert step.type == FlowStepType.FORM
        assert step.step_id == "user"
        assert step.translation_owner == "provider.airplay"
        finish_step = await flow_mass.config.submit_setup_flow(step.flow_id, {"pin": "1234"})
    assert finish_step.type == FlowStepType.FINISH
    assert finish_step.result == {"player_id": child.player_id}


async def test_player_setup_without_flow_capable_children_aborts(
    flow_mass: MusicAssistant,
) -> None:
    """A wrapper player whose children offer no flow at all reports nothing to configure."""
    parent_provider = MockProvider("universal_player", instance_id="universal_player")
    child_provider = MockProvider("dlna", instance_id="dlna")
    parent = MockPlayer(parent_provider, "up_parent", "Study")
    child = MockPlayer(child_provider, "dlna_child", "Study DLNA", player_type=PlayerType.PROTOCOL)
    parent._cache["output_protocols"] = [_child_output_protocol(child)]
    _set_player_conf(flow_mass, parent)
    players = {parent.player_id: parent, child.player_id: child}
    with patch.object(
        flow_mass.players, "get_player", side_effect=lambda pid, *_a, **_k: players.get(pid)
    ):
        step = await flow_mass.config.setup_player(parent.player_id)
    assert step.type == FlowStepType.ABORT
    assert step.reason == "nothing_to_configure"


async def test_player_setup_reason_in_state_fingerprint() -> None:
    """setup_reason is exposed on the player and tracked in the state fingerprint."""
    provider = MockProvider("test_players", instance_id="test_players--1")
    player = MockPlayer(provider, "sr_player", "Reason Player")
    player._attr_needs_setup = True
    player._attr_setup_reason = "pairing_required"
    assert player.setup_reason == "pairing_required"
    # the reason is a tracked leaf of the state fingerprint (so it drives change events)
    player_state = player.state
    player_state.setup_reason = "pairing_required"
    assert _state_fingerprint(player_state)["setup_reason"] == "pairing_required"


async def test_has_setup_flow_serialized_for_own_flow() -> None:
    """A player implementing its own flow serializes has_setup_flow (regardless of needs_setup)."""
    provider = MockProvider("sendspin", instance_id="sendspin")
    plain = MockPlayer(provider, "plain_player", "Plain Player")
    player = _FlowPlayer(provider, "flow_player", "Flow Player")
    assert plain.has_setup_flow is False
    assert player.has_setup_flow is True
    # not gated on needs_setup: the flow stays re-runnable once setup completed
    assert player.needs_setup is False
    player.update_state(force_update=True)
    assert player.state.has_setup_flow is True
    assert player.to_dict()["has_setup_flow"] is True
    # tracked leaf of the fingerprint, so a late-binding protocol child drives an event
    assert _state_fingerprint(player.state)["has_setup_flow"] is True


async def test_has_setup_flow_serialized_for_protocol_child() -> None:
    """A wrapper player inherits has_setup_flow from a linked protocol child with a flow."""
    parent_provider = MockProvider("universal_player", instance_id="universal_player")
    child_provider = MockProvider("airplay", instance_id="airplay")
    parent = MockPlayer(parent_provider, "up_parent", "Hallway")
    child = _ProtocolChildPlayer(child_provider, "ap_child", "Hallway AirPlay")
    child._attr_needs_setup = False  # already set up, but its flow can be re-run
    players = {parent.player_id: parent, child.player_id: child}
    # link the child: set_linked_output_protocols survives the update_state cache flush
    parent.set_linked_output_protocols([_child_output_protocol(child)])
    with patch.object(
        parent.mass.players, "get_player", side_effect=lambda pid, *_a, **_k: players.get(pid)
    ):
        assert parent.has_setup_flow is True
        parent.update_state(force_update=True)
    assert parent.to_dict()["has_setup_flow"] is True
    assert _state_fingerprint(parent.state)["has_setup_flow"] is True


async def test_secure_values_never_echoed_on_step(flow_mass: MusicAssistant) -> None:
    """Submitted SECURE_STRING values are handed to the flow but never kept on the step."""
    received: dict[str, Any] = {}

    async def run_setup(session: SetupSession) -> None:
        received.update(await session.form([USERNAME_ENTRY, PASSWORD_ENTRY]))
        # keep the flow alive on a second form so the stored step can be inspected
        await session.form([USERNAME_ENTRY], step_id="second")
        await session.finish({})

    with _use_flow(flow_mass, run_setup):
        step = await flow_mass.config.setup_provider(FAKE_DOMAIN)
        # validation failure path: the password must not be echoed back
        error_step = await flow_mass.config.submit_setup_flow(step.flow_id, {"password": "sssh"})
        password_entry = next(x for x in error_step.entries if x.key == "password")
        assert password_entry.value is None
        # success path: the flow receives the value, the stored step does not keep it
        await flow_mass.config.submit_setup_flow(
            step.flow_id, {"username": "marcel", "password": "sssh"}
        )
        assert received["password"] == "sssh"
        await flow_mass.config.abort_setup_flow(step.flow_id)


# --- real provider setup flows driven through the engine (migrated plain-cred providers) ---


async def test_real_provider_flow_qobuz(flow_mass: MusicAssistant) -> None:
    """Qobuz's run_setup collects credentials and persists them as (encrypted) setup_data."""
    with (
        _use_flow(flow_mass, qobuz_run_setup),
        patch.object(flow_mass, "load_provider_config", AsyncMock()) as mock_load,
    ):
        step = await flow_mass.config.setup_provider(FAKE_DOMAIN)
        assert step.type == FlowStepType.FORM
        # only the setup credentials are on the form; the quality option stays behind
        assert {entry.key for entry in step.entries} == {"username", "password"}
        finish_step = await flow_mass.config.submit_setup_flow(
            step.flow_id, {"username": "marcel", "password": "hunter2"}
        )
    assert finish_step.type == FlowStepType.FINISH
    mock_load.assert_awaited_once()
    raw_conf = flow_mass.config.get(f"{CONF_PROVIDERS}/{FAKE_DOMAIN}")
    assert flow_mass.config.decrypt_string(raw_conf["setup_data"]["username"]) == "marcel"
    assert flow_mass.config.decrypt_string(raw_conf["setup_data"]["password"]) == "hunter2"


async def test_real_provider_flow_filesystem_local(flow_mass: MusicAssistant) -> None:
    """filesystem_local's run_setup collects the content type and path as setup_data."""
    with (
        _use_flow(flow_mass, filesystem_local_run_setup),
        patch.object(flow_mass, "load_provider_config", AsyncMock()) as mock_load,
    ):
        step = await flow_mass.config.setup_provider(FAKE_DOMAIN)
        assert step.type == FlowStepType.FORM
        assert {"content_type", "path"} <= {entry.key for entry in step.entries}
        finish_step = await flow_mass.config.submit_setup_flow(
            step.flow_id, {"content_type": "podcasts", "path": "/media/podcasts"}
        )
    assert finish_step.type == FlowStepType.FINISH
    mock_load.assert_awaited_once()
    raw_conf = flow_mass.config.get(f"{CONF_PROVIDERS}/{FAKE_DOMAIN}")
    assert flow_mass.config.decrypt_string(raw_conf["setup_data"]["content_type"]) == "podcasts"
    assert flow_mass.config.decrypt_string(raw_conf["setup_data"]["path"]) == "/media/podcasts"


async def test_real_provider_flow_retry_on_error(flow_mass: MusicAssistant) -> None:
    """A failing load makes the author's loop re-render the form with the error, then succeed."""
    load_mock = AsyncMock(side_effect=[LoginFailed("bad creds"), None])
    with (
        _use_flow(flow_mass, qobuz_run_setup),
        patch.object(flow_mass, "load_provider_config", load_mock),
    ):
        step = await flow_mass.config.setup_provider(FAKE_DOMAIN)
        retry_step = await flow_mass.config.submit_setup_flow(
            step.flow_id, {"username": "marcel", "password": "wrong"}
        )
        assert retry_step.type == FlowStepType.FORM
        # the error slug (LoginFailed.translation_key) is used so it localizes
        # at serialization; the raw message is the fallback for keyless errors
        assert retry_step.errors == {"base": "login_failed"}
        finish_step = await flow_mass.config.submit_setup_flow(
            step.flow_id, {"username": "marcel", "password": "right"}
        )
    assert finish_step.type == FlowStepType.FINISH
    assert flow_mass.config.get(f"{CONF_PROVIDERS}/{FAKE_DOMAIN}") is not None


async def test_spotify_flow_hosted_bounce_roundtrip(
    flow_mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real Spotify flow: hosted-bounce external auth then a stored refresh token."""
    from music_assistant.providers.spotify.constants import (  # noqa: PLC0415
        CONF_REFRESH_TOKEN_GLOBAL,
    )
    from music_assistant.providers.spotify.setup_flow import run_setup  # noqa: PLC0415

    monkeypatch.setattr(
        "music_assistant.providers.spotify.setup_flow.app_var", lambda _key: "ma_client_id"
    )
    # seed the lazy http_session backing field so the token exchange uses our stub
    monkeypatch.setattr(
        flow_mass, "_http_session", _fake_json_session({"refresh_token": "rt_global"})
    )
    with (
        _use_flow(flow_mass, run_setup),
        patch.object(flow_mass, "load_provider_config", AsyncMock()),
    ):
        step = await flow_mass.config.setup_provider(FAKE_DOMAIN)
        # first step is the (required) global authentication via the hosted bounce
        assert step.type == FlowStepType.EXTERNAL
        assert step.step_id == "authenticate"
        assert step.url is not None
        assert "accounts.spotify.com/authorize" in step.url
        # the fixed hosted redirect is used, with the local callback carried in `state`
        assert "music-assistant.io%2Fcallback" in step.url
        assert step.flow_id in step.url
        session = flow_mass.config._setup_flows[step.flow_id].session
        await _fire_callback(flow_mass, step.flow_id, "code=auth_code&state=xyz")
        # after the token exchange the optional developer step is shown
        await _wait_for(
            lambda: session.current_step is not None and session.current_step.step_id == "developer"
        )
        # skipping the developer client id (blank) finishes the flow
        finish_step = await flow_mass.config.submit_setup_flow(step.flow_id, {})
    assert finish_step.type == FlowStepType.FINISH
    raw_conf = flow_mass.config.get(f"{CONF_PROVIDERS}/{FAKE_DOMAIN}")
    assert (
        flow_mass.config.decrypt_string(raw_conf["setup_data"][CONF_REFRESH_TOKEN_GLOBAL])
        == "rt_global"
    )


async def test_gdrive_flow_form_then_hosted_bounce(
    flow_mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real Google Drive flow: credential form, hosted-bounce OAuth, stored refresh token."""
    from music_assistant.providers.filesystem_cloud.base import (  # noqa: PLC0415
        CONF_CLIENT_ID,
        CONF_CLIENT_SECRET,
        CONF_FOLDER_ID,
        CONF_REFRESH_TOKEN,
    )
    from music_assistant.providers.filesystem_google_drive.setup_flow import (  # noqa: PLC0415
        run_setup,
    )

    # seed the lazy http_session backing field so the token exchange uses our stub
    monkeypatch.setattr(
        flow_mass, "_http_session", _fake_json_session({"refresh_token": "rt_drive"})
    )
    with (
        _use_flow(flow_mass, run_setup),
        patch.object(flow_mass, "load_provider_config", AsyncMock()),
    ):
        step = await flow_mass.config.setup_provider(FAKE_DOMAIN)
        # the shared cloud flow first collects content type + OAuth client credentials + folder
        assert step.type == FlowStepType.FORM
        assert step.step_id == "user"
        session = flow_mass.config._setup_flows[step.flow_id].session
        external_step = await flow_mass.config.submit_setup_flow(
            step.flow_id,
            {
                "content_type": "music",
                CONF_CLIENT_ID: "cid",
                CONF_CLIENT_SECRET: "secret",
                CONF_FOLDER_ID: "root",
            },
        )
        assert external_step.type == FlowStepType.EXTERNAL
        assert external_step.url is not None
        auth_url = urlsplit(external_step.url)
        assert auth_url.netloc == "accounts.google.com"
        assert parse_qs(auth_url.query)["redirect_uri"] == ["https://music-assistant.io/callback"]
        await _fire_callback(flow_mass, step.flow_id, "code=auth_code")
        await _wait_for(lambda: session.finished)
    raw_conf = flow_mass.config.get(f"{CONF_PROVIDERS}/{FAKE_DOMAIN}")
    setup_data = raw_conf["setup_data"]
    assert flow_mass.config.decrypt_string(setup_data[CONF_REFRESH_TOKEN]) == "rt_drive"
    assert flow_mass.config.decrypt_string(setup_data[CONF_CLIENT_ID]) == "cid"
    assert flow_mass.config.decrypt_string(setup_data[CONF_FOLDER_ID]) == "root"


async def test_tidal_flow_pkce_url_paste(
    flow_mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real Tidal flow: paste the redirect URL, exchange it, store the tokens."""
    from music_assistant.providers.tidal.auth_manager import TidalAuthManager  # noqa: PLC0415
    from music_assistant.providers.tidal.constants import (  # noqa: PLC0415
        CONF_AUTH_TOKEN,
        CONF_OOPS_URL,
        CONF_REFRESH_TOKEN,
        CONF_USER_ID,
    )
    from music_assistant.providers.tidal.setup_flow import run_setup  # noqa: PLC0415

    monkeypatch.setattr(flow_mass, "_http_session", MagicMock())
    auth_data = {
        "access_token": "at-123",
        "refresh_token": "rt-456",
        "expires_at": 4102444800.0,
        "userId": 42,
    }
    with (
        _use_flow(flow_mass, run_setup),
        patch.object(
            TidalAuthManager,
            "build_pkce_login",
            return_value=("https://login.tidal.com/authorize?x=1", {"code_verifier": "v"}),
        ),
        patch.object(
            TidalAuthManager, "process_pkce_login", AsyncMock(return_value=auth_data)
        ) as mock_exchange,
        patch.object(flow_mass, "load_provider_config", AsyncMock()),
    ):
        step = await flow_mass.config.setup_provider(FAKE_DOMAIN)
        assert step.type == FlowStepType.FORM
        assert step.step_id == "user"
        # the authorize link rides along on the instructions label
        instructions = next(x for x in step.entries if x.key == "auth_instructions")
        assert instructions.help_link == "https://login.tidal.com/authorize?x=1"
        finish_step = await flow_mass.config.submit_setup_flow(
            step.flow_id,
            {CONF_OOPS_URL: "https://tidal.com/android/login/auth?code=abc"},
        )
    assert finish_step.type == FlowStepType.FINISH
    # the pasted redirect URL was handed to the token exchange
    assert mock_exchange.await_args is not None
    assert mock_exchange.await_args.args[2] == "https://tidal.com/android/login/auth?code=abc"
    raw_conf = flow_mass.config.get(f"{CONF_PROVIDERS}/{FAKE_DOMAIN}")
    setup_data = raw_conf["setup_data"]
    assert flow_mass.config.decrypt_string(setup_data[CONF_AUTH_TOKEN]) == "at-123"
    assert flow_mass.config.decrypt_string(setup_data[CONF_REFRESH_TOKEN]) == "rt-456"
    assert setup_data["expiry_time"] == 4102444800.0
    assert flow_mass.config.decrypt_string(setup_data[CONF_USER_ID]) == "42"


async def test_tidal_flow_exchange_error_retries(
    flow_mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed token exchange re-renders the form with an error, then succeeds on retry."""
    from music_assistant_models.errors import LoginFailed  # noqa: PLC0415

    from music_assistant.providers.tidal.auth_manager import TidalAuthManager  # noqa: PLC0415
    from music_assistant.providers.tidal.constants import CONF_OOPS_URL  # noqa: PLC0415
    from music_assistant.providers.tidal.setup_flow import run_setup  # noqa: PLC0415

    monkeypatch.setattr(flow_mass, "_http_session", MagicMock())
    exchange = AsyncMock(
        side_effect=[
            LoginFailed("No authorization code found in redirect URL"),
            {
                "access_token": "at",
                "refresh_token": "rt",
                "expires_at": 1.0,
                "userId": "u",
            },
        ]
    )
    with (
        _use_flow(flow_mass, run_setup),
        patch.object(
            TidalAuthManager, "build_pkce_login", return_value=("https://login.tidal.com/x", {})
        ),
        patch.object(TidalAuthManager, "process_pkce_login", exchange),
        patch.object(flow_mass, "load_provider_config", AsyncMock()),
    ):
        step = await flow_mass.config.setup_provider(FAKE_DOMAIN)
        retry_step = await flow_mass.config.submit_setup_flow(
            step.flow_id, {CONF_OOPS_URL: "https://tidal.com/android/login/auth"}
        )
        assert retry_step.type == FlowStepType.FORM
        # the canonical retry pattern surfaces the error's translation key
        assert retry_step.errors == {"base": "login_failed"}
        finish_step = await flow_mass.config.submit_setup_flow(
            step.flow_id, {CONF_OOPS_URL: "https://tidal.com/android/login/auth?code=abc"}
        )
    assert finish_step.type == FlowStepType.FINISH


async def test_hue_pairing_flow_retry_then_success(
    flow_mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real Hue flow: a missed button press re-forms, then pairing succeeds."""
    from music_assistant.providers.hue_entertainment.constants import (  # noqa: PLC0415
        CONF_BRIDGE_HOST,
        CONF_BRIDGE_ID,
        CONF_CLIENTKEY,
        CONF_USERNAME,
    )
    from music_assistant.providers.hue_entertainment.setup_flow import run_setup  # noqa: PLC0415

    api_instance = MagicMock()
    # first pair attempt times out (button not pressed), second succeeds
    api_instance.pair = AsyncMock(
        side_effect=[TimeoutError("not pressed"), {"username": "abc", "clientkey": "def"}]
    )
    api_instance.get_bridge_id = AsyncMock(return_value="bridge-1")
    api_instance.close = AsyncMock()
    monkeypatch.setattr(
        "music_assistant.providers.hue_entertainment.setup_flow.HueEntertainmentAPI",
        MagicMock(return_value=api_instance),
    )
    with (
        _use_flow(flow_mass, run_setup),
        patch.object(flow_mass, "load_provider_config", AsyncMock()),
    ):
        step = await flow_mass.config.setup_provider(FAKE_DOMAIN)
        assert step.type == FlowStepType.FORM
        assert step.step_id == "user"
        session = flow_mass.config._setup_flows[step.flow_id].session
        await flow_mass.config.submit_setup_flow(step.flow_id, {CONF_BRIDGE_HOST: "1.2.3.4"})
        # the missed button press loops back to the user form with an error
        retry = await _wait_for(
            lambda: (
                session.current_step
                if session.current_step
                and session.current_step.step_id == "user"
                and session.current_step.errors
                else None
            )
        )
        assert retry.errors == {"base": "button_not_pressed"}
        await flow_mass.config.submit_setup_flow(step.flow_id, {CONF_BRIDGE_HOST: "1.2.3.4"})
        await _wait_for(lambda: session.finished)
    setup_data = flow_mass.config.get(f"{CONF_PROVIDERS}/{FAKE_DOMAIN}")["setup_data"]
    assert flow_mass.config.decrypt_string(setup_data[CONF_USERNAME]) == "abc"
    assert flow_mass.config.decrypt_string(setup_data[CONF_CLIENTKEY]) == "def"
    assert flow_mass.config.decrypt_string(setup_data[CONF_BRIDGE_HOST]) == "1.2.3.4"
    assert flow_mass.config.decrypt_string(setup_data[CONF_BRIDGE_ID]) == "bridge-1"


async def test_netease_qr_flow_expiry_then_confirm(
    flow_mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real NetEase QR flow: the first shown QR expires and is refreshed, then confirmed."""
    from music_assistant.models.setup_flow import StepExpiredError  # noqa: PLC0415
    from music_assistant.providers.neteasecloudmusic import setup_flow as nc_flow  # noqa: PLC0415
    from music_assistant.providers.neteasecloudmusic.constants import (  # noqa: PLC0415
        CONF_API_BASE_URL,
        CONF_COOKIE,
        CONF_UID,
    )

    monkeypatch.setattr(flow_mass, "_http_session", MagicMock())
    monkeypatch.setattr(nc_flow, "NcmApiClient", MagicMock())
    # a fresh QR is minted for the initial show and again after the first one expires
    create_qr = AsyncMock(
        side_effect=[("key1", "data:image/png;base64,AAA"), ("key2", "data:image/png;base64,BBB")]
    )
    monkeypatch.setattr(nc_flow, "_create_qr", create_qr)
    # first poll hits the expiry deadline, the second (refreshed) QR is confirmed
    monkeypatch.setattr(
        nc_flow,
        "_poll_qr_login",
        AsyncMock(side_effect=[StepExpiredError(), ("cookie-xyz", "42")]),
    )
    with (
        _use_flow(flow_mass, nc_flow.run_setup),
        patch.object(flow_mass, "load_provider_config", AsyncMock()),
    ):
        step = await flow_mass.config.setup_provider(FAKE_DOMAIN)
        assert step.type == FlowStepType.FORM
        assert step.step_id == "user"
        session = flow_mass.config._setup_flows[step.flow_id].session
        await flow_mass.config.submit_setup_flow(
            step.flow_id, {CONF_API_BASE_URL: "http://127.0.0.1:3000"}
        )
        await _wait_for(lambda: session.finished)
    # the QR was minted twice (initial + one in-place refresh after expiry)
    assert create_qr.await_count == 2
    setup_data = flow_mass.config.get(f"{CONF_PROVIDERS}/{FAKE_DOMAIN}")["setup_data"]
    assert flow_mass.config.decrypt_string(setup_data[CONF_COOKIE]) == "cookie-xyz"
    assert flow_mass.config.decrypt_string(setup_data[CONF_UID]) == "42"
    assert flow_mass.config.decrypt_string(setup_data[CONF_API_BASE_URL]) == "http://127.0.0.1:3000"
