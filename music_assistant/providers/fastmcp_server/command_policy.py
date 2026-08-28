"""Capability classification, annotations, and request-dependent preflight."""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fastmcp.exceptions import ToolError
from music_assistant_models.constants import SECURE_STRING_SUBSTITUTE
from music_assistant_models.enums import ConfigEntryType

from .capabilities import Capability
from .config_io.secret_handler import is_secret_key
from .policy import PolicyMode, PolicySnapshot, combine_policy_modes

if TYPE_CHECKING:
    from .command_profiles import CommandProfile


@dataclass(frozen=True, slots=True)
class CommandDecision:
    """Capability and behavior classification for one canonical command."""

    annotations: Mapping[str, bool]
    required_capabilities: frozenset[str] = frozenset()
    preflight: str | None = None
    alternative_capabilities: frozenset[str] = frozenset()
    secret_capability: str | None = None
    hard_denied: bool = False

    def effective_mode(
        self,
        policy: PolicySnapshot,
        additional_required: frozenset[str] = frozenset(),
    ) -> PolicyMode:
        """Resolve fixed, request-derived, and any-of capability modes."""
        if self.hard_denied:
            return PolicyMode.DENY
        required = self.required_capabilities | additional_required
        modes = [policy.mode(capability) for capability in required]
        if self.alternative_capabilities:
            alternatives = [policy.mode(capability) for capability in self.alternative_capabilities]
            modes.append(
                PolicyMode.ALLOW
                if PolicyMode.ALLOW in alternatives
                else PolicyMode.CONFIRM
                if PolicyMode.CONFIRM in alternatives
                else PolicyMode.DENY
            )
        return combine_policy_modes(modes)


@dataclass(frozen=True, slots=True)
class CommandPreflight:
    """State retained between command authorization and result sanitization."""

    secure_config_value: bool | None = None
    additional_required: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class FamilyPolicy:
    """Capability columns and annotation behavior for a command prefix."""

    prefix: str
    capabilities: Mapping[str, Capability]
    readonly: bool = False


_READ_ANNOTATIONS = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}
_CONTROL_ANNOTATIONS = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": False,
    "openWorldHint": False,
}
_DESTRUCTIVE_ANNOTATIONS = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": False,
    "openWorldHint": False,
}
_SYSTEM_ANNOTATIONS = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": False,
    "openWorldHint": False,
}
_DESTRUCTIVE_VERBS = frozenset({"clear", "delete", "remove", "reset", "revoke"})
_SYSTEM_COMMAND_PREFIXES = ("audio_analysis/", "dashboard/", "logging/", "tasks/")
_HARD_DENIED_COMMANDS = frozenset(
    {"dashboard/register", "dashboard/unregister", "music/tracks/preview"}
)
_HARD_DENIED_PREFIXES = ("auth/",)

_PLAYBACK_COMMANDS = frozenset(
    {
        "player_queues/autoplay",
        "player_queues/crossfade",
        "player_queues/dont_stop_the_music",
        "player_queues/next",
        "player_queues/overlay",
        "player_queues/pause",
        "player_queues/play",
        "player_queues/play_index",
        "player_queues/play_media",
        "player_queues/play_pause",
        "player_queues/previous",
        "player_queues/repeat",
        "player_queues/resume",
        "player_queues/seek",
        "player_queues/set_playback_speed",
        "player_queues/shuffle",
        "player_queues/skip",
        "player_queues/stop",
        "player_queues/transfer",
        "players/cmd/next",
        "players/cmd/pause",
        "players/cmd/play",
        "players/cmd/play_pause",
        "players/cmd/previous",
        "players/cmd/repeat",
        "players/cmd/resume",
        "players/cmd/seek",
        "players/cmd/shuffle",
        "players/cmd/stop",
    }
)
_MEDIA_CONTROL_COMMANDS = frozenset(
    {
        "music/mark_played",
        "music/mark_unplayed",
        "players/cmd/play_announcement",
    }
)
_VOLUME_COMMANDS = frozenset(
    {
        "players/cmd/group_volume",
        "players/cmd/group_volume_down",
        "players/cmd/group_volume_mute",
        "players/cmd/group_volume_up",
        "players/cmd/volume_down",
        "players/cmd/volume_mute",
        "players/cmd/volume_set",
        "players/cmd/volume_up",
    }
)


