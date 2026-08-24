"""Command policy and config preflight contract tests."""

from __future__ import annotations

import ast
from pathlib import Path
from types import MethodType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, create_autospec

import pytest
from fastmcp.exceptions import ToolError
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

import music_assistant
from music_assistant.providers.fastmcp_server.capabilities import Capability
from music_assistant.providers.fastmcp_server.command_policy import (
    CommandDecision,
    preflight_command,
    resolve_command_policy,
)
from music_assistant.providers.fastmcp_server.command_profiles import (
    COMMAND_PROFILES,
    CommandProfile,
)
from music_assistant.providers.fastmcp_server.policy import (
    PolicyMode,
    PolicyProfile,
    policy_snapshot,
)


def _current_ma_provider_entries_autospec() -> Any:
    """Return an autospec generated from MA's installed provider-entry API source."""
    source_path = Path(music_assistant.__file__).parent / "controllers" / "config" / "providers.py"
    source_tree = ast.parse(source_path.read_text())
    provider_mixin = next(
        node
        for node in source_tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ProviderConfigMixin"
    )
    method = next(
        node
        for node in provider_mixin.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "get_provider_config_entries"
    )
    method.decorator_list = []
    module = ast.Module(
        body=[
            ast.ImportFrom(
                module="__future__",
                names=[ast.alias(name="annotations")],
                level=0,
            ),
            method,
        ],
        type_ignores=[],
    )
    namespace: dict[str, Any] = {}
    exec(compile(ast.fix_missing_locations(module), str(source_path), "exec"), namespace)  # noqa: S102
    return create_autospec(namespace[method.name], spec_set=True)


@pytest.mark.parametrize("command", ["player_queues/delete_item", "player_queues/clear"])
def test_direct_queue_deletes_require_only_the_delete_capability(command: str) -> None:
    """Queue deletion classification has no independent risk or confirmation gate."""
    decision = resolve_command_policy(command, "queues.control", profile=None)
    assert decision.required_capabilities == frozenset({str(Capability.DELETE_QUEUE)})
    assert decision.annotations == {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    }


def test_system_health_keeps_annotations_separate_from_capability() -> None:
    """Read-only behavior hints do not weaken a debug capability requirement."""
    decision = resolve_command_policy("fastmcp/debug/health", "system.read", profile=None)
    assert decision.required_capabilities == frozenset({str(Capability.DEBUG_PROVIDERS)})
    assert decision.annotations["readOnlyHint"] is True
    assert decision.annotations["destructiveHint"] is False
    assert decision.annotations["idempotentHint"] is True


@pytest.mark.parametrize(
    ("command", "capability"),
    [
        ("music/radios/radio_tracks", Capability.QUERY_LIBRARY),
        ("players/tts_engines", Capability.QUERY_PLAYERS),
    ],
)
def test_current_ma_read_commands_have_explicit_capabilities(
    command: str, capability: Capability
) -> None:
    """New authenticated MA readers remain visible without weakening fail-closed policy."""
    decision = resolve_command_policy(command, "read", profile=None)

    assert decision.hard_denied is False
    assert decision.required_capabilities == frozenset({str(capability)})
    assert decision.annotations == {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }


@pytest.mark.parametrize("scope", [None, "library.read", "system.read"])
def test_unknown_command_fails_closed_instead_of_inheriting_scope(scope: str | None) -> None:
    """Upstream scope metadata alone cannot classify an unknown command family."""
    decision = resolve_command_policy("future/new_command", scope, None)
    assert decision.hard_denied is True
    assert decision.required_capabilities == frozenset()


@pytest.mark.parametrize(
    ("command", "scope"),
    [
        ("music/future_command", "library.read"),
        ("player_queues/future_command", "queues.control"),
        ("config/core/future_command", "config.core.read"),
        ("players/cmd/future_command", "players.control"),
    ],
)
def test_unknown_descendant_of_known_family_fails_closed(command: str, scope: str) -> None:
    """A recognized family cannot classify an unpinned future command."""
    decision = resolve_command_policy(command, scope, None)
    assert decision.hard_denied is True
    assert decision.required_capabilities == frozenset()


@pytest.mark.parametrize("command", ["providers_elevated", "config/core_backup/read"])
def test_family_prefix_near_matches_fail_closed(command: str) -> None:
    """A command must match a complete family path segment, not only its text prefix."""
    decision = resolve_command_policy(command, "config.core.read", None)
    assert decision.hard_denied is True


