"""Registration, authorization, and lifecycle tests for provider MA commands."""
# ruff: noqa: D102, D107, PT012

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast, get_type_hints
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken
from music_assistant_models.auth import Scope, User, UserRole
from music_assistant_models.errors import AuthenticationRequired, InsufficientPermissions

from music_assistant.helpers.api import APICommandHandler, parse_arguments
from music_assistant.providers.fastmcp_server.capabilities import Capability
from music_assistant.providers.fastmcp_server.commands import ProviderCommandSet, authorization
from music_assistant.providers.fastmcp_server.commands import debug as debug_commands
from music_assistant.providers.fastmcp_server.commands import queue as queue_commands
from music_assistant.providers.fastmcp_server.commands import registry as command_registry
from music_assistant.providers.fastmcp_server.commands.authorization import (
    authorize_extension,
    scope_allowed,
)
from music_assistant.providers.fastmcp_server.constants import (
    CONF_DEFAULT_POLICY,
    CONF_MANUAL_TOKEN_IDS,
    CONF_POLICY_TOKEN_SUFFIXES,
    CONF_REQUIRE_AUTH,
)
from music_assistant.providers.fastmcp_server.dynamic_api import DynamicAPIAdapter
from music_assistant.providers.fastmcp_server.dynamic_signatures import compile_signature
from music_assistant.providers.fastmcp_server.models import (
    EventBufferStats,
    EventSnapshot,
    HealthSummary,
    LogStatsResult,
    LogTailResult,
    PackageVersions,
    RemoveFromQueueResult,
    RouteList,
)
from music_assistant.providers.fastmcp_server.policy import (
    PolicyMode,
    PolicyProfile,
    PolicySnapshot,
    policy_snapshot,
)
from music_assistant.providers.fastmcp_server.policy_config import (
    policy_mode_key,
    policy_token_suffix,
    token_policy_key,
)
from music_assistant.providers.fastmcp_server.token_identity import TokenIdentity

if TYPE_CHECKING:
    from fastmcp import Context

COMMAND_ORDER = (
    "fastmcp/queue/remove_items_safe",
    "fastmcp/debug/tail_log",
    "fastmcp/debug/log_stats",
    "fastmcp/debug/recent_events",
    "fastmcp/debug/event_buffer_stats",
    "fastmcp/debug/health",
    "fastmcp/debug/routes",
    "fastmcp/debug/packages",
)
COMMANDS = set(COMMAND_ORDER)


class CommandRegistry:
    """Small real registry surface mirroring current MA registration semantics."""

    def __init__(
        self,
        *,
        fail_at: int | None = None,
        subscribe_error: Exception | None = None,
    ) -> None:
        self.handlers: dict[str, Callable[..., Any]] = {}
        self.command_handlers: dict[str, APICommandHandler] = {}
        self.webserver: Any = None
        self.translations = SimpleNamespace(get_translation=MagicMock(return_value=None))
        self.options: dict[str, dict[str, Any]] = {}
        self.removed: list[str] = []
        self.fail_at = fail_at
        self.subscribe_error = subscribe_error
        self.subscribed = 0
        self.unsubscribed = 0
        self.subscribers: list[Callable[..., Any]] = []

    def register_api_command(
        self,
        command: str,
        handler: Callable[..., Any],
        authenticated: bool = True,
        required_scope: Scope | None = None,
    ) -> Callable[[], None]:
        if self.fail_at is not None and len(self.handlers) == self.fail_at:
            raise RuntimeError("registration failed")
        if command in self.handlers:
            raise RuntimeError(f"duplicate {command}")
        self.handlers[command] = handler
        self.options[command] = {
            "authenticated": authenticated,
            "required_scope": required_scope,
        }

        def unregister() -> None:
            self.handlers.pop(command, None)
            self.removed.append(command)

        return unregister

    def subscribe(self, callback: Callable[..., Any]) -> Callable[[], None]:
        """Mirror MA subscriptions, including one-shot unsubscription."""
        self.subscribed += 1
        if self.subscribe_error is not None:
            raise self.subscribe_error
        self.subscribers.append(callback)

        def unsubscribe() -> None:
            if callback in self.subscribers:
                self.subscribers.remove(callback)
                self.unsubscribed += 1

        return unsubscribe


def _config(*enabled: Capability) -> MagicMock:
    config = MagicMock()
    values: dict[str, object] = {CONF_DEFAULT_POLICY: "Custom"}
    values.update({policy_mode_key(capability): "allow" for capability in enabled})
    config.get_value.side_effect = lambda key, default=None: values.get(key, default)
    return config