def _family(
    prefix: str,
    *,
    read: Capability | None = None,
    control: Capability | None = None,
    write: Capability | None = None,
    delete: Capability | None = None,
    readonly: bool = False,
) -> FamilyPolicy:
    """Build one compact family-policy declaration."""
    capabilities = {
        operation: capability
        for operation, capability in (
            ("read", read),
            ("control", control),
            ("write", write),
            ("delete", delete),
        )
        if capability is not None
    }
    return FamilyPolicy(prefix, capabilities, readonly=readonly)


FAMILY_POLICIES = (
    _family(
        "music/playlists/",
        read=Capability.QUERY_LIBRARY,
        write=Capability.EDIT_PLAYLISTS,
        delete=Capability.DELETE_PLAYLISTS,
    ),
    _family(
        "music/favorites/",
        read=Capability.QUERY_LIBRARY,
        write=Capability.EDIT_FAVORITES,
        delete=Capability.DELETE_FAVORITES,
    ),
    _family(
        "music/",
        read=Capability.QUERY_LIBRARY,
        control=Capability.CONTROL_MEDIA,
        write=Capability.EDIT_LIBRARY,
        delete=Capability.DELETE_LIBRARY,
    ),
    _family("players/cmd/volume_", control=Capability.CONTROL_VOLUME),
    _family("players/cmd/", control=Capability.CONTROL_PLAYERS),
    _family(
        "players/sleep_timer/",
        read=Capability.QUERY_PLAYERS,
        control=Capability.CONTROL_PLAYBACK,
        delete=Capability.CONTROL_PLAYBACK,
    ),
    _family("players/", read=Capability.QUERY_PLAYERS),
    _family(
        "player_queues/",
        read=Capability.QUERY_QUEUE,
        control=Capability.EDIT_QUEUE,
        write=Capability.EDIT_QUEUE,
        delete=Capability.DELETE_QUEUE,
    ),
    _family("metadata/", read=Capability.QUERY_METADATA),
    _family(
        "config/providers",
        read=Capability.CONFIG_READ,
        write=Capability.CONFIG_WRITE_PROVIDER,
    ),
    _family("config/core", read=Capability.CONFIG_READ, write=Capability.CONFIG_WRITE_CORE),
    _family(
        "config/players",
        read=Capability.CONFIG_READ,
        write=Capability.CONFIG_WRITE_PLAYER,
    ),
    _family(
        "config/player_queues",
        read=Capability.CONFIG_READ,
        write=Capability.CONFIG_WRITE_PLAYER,
    ),
    _family(
        "config/dsp_presets/",
        read=Capability.CONFIG_READ,
        write=Capability.CONFIG_WRITE_PLAYER,
    ),
    _family(
        "config/dsp_irs/",
        read=Capability.CONFIG_READ,
        write=Capability.CONFIG_WRITE_PLAYER,
    ),
    _family(
        "diagnostics/",
        read=Capability.DEBUG_INSPECT,
        readonly=True,
    ),
    _family("providers", read=Capability.DEBUG_PROVIDERS),
)


def _readonly_debug(capability: Capability) -> CommandDecision:
    """Return a read-only command guarded by one debug capability."""
    return CommandDecision(
        _READ_ANNOTATIONS,
        frozenset({str(capability)}),
    )


def _destructive_write(capability: Capability) -> CommandDecision:
    """Return a destructive annotation with one write capability."""
    return CommandDecision(
        _DESTRUCTIVE_ANNOTATIONS,
        frozenset({str(capability)}),
    )


def _control(capability: Capability) -> CommandDecision:
    """Return a non-destructive control decision for one capability."""
    return CommandDecision(
        _CONTROL_ANNOTATIONS,
        frozenset({str(capability)}),
    )