def test_exact_policy_precedes_profile_and_family_policy() -> None:
    """An ergonomic profile cannot weaken an exact destructive capability."""
    profile = CommandProfile(
        command="player_queues/clear",
        operation_override="control",
        annotations={"destructiveHint": False},
    )
    decision = resolve_command_policy("player_queues/clear", "queues.control", profile)
    assert decision.required_capabilities == frozenset({str(Capability.DELETE_QUEUE)})
    assert decision.annotations["destructiveHint"] is True


def test_safe_queue_extension_keeps_destructive_annotation_and_capability() -> None:
    """The safe-removal extension is controlled only by delete:queue mode."""
    decision = resolve_command_policy("fastmcp/queue/remove_items_safe", "queues.control", None)
    assert decision.required_capabilities == frozenset({str(Capability.DELETE_QUEUE)})
    assert decision.annotations["destructiveHint"] is True


def test_player_queue_write_operations_require_edit_queue_permission() -> None:
    """Saving a queue is an edit, not an untagged write-scope escape hatch."""
    decision = resolve_command_policy("player_queues/save_as_playlist", "library.write", None)

    assert decision.required_capabilities == frozenset({str(Capability.EDIT_QUEUE)})


def test_required_and_alternative_capabilities_have_distinct_mode_semantics() -> None:
    """Required capabilities combine while an any-of path picks its least restrictive mode."""
    decision = CommandDecision(
        annotations={},
        required_capabilities=frozenset(
            {str(Capability.QUERY_LIBRARY), str(Capability.CONFIG_READ)}
        ),
        alternative_capabilities=frozenset(
            {str(Capability.CONFIG_WRITE_PROVIDER), str(Capability.CONFIG_WRITE_PLAYER)}
        ),
    )
    snapshot = policy_snapshot(
        PolicyProfile.CUSTOM,
        {
            Capability.QUERY_LIBRARY: PolicyMode.ALLOW,
            Capability.CONFIG_READ: PolicyMode.CONFIRM,
            Capability.CONFIG_WRITE_PROVIDER: PolicyMode.DENY,
            Capability.CONFIG_WRITE_PLAYER: PolicyMode.ALLOW,
        },
    )
    assert decision.effective_mode(snapshot) is PolicyMode.CONFIRM


def test_provider_reload_uses_config_mode_without_mandatory_confirmation() -> None:
    """Native provider reload has no classifier-owned confirmation behavior."""
    decision = resolve_command_policy("config/providers/reload", "config.providers.write", None)

    assert decision.required_capabilities == frozenset({str(Capability.CONFIG_WRITE_PROVIDER)})
    assert decision.annotations == {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    }


@pytest.mark.parametrize(
    "command",
    [
        "auth/token/create",
        "auth/future_command",
        "dashboard/register",
        "dashboard/unregister",
    ],
)
def test_auth_and_dashboard_families_are_explicitly_hard_denied(command: str) -> None:
    """Credential and dashboard transport families remain unavailable under every policy."""
    decision = resolve_command_policy(command, "system.all", None)
    assert decision.hard_denied is True
    assert decision.required_capabilities == frozenset()


def test_nonregistration_dashboard_commands_require_system_admin() -> None:
    """Dashboard operations outside registration remain system-admin classified."""
    decision = resolve_command_policy("dashboard/show", "users.invite", None)
    assert decision.hard_denied is False
    assert decision.required_capabilities == frozenset({str(Capability.SYSTEM_ADMIN)})


@pytest.mark.parametrize("command", ["audio_analysis/coverage", "logging/get", "tasks/list"])
def test_system_command_families_require_system_admin(command: str) -> None:
    """System commands are decided solely by the system:admin capability mode."""
    decision = resolve_command_policy(command, "system.read", None)
    assert decision.hard_denied is False
    assert decision.required_capabilities == frozenset({str(Capability.SYSTEM_ADMIN)})


