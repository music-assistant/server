"""Request-scoped Permissions & Confirmations v2 enforcement tests."""

from __future__ import annotations

import hashlib
import inspect
import logging
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp import Client, Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken
from mcp.shared.exceptions import McpError
from music_assistant_models.auth import Scope
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from music_assistant.providers.fastmcp_server.auth import LOOKUP_FAILURE_CLIENT_ID
from music_assistant.providers.fastmcp_server.capabilities import Capability
from music_assistant.providers.fastmcp_server.dynamic_api import DynamicAPIAdapter
from music_assistant.providers.fastmcp_server.meta_discovery import MetaDiscoveryService
from music_assistant.providers.fastmcp_server.middleware import TagFilterMiddleware
from music_assistant.providers.fastmcp_server.policy import (
    PolicyMode,
    PolicyProfile,
    PolicySnapshot,
    policy_snapshot,
)
from music_assistant.providers.fastmcp_server.server import build_tag_lookup
from music_assistant.providers.fastmcp_server.token_identity import TokenIdentity


def _handler(
    command: str,
    target: Any,
    scope: str = "library.read",
    *,
    allow_impersonation: bool = False,
) -> Any:
    """Build the stable subset of MA's API handler contract."""
    return SimpleNamespace(
        command=command,
        signature=inspect.signature(target),
        type_hints=target.__annotations__,
        target=target,
        authenticated=True,
        required_scope=scope,
        allow_impersonation=allow_impersonation,
        alias=False,
    )


def _custom(**modes: PolicyMode) -> PolicySnapshot:
    """Build a literal Custom snapshot from capability fragments."""
    return policy_snapshot(
        PolicyProfile.CUSTOM,
        {capability.replace("__", ":"): mode for capability, mode in modes.items()},
    )


def _adapter(
    handlers: list[Any],
    *,
    current_token: list[AccessToken],
    policies: dict[str, PolicySnapshot],
    user: Any | None = None,
    audit_sink: Any | None = None,
) -> DynamicAPIAdapter:
    """Build an adapter whose token and policy can change during one request."""
    mass = MagicMock()
    mass.command_handlers = {handler.command: handler for handler in handlers}
    user = user or SimpleNamespace(
        user_id="same-user",
        enabled=True,
        role="admin",
        player_filter=[],
        provider_filter=[],
    )
    mass.webserver.auth.authenticate_with_token = AsyncMock(return_value=user)
    mass.webserver.auth.get_token_id_from_token = AsyncMock(
        side_effect=lambda _bearer: current_token[0].client_id
    )
    return DynamicAPIAdapter(
        mass,
        auth_required_provider=lambda: True,
        token_provider=lambda: current_token[0],
        scope_checker=lambda _user, _scope: True,
        policy_provider=lambda bearer: policies[bearer],
        default_policy_provider=lambda: next(iter(policies.values())),
        identity_provider=lambda _bearer: TokenIdentity("same-user", current_token[0].client_id),
        audit_sink=audit_sink,
    )


@pytest.mark.parametrize(
    ("elicitation", "expected_outcome", "raises"),
    [
        (SimpleNamespace(action="accept", data=True), "confirmation.accepted", False),
        (SimpleNamespace(action="decline", data=False), "confirmation.declined", True),
        (NotImplementedError(), "confirmation.unsupported", True),
    ],
)
async def test_confirmation_audit_records_controlled_outcomes(
    elicitation: object,
    expected_outcome: str,
    raises: bool,
) -> None:
    """Each elicitation attempt records requested plus one controlled terminal outcome."""

    async def search() -> str:
        return "ok"

    records: list[Any] = []
    token = AccessToken(token="raw-bearer-must-not-appear", client_id="exact-token-id", scopes=[])
    adapter = _adapter(
        [_handler("music/search", search)],
        current_token=[token],
        policies={"raw-bearer-must-not-appear": _custom(query__library=PolicyMode.CONFIRM)},
        audit_sink=records.append,
    )
    elicit = (
        AsyncMock(side_effect=elicitation)
        if isinstance(elicitation, BaseException)
        else AsyncMock(return_value=elicitation)
    )
    call = adapter.call(
        "ma_api:music/search",
        {},
        response_mode="compact",
        fields=None,
        max_items=None,
        ctx=cast("Context", SimpleNamespace(elicit=elicit)),
    )
    if raises:
        with pytest.raises(ToolError):
            await call
    else:
        await call

    assert [record.outcome for record in records] == [
        "confirmation.requested",
        expected_outcome,
    ]
    assert {
        (
            record.user_id,
            record.client_id,
            record.command,
            record.capability,
            record.mode,
        )
        for record in records
    } == {("same-user", "exact-token-id", "music/search", "query:library", "confirm")}
    assert "raw-bearer-must-not-appear" not in repr(records)


@pytest.mark.parametrize("failure", [False, True])
async def test_privileged_dynamic_execution_audits_success_or_controlled_failure(
    failure: bool,
) -> None:
    """Privileged execution records omit arguments, secrets, and exception details."""

    async def update_metadata(uri: str) -> str:
        del uri
        if failure:
            raise RuntimeError("exception-secret-must-not-appear")
        return "ok"

    records: list[Any] = []
    bearer = "raw-bearer-must-not-appear"
    token = AccessToken(token=bearer, client_id="exact-token-id", scopes=[])
    adapter = _adapter(
        [_handler("metadata/update_metadata", update_metadata)],
        current_token=[token],
        policies={bearer: _custom(edit__library=PolicyMode.ALLOW)},
        audit_sink=records.append,
    )
    call = adapter.call(
        "ma_api:metadata/update_metadata",
        {"uri": "secret-argument-must-not-appear"},
        response_mode="compact",
        fields=None,
        max_items=None,
        ctx=cast("Context", MagicMock()),
    )
    if failure:
        with pytest.raises(ToolError):
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
    ) == ("same-user", "exact-token-id", "metadata/update_metadata", "edit:library", "allow")
    emitted = repr(records)
    for forbidden in (
        bearer,
        "secret-argument-must-not-appear",
        "exception-secret-must-not-appear",
    ):
        assert forbidden not in emitted