EXACT_POLICIES: dict[str, CommandDecision] = {
    **{command: _control(Capability.CONTROL_PLAYBACK) for command in _PLAYBACK_COMMANDS},
    **{command: _control(Capability.CONTROL_MEDIA) for command in _MEDIA_CONTROL_COMMANDS},
    **{command: _control(Capability.CONTROL_VOLUME) for command in _VOLUME_COMMANDS},
    "music/radios/radio_tracks": CommandDecision(
        _READ_ANNOTATIONS,
        frozenset({str(Capability.QUERY_LIBRARY)}),
    ),
    "players/tts_engines": CommandDecision(
        _READ_ANNOTATIONS,
        frozenset({str(Capability.QUERY_PLAYERS)}),
    ),
    "player_queues/save_as_playlist": CommandDecision(
        _CONTROL_ANNOTATIONS,
        frozenset({str(Capability.EDIT_PLAYLISTS)}),
    ),
    "player_queues/delete_item": _destructive_write(Capability.DELETE_QUEUE),
    "player_queues/clear": _destructive_write(Capability.DELETE_QUEUE),
    "fastmcp/queue/remove_items_safe": _destructive_write(Capability.DELETE_QUEUE),
    "config/providers/reload": _destructive_write(Capability.CONFIG_WRITE_PROVIDER),
    "config/flows/submit": CommandDecision(
        _CONTROL_ANNOTATIONS,
        frozenset(),
        "config_flow_submit",
        frozenset({str(Capability.CONFIG_WRITE_PROVIDER), str(Capability.CONFIG_WRITE_PLAYER)}),
        str(Capability.CONFIG_WRITE_SECRET),
    ),
    "config/flows/get": CommandDecision(
        _READ_ANNOTATIONS,
        frozenset({str(Capability.CONFIG_READ)}),
    ),
    "config/flows/abort": CommandDecision(
        _DESTRUCTIVE_ANNOTATIONS,
        preflight="config_flow_abort",
        alternative_capabilities=frozenset(
            {str(Capability.CONFIG_WRITE_PROVIDER), str(Capability.CONFIG_WRITE_PLAYER)}
        ),
    ),
    "players/create_group_player": CommandDecision(
        _CONTROL_ANNOTATIONS,
        frozenset({str(Capability.CONFIG_WRITE_PLAYER)}),
    ),
    "players/remove_group_player": _destructive_write(Capability.CONFIG_WRITE_PLAYER),
    "players/remove": _destructive_write(Capability.CONFIG_WRITE_PLAYER),
    "players/add_currently_playing_to_favorites": CommandDecision(
        _CONTROL_ANNOTATIONS,
        frozenset({str(Capability.EDIT_FAVORITES)}),
    ),
    "metadata/set_default_preferred_language": CommandDecision(
        _CONTROL_ANNOTATIONS,
        frozenset({str(Capability.CONFIG_WRITE_CORE)}),
    ),
    "metadata/set_preferred_language": CommandDecision(
        _CONTROL_ANNOTATIONS,
        frozenset({str(Capability.EDIT_LIBRARY)}),
    ),
    "metadata/update_metadata": CommandDecision(
        _CONTROL_ANNOTATIONS,
        frozenset({str(Capability.EDIT_LIBRARY)}),
    ),
    "fastmcp/debug/tail_log": _readonly_debug(Capability.DEBUG_LOGS),
    "fastmcp/debug/log_stats": _readonly_debug(Capability.DEBUG_LOGS),
    "fastmcp/debug/recent_events": _readonly_debug(Capability.DEBUG_EVENTS),
    "fastmcp/debug/event_buffer_stats": _readonly_debug(Capability.DEBUG_EVENTS),
    "fastmcp/debug/health": _readonly_debug(Capability.DEBUG_PROVIDERS),
    "fastmcp/debug/routes": _readonly_debug(Capability.DEBUG_PROVIDERS),
    "fastmcp/debug/packages": _readonly_debug(Capability.DEBUG_PROVIDERS),
}