@pytest.mark.parametrize(
    ("command", "scope", "capability"),
    [
        ("players/cmd/play", "players.control", Capability.CONTROL_PLAYBACK),
        ("players/cmd/pause", "players.control", Capability.CONTROL_PLAYBACK),
        ("players/cmd/stop", "players.control", Capability.CONTROL_PLAYBACK),
        ("players/cmd/seek", "players.control", Capability.CONTROL_PLAYBACK),
        ("players/cmd/shuffle", "players.control", Capability.CONTROL_PLAYBACK),
        ("players/cmd/repeat", "players.control", Capability.CONTROL_PLAYBACK),
        ("player_queues/skip", "queues.control", Capability.CONTROL_PLAYBACK),
        ("player_queues/play_media", "queues.control", Capability.CONTROL_PLAYBACK),
        ("player_queues/play_index", "queues.control", Capability.CONTROL_PLAYBACK),
        ("players/cmd/play_announcement", "players.control", Capability.CONTROL_MEDIA),
        ("music/mark_played", "library.write", Capability.CONTROL_MEDIA),
        ("music/mark_unplayed", "library.write", Capability.CONTROL_MEDIA),
        ("players/cmd/volume_set", "players.control", Capability.CONTROL_VOLUME),
        ("players/cmd/group_volume", "players.control", Capability.CONTROL_VOLUME),
        ("players/cmd/group_volume_mute", "players.control", Capability.CONTROL_VOLUME),
    ],
)
def test_fine_grained_control_commands_use_their_named_capability(
    command: str,
    scope: str,
    capability: Capability,
) -> None:
    """Playback, media, and volume controls cannot bypass their Custom-policy mode."""
    decision = resolve_command_policy(command, scope, COMMAND_PROFILES.get(command))
    assert decision.required_capabilities == frozenset({str(capability)})


@pytest.mark.parametrize(
    ("command", "scope", "capability"),
    [
        ("music/search", "library.read", Capability.QUERY_LIBRARY),
        ("music/playlists/create_playlist", "library.write", Capability.EDIT_PLAYLISTS),
        (
            "music/playlists/remove_playlist_tracks",
            "library.write",
            Capability.DELETE_PLAYLISTS,
        ),
        ("players/cmd/volume_set", "players.control", Capability.CONTROL_VOLUME),
        ("player_queues/items", "queues.read", Capability.QUERY_QUEUE),
        ("config/core/save", "config.core.write", Capability.CONFIG_WRITE_CORE),
    ],
)
def test_longest_family_policy_assigns_required_capability(
    command: str, scope: str, capability: Capability
) -> None:
    """Family policy selects the narrow stable capability for each operation."""
    decision = resolve_command_policy(command, scope, None)
    assert decision.required_capabilities == frozenset({str(capability)})


def test_config_preflight_metadata_represents_alternatives_and_secret_escalation() -> None:
    """Task 3 can resolve setup-flow alternatives and secret writes per request."""
    flow = resolve_command_policy("config/flows/submit", None, None)
    save = resolve_command_policy("config/providers/save", "config.providers.write", None)
    assert flow.alternative_capabilities == frozenset(
        {str(Capability.CONFIG_WRITE_PROVIDER), str(Capability.CONFIG_WRITE_PLAYER)}
    )
    assert flow.secret_capability == str(Capability.CONFIG_WRITE_SECRET)
    assert save.secret_capability == str(Capability.CONFIG_WRITE_SECRET)


def _config_mass() -> SimpleNamespace:
    entries = [
        ConfigEntry(key="name", type=ConfigEntryType.STRING, label="Name"),
        ConfigEntry(key="token", type=ConfigEntryType.SECURE_STRING, label="Token"),
    ]
    config = SimpleNamespace(
        get_provider_config_entries=AsyncMock(return_value=entries),
        get_core_config_entries=AsyncMock(return_value=entries),
        get_player_config_entries=AsyncMock(return_value=entries),
    )
    return SimpleNamespace(config=config)


def _flow_mass(
    scope: str | None,
    entries: list[ConfigEntry],
) -> SimpleNamespace:
    """Build the current MA setup-flow API surface needed by request preflight."""
    config = SimpleNamespace(
        get_setup_flow_required_scope=lambda _flow_id: scope,
        get_setup_flow=AsyncMock(return_value=SimpleNamespace(entries=entries)),
    )
    return SimpleNamespace(config=config)