def _user(role: UserRole = UserRole.ADMIN, *, enabled: bool = True) -> User:
    return User(user_id="u1", username="tester", role=role, enabled=enabled)


def test_authorization_rejects_missing_and_disabled_users(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every native handler requires a present, enabled MA user."""
    config = _config(Capability.DEBUG_LOGS)
    monkeypatch.setattr(authorization, "get_current_user", lambda: None)
    with pytest.raises(AuthenticationRequired, match="enabled Music Assistant user"):
        authorize_extension(
            config, required_scope="system.read", required_capability=str(Capability.DEBUG_LOGS)
        )

    monkeypatch.setattr(authorization, "get_current_user", lambda: _user(enabled=False))
    with pytest.raises(AuthenticationRequired, match="enabled Music Assistant user"):
        authorize_extension(
            config, required_scope="system.read", required_capability=str(Capability.DEBUG_LOGS)
        )


def test_authorization_rejects_wrong_scope_and_disabled_provider_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MA scope and provider config permissions are independent gates."""
    monkeypatch.setattr(authorization, "has_scope", lambda _user, _scope: False, raising=False)
    monkeypatch.setattr(authorization, "get_current_user", lambda: _user(UserRole.USER))
    with pytest.raises(InsufficientPermissions, match=r"system\.read"):
        authorize_extension(
            _config(Capability.DEBUG_LOGS),
            required_scope="system.read",
            required_capability=str(Capability.DEBUG_LOGS),
        )

    monkeypatch.setattr(authorization, "get_current_user", lambda: _user())
    monkeypatch.setattr(authorization, "has_scope", lambda _user, _scope: True, raising=False)
    with pytest.raises(InsufficientPermissions, match="debug:logs"):
        authorize_extension(
            _config(),
            required_scope="system.read",
            required_capability=str(Capability.DEBUG_LOGS),
            policy_provider=lambda _bearer: policy_snapshot(PolicyProfile.CUSTOM),
        )


def test_scope_allowed_delegates_to_current_ma_scope_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scope decisions use MA's live helper and short-circuit disabled users."""
    checked: list[tuple[User, Scope]] = []

    def check(user: User, scope: Scope) -> bool:
        checked.append((user, scope))
        return scope is Scope.QUEUES_CONTROL

    monkeypatch.setattr(authorization, "has_scope", check, raising=False)
    user = _user(UserRole.USER)

    assert scope_allowed(user, "queues.control") is True
    assert scope_allowed(user, "system.read") is False
    assert scope_allowed(_user(UserRole.ADMIN, enabled=False), "queues.control") is False
    assert checked == [(user, Scope.QUEUES_CONTROL), (user, Scope.SYSTEM_READ)]


@pytest.mark.parametrize("required_scope", [Scope.UNKNOWN, "future.scope", object()])
def test_scope_allowed_rejects_unknown_scopes_without_calling_ma(
    required_scope: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An MA scope added after this provider release fails closed locally."""
    checked: list[tuple[User, Scope]] = []

    def check(user: User, scope: Scope) -> bool:
        checked.append((user, scope))
        return True

    monkeypatch.setattr(authorization, "has_scope", check, raising=False)

    assert scope_allowed(_user(UserRole.USER), required_scope) is False
    assert checked == []


def test_start_registers_exact_command_set_with_native_scopes() -> None:
    """No legacy or duplicate command leaks into MA's registry."""
    mass = CommandRegistry()
    command_set = ProviderCommandSet(
        mass,
        _config(*Capability),
        policy_provider=lambda _bearer: policy_snapshot(
            PolicyProfile.CUSTOM,
            dict.fromkeys(Capability, PolicyMode.ALLOW),
        ),
    )

    command_set.start()

    assert set(mass.handlers) == COMMANDS
    assert all(options["authenticated"] is True for options in mass.options.values())
    assert all(isinstance(options["required_scope"], Scope) for options in mass.options.values())


def test_registration_uses_current_ma_contract_without_signature_reflection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Native scopes are passed directly without feature-detecting MA internals."""
    reflection = SimpleNamespace(
        signature=MagicMock(side_effect=AssertionError("signature reflection is forbidden"))
    )
    monkeypatch.setattr(command_registry, "inspect", reflection, raising=False)
    mass = CommandRegistry()

    ProviderCommandSet(
        mass,
        _config(*Capability),
        policy_provider=lambda _bearer: policy_snapshot(PolicyProfile.TRUSTED),
    ).start()

    assert all(options["required_scope"] is not None for options in mass.options.values())


async def test_registered_handlers_keep_native_parseable_signatures_and_result_types(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MA's command parser and catalog compiler retain all native command contracts."""
    mass = CommandRegistry()
    command_set = ProviderCommandSet(
        mass,
        _config(*Capability),
        policy_provider=lambda _bearer: policy_snapshot(
            PolicyProfile.CUSTOM,
            dict.fromkeys(Capability, PolicyMode.ALLOW),
        ),
    )
    command_set.start()

    expected_returns = {
        "fastmcp/queue/remove_items_safe": RemoveFromQueueResult,
        "fastmcp/debug/tail_log": LogTailResult,
        "fastmcp/debug/log_stats": LogStatsResult,
        "fastmcp/debug/recent_events": EventSnapshot,
        "fastmcp/debug/event_buffer_stats": EventBufferStats,
        "fastmcp/debug/health": HealthSummary,
        "fastmcp/debug/routes": RouteList,
        "fastmcp/debug/packages": PackageVersions,
    }
    for command, expected_return in expected_returns.items():
        handler = mass.handlers[command]
        ma_handler = APICommandHandler.parse(command, handler)
        signature = ma_handler.signature
        hints = ma_handler.type_hints
        compiled = compile_signature(signature, hints)
        assert ma_handler.target is handler
        assert hints["return"] is expected_return
        assert compiled.output_schema() is not None
        assert all(
            param.kind is not inspect.Parameter.VAR_KEYWORD
            for param in signature.parameters.values()
        )

    tail = mass.handlers["fastmcp/debug/tail_log"]
    tail_signature = inspect.signature(tail)
    tail_hints = get_type_hints(tail)
    parsed = parse_arguments(
        tail_signature,
        tail_hints,
        {"lines": 3, "level": "error", "name": "musicassistant.log"},
        strict=True,
    )
    assert parsed["lines"] == 3
    assert parsed["level"] == "error"
    assert parsed["name"] == "musicassistant.log"
    assert set(compile_signature(tail_signature, tail_hints).input_schema["properties"]) >= {
        "lines",
        "level",
        "component_regex",
        "search",
        "since_seconds",
        "before",
        "name",
    }

    log_stats_handler = APICommandHandler.parse(
        "fastmcp/debug/log_stats", mass.handlers["fastmcp/debug/log_stats"]
    )
    assert (
        parse_arguments(
            log_stats_handler.signature,
            log_stats_handler.type_hints,
            {"since_seconds": 60, "name": "musicassistant.log.1"},
            strict=True,
        )["since_seconds"]
        == 60
    )
    assert set(
        compile_signature(log_stats_handler.signature, log_stats_handler.type_hints).input_schema[
            "properties"
        ]
    ) == {"since_seconds", "name"}

    recent = mass.handlers["fastmcp/debug/recent_events"]
    recent_signature = inspect.signature(recent)
    recent_hints = get_type_hints(recent)
    parsed_recent = parse_arguments(
        recent_signature,
        recent_hints,
        {"limit": 2, "event_types": ["player_updated"], "id_filter": "kitchen"},
        strict=True,
    )
    assert parsed_recent["limit"] == 2
    assert parsed_recent["event_types"] == ["player_updated"]
    assert parsed_recent["id_filter"] == "kitchen"

    monkeypatch.setattr(authorization, "has_scope", lambda _user, _scope: True, raising=False)
    monkeypatch.setattr(authorization, "get_current_user", lambda: _user())
    monkeypatch.setattr(authorization, "get_current_token", lambda: "request-token")
    plain_tail = AsyncMock(
        return_value=LogTailResult(log_path="x", lines=[], bytes_scanned=0, truncated=False)
    )
    monkeypatch.setattr(debug_commands, "tail_log", plain_tail)
    result = await tail(**parsed)
    assert result.log_path == "x"
    plain_tail.assert_awaited_once_with(mass, **parsed)


def test_partial_start_rolls_back_in_reverse_and_can_retry() -> None:
    """A failed start leaves no duplicates and unregisters in LIFO order."""
    mass = CommandRegistry(fail_at=3)
    command_set = ProviderCommandSet(
        mass,
        _config(*Capability),
        policy_provider=lambda _bearer: policy_snapshot(PolicyProfile.TRUSTED),
    )

    with pytest.raises(RuntimeError, match="registration failed"):
        command_set.start()

    assert mass.handlers == {}
    assert mass.removed == [
        "fastmcp/debug/log_stats",
        "fastmcp/debug/tail_log",
        "fastmcp/queue/remove_items_safe",
    ]
    mass.fail_at = None
    command_set.start()
    assert set(mass.handlers) == COMMANDS


def test_subscription_failure_rolls_back_commands_and_allows_retry() -> None:
    """Event capture is part of the same all-or-nothing startup transaction."""
    mass = CommandRegistry(subscribe_error=RuntimeError("event bus offline"))
    command_set = ProviderCommandSet(
        mass,
        _config(*Capability),
        policy_provider=lambda _bearer: policy_snapshot(PolicyProfile.TRUSTED),
    )

    with pytest.raises(RuntimeError, match="event bus offline"):
        command_set.start()

    assert mass.handlers == {}
    assert mass.removed == list(reversed(COMMAND_ORDER))
    mass.subscribe_error = None
    command_set.start()
    assert set(mass.handlers) == COMMANDS
    assert mass.subscribed == 2


async def test_provider_debug_guard_uses_exact_request_policy_not_global_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provider debug handlers resolve the current bearer instead of global tags."""
    mass = CommandRegistry()
    policies = {
        "deny": policy_snapshot(PolicyProfile.SAFE_QUERIES),
        "allow": policy_snapshot(
            PolicyProfile.CUSTOM,
            {Capability.DEBUG_PROVIDERS: PolicyMode.ALLOW},
        ),
    }
    current = ["deny"]

    def request_policy(bearer: str | None) -> PolicySnapshot:
        assert bearer is not None
        return policies[bearer]

    command_set = ProviderCommandSet(
        mass,
        _config(Capability.DEBUG_PROVIDERS),
        policy_provider=request_policy,
    )
    command_set.start()
    monkeypatch.setattr(authorization, "get_current_user", lambda: _user())
    monkeypatch.setattr(authorization, "get_current_token", lambda: current[0])
    packages_handler = mass.handlers["fastmcp/debug/packages"]

    with pytest.raises(InsufficientPermissions, match="debug:providers"):
        awaitable = packages_handler()
        await awaitable

    current[0] = "allow"
    awaitable = packages_handler()
    result = await awaitable
    assert "fastmcp" in result.packages

    command_set.stop()
    command_set.stop()
    assert mass.handlers == {}
    assert len(mass.removed) == 8


@pytest.mark.parametrize("failure", [False, True])
async def test_provider_owned_privileged_execution_audits_once_without_payloads(
    failure: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct provider-owned writes emit one controlled, value-free execution outcome."""
    mass = CommandRegistry()
    records: list[Any] = []
    policy = policy_snapshot(
        PolicyProfile.CUSTOM,
        {Capability.DELETE_QUEUE: PolicyMode.ALLOW},
    )
    command_set = ProviderCommandSet(
        mass,
        _config(Capability.DELETE_QUEUE),
        policy_provider=lambda _bearer: policy,
        audit_sink=records.append,
        audit_client_id_provider=lambda _bearer: "exact-token-id",
    )
    command_set.start()
    monkeypatch.setattr(authorization, "get_current_user", lambda: _user())
    monkeypatch.setattr(authorization, "get_current_token", lambda: "raw-provider-bearer")

    async def remove_items(_mass: Any, _queue_id: str, _item_ids: list[str]) -> Any:
        if failure:
            raise RuntimeError("exception-secret-must-not-appear")
        return RemoveFromQueueResult()

    monkeypatch.setattr(queue_commands, "remove_items_safe", remove_items)
    call = mass.handlers["fastmcp/queue/remove_items_safe"](
        "secret-queue-argument", ["secret-item-argument"]
    )
    if failure:
        with pytest.raises(RuntimeError):
            await call
    else:
        await call

    assert len(records) == 1
    record = records[0]
    assert record.outcome == ("execution.failed" if failure else "execution.succeeded")
    assert (
        record.user_id,
        record.client_id,
        record.command,
        record.capability,
        record.mode,
    ) == ("u1", "exact-token-id", "fastmcp/queue/remove_items_safe", "delete:queue", "allow")
    emitted = repr(records)
    for forbidden in (
        "raw-provider-bearer",
        "secret-queue-argument",
        "secret-item-argument",
        "exception-secret-must-not-appear",
    ):
        assert forbidden not in emitted


async def test_provider_owned_denial_audits_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A direct provider-owned policy denial emits one authorization record."""
    mass = CommandRegistry()
    records: list[Any] = []
    command_set = ProviderCommandSet(
        mass,
        _config(),
        policy_provider=lambda _bearer: policy_snapshot(PolicyProfile.SAFE_QUERIES),
        audit_sink=records.append,
        audit_client_id_provider=lambda _bearer: "exact-token-id",
    )
    command_set.start()
    monkeypatch.setattr(authorization, "get_current_user", lambda: _user())
    monkeypatch.setattr(authorization, "get_current_token", lambda: "raw-provider-bearer")

    with pytest.raises(InsufficientPermissions):
        await mass.handlers["fastmcp/debug/packages"]()

    assert len(records) == 1
    assert records[0].outcome == "authorization.denied"
    assert records[0].capability == "debug:providers"
    assert records[0].mode == "deny"


async def test_dynamic_provider_execution_is_not_double_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The provider-owned wrapper, not the outer dynamic dispatcher, owns execution audit."""
    mass = CommandRegistry()
    records: list[Any] = []
    user = _user()
    policy = policy_snapshot(
        PolicyProfile.CUSTOM,
        {Capability.DELETE_QUEUE: PolicyMode.ALLOW},
    )
    command_set = ProviderCommandSet(
        mass,
        _config(Capability.DELETE_QUEUE),
        policy_provider=lambda _bearer: policy,
        audit_sink=records.append,
        audit_client_id_provider=lambda _bearer: "token-id",
    )
    command_set.start()
    command = "fastmcp/queue/remove_items_safe"
    native_handler = mass.handlers[command]
    mass.command_handlers = {command: APICommandHandler.parse(command, native_handler)}
    mass.webserver = SimpleNamespace(
        auth=SimpleNamespace(
            authenticate_with_token=AsyncMock(return_value=user),
            get_token_id_from_token=AsyncMock(return_value="token-id"),
        )
    )
    monkeypatch.setattr(
        queue_commands,
        "remove_items_safe",
        AsyncMock(return_value=RemoveFromQueueResult()),
    )
    adapter = DynamicAPIAdapter(
        mass,
        auth_required_provider=lambda: True,
        token_provider=lambda: AccessToken(token="bearer", client_id="token-id", scopes=[]),
        policy_provider=lambda _bearer: policy,
        default_policy_provider=lambda: policy,
        identity_provider=lambda _bearer: TokenIdentity("u1", "token-id"),
        audit_sink=records.append,
    )

    await adapter.call(
        f"ma_api:{command}",
        {"queue_id": "queue", "item_ids": ["item"]},
        response_mode="compact",
        fields=None,
        max_items=None,
        ctx=cast("Context", MagicMock()),
    )

    assert [record.outcome for record in records] == ["execution.succeeded"]


async def test_debug_health_uses_request_policy_for_optional_log_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Global debug-log allowance cannot leak log diagnostics to a denied token."""
    mass = CommandRegistry()
    policy = policy_snapshot(
        PolicyProfile.CUSTOM,
        {Capability.DEBUG_PROVIDERS: PolicyMode.ALLOW},
    )
    command_set = ProviderCommandSet(
        mass,
        _config(Capability.DEBUG_PROVIDERS, Capability.DEBUG_LOGS),
        policy_provider=lambda _bearer: policy,
    )
    command_set.start()
    monkeypatch.setattr(authorization, "get_current_user", lambda: _user())
    monkeypatch.setattr(authorization, "get_current_token", lambda: "request-token")
    health = AsyncMock(return_value=MagicMock(spec=HealthSummary))
    monkeypatch.setattr(debug_commands, "health", health)

    await mass.handlers["fastmcp/debug/health"]()

    assert health.await_args is not None
    assert health.await_args.kwargs["logs_enabled"] is False
    assert health.await_args.kwargs["policy_schema_version"] == 2
    assert health.await_args.kwargs["policy_profile"] == "Custom"
    assert health.await_args.kwargs["token_resolution_failures"] == 0


async def test_direct_provider_confirm_requires_dispatcher_confirmation_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A native provider handler cannot treat Confirm as Allow outside MCP dispatch."""
    mass = CommandRegistry()
    policy = policy_snapshot(
        PolicyProfile.CUSTOM,
        {Capability.DEBUG_PROVIDERS: PolicyMode.CONFIRM},
    )
    command_set = ProviderCommandSet(
        mass,
        _config(Capability.DEBUG_PROVIDERS),
        policy_provider=lambda _bearer: policy,
    )
    command_set.start()
    monkeypatch.setattr(authorization, "get_current_user", lambda: _user())
    monkeypatch.setattr(authorization, "get_current_token", lambda: "request-token")

    with pytest.raises(InsufficientPermissions) as exc_info:
        await mass.handlers["fastmcp/debug/packages"]()

    message = str(exc_info.value)
    assert "debug:providers" in message
    assert "Allow" in message
    assert "elicitation-capable client" in message


async def test_auth_off_provider_command_uses_global_default_without_request_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provider-owned commands share the auth-off global-default request policy."""
    mass = CommandRegistry()
    config = _config()
    config.get_value.side_effect = lambda key, default=None: {
        CONF_DEFAULT_POLICY: "Custom",
        CONF_REQUIRE_AUTH: False,
    }.get(key, default)
    policies = [
        policy_snapshot(
            PolicyProfile.CUSTOM,
            {Capability.DEBUG_PROVIDERS: PolicyMode.ALLOW},
        )
    ]
    command_set = ProviderCommandSet(
        mass,
        config,
        policy_provider=lambda _bearer: policies[0],
    )
    command_set.start()
    monkeypatch.setattr(authorization, "get_current_user", lambda: None)
    monkeypatch.setattr(authorization, "get_current_token", lambda: None)
    packages = AsyncMock(return_value=MagicMock(spec=PackageVersions))
    monkeypatch.setattr(debug_commands, "packages", packages)

    await mass.handlers["fastmcp/debug/packages"]()
    packages.assert_awaited_once()

    policies[0] = policy_snapshot(PolicyProfile.CUSTOM)
    with pytest.raises(InsufficientPermissions, match="debug:providers"):
        await mass.handlers["fastmcp/debug/packages"]()


async def test_dispatcher_confirmation_context_is_scoped_and_not_remembered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Accepted provider confirmations work once and cannot bless later native calls."""
    mass = CommandRegistry()
    user = _user()
    policy = policy_snapshot(
        PolicyProfile.CUSTOM,
        {Capability.DEBUG_PROVIDERS: PolicyMode.CONFIRM},
    )
    command_set = ProviderCommandSet(
        mass,
        _config(Capability.DEBUG_PROVIDERS),
        policy_provider=lambda _bearer: policy,
    )
    command_set.start()
    command = "fastmcp/debug/packages"
    native_handler = mass.handlers[command]
    mass.command_handlers = {command: APICommandHandler.parse(command, native_handler)}
    mass.webserver = SimpleNamespace(
        auth=SimpleNamespace(
            authenticate_with_token=AsyncMock(return_value=user),
            get_token_id_from_token=AsyncMock(return_value="token-id"),
        )
    )
    token = AccessToken(token="bearer", client_id="token-id", scopes=[])
    adapter = DynamicAPIAdapter(
        mass,
        auth_required_provider=lambda: True,
        token_provider=lambda: token,
        policy_provider=lambda _bearer: policy,
        default_policy_provider=lambda: policy,
        identity_provider=lambda _bearer: TokenIdentity("u1", "token-id"),
    )
    ctx = SimpleNamespace(
        elicit=AsyncMock(return_value=SimpleNamespace(action="accept", data=True))
    )

    for _ in range(2):
        await adapter.call(
            f"ma_api:{command}",
            {},
            response_mode="compact",
            fields=None,
            max_items=None,
            ctx=cast("Context", ctx),
        )
    assert ctx.elicit.await_count == 2

    monkeypatch.setattr(authorization, "get_current_user", lambda: user)
    monkeypatch.setattr(authorization, "get_current_token", lambda: "bearer")
    with pytest.raises(InsufficientPermissions, match="elicitation-capable client"):
        await native_handler()


async def test_dispatcher_confirmation_rejects_copied_child_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A copied child context cannot consume its parent's confirmation grant."""
    mass = CommandRegistry()
    user = _user()
    policy = policy_snapshot(
        PolicyProfile.CUSTOM,
        {Capability.DEBUG_PROVIDERS: PolicyMode.CONFIRM},
    )
    command_set = ProviderCommandSet(
        mass,
        _config(Capability.DEBUG_PROVIDERS),
        policy_provider=lambda _bearer: policy,
    )
    command_set.start()
    command = "fastmcp/debug/packages"
    native_handler = mass.handlers[command]
    mass.command_handlers = {command: APICommandHandler.parse(command, native_handler)}
    mass.webserver = SimpleNamespace(
        auth=SimpleNamespace(
            authenticate_with_token=AsyncMock(return_value=user),
            get_token_id_from_token=AsyncMock(return_value="token-id"),
        )
    )
    adapter = DynamicAPIAdapter(
        mass,
        auth_required_provider=lambda: True,
        token_provider=lambda: AccessToken(token="bearer", client_id="token-id", scopes=[]),
        policy_provider=lambda _bearer: policy,
        default_policy_provider=lambda: policy,
        identity_provider=lambda _bearer: TokenIdentity("u1", "token-id"),
    )
    release_delayed = asyncio.Event()
    owner_task = asyncio.current_task()
    delayed_task: asyncio.Task[PackageVersions] | None = None
    immediate_denied = False

    async def delayed_direct_call() -> PackageVersions:
        await release_delayed.wait()
        return cast("PackageVersions", await native_handler())

    async def packages() -> PackageVersions:
        nonlocal delayed_task, immediate_denied
        if asyncio.current_task() is owner_task:
            immediate_task = asyncio.create_task(native_handler())
            try:
                await immediate_task
            except InsufficientPermissions:
                immediate_denied = True
            delayed_task = asyncio.create_task(delayed_direct_call())
        return PackageVersions(packages={})

    monkeypatch.setattr(debug_commands, "packages", packages)
    ctx = SimpleNamespace(
        elicit=AsyncMock(return_value=SimpleNamespace(action="accept", data=True))
    )

    await adapter.call(
        f"ma_api:{command}",
        {},
        response_mode="compact",
        fields=None,
        max_items=None,
        ctx=cast("Context", ctx),
    )
    assert immediate_denied is True
    assert delayed_task is not None
    release_delayed.set()
    with pytest.raises(InsufficientPermissions, match="elicitation-capable client"):
        await delayed_task


async def test_dispatcher_confirmation_revokes_copied_context_when_handler_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exceptional dispatcher cleanup revokes grants already copied by child tasks."""
    mass = CommandRegistry()
    user = _user()
    policy = policy_snapshot(
        PolicyProfile.CUSTOM,
        {Capability.DEBUG_PROVIDERS: PolicyMode.CONFIRM},
    )
    command_set = ProviderCommandSet(
        mass,
        _config(Capability.DEBUG_PROVIDERS),
        policy_provider=lambda _bearer: policy,
    )
    command_set.start()
    command = "fastmcp/debug/packages"
    native_handler = mass.handlers[command]
    mass.command_handlers = {command: APICommandHandler.parse(command, native_handler)}
    mass.webserver = SimpleNamespace(
        auth=SimpleNamespace(
            authenticate_with_token=AsyncMock(return_value=user),
            get_token_id_from_token=AsyncMock(return_value="token-id"),
        )
    )
    adapter = DynamicAPIAdapter(
        mass,
        auth_required_provider=lambda: True,
        token_provider=lambda: AccessToken(token="bearer", client_id="token-id", scopes=[]),
        policy_provider=lambda _bearer: policy,
        default_policy_provider=lambda: policy,
        identity_provider=lambda _bearer: TokenIdentity("u1", "token-id"),
    )
    release_child = asyncio.Event()
    owner_task = asyncio.current_task()
    child_task: asyncio.Task[PackageVersions] | None = None

    async def delayed_direct_call() -> PackageVersions:
        await release_child.wait()
        return cast("PackageVersions", await native_handler())

    async def packages() -> PackageVersions:
        nonlocal child_task
        if asyncio.current_task() is owner_task:
            child_task = asyncio.create_task(delayed_direct_call())
            raise RuntimeError("provider handler failed")
        return PackageVersions(packages={})

    monkeypatch.setattr(debug_commands, "packages", packages)
    ctx = SimpleNamespace(
        elicit=AsyncMock(return_value=SimpleNamespace(action="accept", data=True))
    )

    with pytest.raises(ToolError, match=r"\[execution_failed\]"):
        await adapter.call(
            f"ma_api:{command}",
            {},
            response_mode="compact",
            fields=None,
            max_items=None,
            ctx=cast("Context", ctx),
        )
    assert child_task is not None
    release_child.set()
    with pytest.raises(InsufficientPermissions, match="elicitation-capable client"):
        await child_task


def test_event_buffer_survives_event_hot_toggles_and_resizes_before_restart() -> None:
    """The command owner retains one buffer until a non-hot capacity change replaces it."""
    mass = CommandRegistry()
    disabled = _config()
    command_set = ProviderCommandSet(
        mass,
        disabled,
        policy_provider=lambda _bearer: policy_snapshot(PolicyProfile.TRUSTED),
    )

    command_set.start()
    buffer = command_set.event_buffer
    assert buffer is not None
    assert mass.subscribed == 0

    command_set.update_config(_config(Capability.DEBUG_EVENTS))
    assert command_set.event_buffer is buffer
    assert mass.subscribed == 1

    command_set.update_config(_config())
    assert command_set.event_buffer is buffer
    assert mass.unsubscribed == 1

    command_set.update_config(_config(Capability.DEBUG_EVENTS))
    assert command_set.event_buffer is buffer
    assert mass.subscribed == 2

    resized = _config(Capability.DEBUG_EVENTS)
    resized.get_value.side_effect = lambda key, default=None: {
        CONF_DEFAULT_POLICY: "Custom",
        policy_mode_key(Capability.DEBUG_EVENTS): "allow",
        "debug_event_buffer_capacity": 250,
    }.get(key, default)
    command_set.update_config(resized)

    assert command_set.event_buffer is not buffer
    assert command_set.event_buffer is not None
    assert command_set.event_buffer.stats().capacity == 250
    assert mass.unsubscribed == 2
    assert mass.subscribed == 3

    command_set.stop()
    assert mass.unsubscribed == 3


def test_manual_token_policy_activates_event_buffer() -> None:
    """A resolvable manual override starts retention even when the default denies it."""
    mass = CommandRegistry()
    token_id = "foreign-token-id"
    values = {
        CONF_DEFAULT_POLICY: "Safe queries",
        CONF_MANUAL_TOKEN_IDS: [token_id],
        token_policy_key(token_id): "Custom",
        policy_mode_key(Capability.DEBUG_EVENTS, token_id): "confirm",
        "debug_event_buffer_capacity": 100,
    }
    config = MagicMock()
    config.get_value.side_effect = lambda key, default=None: values.get(key, default)
    command_set = ProviderCommandSet(
        mass,
        config,
        policy_provider=lambda _bearer: policy_snapshot(PolicyProfile.TRUSTED),
    )

    command_set.start()

    assert mass.subscribed == 1


def test_authenticated_discovered_token_policy_activates_event_buffer() -> None:
    """A bound discovered token makes its non-deny debug policy retain events."""
    mass = CommandRegistry()
    token_id = "discovered-token-id"
    values = {
        CONF_DEFAULT_POLICY: "Safe queries",
        token_policy_key(token_id): "Custom",
        policy_mode_key(Capability.DEBUG_EVENTS, token_id): "allow",
        "debug_event_buffer_capacity": 100,
    }
    config = MagicMock()
    config.get_value.side_effect = lambda key, default=None: values.get(key, default)
    command_set = ProviderCommandSet(
        mass,
        config,
        policy_provider=lambda _bearer: policy_snapshot(PolicyProfile.TRUSTED),
    )
    command_set.start()

    command_set.update_config(config, active_token_ids={token_id})

    assert mass.subscribed == 1

    command_set.update_config(config)

    assert mass.unsubscribed == 0


def test_hashed_token_override_hot_update_activates_event_buffer_without_identity() -> None:
    """A raw auto-token override starts retention on the config hot-update itself."""
    mass = CommandRegistry()
    token_id = "never-authenticated-token-id"

    def configured(debug_mode: str | None) -> MagicMock:
        values = {
            CONF_DEFAULT_POLICY: "Safe queries",
            "debug_event_buffer_capacity": 100,
        }
        if debug_mode is not None:
            values[CONF_POLICY_TOKEN_SUFFIXES] = [policy_token_suffix(token_id)]
            values[token_policy_key(token_id)] = "Custom"
            values[policy_mode_key(Capability.DEBUG_EVENTS, token_id)] = debug_mode
        config = MagicMock()
        config.get_value.side_effect = lambda key, default=None: values.get(key, default)
        config.values = {key: SimpleNamespace(value=value) for key, value in values.items()}
        return config

    disabled = configured(None)
    command_set = ProviderCommandSet(
        mass,
        disabled,
        policy_provider=lambda _bearer: policy_snapshot(PolicyProfile.TRUSTED),
    )
    command_set.start()
    assert mass.subscribed == 0

    command_set.update_config(configured("confirm"))

    assert mass.subscribed == 1


def test_stop_attempts_all_unregistrations_then_raises_first_error() -> None:
    """A bad unregister callback cannot leave later commands registered forever."""
    mass = CommandRegistry()
    command_set = ProviderCommandSet(
        mass,
        _config(*Capability),
        policy_provider=lambda _bearer: policy_snapshot(PolicyProfile.TRUSTED),
    )
    command_set.start()
    original = command_set._unregister[-2]

    def broken_unregister() -> None:
        original()
        raise RuntimeError("unregister failed")

    command_set._unregister[-2] = broken_unregister
    with pytest.raises(RuntimeError, match="unregister failed"):
        command_set.stop()

    assert mass.handlers == {}
    assert mass.unsubscribed == 1
    command_set.stop()