def resolve_command_policy(
    command: str, scope: Any, profile: CommandProfile | None
) -> CommandDecision:
    """
    Resolve behavior hints and capabilities for a command.

    :param command: Canonical Music Assistant command.
    :param scope: Live MA required-scope value or enum member.
    :param profile: Optional ergonomic command profile.
    """
    if command_is_hard_denied(command):
        return CommandDecision({}, hard_denied=True)
    if exact := EXACT_POLICIES.get(command):
        return exact

    if command.startswith(_SYSTEM_COMMAND_PREFIXES) or command in {
        "info",
        "translations/locales",
    }:
        return CommandDecision(
            _SYSTEM_ANNOTATIONS,
            frozenset({str(Capability.SYSTEM_ADMIN)}),
        )

    family = _matching_family(command)
    if family is None:
        return CommandDecision({}, hard_denied=True)
    operation = _operation(command, scope, profile, family)
    destructive = operation == "delete"
    annotations = dict(
        _READ_ANNOTATIONS if family.readonly else _annotations(operation, destructive=destructive)
    )
    if profile is not None:
        annotations.update(profile.annotations)
    required_capabilities = _required_capabilities(family, operation)
    if not required_capabilities:
        return CommandDecision(annotations, hard_denied=True)
    preflight = (
        "config_secret_read"
        if command
        in {
            "config/providers/get_value",
            "config/core/get_value",
            "config/players/get_value",
        }
        else "config_secret_write"
        if command
        in {
            "config/providers/save",
            "config/core/save",
            "config/players/save",
        }
        else None
    )
    return CommandDecision(
        annotations,
        required_capabilities,
        preflight,
        secret_capability=(
            str(Capability.CONFIG_WRITE_SECRET) if preflight == "config_secret_write" else None
        ),
    )


def command_is_hard_denied(command: str) -> bool:
    """Return whether a command belongs to an unconditional deny family."""
    return command in _HARD_DENIED_COMMANDS or command.startswith(_HARD_DENIED_PREFIXES)


async def preflight_command(
    mass: Any,
    decision: CommandDecision,
    arguments: Mapping[str, Any],
) -> CommandPreflight:
    """
    Enforce request-dependent guards before confirmation and execution.

    :param mass: Running Music Assistant instance.
    :param decision: Resolved command policy.
    :param arguments: Strictly parsed command arguments.
    """
    if decision.preflight == "config_secret_read":
        return CommandPreflight(secure_config_value=await _config_value_is_secure(mass, arguments))
    if decision.preflight == "config_secret_write":
        values = arguments.get("values")
        if not isinstance(values, Mapping):
            return CommandPreflight()
        getter_name, target = _config_entries_target(arguments)
        entries = getattr(mass.config, getter_name)(target)
        if inspect.isawaitable(entries):
            entries = await entries
        if any(is_secret_key(entries, str(key)) for key in values):
            return CommandPreflight(
                additional_required=frozenset({str(Capability.CONFIG_WRITE_SECRET)})
            )
    elif decision.preflight == "config_flow_submit":
        return await _preflight_setup_flow_submit(mass, arguments)
    elif decision.preflight == "config_flow_abort":
        return await _preflight_setup_flow_abort(mass, arguments)
    return CommandPreflight()


def revalidate_preflight_command_sync(
    mass: Any,
    decision: CommandDecision,
    arguments: Mapping[str, Any],
    preflight: CommandPreflight,
) -> CommandPreflight:
    """
    Reclassify request-dependent state synchronously after final authentication.

    A live getter that only returns an awaitable cannot prove that its earlier
    result survived the final authentication await. Such cases are classified
    conservatively: reads remain masked and writes require the secret
    capability. Setup-flow category is required to have a synchronous proof.
    """
    if decision.preflight == "config_secret_read":
        secure = _config_value_is_secure_sync(mass, arguments)
        return CommandPreflight(secure_config_value=True if secure is None else secure)
    if decision.preflight == "config_secret_write":
        values = arguments.get("values")
        if not isinstance(values, Mapping):
            return CommandPreflight()
        entries = _config_entries_sync(mass, arguments)
        requires_secret = entries is None or any(is_secret_key(entries, str(key)) for key in values)
        return CommandPreflight(
            additional_required=(
                frozenset({str(Capability.CONFIG_WRITE_SECRET)}) if requires_secret else frozenset()
            )
        )
    if decision.preflight == "config_flow_submit":
        return _revalidate_setup_flow_submit_sync(mass, arguments)
    if decision.preflight == "config_flow_abort":
        return _revalidate_setup_flow_abort_sync(mass, arguments)
    return preflight