async def test_secure_config_preflight_requires_independent_secret_tag() -> None:
    """Generic provider config-write permission cannot authorize a secret value."""
    mass = _config_mass()
    decision = resolve_command_policy("config/providers/save", "config.providers.write", None)
    arguments = {
        "provider_domain": "demo",
        "instance_id": "demo--1",
        "values": {"token": "secret"},
    }
    preflight = await preflight_command(mass, decision, arguments)
    assert preflight.additional_required == frozenset({str(Capability.CONFIG_WRITE_SECRET)})


async def test_nonsecret_config_preflight_needs_no_secret_tag() -> None:
    """Ordinary config writes remain authorized by their existing family toggle."""
    mass = _config_mass()
    decision = resolve_command_policy("config/providers/save", "config.providers.write", None)
    await preflight_command(
        mass,
        decision,
        {
            "provider_domain": "demo",
            "instance_id": "demo--1",
            "values": {"name": "Kitchen"},
        },
    )


async def test_provider_secure_config_read_uses_current_ma_entries_signature() -> None:
    """Provider secure reads use MA's instance-ID-only schema lookup contract."""
    entries = [ConfigEntry(key="token", type=ConfigEntryType.SECURE_STRING, label="Token")]
    getter = _current_ma_provider_entries_autospec()
    getter.return_value = entries
    config = SimpleNamespace()
    config.get_provider_config_entries = MethodType(getter, config)
    decision = resolve_command_policy("config/providers/get_value", "config.read", None)

    preflight = await preflight_command(
        SimpleNamespace(config=config), decision, {"instance_id": "demo--1", "key": "token"}
    )

    assert preflight.secure_config_value is True
    getter.assert_awaited_once_with(config, "demo--1")


@pytest.mark.parametrize(
    ("command", "arguments", "getter_name", "getter_arguments"),
    [
        (
            "config/core/get_value",
            {"domain": "webserver", "key": "token"},
            "get_core_config_entries",
            ("webserver",),
        ),
        (
            "config/players/get_value",
            {"player_id": "kitchen", "key": "token"},
            "get_player_config_entries",
            ("kitchen",),
        ),
    ],
)
async def test_secure_config_value_reads_retain_secure_preflight_state(
    command: str,
    arguments: dict[str, str],
    getter_name: str,
    getter_arguments: tuple[str, ...],
) -> None:
    """Every config value family masks keys declared as SECURE_STRING."""
    entries = [ConfigEntry(key="token", type=ConfigEntryType.SECURE_STRING, label="Token")]
    config = SimpleNamespace(
        get_core_config_entries=AsyncMock(return_value=entries),
        get_player_config_entries=AsyncMock(return_value=entries),
    )
    decision = resolve_command_policy(command, "config.read", None)

    preflight = await preflight_command(SimpleNamespace(config=config), decision, arguments)

    assert preflight.secure_config_value is True
    getter = getattr(config, getter_name)
    getter.assert_awaited_once_with(*getter_arguments)


async def test_nonsecure_config_value_read_retains_nonsecure_preflight_state() -> None:
    """Ordinary config values pass through unchanged."""
    config = SimpleNamespace(
        get_core_config_entries=AsyncMock(
            return_value=[ConfigEntry(key="name", type=ConfigEntryType.STRING, label="Name")]
        )
    )
    decision = resolve_command_policy("config/core/get_value", "config.core.read", None)

    preflight = await preflight_command(
        SimpleNamespace(config=config), decision, {"domain": "webserver", "key": "name"}
    )

    assert preflight.secure_config_value is False


async def test_runtime_string_secure_type_is_classified_secure() -> None:
    """Runtime string enum values cannot bypass secure-value masking."""
    runtime_type: Any = "secure_string"
    config = SimpleNamespace(
        get_core_config_entries=AsyncMock(
            return_value=[ConfigEntry(key="token", type=runtime_type, label="Token")]
        )
    )
    decision = resolve_command_policy("config/core/get_value", "config.core.read", None)

    preflight = await preflight_command(
        SimpleNamespace(config=config), decision, {"domain": "webserver", "key": "token"}
    )

    assert preflight.secure_config_value is True


async def test_unknown_runtime_config_type_fails_closed() -> None:
    """Unknown runtime config-entry types cannot be treated as nonsecure."""
    runtime_type: Any = "future-secret"
    config = SimpleNamespace(
        get_core_config_entries=AsyncMock(
            return_value=[ConfigEntry(key="token", type=runtime_type, label="Token")]
        )
    )
    decision = resolve_command_policy("config/core/get_value", "config.core.read", None)

    with pytest.raises(ToolError, match="Unable to classify config value"):
        await preflight_command(
            SimpleNamespace(config=config), decision, {"domain": "webserver", "key": "token"}
        )


