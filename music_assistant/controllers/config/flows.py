"""Setup flow engine: registry and API commands for interactive provider/player setup."""

from __future__ import annotations

import asyncio
import importlib
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from music_assistant_models.auth import Scope
from music_assistant_models.enums import EventType, FlowStepType
from music_assistant_models.errors import (
    ActionUnavailable,
    InsufficientPermissions,
    SetupFailedError,
)
from music_assistant_models.setup_flow import SetupFlowStep

from music_assistant.constants import CONF_PLAYERS, CONF_PROVIDERS
from music_assistant.controllers.config.helpers import _AUTH_ERROR_CODES
from music_assistant.helpers.api import api_command
from music_assistant.helpers.util import load_provider_module
from music_assistant.models.player import Player
from music_assistant.models.setup_flow import (
    AbortFlow,
    FlowReason,
    SetupFlowContext,
    SetupFlowError,
    SetupSession,
    StepExpiredError,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant import MusicAssistant

LOGGER = logging.getLogger(__name__)

# a flow with no client interaction or step changes for this long is garbage collected
IDLE_FLOW_TTL = 15 * 60
FLOW_SWEEP_INTERVAL = 60
# how long start/submit wait for the flow coroutine to produce the (next) step;
# generous because finish() may install requirements and load the provider
NEXT_STEP_TIMEOUT = 120


@dataclass
class ActiveSetupFlow:
    """Registry record for a running setup flow."""

    session: SetupSession
    # target_key: identifies what the flow is (re)configuring; one flow per target
    target_key: str
    # required_scope: the scope the starting command required; re-checked on
    # every flows/* continuation command
    required_scope: Scope
    task: asyncio.Task[None] | None = None


class SetupFlowMixin:
    """Mixin providing the setup flow engine for the ConfigController."""

    # registry of running flows, keyed by flow_id (lazily created per instance)
    _flows: dict[str, ActiveSetupFlow] | None = None
    _flow_sweep_handle: asyncio.TimerHandle | None = None

    # Type hints for attributes/methods provided by the class this mixin is used with
    if TYPE_CHECKING:
        mass: MusicAssistant

        @property
        def onboard_done(self) -> bool: ...  # noqa: D102

        def get(self, key: str, default: Any = None) -> Any: ...  # noqa: D102

        def set(self, key: str, value: Any, immediate: bool = False) -> None: ...  # noqa: D102

        def encrypt_string(self, str_value: str) -> str: ...  # noqa: D102

        def decrypt_string(self, encrypted_str: str) -> str: ...  # noqa: D102

        async def get_provider_configs(  # noqa: D102
            self, provider_type: Any = None, provider_domain: str | None = None
        ) -> list[ProviderConfig]: ...

        async def get_provider_config(self, instance_id: str) -> ProviderConfig: ...  # noqa: D102

        def update_provider_last_error(self, instance_id: str, error: Any) -> None: ...  # noqa: D102

        async def get_player_config(self, player_id: str) -> Any: ...  # noqa: D102

        async def _create_provider_instance(
            self,
            provider_domain: str,
            values: dict[str, ConfigValueType],
            setup_data: dict[str, Any] | None = None,
        ) -> ProviderConfig: ...

    @api_command("config/providers/setup", required_scope=Scope.CONFIG_PROVIDERS_WRITE)
    async def setup_provider(self, provider_domain: str) -> SetupFlowStep:
        """
        Start the setup flow to add a new instance of the given provider.

        For a provider without a setup flow (no user input needed) the instance is
        created right away and a FINISH step is returned, so the add-provider path
        is uniform for all providers.

        :param provider_domain: Domain of the provider to add an instance of.
        """
        for manifest in self.mass.get_provider_manifests():
            if manifest.domain == provider_domain:
                break
        else:
            msg = f"Unknown provider domain: {provider_domain}"
            raise KeyError(msg)
        owner = f"provider.{provider_domain}"
        # fail fast on conditions that would otherwise only surface at save
        existing = await self.get_provider_configs(provider_domain=provider_domain)
        if existing and not manifest.multi_instance:
            return self._synthesized_step(FlowStepType.ABORT, owner, reason="already_configured")
        if manifest.depends_on:
            dep_configs = await self.get_provider_configs(provider_domain=manifest.depends_on)
            if not any(dep_conf.enabled for dep_conf in dep_configs):
                return self._synthesized_step(
                    FlowStepType.ABORT, owner, reason="missing_dependency"
                )
        flow_module = await self._get_setup_flow_module(manifest)
        if flow_module is None:
            # zero-input provider: create the instance immediately and report it
            # as an (already) finished flow
            config = await self._create_provider_instance(provider_domain, {})
            return self._synthesized_step(
                FlowStepType.FINISH, owner, result={"instance_id": config.instance_id}
            )
        context = SetupFlowContext(kind="setup", reason="user", domain=provider_domain)
        return await self._start_flow(
            flow_coro=flow_module.run_setup,
            context=context,
            target_key=f"provider_setup:{provider_domain}",
            required_scope=Scope.CONFIG_PROVIDERS_WRITE,
            finish_handler=self._finish_provider_setup,
        )

    @api_command("config/providers/reconfigure", required_scope=Scope.CONFIG_PROVIDERS_WRITE)
    async def reconfigure_provider(self, instance_id: str) -> SetupFlowStep:
        """
        Start the reconfigure flow on an existing provider instance (covers reauth).

        :param instance_id: The provider instance to reconfigure.
        """
        raw_conf = self.get(f"{CONF_PROVIDERS}/{instance_id}")
        if not raw_conf:
            msg = f"No config found for provider id {instance_id}"
            raise KeyError(msg)
        domain: str = raw_conf["domain"]
        manifest = self.mass.get_provider_manifest(domain)
        owner = f"provider.{domain}"
        flow_module = await self._get_setup_flow_module(manifest)
        if flow_module is None:
            # flow-less providers have nothing to reconfigure;
            # their failures are environmental (reload/retry covers them)
            return self._synthesized_step(FlowStepType.ABORT, owner, reason="nothing_to_configure")
        context = SetupFlowContext(
            kind="reconfigure",
            reason=self._reconfigure_reason(raw_conf.get("last_error")),
            domain=domain,
            instance_id=instance_id,
            setup_data=self._decrypt_values(raw_conf.get("setup_data") or {}),
            values=self._decrypt_values(raw_conf.get("values") or {}),
        )
        return await self._start_flow(
            flow_coro=flow_module.run_setup,
            context=context,
            target_key=f"provider_reconfigure:{instance_id}",
            required_scope=Scope.CONFIG_PROVIDERS_WRITE,
            finish_handler=self._finish_provider_reconfigure,
        )

    @api_command("config/players/setup", required_scope=Scope.CONFIG_PLAYERS_WRITE)
    async def setup_player(self, player_id: str) -> SetupFlowStep:
        """
        Start the setup flow for a player (e.g. pairing).

        :param player_id: The player to set up.
        """
        # deliberately no raise_unavailable: a player that needs setup is serialized
        # as unavailable, and that is exactly the player this command targets
        player = self.mass.players.get_player(player_id)
        if player is None:
            msg = f"Player {player_id} not found"
            raise KeyError(msg)
        owner = f"provider.{player.provider.domain}"
        if type(player).run_setup_flow is Player.run_setup_flow:
            # the player (provider) implements no setup flow
            return self._synthesized_step(FlowStepType.ABORT, owner, reason="nothing_to_configure")
        raw_conf = self.get(f"{CONF_PLAYERS}/{player_id}") or {}
        context = SetupFlowContext(
            kind="setup",
            reason="user",
            domain=player.provider.domain,
            instance_id=player.provider.instance_id,
            player_id=player_id,
            setup_data=self._decrypt_values(raw_conf.get("setup_data") or {}),
            values=self._decrypt_values(raw_conf.get("values") or {}),
        )
        return await self._start_flow(
            flow_coro=player.run_setup_flow,
            context=context,
            target_key=f"player_setup:{player_id}",
            required_scope=Scope.CONFIG_PLAYERS_WRITE,
            finish_handler=self._finish_player_setup,
        )

    @api_command("config/flows/submit")
    async def submit_setup_flow(
        self, flow_id: str, values: dict[str, ConfigValueType]
    ) -> SetupFlowStep:
        """
        Submit the user's values for the flow's pending FORM step.

        Returns the flow's next step, or the same FORM step (with per-field errors
        set) when validation failed.

        :param flow_id: The id of the running flow.
        :param values: The raw values for the form's config entries.
        """
        flow = self._get_flow(flow_id)
        self._check_flow_permission(flow)
        if (error_step := flow.session.handle_submit(values)) is not None:
            return error_step
        # wait (bounded) for the coroutine to produce the next step; on the rare
        # timeout the stored step is returned as-is and the client picks up the real
        # next step from the SETUP_FLOW_UPDATED push event
        await flow.session.wait_for_step_change(NEXT_STEP_TIMEOUT)
        step = flow.session.current_step
        assert step is not None  # an accepted submit implies a published FORM step
        return step

    @api_command("config/flows/get")
    async def get_setup_flow(self, flow_id: str) -> SetupFlowStep:
        """
        Return the current step of a running flow (idempotent re-render, never advances).

        :param flow_id: The id of the running flow.
        """
        flow = self._get_flow(flow_id)
        self._check_flow_permission(flow)
        flow.session.last_activity = time.monotonic()
        if (step := flow.session.current_step) is None:
            raise ActionUnavailable("The setup flow has not produced a step yet")
        return step

    @api_command("config/flows/abort")
    async def abort_setup_flow(self, flow_id: str) -> None:
        """
        Abort a running flow (user cancelled).

        :param flow_id: The id of the running flow.
        """
        flow = self._get_flow(flow_id)
        self._check_flow_permission(flow)
        await self._abort_flow(flow, reason="aborted")

    async def _start_flow(
        self,
        *,
        flow_coro: Callable[[SetupSession], Awaitable[Any]],
        context: SetupFlowContext,
        target_key: str,
        required_scope: Scope,
        finish_handler: Callable[
            [SetupSession, dict[str, ConfigValueType]], Awaitable[dict[str, str]]
        ],
    ) -> SetupFlowStep:
        """Register and start a new flow, returning its first published step."""
        # one flow per target: starting anew replaces (aborts) a lingering previous flow
        for existing_flow in list(self._setup_flows.values()):
            if existing_flow.target_key == target_key:
                await self._abort_flow(existing_flow, reason="replaced")
        flow_id = uuid4().hex
        session = SetupSession(self.mass, flow_id, context, finish_handler)
        flow = ActiveSetupFlow(
            session=session, target_key=target_key, required_scope=required_scope
        )
        self._setup_flows[flow_id] = flow
        flow.task = self.mass.create_task(self._run_flow(flow, flow_coro))
        self._schedule_flow_sweep()
        if session.current_step is None:
            await session.wait_for_step_change(NEXT_STEP_TIMEOUT)
        if (step := session.current_step) is None:
            await self._abort_flow(flow, reason="internal_error")
            raise SetupFailedError("Setup flow did not produce a first step")
        return step

    async def _run_flow(
        self, flow: ActiveSetupFlow, flow_coro: Callable[[SetupSession], Awaitable[Any]]
    ) -> None:
        """Drive the flow coroutine and convert its outcome into a terminal step."""
        session = flow.session
        try:
            await flow_coro(session)
        except AbortFlow as err:
            session.publish_abort(err.reason)
        except StepExpiredError:
            session.publish_abort("timed_out")
        except SetupFlowError as err:
            # the author did not catch a finish failure: end with the failure message
            session.publish_abort(str(err) or "internal_error")
        except asyncio.CancelledError:
            # abort/replace/shutdown: the author's cleanup (finally blocks) has run;
            # the canceller publishes the ABORT step. Never swallow the cancellation.
            raise
        except Exception:
            LOGGER.exception("Unhandled error in setup flow for %s", session.context.domain)
            session.publish_abort("internal_error")
        else:
            if not session.finished:
                LOGGER.error(
                    "Setup flow for %s returned without calling finish()", session.context.domain
                )
                session.publish_abort("internal_error")
        finally:
            session.close()
            self._setup_flows.pop(session.flow_id, None)

    async def _abort_flow(self, flow: ActiveSetupFlow, reason: str) -> None:
        """
        Abort the given flow with the given reason and clean it up.

        Cancelling raises CancelledError inside the flow coroutine so the author's
        cleanup (try/finally around pairing sessions etc.) runs before the terminal
        ABORT step goes out.
        """
        if flow.task is not None and not flow.task.done():
            flow.task.cancel()
            # wait() shields us from the task's CancelledError without
            # masking a cancellation of the caller itself
            await asyncio.wait([flow.task])
        self._setup_flows.pop(flow.session.flow_id, None)
        current_step = flow.session.current_step
        if current_step is None or current_step.type not in (
            FlowStepType.FINISH,
            FlowStepType.ABORT,
        ):
            flow.session.publish_abort(reason)

    async def _finish_provider_setup(
        self, session: SetupSession, values: dict[str, ConfigValueType]
    ) -> dict[str, str]:
        """Finish handler for provider setup flows: create, persist and load the instance."""
        try:
            config = await self._create_provider_instance(
                session.context.domain, {}, setup_data=self._encrypt_values(values)
            )
        except Exception as err:
            raise SetupFlowError(
                str(err) or err.__class__.__name__,
                translation_key=getattr(err, "translation_key", None),
            ) from err
        return {"instance_id": config.instance_id}

    async def _finish_provider_reconfigure(
        self, session: SetupSession, values: dict[str, ConfigValueType]
    ) -> dict[str, str]:
        """Finish handler for provider reconfigure flows: merge setup_data and reload."""
        instance_id = session.context.instance_id
        assert instance_id is not None  # always set for reconfigure flows
        conf_key = f"{CONF_PROVIDERS}/{instance_id}"
        raw_conf = self.get(conf_key)
        if not raw_conf:
            raise SetupFlowError(f"Provider {instance_id} no longer exists")
        snapshot = dict(raw_conf.get("setup_data") or {})
        self.set(f"{conf_key}/setup_data", {**snapshot, **self._encrypt_values(values)})
        try:
            config = await self.get_provider_config(instance_id)
            await self.mass.load_provider_config(config)
        except asyncio.CancelledError:
            self.set(f"{conf_key}/setup_data", snapshot)
            raise
        except Exception as err:
            # reloading with the new values failed: restore the previous setup_data
            self.set(f"{conf_key}/setup_data", snapshot)
            raise SetupFlowError(
                str(err) or err.__class__.__name__,
                translation_key=getattr(err, "translation_key", None),
            ) from err
        self.update_provider_last_error(instance_id, None)
        return {"instance_id": instance_id}

    async def _finish_player_setup(
        self, session: SetupSession, values: dict[str, ConfigValueType]
    ) -> dict[str, str]:
        """Finish handler for player setup flows: persist setup_data on the player config."""
        player_id = session.context.player_id
        assert player_id is not None  # always set for player flows
        conf_key = f"{CONF_PLAYERS}/{player_id}"
        raw_conf = self.get(conf_key)
        if not raw_conf:
            raise SetupFlowError(f"No config found for player {player_id}")
        existing_setup_data = dict(raw_conf.get("setup_data") or {})
        self.set(f"{conf_key}/setup_data", {**existing_setup_data, **self._encrypt_values(values)})
        config = await self.get_player_config(player_id)
        self.mass.signal_event(EventType.PLAYER_CONFIG_UPDATED, object_id=player_id, data=config)
        return {"player_id": player_id}

    async def _get_setup_flow_module(self, manifest: ProviderManifest) -> Any | None:
        """
        Import (lazily) the provider's setup_flow module, or None when it has none.

        A provider without a setup_flow module needs no setup input at all.
        """
        # ensure the provider's requirements are installed and its package imports
        # cleanly first: the setup_flow submodule may rely on those requirements
        await load_provider_module(manifest.domain, manifest.requirements)
        module_path = f"music_assistant.providers.{manifest.domain}.setup_flow"
        try:
            return await asyncio.to_thread(importlib.import_module, module_path)
        except ModuleNotFoundError as err:
            if err.name == module_path:
                # the provider ships no setup_flow module: it needs no setup input
                return None
            # an import *inside* setup_flow.py failed: an actual bug, surface it
            raise

    @property
    def _setup_flows(self) -> dict[str, ActiveSetupFlow]:
        """Return the registry of running flows (created lazily)."""
        if self._flows is None:
            self._flows = {}
        return self._flows

    def _get_flow(self, flow_id: str) -> ActiveSetupFlow:
        """Return the running flow for the given id."""
        if flow := self._setup_flows.get(flow_id):
            return flow
        msg = f"Unknown (or finished) setup flow: {flow_id}"
        raise KeyError(msg)

    def _check_flow_permission(self, flow: ActiveSetupFlow) -> None:
        """Verify the calling user holds the scope the flow's start command required."""
        # imported here: the webserver helpers pull in the full auth stack,
        # which must not be imported with the config controller at startup
        from music_assistant.controllers.webserver.helpers.auth_middleware import (  # noqa: PLC0415
            get_current_user,
            has_scope,
        )

        user = get_current_user()
        # no user context means an internal (server-side) caller, which is trusted
        if user is not None and not has_scope(user, flow.required_scope):
            raise InsufficientPermissions(
                f"This action requires the {flow.required_scope.value} scope"
            )

    def _synthesized_step(
        self,
        step_type: FlowStepType,
        translation_owner: str,
        *,
        result: dict[str, str] | None = None,
        reason: str | None = None,
    ) -> SetupFlowStep:
        """Return a terminal step for a flow that ended before a session was needed."""
        return SetupFlowStep(
            flow_id=uuid4().hex,
            step_id="finish" if step_type == FlowStepType.FINISH else "abort",
            type=step_type,
            result=result,
            reason=reason,
            translation_owner=translation_owner,
        )

    def _reconfigure_reason(self, last_error: Any) -> FlowReason:
        """Derive the reconfigure flow reason from the provider's stored last_error."""
        if not last_error:
            return "user"
        # legacy settings may still hold a plain string last_error
        error_code = last_error.get("error_code") if isinstance(last_error, dict) else None
        if error_code in _AUTH_ERROR_CODES:
            return "auth"
        return "error"

    def _encrypt_values(self, values: dict[str, ConfigValueType]) -> dict[str, Any]:
        """Return a copy of the values with all string values encrypted (at-rest form)."""
        return {
            key: self.encrypt_string(value) if isinstance(value, str) else value
            for key, value in values.items()
        }

    def _decrypt_values(self, values: dict[str, Any]) -> dict[str, Any]:
        """Return a copy of the values with all (encrypted) string values decrypted."""
        return {
            key: self.decrypt_string(value) if isinstance(value, str) else value
            for key, value in values.items()
        }

    def _schedule_flow_sweep(self) -> None:
        """Arm the periodic idle-flow sweeper (idempotent)."""
        if self._flow_sweep_handle is not None:
            return
        self._flow_sweep_handle = self.mass.loop.call_later(
            FLOW_SWEEP_INTERVAL, self._sweep_idle_flows
        )

    def _sweep_idle_flows(self) -> None:
        """Abort flows that have been idle for longer than the TTL."""
        self._flow_sweep_handle = None
        if self.mass.closing:
            return
        now = time.monotonic()
        for flow in list(self._setup_flows.values()):
            if now - flow.session.last_activity >= IDLE_FLOW_TTL:
                self.mass.create_task(self._abort_flow(flow, "timed_out"))
        if self._setup_flows:
            self._schedule_flow_sweep()
