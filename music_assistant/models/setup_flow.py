"""
Session model and exceptions for interactive setup flows.

A setup flow is the interactive, multi-step process (credentials, OAuth, pairing, ...)
that creates or reconfigures a provider or player. A flow is authored as one plain
coroutine that awaits the user through a SetupSession:

    # music_assistant/providers/<domain>/setup_flow.py
    async def run_setup(session: SetupSession) -> None:
        values = await session.form([...])
        await session.finish(values)

The engine (config controller's SetupFlowMixin) owns the session lifecycle: it stores
the current step, pushes SETUP_FLOW_UPDATED events, feeds submitted values/callback
params back into the awaiting coroutine and persists the collected values on finish.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal, TypeVar

from aiohttp.web import Request, Response
from music_assistant_models.config_entries import UI_ONLY, ConfigEntry, ConfigValueType
from music_assistant_models.enums import ConfigEntryType, EventType, FlowStepType
from music_assistant_models.errors import ActionUnavailable
from music_assistant_models.setup_flow import SetupFlowStep

from music_assistant.helpers.json import json_loads

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from music_assistant.mass import MusicAssistant

LOGGER = logging.getLogger(__name__)

_T = TypeVar("_T")

FlowKind = Literal["setup", "reconfigure"]
FlowReason = Literal["user", "auth", "error"]

CALLBACK_RESPONSE_HTML = """
<html>
<body onload="window.close();">
    Authentication completed, you may now close this window.