async def test_dynamic_denial_is_audited_once() -> None:
    """A denied visible-name call emits one denial record and no execution record."""

    async def search() -> str:
        return "unreachable"

    records: list[Any] = []
    bearer = "denied-bearer"
    adapter = _adapter(
        [_handler("music/search", search)],
        current_token=[AccessToken(token=bearer, client_id="exact-token-id", scopes=[])],
        policies={bearer: _custom()},
        audit_sink=records.append,
    )

    with pytest.raises(ToolError, match="not permitted"):
        await adapter.call(
            "ma_api:music/search",
            {},
            response_mode="compact",
            fields=None,
            max_items=None,
            ctx=cast("Context", MagicMock()),
        )

    assert len(records) == 1
    assert records[0].outcome == "authorization.denied"
    assert records[0].capability == "query:library"
    assert records[0].mode == "deny"


async def test_unknown_denial_uses_fixed_command_sentinel() -> None:
    """Caller-controlled unknown names cannot enter the structured audit fields."""

    async def search() -> str:
        return "unreachable"

    records: list[Any] = []
    bearer = "known-bearer"
    adapter = _adapter(
        [_handler("music/search", search)],
        current_token=[AccessToken(token=bearer, client_id="exact-token-id", scopes=[])],
        policies={bearer: _custom(query__library=PolicyMode.ALLOW)},
        audit_sink=records.append,
    )

    with pytest.raises(ToolError, match="not permitted"):
        await adapter.call(
            "ma_api:secret-name-must-not-appear",
            {},
            response_mode="compact",
            fields=None,
            max_items=None,
            ctx=cast("Context", MagicMock()),
        )

    assert len(records) == 1
    assert records[0].command == "unknown"
    assert "secret-name-must-not-appear" not in repr(records)


