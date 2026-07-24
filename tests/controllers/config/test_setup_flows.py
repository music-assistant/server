"""Tests for the setup flow engine (SetupFlowMixin + SetupSession)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import make_mocked_request
from music_assistant_models.config_entries import ConfigEntry, PlayerConfig
from music_assistant_models.enums import ConfigEntryType, EventType, FlowStepType, ProviderType
from music_assistant_models.errors import ActionUnavailable, LoginFailed, PlayerUnavailableError
from music_assistant_models.provider import ProviderManifest

from music_assistant.constants import CONF_PLAYERS, CONF_PROVIDERS, ENCRYPT_SUFFIX
from music_assistant.mass import MusicAssistant
from music_assistant.models.player import Player
from music_assistant.models.setup_flow import AbortFlow, SetupSession, StepExpiredError
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
    """A player flow persists (encrypted) setup_data on the player config."""
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
    config_event = await _wait_for(lambda: next(iter(events), None))
    assert config_event.object_id == player_id


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