async def postflight_command(
    mass: Any,
    decision: CommandDecision,
    arguments: Mapping[str, Any],
    preflight: CommandPreflight,
    result: Any,
) -> Any:
    """Sanitize one native command result after execution and before serialization."""
    if decision.preflight != "config_secret_read":
        return result
    secure_after_execution = await _config_value_is_secure(mass, arguments)
    if not preflight.secure_config_value and not secure_after_execution:
        return result
    return None if result is None else SECURE_STRING_SUBSTITUTE


def _matching_family(command: str) -> FamilyPolicy | None:
    """Return the longest matching family policy."""
    matches = (
        family
        for family in FAMILY_POLICIES
        if (
            command.startswith(family.prefix)
            if family.prefix.endswith(("/", "_"))
            else command == family.prefix or command.startswith(f"{family.prefix}/")
        )
    )
    return max(matches, key=lambda family: len(family.prefix), default=None)


def _operation(
    command: str,
    scope: Any,
    profile: CommandProfile | None,
    family: FamilyPolicy | None,
) -> str:
    """Resolve the operation column independently from MCP annotations."""
    parts = command.casefold().replace("-", "_").split("/")
    words = {word for part in parts for word in part.split("_")}
    if family is not None and family.readonly:
        return "read"
    if words & _DESTRUCTIVE_VERBS:
        return "delete"
    if profile is not None and profile.operation_override is not None:
        return profile.operation_override
    scope_operation = _scope_operation(scope)
    if scope_operation is not None:
        return scope_operation
    if family is not None and len(family.capabilities) == 1:
        return next(iter(family.capabilities))
    return "system"


def _scope_operation(scope: Any) -> str | None:
    """Map current MA scope metadata to an operation column."""
    value = str(getattr(scope, "value", scope) or "").casefold()
    if not value:
        return None
    if value.startswith("system."):
        return "system"
    if value.endswith(".control"):
        return "control"
    if value.endswith((".write", ".manage")):
        return "write"
    if value.endswith(".read"):
        return "read"
    return None


def _annotations(operation: str, *, destructive: bool) -> Mapping[str, bool]:
    """Return default behavior annotations without changing capabilities."""
    if destructive:
        return _DESTRUCTIVE_ANNOTATIONS
    if operation == "read":
        return _READ_ANNOTATIONS
    if operation in {"control", "write"}:
        return _CONTROL_ANNOTATIONS
    return _SYSTEM_ANNOTATIONS


def _required_capabilities(family: FamilyPolicy, operation: str) -> frozenset[str]:
    """Select the family capability for the resolved operation."""
    capability = family.capabilities.get(operation)
    if capability is None and operation == "delete":
        capability = family.capabilities.get("write")
    return frozenset({str(capability)}) if capability is not None else frozenset()


def _config_entries_target(arguments: Mapping[str, Any]) -> tuple[str, str]:
    """Resolve the live config-entry getter and target identifier."""
    if "provider_domain" in arguments:
        target = arguments.get("instance_id") or arguments["provider_domain"]
        return "get_provider_config_entries", str(target)
    if "domain" in arguments:
        return "get_core_config_entries", str(arguments["domain"])
    if "player_id" in arguments:
        return "get_player_config_entries", str(arguments["player_id"])
    raise ValueError("Config save arguments do not identify a target")


def _close_awaitable(value: Any) -> None:
    """Dispose an unawaited coroutine created only to test synchronous proof."""
    close = getattr(value, "close", None)
    if callable(close):
        close()


