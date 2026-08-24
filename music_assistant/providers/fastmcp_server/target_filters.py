"""Command-specific target authorization declarations."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from fnmatch import fnmatchcase
from typing import Any

from fastmcp.exceptions import ToolError


class TargetKind(StrEnum):
    """Kinds of Music Assistant targets constrained by user filters."""

    PLAYER = "player"
    PLAYERS = "players"
    MUSIC_PROVIDER = "music_provider"
    MUSIC_PROVIDERS = "music_providers"


@dataclass(frozen=True, slots=True)
class TargetRule:
    """One command-pattern and argument target classification."""

    command_pattern: str
    argument: str
    kind: TargetKind


TARGET_RULES: tuple[TargetRule, ...] = (
    # Player controller commands.
    TargetRule("players/*", "player_id", TargetKind.PLAYER),
    TargetRule("players/*", "target_player", TargetKind.PLAYER),
    TargetRule("players/*", "source_player", TargetKind.PLAYER),
    TargetRule("players/*", "child_player_ids", TargetKind.PLAYERS),
    TargetRule("players/*", "player_ids", TargetKind.PLAYERS),
    TargetRule("players/*", "player_ids_to_add", TargetKind.PLAYERS),
    TargetRule("players/*", "player_ids_to_remove", TargetKind.PLAYERS),
    # Queue ids are player ids in Music Assistant's authorization model.
    TargetRule("player_queues/*", "player_id", TargetKind.PLAYER),
    TargetRule("player_queues/*", "queue_id", TargetKind.PLAYER),
    TargetRule("player_queues/*", "source_queue_id", TargetKind.PLAYER),
    TargetRule("player_queues/*", "target_queue_id", TargetKind.PLAYER),
    TargetRule("player_queues/*", "queue_ids", TargetKind.PLAYERS),
    # Configuration and provider-owned commands that directly target players.
    TargetRule("config/players/*", "player_id", TargetKind.PLAYER),
    TargetRule("config/player_queues/*", "queue_id", TargetKind.PLAYER),
    TargetRule("config/dsp_presets/*", "player_id", TargetKind.PLAYER),
    TargetRule("config/dsp_irs/*", "player_id", TargetKind.PLAYER),
    TargetRule("fastmcp/*", "player_id", TargetKind.PLAYER),
    TargetRule("fastmcp/*", "queue_id", TargetKind.PLAYER),
    # Only music commands use the user's music-provider filter. In particular,
    # config/providers/* may target player, metadata, plugin, and core providers.
    TargetRule("music/*", "instance_id", TargetKind.MUSIC_PROVIDER),
    TargetRule("music/*", "provider", TargetKind.MUSIC_PROVIDER),
    TargetRule("music/*", "provider_instance_id", TargetKind.MUSIC_PROVIDER),
    TargetRule("music/*", "provider_instance_or_domain", TargetKind.MUSIC_PROVIDER),
    TargetRule("music/*", "provider_instance_id_or_domain", TargetKind.MUSIC_PROVIDER),
    TargetRule("music/*", "providers", TargetKind.MUSIC_PROVIDERS),
    TargetRule("music/*", "provider_instance_ids", TargetKind.MUSIC_PROVIDERS),
)


def target_rule(command: str, argument: str) -> TargetRule | None:
    """Return the declaration for one canonical command argument."""
    return next(
        (
            rule
            for rule in TARGET_RULES
            if rule.argument == argument and fnmatchcase(command, rule.command_pattern)
        ),
        None,
    )


def enforce_target_filters(
    mass: Any,
    user: Any,
    command: str,
    arguments: Mapping[str, Any],
) -> None:
    """Enforce current user target filters for one canonical command."""
    if str(getattr(user, "role", "")).casefold() == "admin":
        return
    for argument, value in arguments.items():
        if value is None or (rule := target_rule(command, argument)) is None:
            continue
        values = _target_values(value, sequence=rule.kind in _SEQUENCE_KINDS)
        if rule.kind in _PLAYER_KINDS:
            _enforce_allowed(values, getattr(user, "player_filter", None))
        else:
            _enforce_music_providers(
                mass,
                values,
                getattr(user, "provider_filter", None),
            )


def filter_collection_result(user: Any, command: str, result: Any) -> Any:
    """
    Hide collection rows the current user is not allowed to see.

    :param user: Authenticated Music Assistant user.
    :param command: Canonical command that produced ``result``.
    :param result: Native command return value.
    """
    if (
        user is None
        or command != "player_queues/all"
        or str(getattr(user, "role", "")).casefold() == "admin"
    ):
        return result
    allowed = _allowed_values(getattr(user, "player_filter", None))
    if allowed is None or not isinstance(result, list | tuple):
        return result
    filtered = tuple(
        item
        for item in result
        if str(getattr(item, "queue_id", getattr(item, "player_id", ""))) in allowed
    )
    return filtered if isinstance(result, tuple) else list(filtered)


_PLAYER_KINDS = frozenset({TargetKind.PLAYER, TargetKind.PLAYERS})
_SEQUENCE_KINDS = frozenset({TargetKind.PLAYERS, TargetKind.MUSIC_PROVIDERS})
_INTERNAL_MUSIC_TARGETS = frozenset({"builtin", "database", "library"})


def _target_values(value: Any, *, sequence: bool) -> set[str]:
    """Normalize one scalar or declared sequence without iterating text."""
    if not sequence or isinstance(value, str):
        return {str(value)}
    if isinstance(value, list | tuple | set | frozenset):
        return {str(item) for item in value}
    return {str(value)}


def _allowed_values(value: Any) -> set[str] | None:
    """Return a normalized active filter, or None for unrestricted users."""
    if not isinstance(value, list | tuple | set | frozenset) or not value:
        return None
    return {str(item) for item in value}


def _enforce_allowed(requested: set[str], configured: Any) -> None:
    """Reject requested identifiers outside one active allowlist."""
    allowed = _allowed_values(configured)
    if allowed is not None and not requested.issubset(allowed):
        raise ToolError("Command target is not permitted for the current user")


def _resolve_music_provider(mass: Any, submitted: str) -> Any:
    """Resolve one submitted target without aliasing an unavailable instance."""
    exact = mass.get_provider(submitted, return_unavailable=True)
    if exact is not None and str(getattr(exact, "instance_id", "")) == submitted:
        return exact
    return mass.get_provider(submitted)


def _enforce_music_providers(mass: Any, requested: set[str], configured: Any) -> None:
    """Resolve submitted domains and compare actual music-provider instance ids."""
    allowed = _allowed_values(configured)
    if allowed is None:
        return
    for submitted in requested:
        if submitted in _INTERNAL_MUSIC_TARGETS or submitted in allowed:
            continue
        provider = _resolve_music_provider(mass, submitted)
        provider_type = getattr(getattr(provider, "type", None), "value", None)
        if (
            provider is None
            or str(provider_type).casefold() != "music"
            or str(getattr(provider, "instance_id", "")) not in allowed
        ):
            raise ToolError("Command target is not permitted for the current user")