async def test_unknown_config_value_key_fails_closed() -> None:
    """An unrecognized key cannot bypass secure-value classification."""
    config = SimpleNamespace(get_core_config_entries=AsyncMock(return_value=[]))
    decision = resolve_command_policy("config/core/get_value", "config.core.read", None)

    with pytest.raises(ToolError, match="Unable to classify config value"):
        await preflight_command(
            SimpleNamespace(config=config),
            decision,
            {"domain": "webserver", "key": "future-secret"},
        )


async def test_config_value_schema_failure_fails_closed() -> None:
    """Schema lookup failures cannot expose an unclassified value."""
    config = SimpleNamespace(
        get_player_config_entries=AsyncMock(side_effect=RuntimeError("player disappeared"))
    )
    decision = resolve_command_policy("config/players/get_value", "config.players.read", None)

    with pytest.raises(ToolError, match="Unable to classify config value"):
        await preflight_command(
            SimpleNamespace(config=config), decision, {"player_id": "missing", "key": "token"}
        )


async def test_secret_config_preflight_accepts_explicit_secret_tag() -> None:
    """The dedicated secret toggle authorizes secure config values."""
    mass = _config_mass()
    decision = resolve_command_policy("config/providers/save", "config.providers.write", None)
    await preflight_command(
        mass,
        decision,
        {
            "provider_domain": "demo",
            "instance_id": "demo--1",
            "values": {"token": "secret"},
        },
    )


async def test_provider_setup_flow_secret_requires_secret_tag() -> None:
    """A provider flow cannot submit a secure value without the orthogonal capability."""
    mass = _flow_mass(
        "config.providers.write",
        [ConfigEntry(key="token", type=ConfigEntryType.SECURE_STRING, label="Token")],
    )
    decision = resolve_command_policy("config/flows/submit", None, None)

    preflight = await preflight_command(
        mass, decision, {"flow_id": "provider-flow", "values": {"token": "secret"}}
    )
    assert preflight.additional_required == frozenset(
        {str(Capability.CONFIG_WRITE_PROVIDER), str(Capability.CONFIG_WRITE_SECRET)}
    )


async def test_provider_setup_flow_secret_accepts_provider_and_secret_tags() -> None:
    """A provider flow accepts secure values with both required permissions."""
    mass = _flow_mass(
        "config.providers.write",
        [ConfigEntry(key="token", type=ConfigEntryType.SECURE_STRING, label="Token")],
    )
    decision = resolve_command_policy("config/flows/submit", None, None)

    await preflight_command(
        mass, decision, {"flow_id": "provider-flow", "values": {"token": "secret"}}
    )


async def test_player_setup_flow_allows_player_only_nonsecret_write() -> None:
    """A player flow needs its own category capability, not the provider category."""
    mass = _flow_mass(
        "config.players.write",
        [ConfigEntry(key="name", type=ConfigEntryType.STRING, label="Name")],
    )
    decision = resolve_command_policy("config/flows/submit", None, None)

    await preflight_command(
        mass, decision, {"flow_id": "player-flow", "values": {"name": "Kitchen"}}
    )


async def test_setup_flow_rejects_the_wrong_config_category() -> None:
    """Provider flow submission cannot use the player-write permission."""
    mass = _flow_mass(
        "config.providers.write",
        [ConfigEntry(key="name", type=ConfigEntryType.STRING, label="Name")],
    )
    decision = resolve_command_policy("config/flows/submit", None, None)

    preflight = await preflight_command(
        mass, decision, {"flow_id": "provider-flow", "values": {"name": "Kitchen"}}
    )
    assert preflight.additional_required == frozenset({str(Capability.CONFIG_WRITE_PROVIDER)})


async def test_unknown_setup_flow_fails_closed() -> None:
    """A missing flow scope cannot become an unguarded config write."""
    mass = _flow_mass(None, [])
    decision = resolve_command_policy("config/flows/submit", None, None)

    with pytest.raises(ToolError, match="setup flow"):
        await preflight_command(mass, decision, {"flow_id": "missing-flow", "values": {}})