def _config_entries_sync(mass: Any, arguments: Mapping[str, Any]) -> Any | None:
    """Return live config entries only when the getter can prove state synchronously."""
    try:
        getter_name, target = _config_entries_target(arguments)
        entries = getattr(mass.config, getter_name)(target)
    except Exception:
        return None
    if inspect.isawaitable(entries):
        _close_awaitable(entries)
        return None
    return entries


def _config_value_is_secure_sync(
    mass: Any,
    arguments: Mapping[str, Any],
) -> bool | None:
    """Classify one value without awaiting, or return unknown for fail-closed masking."""
    key = arguments.get("key")
    if not isinstance(key, str) or not key:
        return None
    entries = _config_entries_sync(mass, arguments)
    if entries is None:
        return None
    try:
        entry = next((entry for entry in entries if entry.key == key), None)
        if entry is None:
            return None
        entry_type = ConfigEntryType(entry.type)
    except TypeError, ValueError:
        return None
    return (
        True
        if entry_type is ConfigEntryType.SECURE_STRING
        else None
        if entry_type is ConfigEntryType.UNKNOWN
        else False
    )


async def _config_value_is_secure(
    mass: Any,
    arguments: Mapping[str, Any],
) -> bool:
    """Return whether one live config value is declared secure."""
    key = arguments.get("key")
    if not isinstance(key, str) or not key:
        raise ToolError("Unable to classify config value")
    try:
        if isinstance(instance_id := arguments.get("instance_id"), str) and instance_id:
            entries = mass.config.get_provider_config_entries(instance_id)
        elif isinstance(domain := arguments.get("domain"), str) and domain:
            entries = mass.config.get_core_config_entries(domain)
        elif isinstance(player_id := arguments.get("player_id"), str) and player_id:
            entries = mass.config.get_player_config_entries(player_id)
        else:
            raise ValueError("Config value arguments do not identify a target")
        if inspect.isawaitable(entries):
            entries = await entries
        entry = next((entry for entry in entries if entry.key == key), None)
    except Exception as exc:
        raise ToolError("Unable to classify config value") from exc
    if entry is None:
        raise ToolError("Unable to classify config value")
    try:
        entry_type = ConfigEntryType(entry.type)
    except (TypeError, ValueError) as exc:
        raise ToolError("Unable to classify config value") from exc
    if entry_type is ConfigEntryType.UNKNOWN:
        raise ToolError("Unable to classify config value")
    return entry_type is ConfigEntryType.SECURE_STRING


async def _preflight_setup_flow_submit(
    mass: Any,
    arguments: Mapping[str, Any],
) -> CommandPreflight:
    """Authorize one live setup-flow submission and gate its secure fields."""
    flow_id = arguments.get("flow_id")
    values = arguments.get("values")
    if not isinstance(flow_id, str) or not flow_id or not isinstance(values, Mapping):
        raise ToolError("Invalid setup flow submission")
    get_scope = getattr(mass.config, "get_setup_flow_required_scope", None)
    get_flow = getattr(mass.config, "get_setup_flow", None)
    if not callable(get_scope) or not callable(get_flow):
        raise ToolError("Unable to authorize setup flow submission")
    scope = get_scope(flow_id)
    if inspect.isawaitable(scope):
        scope = await scope
    required_capability = _setup_flow_write_capability(scope)
    if required_capability is None:
        raise ToolError("Unknown setup flow or unsupported setup flow scope")
    try:
        step = get_flow(flow_id)
        if inspect.isawaitable(step):
            step = await step
    except Exception as exc:
        raise ToolError("Unable to inspect setup flow") from exc
    entries = getattr(step, "entries", None)
    if not isinstance(entries, list | tuple):
        raise ToolError("Malformed setup flow step")
    required = {str(required_capability)}
    if any(is_secret_key(entries, str(key)) for key in values):
        required.add(str(Capability.CONFIG_WRITE_SECRET))
    return CommandPreflight(additional_required=frozenset(required))


