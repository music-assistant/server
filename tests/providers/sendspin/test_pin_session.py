"""Tests for the Sendspin operator PIN pairing session state machine and orchestration."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections import deque
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from aiosendspin.models.core import PairMethodDescriptor
from aiosendspin.models.types import PairAbortReason, PairMethod
from aiosendspin.noise.driver import HandshakeAbortedError
from aiosendspin.noise.pairing import (
    LocalPairingAbortError,
    PairingError,
    RemotePairingAbortError,
)

import music_assistant.providers.sendspin.provider as provider_module
from music_assistant.providers.sendspin.helpers import SecurityActionError
from music_assistant.providers.sendspin.provider import (
    PinPairingSession,
    SendspinProvider,
    _pin_idle_task_id,
)

if TYPE_CHECKING:
    from aiosendspin.noise.pairing import PairingAttempt
    from aiosendspin.server import SendspinServer
    from aiosendspin.server.client import SendspinClient

    from music_assistant.mass import MusicAssistant


def _desc(method: PairMethod, *, locked_out: bool = False) -> PairMethodDescriptor:
    return PairMethodDescriptor(method=method, locked_out=locked_out)


async def _blocked() -> None:
    await asyncio.Event().wait()


class _FakeMass:
    """MusicAssistant stand-in mirroring create_task / call_later timer bookkeeping."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.timers: dict[str, asyncio.TimerHandle] = {}
        self.tasks: dict[str, asyncio.Task[Any]] = {}

    def create_task(
        self, coro: Any, *, task_id: str | None = None, abort_existing: bool = False
    ) -> asyncio.Task[Any]:
        if task_id and (existing := self.tasks.get(task_id)) and not existing.done():
            if abort_existing:
                existing.cancel()
            else:
                if asyncio.iscoroutine(coro):
                    coro.close()
                return existing
        task = self.loop.create_task(coro)
        if task_id is not None:
            key = task_id
            self.tasks[key] = task

            def _discard(_task: asyncio.Task[Any]) -> None:
                self.tasks.pop(key, None)

            task.add_done_callback(_discard)
        return task

    def call_later(
        self, delay: float, target: Any, *args: Any, task_id: str, **kwargs: Any
    ) -> asyncio.TimerHandle:
        if existing := self.timers.get(task_id):
            existing.cancel()

        def _fire() -> None:
            self.timers.pop(task_id, None)
            if inspect.iscoroutinefunction(target) or inspect.iscoroutine(target):
                self.create_task(target(*args, **kwargs), task_id=task_id, abort_existing=True)
            else:
                target(*args, **kwargs)

        handle = self.loop.call_later(delay, _fire)
        self.timers[task_id] = handle
        return handle

    def cancel_timer(self, task_id: str) -> None:
        if handle := self.timers.pop(task_id, None):
            handle.cancel()

    def cancel_task(self, task_id: str) -> None:
        if task := self.tasks.pop(task_id, None):
            task.cancel()


def _timers(provider: SendspinProvider) -> dict[str, asyncio.TimerHandle]:
    """Pending call_later timers on the provider's fake mass."""
    return cast("_FakeMass", provider.mass).timers