</body>
</html>
"""


class SetupFlowError(Exception):
    """
    Error raised into the flow coroutine when session.finish() failed.

    The author may catch it to re-render a form with the error message and retry,
    or let it propagate so the engine aborts the flow with the failure message.
    """

    def __init__(self, message: str, translation_key: str | None = None) -> None:
        """
        Initialize the error.

        :param message: Human readable (English) description of the failure.
        :param translation_key: Optional (bare) translation slug of the underlying error,
            usable by flow authors as a localizable form error.
        """
        super().__init__(message)
        self.translation_key = translation_key


class StepExpiredError(Exception):
    """
    Raised into the flow coroutine when the active step's deadline passed.

    The author may catch it to refresh the step (e.g. a new QR code or pairing
    session) or let it propagate so the engine aborts the flow as timed out.
    """


class AbortFlow(Exception):
    """
    Raise anywhere inside a flow coroutine to cleanly abort the flow.

    :param reason: Slug describing why the flow was aborted; resolved from the
        translations (setup_flow.abort.<reason>) when the ABORT step is served.
    """

    def __init__(self, reason: str = "aborted") -> None:
        """Initialize with the abort reason slug."""
        super().__init__(reason)
        self.reason = reason


@dataclass(kw_only=True)
class SetupFlowContext:
    """Static context describing what a running setup flow is (re)configuring."""

    # kind: whether this flow sets up a new target or reconfigures an existing one
    kind: FlowKind
    # reason: what triggered the flow (user initiated, auth failure or another error)
    reason: FlowReason
    # domain: the provider domain the flow belongs to (for players: the player's provider)
    domain: str
    # instance_id: the existing provider instance (reconfigure/player flows)
    instance_id: str | None = None
    # player_id: the player being set up (player flows only)
    player_id: str | None = None
    # setup_data: decrypted values collected by an earlier flow run, for prefill
    setup_data: dict[str, Any] = field(default_factory=dict)
    # values: the target's existing (options) config values, for prefill
    values: dict[str, ConfigValueType] = field(default_factory=dict)


class SetupSession:
    """
    Coroutine-facing session handle for a running setup flow.

    Handed to the flow coroutine (``run_setup(session)``); the form/external/progress
    methods each publish a step to the client(s) and - where applicable - suspend the
    coroutine until the engine feeds the user's response back in.
    """

    def __init__(
        self,
        mass: MusicAssistant,
        flow_id: str,
        context: SetupFlowContext,
        finish_handler: Callable[
            [SetupSession, dict[str, ConfigValueType]], Awaitable[dict[str, str]]
        ],
    ) -> None:
        """
        Initialize the session (done by the flow engine, never by flow authors).

        :param mass: The MusicAssistant instance.
        :param flow_id: Unique id of this flow.
        :param context: Static context describing the flow's target.
        :param finish_handler: Engine callback that persists the collected values
            and creates/reloads the target when the flow finishes.
        """
        self.mass = mass
        self.flow_id = flow_id
        self.context = context
        self.current_step: SetupFlowStep | None = None
        self.finished = False
        self.last_activity = time.monotonic()
        self._finish_handler = finish_handler
        self._translation_owner = f"provider.{context.domain}"
        self._callback_path = f"/setup_flow/callback/{flow_id}"
        self._input_future: asyncio.Future[dict[str, ConfigValueType]] | None = None
        self._callback_future: asyncio.Future[dict[str, str]] | None = None
        self._step_changed = asyncio.Event()
        self._unregister_callback_route: Callable[[], None] | None = None

    @property
    def callback_url(self) -> str:
        """Return the public callback URL that resumes this flow's external step."""
        self._ensure_callback_route()
        return f"{self.mass.webserver.base_url}{self._callback_path}"

    async def form(
        self,
        entries: list[ConfigEntry],
        step_id: str = "user",
        errors: dict[str, str] | None = None,
        last_step: bool | None = None,
        expires_in: float | None = None,
    ) -> dict[str, ConfigValueType]:
        """
        Show a form to the user and wait for the submitted (validated) values.

        :param entries: The config entries that make up the form fields.
        :param step_id: Stable slug identifying this step (also the i18n key segment).
        :param errors: Optional field-key (or "base") -> error slug to display.
        :param last_step: Optional hint that this is the flow's final form.
        :param expires_in: Optional deadline in seconds; when it passes,
            StepExpiredError is raised here (and the client countdown runs out).
        """
        step = self._build_step(
            FlowStepType.FORM,
            step_id,
            entries=self._prepare_entries(entries),
            errors=dict(errors) if errors else {},
            last_step=last_step,
            expires_in=expires_in,
        )
        self._input_future = asyncio.get_running_loop().create_future()
        self._publish_step(step)
        try:
            return await self._await_with_deadline(self._input_future, expires_in)
        finally:
            self._input_future = None

    async def external(
        self,
        url: str,
        step_id: str = "auth",
        expires_in: float | None = None,
    ) -> dict[str, str]:
        """
        Send the user to an external URL (e.g. OAuth) and wait for the callback params.

        The flow resumes when the external party (or a bounce page) hits this flow's
        ``callback_url``; GET query and POST body parameters are merged and returned.

        :param url: The URL the user must open.
        :param step_id: Stable slug identifying this step (also the i18n key segment).
        :param expires_in: Optional deadline in seconds; when it passes,
            StepExpiredError is raised here (and the client countdown runs out).
        """
        self._ensure_callback_route()
        step = self._build_step(FlowStepType.EXTERNAL, step_id, url=url, expires_in=expires_in)
        self._callback_future = asyncio.get_running_loop().create_future()
        self._publish_step(step)
        try:
            return await self._await_with_deadline(self._callback_future, expires_in)
        finally:
            self._callback_future = None

    def progress(
        self,
        step_id: str,
        text: str | None = None,
        pct: float | None = None,
        image: str | None = None,
    ) -> None:
        """
        Publish a display-only progress step (fire-and-forget, re-emittable).

        :param step_id: Stable slug identifying this step (also the i18n key segment).
        :param text: Optional progress text (slug, resolved from the translations).
        :param pct: Optional completion fraction between 0 and 1.
        :param image: Optional data-URI illustration (e.g. a pairing QR code).
        """
        step = self._build_step(
            FlowStepType.PROGRESS, step_id, progress_text=text, progress=pct, image=image
        )
        self._publish_step(step)

    async def progress_until(
        self,
        awaitable: Awaitable[_T],
        step_id: str,
        text: str | None = None,
        image: str | None = None,
        expires_in: float | None = None,
    ) -> _T:
        """
        Publish a progress step and wait for the given awaitable, deadline-enforced.

        Display and enforcement come from the single ``expires_in`` declaration: the
        step's countdown and the engine deadline cannot drift. On the deadline the
        awaitable is cancelled and StepExpiredError is raised here.

        :param awaitable: The work/wait to perform while the progress step shows.
        :param step_id: Stable slug identifying this step (also the i18n key segment).
        :param text: Optional progress text (slug, resolved from the translations).
        :param image: Optional data-URI illustration (e.g. a pairing QR code).
        :param expires_in: Optional deadline in seconds.
        """
        step = self._build_step(
            FlowStepType.PROGRESS, step_id, progress_text=text, image=image, expires_in=expires_in
        )
        self._publish_step(step)
        return await self._await_with_deadline(awaitable, expires_in)

    async def finish(self, values: dict[str, ConfigValueType]) -> dict[str, str]:
        """
        Complete the flow: persist the collected values and create/reload the target.

        Raises SetupFlowError when applying the values failed (e.g. the provider
        does not load with them); the author may catch it and loop back to a form.
        On success the FINISH step is published and the result reference
        (e.g. ``{"instance_id": ...}``) is returned.

        :param values: The values to persist as the target's setup_data.
        """
        result = await self._finish_handler(self, values)
        self.finished = True
        step = self._build_step(FlowStepType.FINISH, "finish", result=result)
        self._publish_step(step)
        return result

    def retarget(
        self,
        *,
        domain: str,
        instance_id: str | None,
        player_id: str,
        setup_data: dict[str, Any],
        values: dict[str, ConfigValueType],
    ) -> None:
        """
        Re-point this running flow at a different (child) player.

        Used by a parent player's wrapper flow to delegate to one of its protocol
        children: after re-pointing, the child's steps localize under the child's
        provider namespace and ``finish()`` persists to the child's config. The steps
        published before the retarget (e.g. a "which device" selection form owned by
        the parent) keep the parent's namespace.

        :param domain: The child provider's domain.
        :param instance_id: The child provider instance id.
        :param player_id: The child player id (the new persist/finish target).
        :param setup_data: The child's decrypted setup_data, for prefill.
        :param values: The child's existing config values, for prefill.
        """
        self.context = replace(
            self.context,
            domain=domain,
            instance_id=instance_id,
            player_id=player_id,
            setup_data=setup_data,
            values=values,
        )
        self._translation_owner = f"provider.{domain}"

    # ------------------------------------------------------------------------------
    # Engine-facing methods below: called by the SetupFlowMixin, never by flow authors
    # ------------------------------------------------------------------------------

    def handle_submit(self, values: dict[str, ConfigValueType]) -> SetupFlowStep | None:
        """
        Validate submitted form values and hand them to the awaiting flow coroutine.

        Returns the updated FORM step when validation failed (the coroutine is not
        woken), or None when the values were accepted and the coroutine will resume.

        :param values: The raw values as submitted by the client.
        """
        step = self.current_step
        if (
            step is None
            or step.type != FlowStepType.FORM
            or self._input_future is None
            or self._input_future.done()
        ):
            raise ActionUnavailable("The setup flow is not awaiting form input")
        self.last_activity = time.monotonic()
        errors: dict[str, str] = {}
        parsed: dict[str, ConfigValueType] = {}
        for entry in step.entries:
            if entry.type in UI_ONLY:
                continue
            raw_value = values.get(entry.key, entry.value)
            try:
                # parse_value also runs the entry's optional validate callback and
                # stores the parsed value on the entry (echoed on a re-render)
                parsed[entry.key] = entry.parse_value(raw_value, allow_none=False)
            except TypeError, ValueError:
                errors[entry.key] = "required" if raw_value in (None, "") else "invalid_value"
                if not isinstance(raw_value, list):
                    entry.value = raw_value
            if entry.type == ConfigEntryType.SECURE_STRING:
                # secure values are only handed to the flow coroutine; they are never
                # kept on (or echoed back with) the stored step
                entry.value = None
        if errors:
            step.errors = errors
            self._publish_step(step)
            return step
        step.errors = {}
        # clear the step-changed marker before waking the coroutine, so the submit
        # caller can await the *next* published step without racing the coroutine
        self._step_changed.clear()
        self._input_future.set_result(parsed)
        return None

    async def handle_callback(self, request: Request) -> Response:
        """
        Handle an incoming request on this flow's external-step callback route.

        :param request: The incoming (GET or POST) request; query parameters and any
            POST body (JSON or form encoded) are merged into the callback params.
        """
        params: dict[str, str] = dict(request.query)
        if request.method == "POST" and request.can_read_body:
            try:
                if request.content_type == "application/json":
                    body = json_loads(await request.read())
                    if isinstance(body, dict):
                        # coerce to str so the declared params contract holds for
                        # json bodies carrying numbers/bools
                        params.update({str(key): str(val) for key, val in body.items()})
                    else:
                        LOGGER.error("Ignoring non-object JSON setup flow callback body")
                else:
                    params.update({key: str(val) for key, val in (await request.post()).items()})
            except Exception as err:
                LOGGER.error("Failed to parse setup flow callback body: %s", err)
        self.last_activity = time.monotonic()
        if self._callback_future is not None and not self._callback_future.done():
            self._callback_future.set_result(params)
        else:
            LOGGER.debug(
                "Received setup flow callback for %s while no external step is pending",
                self.flow_id,
            )
        return Response(body=CALLBACK_RESPONSE_HTML, headers={"content-type": "text/html"})

    async def wait_for_step_change(self, timeout: float) -> None:
        """
        Wait (bounded) until the flow publishes a step, returning silently on timeout.

        :param timeout: Maximum time in seconds to wait.
        """
        try:
            async with asyncio.timeout(timeout):
                await self._step_changed.wait()
        except TimeoutError:
            return

    def publish_abort(self, reason: str) -> None:
        """
        Publish the terminal ABORT step for this flow.

        :param reason: Slug describing why the flow was aborted; resolved from the
            translations (setup_flow.abort.<reason>) when the step is served.
        """
        step = self._build_step(FlowStepType.ABORT, "abort", reason=reason)
        self._publish_step(step)

    def close(self) -> None:
        """Release the session's engine resources (callback route); called on flow end."""
        if self._unregister_callback_route is not None:
            self._unregister_callback_route()
            self._unregister_callback_route = None

    async def _await_with_deadline(self, awaitable: Awaitable[_T], expires_in: float | None) -> _T:
        """
        Await the given awaitable, converting the step deadline into StepExpiredError.

        asyncio.timeout cancels the inner await at the deadline and raises TimeoutError
        in *this* (the flow's) coroutine - exactly the required inject-into-the-author
        semantic: catch it right at the awaiting call to refresh the step, or let it
        propagate so the engine converts it into a timed_out ABORT.
        """
        if expires_in is None:
            return await awaitable
        try:
            async with asyncio.timeout(expires_in):
                return await awaitable
        except TimeoutError as err:
            raise StepExpiredError from err

    def _build_step(  # noqa: PLR0913 - mirrors the step model's fields
        self,
        step_type: FlowStepType,
        step_id: str,
        *,
        entries: list[ConfigEntry] | None = None,
        errors: dict[str, str] | None = None,
        last_step: bool | None = None,
        url: str | None = None,
        progress_text: str | None = None,
        progress: float | None = None,
        image: str | None = None,
        result: dict[str, str] | None = None,
        reason: str | None = None,
        expires_in: float | None = None,
    ) -> SetupFlowStep:
        """Build a SetupFlowStep for this flow, stamping owner and absolute deadline."""
        return SetupFlowStep(
            flow_id=self.flow_id,
            step_id=step_id,
            type=step_type,
            entries=entries or [],
            errors=errors or {},
            last_step=last_step,
            url=url,
            progress_text=progress_text,
            progress=progress,
            image=image,
            # absolute epoch deadline so the remaining time survives a client re-fetch
            expires_at=time.time() + expires_in if expires_in is not None else None,
            result=result,
            reason=reason,
            translation_owner=self._translation_owner,
        )

    def _prepare_entries(self, entries: list[ConfigEntry]) -> list[ConfigEntry]:
        """Return copies of the form entries, validated and stamped for this flow."""
        prepared: list[ConfigEntry] = []
        for entry in entries:
            if entry.action:
                # actions are the pseudo-flow mechanism of the options surface;
                # inside real flows they are structurally banned
                msg = f"Config entry {entry.key} carries an action, not allowed in setup flows"
                raise ValueError(msg)
            # replace() returns a copy so the (often module-level) entry definitions
            # are never mutated by submit-side value/owner stamping
            copied = replace(
                entry, translation_owner=entry.translation_owner or self._translation_owner
            )
            if copied.type == ConfigEntryType.SECURE_STRING:
                # secrets never leave the server: a (prefilled) secure value is
                # stripped from the step; the user always (re)types it
                copied.value = None
            prepared.append(copied)
        return prepared

    def _publish_step(self, step: SetupFlowStep) -> None:
        """Store the step as current, mark activity and push it to subscribers."""
        self.current_step = step
        self.last_activity = time.monotonic()
        self._step_changed.set()
        self.mass.signal_event(EventType.SETUP_FLOW_UPDATED, object_id=self.flow_id, data=step)

    def _ensure_callback_route(self) -> None:
        """Register the flow's dynamic callback route (idempotent)."""
        if self._unregister_callback_route is not None:
            return
        self._unregister_callback_route = self.mass.webserver.register_dynamic_route(
            self._callback_path, self.handle_callback
        )