def _setup_flow_scope_sync(mass: Any, flow_id: str) -> Any:
    """Return a live setup-flow scope only when it is synchronously provable."""
    getter = getattr(mass.config, "get_setup_flow_required_scope", None)
    if not callable(getter):
        raise ToolError("Unable to authorize setup flow")
    scope = getter(flow_id)
    if inspect.isawaitable(scope):
        _close_awaitable(scope)
        raise ToolError("Unable to synchronously authorize setup flow")
    return scope


def _setup_flow_step_sync(mass: Any, flow_id: str) -> Any | None:
    """Read a current step synchronously, including MA's in-memory flow registry."""
    getter = getattr(mass.config, "get_setup_flow", None)
    if callable(getter):
        try:
            step = getter(flow_id)
        except Exception:
            step = None
        if inspect.isawaitable(step):
            _close_awaitable(step)
        elif step is not None:
            return step
    flows = getattr(mass.config, "_setup_flows", None)
    if isinstance(flows, Mapping) and (flow := flows.get(flow_id)) is not None:
        session = getattr(flow, "session", None)
        return getattr(session, "current_step", None)
    return None


def _revalidate_setup_flow_submit_sync(
    mass: Any,
    arguments: Mapping[str, Any],
) -> CommandPreflight:
    """Seal live flow category synchronously and conservatively classify secrets."""
    flow_id = arguments.get("flow_id")
    values = arguments.get("values")
    if not isinstance(flow_id, str) or not flow_id or not isinstance(values, Mapping):
        raise ToolError("Invalid setup flow submission")
    required_capability = _setup_flow_write_capability(_setup_flow_scope_sync(mass, flow_id))
    if required_capability is None:
        raise ToolError("Unknown setup flow or unsupported setup flow scope")
    required = {str(required_capability)}
    step = _setup_flow_step_sync(mass, flow_id)
    entries = getattr(step, "entries", None) if step is not None else None
    if not isinstance(entries, list | tuple) or any(
        is_secret_key(entries, str(key)) for key in values
    ):
        required.add(str(Capability.CONFIG_WRITE_SECRET))
    return CommandPreflight(additional_required=frozenset(required))


def _revalidate_setup_flow_abort_sync(
    mass: Any,
    arguments: Mapping[str, Any],
) -> CommandPreflight:
    """Seal the exact live category of one flow abort synchronously."""
    flow_id = arguments.get("flow_id")
    if not isinstance(flow_id, str) or not flow_id:
        raise ToolError("Invalid setup flow abort")
    required_capability = _setup_flow_write_capability(_setup_flow_scope_sync(mass, flow_id))
    if required_capability is None:
        raise ToolError("Unknown setup flow or unsupported setup flow scope")
    return CommandPreflight(additional_required=frozenset({str(required_capability)}))


async def _preflight_setup_flow_abort(
    mass: Any,
    arguments: Mapping[str, Any],
) -> CommandPreflight:
    """Classify an abort by the exact live setup-flow category."""
    flow_id = arguments.get("flow_id")
    if not isinstance(flow_id, str) or not flow_id:
        raise ToolError("Invalid setup flow abort")
    get_scope = getattr(mass.config, "get_setup_flow_required_scope", None)
    if not callable(get_scope):
        raise ToolError("Unable to authorize setup flow abort")
    scope = get_scope(flow_id)
    if inspect.isawaitable(scope):
        scope = await scope
    required_capability = _setup_flow_write_capability(scope)
    if required_capability is None:
        raise ToolError("Unknown setup flow or unsupported setup flow scope")
    return CommandPreflight(additional_required=frozenset({str(required_capability)}))


def _setup_flow_write_capability(scope: Any) -> Capability | None:
    """Map a current MA setup-flow scope to its one config write capability."""
    value = str(getattr(scope, "value", scope) or "").casefold()
    if value == "config.providers.write":
        return Capability.CONFIG_WRITE_PROVIDER
    if value == "config.players.write":
        return Capability.CONFIG_WRITE_PLAYER
    return None