class _FakeServerApi:
    """Scripts pairing outcomes and records end_pairing calls, mirroring the real wiring."""

    def __init__(
        self,
        methods: list[PairMethodDescriptor],
        *,
        await_pin: bool = True,
        gesture: asyncio.Event | None = None,
    ) -> None:
        self._client = cast(
            "SendspinClient",
            SimpleNamespace(info_or_none=SimpleNamespace(supported_pair_methods=methods)),
        )
        self._await_pin = await_pin
        self._gesture = gesture
        self._active_cancel: asyncio.Event | None = None
        self._cancel_requested = False
        self.outcomes: deque[BaseException | None] = deque()
        self.attempts: list[PairingAttempt] = []
        self.initiate_calls = 0
        self.end_pairing_calls = 0

    def get_client(self, client_id: str) -> SendspinClient | None:
        return self._client

    async def initiate_pairing(self, client_id: str, attempt: PairingAttempt) -> None:
        self.initiate_calls += 1
        self.attempts.append(attempt)
        if self._gesture is not None:
            await self._gesture.wait()
        if self._await_pin and attempt.pin_provider is not None:
            if not self._cancel_requested:
                cancel = asyncio.Event()
                self._active_cancel = cancel
                cancel_task = asyncio.ensure_future(cancel.wait())
                pin_task = asyncio.ensure_future(attempt.pin_provider())
                try:
                    await asyncio.wait(
                        {pin_task, cancel_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    cancel_task.cancel()
                    self._active_cancel = None
            if self._cancel_requested:
                # end_pairing cancels the attempt regardless of ordering, as the real one does.
                raise LocalPairingAbortError(PairAbortReason.USER_CANCELLED)
        outcome = self.outcomes.popleft() if self.outcomes else None
        if isinstance(outcome, BaseException):
            raise outcome

    async def end_pairing(self, client_id: str) -> None:
        self.end_pairing_calls += 1
        self._cancel_requested = True
        if self._active_cancel is not None:
            self._active_cancel.set()


def _make_provider(
    server_api: _FakeServerApi, monkeypatch: pytest.MonkeyPatch
) -> tuple[SendspinProvider, list[str]]:
    provider = SendspinProvider.__new__(SendspinProvider)
    provider.mass = cast("MusicAssistant", _FakeMass(asyncio.get_running_loop()))
    provider.server_api = cast("SendspinServer", server_api)
    provider._pin_sessions = {}
    provider._pairing_config_snapshots = {}
    provider.logger = logging.getLogger("test.sendspin.pin")
    refreshed: list[str] = []

    async def _record_refresh(client_id: str) -> None:
        refreshed.append(client_id)

    monkeypatch.setattr(provider, "_refresh_player", _record_refresh)
    return provider, refreshed


async def test_session_running_states() -> None:
    """A running attempt tracks the gesture and PIN waits independently."""
    loop = asyncio.get_running_loop()
    running: asyncio.Task[None] = loop.create_task(_blocked())

    awaiting_future: asyncio.Future[str] = loop.create_future()
    awaiting = PinPairingSession(
        client_id="c", method=PairMethod.DYNAMIC_PIN, pin_future=awaiting_future, task=running
    )
    assert awaiting.attempt_running
    assert awaiting.awaiting_pin
    assert awaiting.awaiting_gesture
    assert not awaiting.can_retry
    assert not awaiting.finished

    submitted_future: asyncio.Future[str] = loop.create_future()
    submitted_future.set_result("123456")
    submitted_early = PinPairingSession(
        client_id="c", method=PairMethod.DYNAMIC_PIN, pin_future=submitted_future, task=running
    )
    assert submitted_early.attempt_running
    assert not submitted_early.awaiting_pin
    assert submitted_early.awaiting_gesture

    in_progress = PinPairingSession(
        client_id="c", method=PairMethod.DYNAMIC_PIN, pin_future=submitted_future, task=running
    )
    in_progress.pin_request_event.set()
    assert in_progress.attempt_running
    assert not in_progress.awaiting_pin
    assert not in_progress.awaiting_gesture
    running.cancel()


async def test_session_terminal_states() -> None:
    """A completed attempt is retryable while retryable is set, terminal once cleared."""
    loop = asyncio.get_running_loop()
    done: asyncio.Task[None] = loop.create_task(asyncio.sleep(0))
    await done
    pin_future: asyncio.Future[str] = loop.create_future()
    pin_future.cancel()

    retryable = PinPairingSession(
        client_id="c",
        method=PairMethod.DYNAMIC_PIN,
        pin_future=pin_future,
        task=done,
        retryable=True,
    )
    assert retryable.can_retry
    assert not retryable.finished
    assert not retryable.awaiting_pin
    assert not retryable.awaiting_gesture

    terminal = PinPairingSession(
        client_id="c", method=PairMethod.DYNAMIC_PIN, pin_future=pin_future, task=done
    )
    assert terminal.finished
    assert not terminal.can_retry


async def test_pin_pairing_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """A submitted PIN that succeeds finishes the session and refreshes the player."""
    api = _FakeServerApi([_desc(PairMethod.DYNAMIC_PIN)])
    provider, refreshed = _make_provider(api, monkeypatch)
    session = await provider.start_pin_pairing("c")
    assert session.awaiting_pin
    await provider.submit_pin("c", "123456")
    assert session.finished
    assert session.error is None
    assert refreshed == ["c"]
    assert api.end_pairing_calls == 0


async def test_pin_submitted_before_gesture(monkeypatch: pytest.MonkeyPatch) -> None:
    """A PIN submitted while the gesture is pending is consumed once the client enters pairing."""
    gesture = asyncio.Event()
    api = _FakeServerApi([_desc(PairMethod.STATIC_PIN)], gesture=gesture)
    provider, refreshed = _make_provider(api, monkeypatch)
    monkeypatch.setattr(provider_module, "PIN_SUBMIT_FEEDBACK_TIMEOUT", 0)
    monkeypatch.setattr(provider_module, "PIN_REQUEST_FEEDBACK_TIMEOUT", 0)
    session = await provider.start_pin_pairing("c")
    await asyncio.sleep(0)
    assert (session.awaiting_gesture, session.awaiting_pin) == (True, True)

    await provider.submit_pin("c", "12345678")
    assert (session.awaiting_gesture, session.awaiting_pin) == (True, False)
    assert session.attempt_running

    gesture.set()
    assert session.task is not None
    await session.task
    assert not session.awaiting_gesture
    assert session.finished
    assert session.error is None
    assert refreshed == ["c"]


async def test_default_prefers_dynamic_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    """When both PIN methods are offered, the default pick is dynamic."""
    api = _FakeServerApi([_desc(PairMethod.STATIC_PIN), _desc(PairMethod.DYNAMIC_PIN)])
    provider, _refreshed = _make_provider(api, monkeypatch)
    session = await provider.start_pin_pairing("c")
    assert session.method is PairMethod.DYNAMIC_PIN
    await provider.cancel_pin_pairing("c")


async def test_static_override_picks_static_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    """The static override pairs with the static PIN even when dynamic is offered."""
    api = _FakeServerApi([_desc(PairMethod.DYNAMIC_PIN), _desc(PairMethod.STATIC_PIN)])
    provider, refreshed = _make_provider(api, monkeypatch)
    session = await provider.start_pin_pairing("c", static=True)
    assert session.method is PairMethod.STATIC_PIN
    await provider.submit_pin("c", "12345678")
    assert session.finished
    assert session.error is None
    assert refreshed == ["c"]


async def test_static_override_requires_static_offer(monkeypatch: pytest.MonkeyPatch) -> None:
    """The static override fails when the device does not offer a usable static PIN."""
    api = _FakeServerApi(
        [_desc(PairMethod.DYNAMIC_PIN), _desc(PairMethod.STATIC_PIN, locked_out=True)]
    )
    provider, _refreshed = _make_provider(api, monkeypatch)
    with pytest.raises(SecurityActionError) as excinfo:
        await provider.start_pin_pairing("c", static=True)
    assert excinfo.value.alert_key == "pairing_error_locked_out"


async def test_pin_mismatch_is_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A PIN mismatch leaves the session retryable, parked, with an idle deadline armed."""
    api = _FakeServerApi([_desc(PairMethod.DYNAMIC_PIN)])
    api.outcomes.append(RemotePairingAbortError(PairAbortReason.PIN_MISMATCH))
    provider, refreshed = _make_provider(api, monkeypatch)
    session = await provider.start_pin_pairing("c")
    await provider.submit_pin("c", "000000")
    assert session.can_retry
    assert isinstance(session.error, RemotePairingAbortError)
    assert _pin_idle_task_id("c") in _timers(provider)
    assert api.end_pairing_calls == 0
    assert refreshed == []
    provider._cancel_pin_idle_timeout("c")


async def test_retry_resumes_in_place(monkeypatch: pytest.MonkeyPatch) -> None:
    """A retry reuses the session, preserving method and verify mode, and can then succeed."""
    api = _FakeServerApi([_desc(PairMethod.DYNAMIC_PIN)])
    api.outcomes.append(RemotePairingAbortError(PairAbortReason.PIN_MISMATCH))
    provider, refreshed = _make_provider(api, monkeypatch)
    session = await provider.start_pin_pairing("c", verify=True)
    await provider.submit_pin("c", "000000")
    assert session.can_retry

    same = await provider.start_pin_pairing("c")
    assert same is session
    assert session.verify is True
    assert session.method is PairMethod.DYNAMIC_PIN
    assert session.error is None
    assert not session.retryable
    assert _pin_idle_task_id("c") not in _timers(provider)
    assert session.awaiting_pin
    # start_pin_pairing waited for the immediate PIN request, so no gesture is claimed.
    assert not session.awaiting_gesture

    await provider.submit_pin("c", "123456")
    assert session.finished
    assert api.initiate_calls == 2
    assert refreshed == ["c"]


async def test_retry_awaits_gesture_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """A retried attempt waits for the client's pair-init anew."""
    gesture = asyncio.Event()
    gesture.set()
    api = _FakeServerApi([_desc(PairMethod.STATIC_PIN)], gesture=gesture)
    api.outcomes.append(RemotePairingAbortError(PairAbortReason.PIN_MISMATCH))
    provider, _refreshed = _make_provider(api, monkeypatch)
    monkeypatch.setattr(provider_module, "PIN_REQUEST_FEEDBACK_TIMEOUT", 0)
    session = await provider.start_pin_pairing("c")
    await provider.submit_pin("c", "00000000")
    assert session.can_retry

    gesture.clear()
    same = await provider.start_pin_pairing("c")
    assert same is session
    await asyncio.sleep(0)
    assert session.awaiting_gesture

    gesture.set()
    await provider.submit_pin("c", "12345678")
    assert session.finished
    assert session.error is None


async def test_locked_out_is_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """A locked-out abort is terminal and unparks the connection immediately."""
    api = _FakeServerApi([_desc(PairMethod.DYNAMIC_PIN)], await_pin=False)
    api.outcomes.append(RemotePairingAbortError(PairAbortReason.LOCKED_OUT))
    provider, _refreshed = _make_provider(api, monkeypatch)
    session = await provider.start_pin_pairing("c")
    assert session.task is not None
    await session.task
    assert session.finished
    assert not session.can_retry
    assert isinstance(session.error, RemotePairingAbortError)
    assert _pin_idle_task_id("c") not in _timers(provider)
    assert api.end_pairing_calls == 1


async def test_local_user_cancel_records_no_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Our own end_pairing cancel is not surfaced as an error."""
    api = _FakeServerApi([_desc(PairMethod.DYNAMIC_PIN)], await_pin=False)
    api.outcomes.append(LocalPairingAbortError(PairAbortReason.USER_CANCELLED))
    provider, refreshed = _make_provider(api, monkeypatch)
    session = await provider.start_pin_pairing("c")
    assert session.task is not None
    await session.task
    assert session.error is None
    assert not session.retryable
    assert session.finished
    assert refreshed == []


async def test_remote_user_cancel_is_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cancel initiated on the device is retryable from the operator's side."""
    api = _FakeServerApi([_desc(PairMethod.DYNAMIC_PIN)], await_pin=False)
    api.outcomes.append(RemotePairingAbortError(PairAbortReason.USER_CANCELLED))
    provider, _refreshed = _make_provider(api, monkeypatch)
    session = await provider.start_pin_pairing("c")
    assert session.task is not None
    await session.task
    assert session.can_retry
    assert isinstance(session.error, RemotePairingAbortError)
    assert _pin_idle_task_id("c") in _timers(provider)
    provider._cancel_pin_idle_timeout("c")


async def test_non_abort_failure_is_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-abort failure is terminal and does not call end_pairing (server disconnected)."""
    api = _FakeServerApi([_desc(PairMethod.DYNAMIC_PIN)], await_pin=False)
    api.outcomes.append(PairingError("boom"))
    provider, refreshed = _make_provider(api, monkeypatch)
    session = await provider.start_pin_pairing("c")
    assert session.task is not None
    await session.task
    assert session.finished
    assert not session.can_retry
    assert isinstance(session.error, PairingError)
    assert api.end_pairing_calls == 0
    assert refreshed == []


async def test_cancel_pin_pairing_ends_and_pops(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cancelling ends pairing, drops the session, and refreshes the player."""
    api = _FakeServerApi([_desc(PairMethod.DYNAMIC_PIN)])
    provider, refreshed = _make_provider(api, monkeypatch)
    session = await provider.start_pin_pairing("c")
    assert session.awaiting_pin
    await provider.cancel_pin_pairing("c")
    assert provider.get_pin_session("c") is None
    assert api.end_pairing_calls == 1
    assert refreshed == ["c"]


async def test_pair_with_token_failure_unparks(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed token pairing unparks the connection and re-raises."""
    api = _FakeServerApi([], await_pin=False)
    api.outcomes.append(RemotePairingAbortError(PairAbortReason.PIN_MISMATCH))
    provider, refreshed = _make_provider(api, monkeypatch)
    monkeypatch.setattr(
        provider_module,
        "decode_token",
        lambda _value: SimpleNamespace(client_id="c", pairing_psk=b"\x00" * 32),
    )
    with pytest.raises(RemotePairingAbortError):
        await provider.pair_with_token("c", "tok")
    assert api.end_pairing_calls == 1
    assert refreshed == []


async def test_pair_with_token_rejected_maps_to_pairing_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A token the client does not accept surfaces as a friendly PairingError."""
    api = _FakeServerApi([], await_pin=False)
    api.outcomes.append(HandshakeAbortedError("expected Noise message 2 (TEXT), got CLOSE"))
    provider, refreshed = _make_provider(api, monkeypatch)
    monkeypatch.setattr(
        provider_module,
        "decode_token",
        lambda _value: SimpleNamespace(client_id="c", pairing_psk=b"\x00" * 32),
    )
    with pytest.raises(PairingError, match="the token was rejected by the device"):
        await provider.pair_with_token("c", "tok")
    # The server already disconnected the client on a non-abort failure; nothing to unpark.
    assert api.end_pairing_calls == 0
    assert refreshed == []


async def test_pair_with_token_malformed_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """A token that fails to decode surfaces as an invalid-token alert without pairing."""
    api = _FakeServerApi([], await_pin=False)
    provider, refreshed = _make_provider(api, monkeypatch)
    with pytest.raises(SecurityActionError) as excinfo:
        await provider.pair_with_token("c", "not-a-token")
    assert excinfo.value.alert_key == "pairing_error_token_invalid"
    assert api.initiate_calls == 0
    assert refreshed == []


async def test_pair_with_token_rejected_while_pin_attempt_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A token submitted while a PIN attempt is running is rejected without a second attempt."""
    api = _FakeServerApi([_desc(PairMethod.DYNAMIC_PIN)])
    provider, refreshed = _make_provider(api, monkeypatch)
    session = await provider.start_pin_pairing("c")
    assert session.attempt_running
    with pytest.raises(SecurityActionError) as excinfo:
        await provider.pair_with_token("c", "tok")
    assert excinfo.value.alert_key == "pairing_error_concurrent"
    assert api.initiate_calls == 1
    assert refreshed == []
    await provider.cancel_pin_pairing("c")


async def test_pair_with_token_success_refreshes(monkeypatch: pytest.MonkeyPatch) -> None:
    """A successful token pairing refreshes the player without unparking."""
    api = _FakeServerApi([], await_pin=False)
    provider, refreshed = _make_provider(api, monkeypatch)
    monkeypatch.setattr(
        provider_module,
        "decode_token",
        lambda _value: SimpleNamespace(client_id="c", pairing_psk=b"\x00" * 32),
    )
    await provider.pair_with_token("c", "tok")
    assert api.end_pairing_calls == 0
    assert refreshed == ["c"]


async def test_idle_timeout_restores_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """An abandoned retryable session is terminated and the connection restored."""
    api = _FakeServerApi([_desc(PairMethod.DYNAMIC_PIN)], await_pin=False)
    provider, refreshed = _make_provider(api, monkeypatch)
    monkeypatch.setattr(provider_module, "PIN_RETRY_IDLE_TIMEOUT", 0)
    session = PinPairingSession(
        client_id="c",
        method=PairMethod.DYNAMIC_PIN,
        pin_future=asyncio.get_running_loop().create_future(),
        retryable=True,
    )
    provider._pin_sessions["c"] = session
    provider._arm_pin_idle_timeout(session)
    assert _pin_idle_task_id("c") in _timers(provider)
    # The zero-delay timer fires on a later loop iteration, then spawns the idle task.
    for _ in range(20):
        if api.end_pairing_calls:
            break
        await asyncio.sleep(0)
    assert api.end_pairing_calls == 1
    assert isinstance(session.error, TimeoutError)
    assert not session.retryable
    assert refreshed == ["c"]
    session.pin_future.cancel()