async def test_default_audit_log_excludes_bearer_fingerprint_secret_and_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The production logger boundary emits only the fixed redacted record fields."""
    bearer = "raw-bearer-must-not-appear"

    async def update_metadata(uri: str) -> None:
        del uri
        raise RuntimeError("exception-secret-must-not-appear")

    adapter = _adapter(
        [_handler("metadata/update_metadata", update_metadata)],
        current_token=[AccessToken(token=bearer, client_id="exact-token-id", scopes=[])],
        policies={bearer: _custom(edit__library=PolicyMode.ALLOW)},
    )

    with (
        caplog.at_level(logging.INFO, logger="music_assistant.providers.fastmcp_server.audit"),
        pytest.raises(ToolError),
    ):
        await adapter.call(
            "ma_api:metadata/update_metadata",
            {"uri": "secret-argument-must-not-appear"},
            response_mode="compact",
            fields=None,
            max_items=None,
            ctx=cast("Context", MagicMock()),
        )

    assert len(caplog.records) == 1
    rendered = repr(caplog.records[0].__dict__)
    for forbidden in (
        bearer,
        hashlib.sha256(bearer.encode()).hexdigest(),
        "secret-argument-must-not-appear",
        "exception-secret-must-not-appear",
    ):
        assert forbidden not in rendered


async def test_two_tokens_for_one_user_get_distinct_discovery_modes() -> None:
    """A user-level cache cannot collapse exact-token allow and confirm policy."""

    async def search() -> list[str]:
        return []

    tokens = [AccessToken(token="allow", client_id="id-allow", scopes=[])]
    policies = {
        "allow": _custom(query__library=PolicyMode.ALLOW),
        "confirm": _custom(query__library=PolicyMode.CONFIRM),
    }
    adapter = _adapter([_handler("music/search", search)], current_token=tokens, policies=policies)

    allow_entry = (await adapter.visible_entries())[0]
    tokens[0] = AccessToken(token="confirm", client_id="id-confirm", scopes=[])
    confirm_entry = (await adapter.visible_entries())[0]

    assert allow_entry.policy_mode is PolicyMode.ALLOW
    assert confirm_entry.policy_mode is PolicyMode.CONFIRM


async def test_deny_hides_and_confirm_elicits_on_every_call() -> None:
    """Deny is undiscoverable while accepted confirmations are never remembered."""
    calls = 0

    async def search() -> str:
        nonlocal calls
        calls += 1
        return "ok"

    tokens = [AccessToken(token="deny", client_id="id-deny", scopes=[])]
    policies = {
        "deny": _custom(),
        "confirm": _custom(query__library=PolicyMode.CONFIRM),
    }
    adapter = _adapter([_handler("music/search", search)], current_token=tokens, policies=policies)
    assert await adapter.visible_entries() == []

    tokens[0] = AccessToken(token="confirm", client_id="id-confirm", scopes=[])
    ctx = SimpleNamespace(
        elicit=AsyncMock(return_value=SimpleNamespace(action="accept", data=True))
    )
    for _ in range(2):
        await adapter.call(
            "ma_api:music/search",
            {},
            response_mode="compact",
            fields=None,
            max_items=None,
            ctx=cast("Context", ctx),
        )

    assert ctx.elicit.await_count == 2
    assert calls == 2


async def test_confirmation_decline_and_unsupported_errors_are_exact_and_actionable() -> None:
    """Confirm failures neither execute nor conceal the capability/operator remedy."""
    called = False

    async def search() -> None:
        nonlocal called
        called = True

    token = AccessToken(token="confirm", client_id="id-confirm", scopes=[])
    adapter = _adapter(
        [_handler("music/search", search)],
        current_token=[token],
        policies={"confirm": _custom(query__library=PolicyMode.CONFIRM)},
    )
    declined = SimpleNamespace(
        elicit=AsyncMock(return_value=SimpleNamespace(action="decline", data=False))
    )
    with pytest.raises(ToolError, match=r"\[operation_cancelled\]"):
        await adapter.call(
            "ma_api:music/search",
            {},
            response_mode="compact",
            fields=None,
            max_items=None,
            ctx=cast("Context", declined),
        )
    unsupported = SimpleNamespace(elicit=AsyncMock(side_effect=NotImplementedError))
    with pytest.raises(ToolError) as exc_info:
        await adapter.call(
            "ma_api:music/search",
            {},
            response_mode="compact",
            fields=None,
            max_items=None,
            ctx=cast("Context", unsupported),
        )
    message = str(exc_info.value)
    assert "query:library" in message
    assert "Allow" in message
    assert "elicitation-capable client" in message
    assert called is False


@pytest.mark.parametrize("revoked", ["policy", "scope", "target"])
async def test_revocation_during_confirmation_blocks_execution(revoked: str) -> None:
    """Accepted elicitation cannot outrun policy, scope, or target-filter changes."""
    called = False

    async def get_player(player_id: str) -> None:
        nonlocal called
        del player_id
        called = True

    user = SimpleNamespace(
        user_id="same-user",
        enabled=True,
        role="user",
        player_filter=["kitchen"],
        provider_filter=[],
    )
    token = AccessToken(token="confirm", client_id="id-confirm", scopes=[])
    policies = {"confirm": _custom(query__players=PolicyMode.CONFIRM)}
    adapter = _adapter(
        [_handler("players/get", get_player, "players.read")],
        current_token=[token],
        policies=policies,
        user=user,
    )
    scope_allowed = True
    adapter._scope_checker = lambda _user, _scope: scope_allowed

    async def accept_and_revoke(_prompt: str, *, response_type: type[bool]) -> Any:
        nonlocal scope_allowed
        del response_type
        if revoked == "policy":
            policies["confirm"] = _custom()
        elif revoked == "scope":
            scope_allowed = False
        else:
            user.player_filter = ["bedroom"]
        return SimpleNamespace(action="accept", data=True)

    with pytest.raises(ToolError, match="not permitted"):
        await adapter.call(
            "ma_api:players/get",
            {"player_id": "kitchen"},
            response_mode="compact",
            fields=None,
            max_items=None,
            ctx=cast("Context", SimpleNamespace(elicit=accept_and_revoke)),
        )
    assert called is False


async def test_search_schema_and_cursor_revision_include_effective_mode() -> None:
    """Cached discovery state becomes stale when allow changes to confirm."""

    async def search() -> None:
        return None

    async def browse() -> None:
        return None

    token = AccessToken(token="policy", client_id="id-policy", scopes=[])
    policies = {"policy": _custom(query__library=PolicyMode.ALLOW)}
    adapter = _adapter(
        [_handler("music/search", search), _handler("music/browse", browse)],
        current_token=[token],
        policies=policies,
    )
    service = MetaDiscoveryService(adapter)
    first = await service.discover("", limit=1)
    schema = await service.get_schema(first["items"][0]["name"])
    assert first["items"][0]["policy_mode"] == "allow"
    assert schema["policy_mode"] == "allow"
    assert first["next_cursor"] is not None

    policies["policy"] = _custom(query__library=PolicyMode.CONFIRM)
    changed = await service.discover("", limit=1)
    assert changed["items"][0]["policy_mode"] == "confirm"
    assert changed["catalog_revision"] != first["catalog_revision"]
    with pytest.raises(Exception, match="catalog changed"):
        await service.discover("", cursor=first["next_cursor"], limit=1)


async def test_resource_confirm_is_hidden_and_direct_reads_follow_policy_changes() -> None:
    """A cached concrete resource URI works only while its capability is Allow."""
    policy = [_custom(query__library=PolicyMode.ALLOW)]
    mcp: FastMCP = FastMCP(name="resource-policy")

    @mcp.resource("library://track/{track_id}", tags={Capability.QUERY_LIBRARY})  # type: ignore[untyped-decorator, unused-ignore]
    async def track(track_id: str) -> str:
        return track_id

    mcp.add_middleware(
        TagFilterMiddleware(
            build_tag_lookup(mcp),
            policy_provider=lambda: policy[0],
        )
    )
    async with Client(mcp) as client:
        await client.read_resource("library://track/17")
        policy[0] = _custom(query__library=PolicyMode.CONFIRM)
        templates = {str(item.uriTemplate) for item in await client.list_resource_templates()}
        assert "library://track/{track_id}" not in templates
        with pytest.raises(McpError):
            await client.read_resource("library://track/17")


async def test_secret_capability_escalates_and_is_rechecked_after_confirmation() -> None:
    """A secure field adds its own capability and prompt-time denial wins."""
    called = False

    async def save(
        provider_domain: str,
        values: dict[str, Any],
        instance_id: str | None = None,
    ) -> None:
        nonlocal called
        del provider_domain, values, instance_id
        called = True

    token = AccessToken(token="config", client_id="id-config", scopes=[])
    policies = {
        "config": _custom(
            config__write__provider=PolicyMode.ALLOW,
            config__write__secret=PolicyMode.CONFIRM,
        )
    }
    adapter = _adapter(
        [_handler("config/providers/save", save, "config.providers.write")],
        current_token=[token],
        policies=policies,
    )
    adapter.mass.config.get_provider_config_entries = AsyncMock(
        return_value=[ConfigEntry(key="token", type=ConfigEntryType.SECURE_STRING, label="Token")]
    )

    async def accept_then_revoke(_prompt: str, *, response_type: type[bool]) -> Any:
        del response_type
        policies["config"] = _custom(config__write__provider=PolicyMode.ALLOW)
        return SimpleNamespace(action="accept", data=True)

    with pytest.raises(ToolError, match="not permitted"):
        await adapter.call(
            "ma_api:config/providers/save",
            {
                "provider_domain": "demo",
                "instance_id": "demo--1",
                "values": {"token": "new-secret"},
            },
            response_mode="compact",
            fields=None,
            max_items=None,
            ctx=cast("Context", SimpleNamespace(elicit=accept_then_revoke)),
        )
    assert called is False


async def test_policy_revoked_during_preflight_is_rechecked_before_confirmation() -> None:
    """An awaited secret inspection cannot leave a stale allow snapshot executable."""
    called = False

    async def save(
        provider_domain: str,
        values: dict[str, Any],
        instance_id: str | None = None,
    ) -> None:
        nonlocal called
        del provider_domain, values, instance_id
        called = True

    token = AccessToken(token="config", client_id="id-config", scopes=[])
    policies = {
        "config": _custom(
            config__write__provider=PolicyMode.ALLOW,
            config__write__secret=PolicyMode.ALLOW,
        )
    }
    records: list[Any] = []
    adapter = _adapter(
        [_handler("config/providers/save", save, "config.providers.write")],
        current_token=[token],
        policies=policies,
        audit_sink=records.append,
    )

    inspections = 0

    async def inspect_then_revoke(_target: str) -> list[ConfigEntry]:
        nonlocal inspections
        inspections += 1
        if inspections == 2:
            policies["config"] = _custom()
        return [ConfigEntry(key="token", type=ConfigEntryType.SECURE_STRING, label="Token")]

    adapter.mass.config.get_provider_config_entries = inspect_then_revoke
    with pytest.raises(ToolError, match="not permitted"):
        await adapter.call(
            "ma_api:config/providers/save",
            {
                "provider_domain": "demo",
                "instance_id": "demo--1",
                "values": {"token": "new-secret"},
            },
            response_mode="compact",
            fields=None,
            max_items=None,
            ctx=MagicMock(),
        )
    assert called is False
    assert len(records) == 1
    assert records[0].capability == str(Capability.CONFIG_WRITE_SECRET)
    assert records[0].mode == "deny"


async def test_secure_category_changed_during_final_auth_is_recomputed_and_audited() -> None:
    """The final auth await cannot leave a stale non-secret preflight executable."""
    called = False
    authentication_count = 0
    secure_now = False
    records: list[Any] = []

    async def save(
        provider_domain: str,
        values: dict[str, Any],
        instance_id: str | None = None,
    ) -> None:
        nonlocal called
        del provider_domain, values, instance_id
        called = True

    token = AccessToken(token="config", client_id="id-config", scopes=[])
    adapter = _adapter(
        [_handler("config/providers/save", save, "config.providers.write")],
        current_token=[token],
        policies={
            "config": _custom(
                config__write__provider=PolicyMode.ALLOW,
                config__write__secret=PolicyMode.DENY,
            )
        },
        audit_sink=records.append,
    )
    user = await adapter.mass.webserver.auth.authenticate_with_token("config")

    async def authenticate_then_reclassify(_bearer: str) -> Any:
        nonlocal authentication_count, secure_now
        authentication_count += 1
        if authentication_count == 4:
            secure_now = True
        return user

    adapter.mass.webserver.auth.authenticate_with_token = authenticate_then_reclassify

    async def live_entries(_target: str) -> list[ConfigEntry]:
        return [
            ConfigEntry(
                key="token",
                type=(ConfigEntryType.SECURE_STRING if secure_now else ConfigEntryType.STRING),
                label="Token",
            )
        ]

    adapter.mass.config.get_provider_config_entries = live_entries
    with pytest.raises(ToolError, match=r"\[not_found_or_forbidden\]"):
        await adapter.call(
            "ma_api:config/providers/save",
            {
                "provider_domain": "demo",
                "instance_id": "demo--1",
                "values": {"token": "must-not-appear"},
            },
            response_mode="compact",
            fields=None,
            max_items=None,
            ctx=MagicMock(),
        )
    assert called is False
    assert len(records) == 1
    assert records[0].capability == str(Capability.CONFIG_WRITE_SECRET)
    assert records[0].mode == "deny"
    assert "must-not-appear" not in repr(records)


async def test_setup_flow_category_changed_during_final_auth_is_recomputed() -> None:
    """A live provider-to-player flow change cannot retain the earlier category grant."""
    called = False
    authentication_count = 0
    flow_scope = "config.providers.write"
    records: list[Any] = []

    async def submit(flow_id: str, values: dict[str, Any]) -> None:
        nonlocal called
        del flow_id, values
        called = True

    token = AccessToken(token="flow", client_id="id-flow", scopes=[])
    adapter = _adapter(
        [_handler("config/flows/submit", submit, "config.providers.write")],
        current_token=[token],
        policies={
            "flow": _custom(
                config__write__provider=PolicyMode.ALLOW,
                config__write__player=PolicyMode.DENY,
            )
        },
        audit_sink=records.append,
    )
    user = await adapter.mass.webserver.auth.authenticate_with_token("flow")

    async def authenticate_then_change_category(_bearer: str) -> Any:
        nonlocal authentication_count, flow_scope
        authentication_count += 1
        if authentication_count == 4:
            flow_scope = "config.players.write"
        return user

    adapter.mass.webserver.auth.authenticate_with_token = authenticate_then_change_category
    adapter.mass.config.get_setup_flow_required_scope = lambda _flow_id: flow_scope
    adapter.mass.config.get_setup_flow = lambda _flow_id: SimpleNamespace(entries=[])

    with pytest.raises(ToolError, match=r"\[not_found_or_forbidden\]"):
        await adapter.call(
            "ma_api:config/flows/submit",
            {"flow_id": "flow-secret", "values": {}},
            response_mode="compact",
            fields=None,
            max_items=None,
            ctx=MagicMock(),
        )
    assert called is False
    assert len(records) == 1
    assert records[0].capability == str(Capability.CONFIG_WRITE_PLAYER)
    assert "flow-secret" not in repr(records)


async def test_auth_revoked_during_final_preflight_blocks_handler_execution() -> None:
    """The last awaited secure inspection cannot leave bearer auth stale."""
    called = False
    inspections = 0

    async def save(
        provider_domain: str,
        values: dict[str, Any],
        instance_id: str | None = None,
    ) -> None:
        nonlocal called
        del provider_domain, values, instance_id
        called = True

    token = AccessToken(token="config", client_id="id-config", scopes=[])
    policies = {
        "config": _custom(
            config__write__provider=PolicyMode.ALLOW,
            config__write__secret=PolicyMode.ALLOW,
        )
    }
    adapter = _adapter(
        [_handler("config/providers/save", save, "config.providers.write")],
        current_token=[token],
        policies=policies,
    )

    async def inspect_then_revoke_auth(_target: str) -> list[ConfigEntry]:
        nonlocal inspections
        inspections += 1
        if inspections == 3:
            adapter.mass.webserver.auth.authenticate_with_token = AsyncMock(return_value=None)
        return [ConfigEntry(key="token", type=ConfigEntryType.SECURE_STRING, label="Token")]

    adapter.mass.config.get_provider_config_entries = inspect_then_revoke_auth
    with pytest.raises(ToolError, match="Authentication is required"):
        await adapter.call(
            "ma_api:config/providers/save",
            {
                "provider_domain": "demo",
                "instance_id": "demo--1",
                "values": {"token": "new-secret"},
            },
            response_mode="compact",
            fields=None,
            max_items=None,
            ctx=MagicMock(),
        )
    assert called is False


@pytest.mark.parametrize("revoked", ["token_identity", "user"])
async def test_exact_identity_revoked_during_final_preflight_blocks_execution(
    revoked: str,
) -> None:
    """Final preflight cannot leave an exact token binding or enabled user stale."""
    called = False
    inspections = 0
    user = SimpleNamespace(
        user_id="same-user",
        enabled=True,
        role="admin",
        player_filter=[],
        provider_filter=[],
    )

    async def save(
        provider_domain: str,
        values: dict[str, Any],
        instance_id: str | None = None,
    ) -> None:
        nonlocal called
        del provider_domain, values, instance_id
        called = True

    token = AccessToken(token="config", client_id="id-config", scopes=[])
    adapter = _adapter(
        [_handler("config/providers/save", save, "config.providers.write")],
        current_token=[token],
        policies={
            "config": _custom(
                config__write__provider=PolicyMode.ALLOW,
                config__write__secret=PolicyMode.ALLOW,
            )
        },
        user=user,
    )

    async def inspect_then_revoke_identity(_target: str) -> list[ConfigEntry]:
        nonlocal inspections
        inspections += 1
        if inspections == 3:
            if revoked == "token_identity":
                adapter.mass.webserver.auth.get_token_id_from_token = AsyncMock(
                    return_value="replacement"
                )
            else:
                user.enabled = False
        return [ConfigEntry(key="token", type=ConfigEntryType.SECURE_STRING, label="Token")]

    adapter.mass.config.get_provider_config_entries = inspect_then_revoke_identity
    with pytest.raises(ToolError, match="Authentication is required"):
        await adapter.call(
            "ma_api:config/providers/save",
            {
                "provider_domain": "demo",
                "instance_id": "demo--1",
                "values": {"token": "new-secret"},
            },
            response_mode="compact",
            fields=None,
            max_items=None,
            ctx=MagicMock(),
        )
    assert called is False


@pytest.mark.parametrize(
    ("final_target", "permitted"),
    [
        ("retained", True),
        ("disabled", False),
        ("replaced", False),
    ],
)
async def test_impersonation_target_is_fresh_after_final_preflight(
    final_target: str,
    permitted: bool,
) -> None:
    """Retain only the exact enabled target resolved after the final preflight."""
    called = False
    inspections = 0

    async def save(
        provider_domain: str,
        values: dict[str, Any],
        instance_id: str | None = None,
    ) -> None:
        nonlocal called
        del provider_domain, values, instance_id
        called = True

    token = AccessToken(token="config", client_id="id-config", scopes=[])
    adapter = _adapter(
        [
            _handler(
                "config/providers/save",
                save,
                "config.providers.write",
                allow_impersonation=True,
            )
        ],
        current_token=[token],
        policies={
            "config": _custom(
                config__write__provider=PolicyMode.ALLOW,
                config__write__secret=PolicyMode.ALLOW,
            )
        },
    )
    original_target = SimpleNamespace(
        user_id="target",
        enabled=True,
        role="admin",
        player_filter=[],
        provider_filter=[],
    )
    live_target = [original_target]

    async def final_preflight_mutation(_target: str) -> list[ConfigEntry]:
        nonlocal inspections
        inspections += 1
        if inspections == 3:
            if final_target == "disabled":
                live_target[0] = SimpleNamespace(**{**vars(original_target), "enabled": False})
            elif final_target == "replaced":
                live_target[0] = SimpleNamespace(
                    **{**vars(original_target), "user_id": "replacement"}
                )
            else:
                live_target[0] = SimpleNamespace(**vars(original_target))
        return [ConfigEntry(key="token", type=ConfigEntryType.SECURE_STRING, label="Token")]

    adapter.mass.config.get_provider_config_entries = final_preflight_mutation
    resolutions = 0

    async def resolve_live_target(_auth: Any, _requested: str) -> Any:
        nonlocal resolutions
        resolutions += 1
        return original_target if resolutions < 3 else live_target[0]

    cast("Any", adapter)._resolve_impersonated_user = resolve_live_target
    ctx = SimpleNamespace(
        elicit=AsyncMock(return_value=SimpleNamespace(action="accept", data=True))
    )
    call = adapter.call(
        "ma_api:config/providers/save",
        {
            "provider_domain": "demo",
            "instance_id": "demo--1",
            "values": {"token": "new-secret"},
            "user": "target",
        },
        response_mode="compact",
        fields=None,
        max_items=None,
        ctx=cast("Context", ctx),
    )
    if permitted:
        await call
    else:
        with pytest.raises(ToolError, match=r"\[execution_failed\]"):
            await call
    assert inspections == 3
    assert resolutions == 3
    assert called is permitted


async def test_bearer_revoked_during_final_impersonation_lookup_blocks_execution() -> None:
    """Request-local exact bearer replacement during target lookup is sealed out."""
    called = False

    async def save(
        provider_domain: str,
        values: dict[str, Any],
        instance_id: str | None = None,
    ) -> None:
        nonlocal called
        del provider_domain, values, instance_id
        called = True

    token = AccessToken(token="config", client_id="id-config", scopes=[])
    current_token = [token]
    adapter = _adapter(
        [
            _handler(
                "config/providers/save",
                save,
                "config.providers.write",
                allow_impersonation=True,
            )
        ],
        current_token=current_token,
        policies={
            "config": _custom(
                config__write__provider=PolicyMode.ALLOW,
                config__write__secret=PolicyMode.ALLOW,
            )
        },
    )
    adapter.mass.config.get_provider_config_entries = AsyncMock(
        return_value=[ConfigEntry(key="token", type=ConfigEntryType.SECURE_STRING, label="Token")]
    )
    impersonated_user = SimpleNamespace(
        user_id="target",
        enabled=True,
        role="admin",
        player_filter=[],
        provider_filter=[],
    )
    resolutions = 0

    async def resolve_and_revoke_bearer(_auth: Any, _requested: str) -> Any:
        nonlocal resolutions
        resolutions += 1
        if resolutions == 3:
            current_token[0] = AccessToken(
                token="replacement",
                client_id="replacement-id",
                scopes=[],
            )
        return impersonated_user

    cast("Any", adapter)._resolve_impersonated_user = resolve_and_revoke_bearer
    ctx = SimpleNamespace(
        elicit=AsyncMock(return_value=SimpleNamespace(action="accept", data=True))
    )

    with pytest.raises(ToolError, match="Authentication is required"):
        await adapter.call(
            "ma_api:config/providers/save",
            {
                "provider_domain": "demo",
                "instance_id": "demo--1",
                "values": {"token": "new-secret"},
                "user": "target",
            },
            response_mode="compact",
            fields=None,
            max_items=None,
            ctx=cast("Context", ctx),
        )
    assert resolutions == 3
    assert called is False


async def test_final_auth_user_after_impersonation_lookup_is_used_for_execution() -> None:
    """Execution context uses the user returned by the last exact authentication."""
    initial_user = SimpleNamespace(
        user_id="same-user",
        enabled=True,
        role="admin",
        player_filter=[],
        provider_filter=[],
    )
    fresh_user = SimpleNamespace(
        user_id="same-user",
        enabled=True,
        role="admin",
        player_filter=[],
        provider_filter=[],
    )
    execution_user: Any = None

    async def save(
        provider_domain: str,
        values: dict[str, Any],
        instance_id: str | None = None,
    ) -> None:
        nonlocal execution_user
        del provider_domain, values, instance_id
        from music_assistant.controllers.webserver.helpers import (  # noqa: PLC0415
            auth_middleware,
        )

        execution_user = auth_middleware.current_user.get()

    token = AccessToken(token="config", client_id="id-config", scopes=[])
    adapter = _adapter(
        [
            _handler(
                "config/providers/save",
                save,
                "config.providers.write",
                allow_impersonation=True,
            )
        ],
        current_token=[token],
        policies={
            "config": _custom(
                config__write__provider=PolicyMode.ALLOW,
                config__write__secret=PolicyMode.ALLOW,
            )
        },
        user=initial_user,
    )
    inspections = 0

    async def preflight_then_replace_caller(_target: str) -> list[ConfigEntry]:
        nonlocal inspections
        inspections += 1
        if inspections == 3:
            adapter.mass.webserver.auth.authenticate_with_token = AsyncMock(return_value=fresh_user)
        return [ConfigEntry(key="token", type=ConfigEntryType.SECURE_STRING, label="Token")]

    adapter.mass.config.get_provider_config_entries = preflight_then_replace_caller
    impersonated_user = SimpleNamespace(
        user_id="target",
        enabled=True,
        role="admin",
        player_filter=[],
        provider_filter=[],
    )
    resolutions = 0

    async def resolve_and_replace_user(_auth: Any, _requested: str) -> Any:
        nonlocal resolutions
        resolutions += 1
        return impersonated_user

    cast("Any", adapter)._resolve_impersonated_user = resolve_and_replace_user
    ctx = SimpleNamespace(
        elicit=AsyncMock(return_value=SimpleNamespace(action="accept", data=True))
    )
    await adapter.call(
        "ma_api:config/providers/save",
        {
            "provider_domain": "demo",
            "instance_id": "demo--1",
            "values": {"token": "new-secret"},
            "user": "target",
        },
        response_mode="compact",
        fields=None,
        max_items=None,
        ctx=cast("Context", ctx),
    )
    assert inspections == 3
    assert resolutions == 3
    assert execution_user is fresh_user


@pytest.mark.parametrize(
    ("target_user_id", "final_impersonate_scope", "permitted"),
    [
        ("target", False, False),
        ("same-user", False, True),
        ("", True, False),
    ],
)
async def test_final_impersonation_authority_uses_final_caller_and_target_identity(
    target_user_id: str,
    final_impersonate_scope: bool,
    permitted: bool,
) -> None:
    """Only identified same-user or final-scope-authorized cross-user calls execute."""
    called = False
    caller = SimpleNamespace(
        user_id="same-user",
        enabled=True,
        role="user",
        player_filter=[],
        provider_filter=[],
    )

    async def save(
        provider_domain: str,
        values: dict[str, Any],
        instance_id: str | None = None,
    ) -> None:
        nonlocal called
        del provider_domain, values, instance_id
        called = True

    token = AccessToken(token="config", client_id="id-config", scopes=[])
    adapter = _adapter(
        [
            _handler(
                "config/providers/save",
                save,
                "config.providers.write",
                allow_impersonation=True,
            )
        ],
        current_token=[token],
        policies={
            "config": _custom(
                config__write__provider=PolicyMode.ALLOW,
                config__write__secret=PolicyMode.ALLOW,
            )
        },
        user=caller,
    )
    adapter.mass.config.get_provider_config_entries = AsyncMock(
        return_value=[ConfigEntry(key="token", type=ConfigEntryType.SECURE_STRING, label="Token")]
    )
    target = SimpleNamespace(
        user_id=target_user_id,
        enabled=True,
        role="user",
        player_filter=[],
        provider_filter=[],
    )
    resolutions = 0
    impersonate_scope = True

    def check_final_scope(_user: Any, scope: Scope) -> bool:
        if scope is Scope.USERS_IMPERSONATE:
            return impersonate_scope
        return True

    adapter._scope_checker = check_final_scope

    async def resolve_and_change_scope(_auth: Any, _requested: str) -> Any:
        nonlocal resolutions, impersonate_scope
        resolutions += 1
        if resolutions == 3:
            impersonate_scope = final_impersonate_scope
        return target

    cast("Any", adapter)._resolve_impersonated_user = resolve_and_change_scope
    ctx = SimpleNamespace(
        elicit=AsyncMock(return_value=SimpleNamespace(action="accept", data=True))
    )
    call = adapter.call(
        "ma_api:config/providers/save",
        {
            "provider_domain": "demo",
            "instance_id": "demo--1",
            "values": {"token": "new-secret"},
            "user": target_user_id or "unknown-target",
        },
        response_mode="compact",
        fields=None,
        max_items=None,
        ctx=cast("Context", ctx),
    )

    if permitted:
        await call
    else:
        with pytest.raises(ToolError, match=r"\[execution_failed\]"):
            await call
    assert resolutions == 3
    assert called is permitted


async def test_final_revalidation_cannot_reuse_confirmation_for_a_new_capability() -> None:
    """A prompt for one capability cannot bless a different final Confirm requirement."""
    called = False

    async def save(
        provider_domain: str,
        values: dict[str, Any],
        instance_id: str | None = None,
    ) -> None:
        nonlocal called
        del provider_domain, values, instance_id
        called = True

    token = AccessToken(token="config", client_id="id-config", scopes=[])
    policies = {
        "config": _custom(
            config__write__provider=PolicyMode.ALLOW,
            config__write__secret=PolicyMode.CONFIRM,
        )
    }
    adapter = _adapter(
        [_handler("config/providers/save", save, "config.providers.write")],
        current_token=[token],
        policies=policies,
    )
    adapter.mass.config.get_provider_config_entries = AsyncMock(
        return_value=[ConfigEntry(key="token", type=ConfigEntryType.SECURE_STRING, label="Token")]
    )
    user = await adapter.mass.webserver.auth.authenticate_with_token("config")
    authentications = 0

    async def authenticate_and_swap_requirement(_bearer: str) -> Any:
        nonlocal authentications
        authentications += 1
        if authentications == 4:
            policies["config"] = _custom(
                config__write__provider=PolicyMode.CONFIRM,
                config__write__secret=PolicyMode.ALLOW,
            )
        return user

    adapter.mass.webserver.auth.authenticate_with_token = authenticate_and_swap_requirement
    ctx = SimpleNamespace(
        elicit=AsyncMock(return_value=SimpleNamespace(action="accept", data=True))
    )

    with pytest.raises(ToolError, match=r"\[confirmation_required\]"):
        await adapter.call(
            "ma_api:config/providers/save",
            {
                "provider_domain": "demo",
                "instance_id": "demo--1",
                "values": {"token": "new-secret"},
            },
            response_mode="compact",
            fields=None,
            max_items=None,
            ctx=cast("Context", ctx),
        )
    assert ctx.elicit.await_count == 1
    assert called is False


@pytest.mark.parametrize(
    ("flow_scope", "allowed_capability", "denied_capability"),
    [
        (
            "config.providers.write",
            Capability.CONFIG_WRITE_PLAYER,
            Capability.CONFIG_WRITE_PROVIDER,
        ),
        (
            "config.players.write",
            Capability.CONFIG_WRITE_PROVIDER,
            Capability.CONFIG_WRITE_PLAYER,
        ),
    ],
)
async def test_flow_abort_requires_its_exact_category(
    flow_scope: str,
    allowed_capability: Capability,
    denied_capability: Capability,
) -> None:
    """One allowed config category cannot abort a flow owned by the other."""
    called = False

    async def abort(flow_id: str) -> None:
        nonlocal called
        del flow_id
        called = True

    token = AccessToken(token="abort", client_id="id-abort", scopes=[])
    records: list[Any] = []
    adapter = _adapter(
        [_handler("config/flows/abort", abort, "config.providers.write")],
        current_token=[token],
        policies={
            "abort": policy_snapshot(
                PolicyProfile.CUSTOM,
                {allowed_capability: PolicyMode.ALLOW},
            )
        },
        audit_sink=records.append,
    )
    adapter.mass.config.get_setup_flow_required_scope = lambda _flow_id: flow_scope

    with pytest.raises(ToolError, match=r"\[not_found_or_forbidden\]"):
        await adapter.call(
            "ma_api:config/flows/abort",
            {"flow_id": "flow-1"},
            response_mode="compact",
            fields=None,
            max_items=None,
            ctx=MagicMock(),
        )
    assert called is False
    assert len(records) == 1
    assert records[0].capability == str(denied_capability)
    assert records[0].mode == "deny"


async def test_auth_off_uses_global_default_for_discovery_schema_and_execution() -> None:
    """Without request auth, all command surfaces share the global default policy."""
    called = False

    async def search() -> str:
        nonlocal called
        called = True
        return "ok"

    default = [_custom(query__library=PolicyMode.ALLOW)]
    mass = MagicMock()
    handler = _handler("music/search", search)
    mass.command_handlers = {handler.command: handler}
    adapter = DynamicAPIAdapter(
        mass,
        auth_required_provider=lambda: False,
        token_provider=lambda: None,
        scope_checker=lambda _user, _scope: True,
        policy_provider=lambda _bearer: default[0],
        default_policy_provider=lambda: default[0],
    )
    service = MetaDiscoveryService(adapter)

    page = await service.discover("search")
    assert page["items"][0]["policy_mode"] == "allow"
    schema = await service.get_schema("ma_api:music/search")
    assert schema["policy_mode"] == "allow"
    await adapter.call(
        "ma_api:music/search",
        {},
        response_mode="compact",
        fields=None,
        max_items=None,
        ctx=MagicMock(),
    )
    assert called is True

    default[0] = _custom()
    assert await adapter.visible_entries() == []


async def test_token_identity_revocation_during_confirmation_blocks_execution() -> None:
    """A revoked/replaced exact token cannot execute after an accepted prompt."""
    called = False

    async def search() -> None:
        nonlocal called
        called = True

    token = AccessToken(token="confirm", client_id="id-confirm", scopes=[])
    adapter = _adapter(
        [_handler("music/search", search)],
        current_token=[token],
        policies={"confirm": _custom(query__library=PolicyMode.CONFIRM)},
    )

    async def accept_then_replace(_prompt: str, *, response_type: type[bool]) -> Any:
        del response_type
        adapter.mass.webserver.auth.get_token_id_from_token = AsyncMock(return_value="replacement")
        return SimpleNamespace(action="accept", data=True)

    with pytest.raises(ToolError, match="Authentication is required"):
        await adapter.call(
            "ma_api:music/search",
            {},
            response_mode="compact",
            fields=None,
            max_items=None,
            ctx=cast("Context", SimpleNamespace(elicit=accept_then_replace)),
        )
    assert called is False


async def test_lookup_failure_identity_recovery_during_confirmation_fails_closed() -> None:
    """A lookup-failure request cannot adopt a newly resolved token identity mid-call."""
    called = False

    async def search() -> None:
        nonlocal called
        called = True

    token = AccessToken(
        token="lookup-failure",
        client_id=LOOKUP_FAILURE_CLIENT_ID,
        scopes=[],
    )
    adapter = _adapter(
        [_handler("music/search", search)],
        current_token=[token],
        policies={"lookup-failure": _custom(query__library=PolicyMode.CONFIRM)},
    )
    adapter._identity_provider = lambda _bearer: None
    adapter.mass.webserver.auth.get_token_id_from_token = AsyncMock(
        side_effect=RuntimeError("lookup unavailable")
    )

    async def accept_then_recover(_prompt: str, *, response_type: type[bool]) -> Any:
        del response_type
        adapter.mass.webserver.auth.get_token_id_from_token = AsyncMock(return_value="now-known")
        return SimpleNamespace(action="accept", data=True)

    with pytest.raises(ToolError, match="Authentication is required"):
        await adapter.call(
            "ma_api:music/search",
            {},
            response_mode="compact",
            fields=None,
            max_items=None,
            ctx=cast("Context", SimpleNamespace(elicit=accept_then_recover)),
        )
    assert called is False


async def test_alternative_capability_catalog_mode_is_conservative() -> None:
    """Any prompt-capable setup-flow branch makes the shared schema confirm."""

    async def submit(flow_id: str, values: dict[str, Any]) -> None:
        del flow_id, values

    token = AccessToken(token="flows", client_id="id-flows", scopes=[])
    adapter = _adapter(
        [_handler("config/flows/submit", submit, "config.providers.write")],
        current_token=[token],
        policies={
            "flows": _custom(
                config__write__provider=PolicyMode.CONFIRM,
                config__write__player=PolicyMode.ALLOW,
            )
        },
    )
    entry = (await adapter.visible_entries())[0]
    assert entry.policy_mode is PolicyMode.CONFIRM
