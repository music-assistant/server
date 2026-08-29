"""Command-specific target authorization declarations."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from fnmatch import fnmatchcase
from typing import Any

from music_assistant_models.enums import MediaType
from music_assistant_models.errors import InsufficientPermissions


class TargetKind(StrEnum):
    """Kinds of Music Assistant targets constrained by user filters."""

    PLAYER = "player"
    PLAYERS = "players"
    MUSIC_PROVIDER = "music_provider"
    MUSIC_PROVIDERS = "music_providers"
    MUSIC_REFERENCE = "music_reference"
    MUSIC_REFERENCES = "music_references"


@dataclass(frozen=True, slots=True)
class TargetRule:
    """One command-pattern and argument target classification."""

    command_pattern: str
    argument: str
    kind: TargetKind


@dataclass(frozen=True, slots=True)
class CollectionRule:
    """One command-pattern and the row identity used to hide listing results."""

    command_pattern: str
    kind: TargetKind
    row_attributes: tuple[str, ...]


_PLAYER_ROW_ATTRIBUTES = ("player_id", "queue_id")
_MUSIC_PROVIDER_ATTRIBUTES = (
    "provider_instance_id",
    "provider_instance",
    "provider_domain",
    "provider",
)

COLLECTION_RULES: tuple[CollectionRule, ...] = (
    CollectionRule("players/all", TargetKind.PLAYER, _PLAYER_ROW_ATTRIBUTES),
    CollectionRule("player_queues/all", TargetKind.PLAYER, _PLAYER_ROW_ATTRIBUTES),
    CollectionRule("music/search", TargetKind.MUSIC_PROVIDER, _MUSIC_PROVIDER_ATTRIBUTES),
    CollectionRule("music/*/library_items", TargetKind.MUSIC_PROVIDER, _MUSIC_PROVIDER_ATTRIBUTES),
    CollectionRule("music/browse", TargetKind.MUSIC_PROVIDER, _MUSIC_PROVIDER_ATTRIBUTES),
)

_SINGLE_ITEM_RULES = frozenset({"music/item", "music/item_by_uri", "music/get_library_item"})

TARGET_RULES: tuple[TargetRule, ...] = (
    # Player controller commands.
    TargetRule("players/*", "player_id", TargetKind.PLAYER),
    TargetRule("players/*", "target_player", TargetKind.PLAYER),
    TargetRule("players/*", "source_player", TargetKind.PLAYER),
    TargetRule("players/*", "child_player_ids", TargetKind.PLAYERS),
    TargetRule("players/*", "player_ids", TargetKind.PLAYERS),
    TargetRule("players/*", "player_ids_to_add", TargetKind.PLAYERS),
    TargetRule("players/*", "player_ids_to_remove", TargetKind.PLAYERS),
    TargetRule("players/*", "members", TargetKind.PLAYERS),
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
    TargetRule("audio_analysis/*", "provider_instance_id_or_domain", TargetKind.MUSIC_PROVIDER),
    # Provider identities embedded in typed music references.
    TargetRule("music/browse", "path", TargetKind.MUSIC_REFERENCE),
    TargetRule("music/item_by_uri", "uri", TargetKind.MUSIC_REFERENCE),
    TargetRule("music/verify_item_uri", "uri", TargetKind.MUSIC_REFERENCE),
    TargetRule("music/favorites/add_item", "item", TargetKind.MUSIC_REFERENCE),
    TargetRule("music/library/add_item", "item", TargetKind.MUSIC_REFERENCE),
    TargetRule("music/refresh_item", "media_item", TargetKind.MUSIC_REFERENCE),
    TargetRule("music/mark_played", "media_item", TargetKind.MUSIC_REFERENCE),
    TargetRule("music/mark_unplayed", "media_item", TargetKind.MUSIC_REFERENCE),
    TargetRule("music/add_provider_mapping", "mapping", TargetKind.MUSIC_REFERENCE),
    TargetRule("music/remove_provider_mapping", "mapping", TargetKind.MUSIC_REFERENCE),
    TargetRule("music/playlists/add_playlist_tracks", "uris", TargetKind.MUSIC_REFERENCES),
    TargetRule("player_queues/play_media", "media", TargetKind.MUSIC_REFERENCES),
    TargetRule("player_queues/play_media", "start_item", TargetKind.MUSIC_REFERENCE),
    TargetRule("metadata/update_metadata", "item", TargetKind.MUSIC_REFERENCE),
    TargetRule("metadata/get_track_lyrics", "track", TargetKind.MUSIC_REFERENCE),
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
        if rule.kind in _REFERENCE_KINDS:
            values = _reference_provider_ids(value)
        else:
            values = _target_values(value, sequence=rule.kind in _SEQUENCE_KINDS)
        if rule.kind in _PLAYER_KINDS:
            _enforce_allowed(values, getattr(user, "player_filter", None))
        else:
            _enforce_music_providers(
                mass,
                values,
                getattr(user, "provider_filter", None),
            )


def collection_rule(command: str) -> CollectionRule | None:
    """Return the listing-visibility declaration for one canonical command."""
    return next(
        (rule for rule in COLLECTION_RULES if fnmatchcase(command, rule.command_pattern)),
        None,
    )


def collection_row_allowed(user: Any, item: Any, *, kind: TargetKind) -> bool:
    """Return whether one listing row is visible for the current user."""
    if user is None or str(getattr(user, "role", "")).casefold() == "admin":
        return True
    allowed = _allowed_values(
        getattr(user, "player_filter" if kind in _PLAYER_KINDS else "provider_filter", None)
    )
    if allowed is None:
        return True
    attributes = _PLAYER_ROW_ATTRIBUTES if kind in _PLAYER_KINDS else _MUSIC_PROVIDER_ATTRIBUTES
    row_ids = _row_ids(item, attributes)
    return bool(
        row_ids & allowed
        or (kind not in _PLAYER_KINDS and bool(row_ids) and row_ids <= _INTERNAL_MUSIC_TARGETS)
    )


def filter_collection_result(user: Any, command: str, result: Any) -> Any:
    """
    Hide collection rows the current user is not allowed to see.

    :param user: Authenticated Music Assistant user.
    :param command: Canonical command that produced ``result``.
    :param result: Native command return value.
    """
    if user is None or str(getattr(user, "role", "")).casefold() == "admin":
        return result
    rule = collection_rule(command)
    if rule is None:
        if command in _SINGLE_ITEM_RULES and not collection_row_allowed(
            user, result, kind=TargetKind.MUSIC_PROVIDER
        ):
            return None
        return result
    allowed = _allowed_values(
        getattr(user, "player_filter" if rule.kind in _PLAYER_KINDS else "provider_filter", None)
    )
    if allowed is None:
        return result
    if isinstance(result, Mapping):
        return {
            key: (
                _filter_rows(value, allowed, rule.row_attributes)
                if isinstance(value, list | tuple)
                else value
            )
            for key, value in result.items()
        }
    return _filter_rows(result, allowed, rule.row_attributes)


_PLAYER_KINDS = frozenset({TargetKind.PLAYER, TargetKind.PLAYERS})
_SEQUENCE_KINDS = frozenset({TargetKind.PLAYERS, TargetKind.MUSIC_PROVIDERS})
_REFERENCE_KINDS = frozenset({TargetKind.MUSIC_REFERENCE, TargetKind.MUSIC_REFERENCES})
_INTERNAL_MUSIC_TARGETS = frozenset({"builtin", "database", "library"})


def _filter_rows(result: Any, allowed: set[str], attributes: tuple[str, ...]) -> Any:
    """Drop sequence rows whose declared identity is outside the allowlist."""
    if not isinstance(result, list | tuple):
        return result
    filtered = tuple(item for item in result if _row_ids(item, attributes) & allowed)
    return filtered if isinstance(result, tuple) else list(filtered)


def _row_ids(item: Any, attributes: tuple[str, ...]) -> set[str]:
    """Collect declared identifiers from the row and any provider mappings."""
    ids = _attribute_ids(item, attributes)
    mappings = (
        item.get("provider_mappings")
        if isinstance(item, Mapping)
        else getattr(item, "provider_mappings", None)
    ) or ()
    for mapping in mappings:
        ids.update(_attribute_ids(mapping, attributes))
    return ids


def _attribute_ids(value: Any, attributes: tuple[str, ...]) -> set[str]:
    """Return non-empty identifier strings for the declared attributes."""
    if isinstance(value, Mapping):
        return {
            str(candidate)
            for name in attributes
            if (candidate := value.get(name)) is not None and str(candidate)
        }
    return {
        str(candidate)
        for name in attributes
        if (candidate := getattr(value, name, None)) is not None and str(candidate)
    }


def _reference_provider_ids(value: Any) -> set[str]:
    """Collect provider ids from one declared URI or media-reference argument."""
    if isinstance(value, str):
        if value.startswith("https://open."):
            host_parts = value.split("/", 3)[2].split(".")
            return {host_parts[1]} if len(host_parts) > 2 and host_parts[1] else set()
        if value.startswith("https://tidal.com/browse/"):
            return {"tidal"}
        if value.startswith("https://music.apple.com/"):
            return {"apple_music"}
        if value.startswith(("https://www.deezer.com/", "https://deezer.com/")):
            return {"deezer"}
        if value.startswith(("http://", "https://", "rtsp://", "rtmp://")):
            return {"builtin"}
        provider, separator, _remainder = value.partition("://")
        if separator and provider:
            return {provider}
        parts = value.split(":")
        if len(parts) == 3 and all(parts):
            try:
                MediaType(parts[1])
            except ValueError:
                return set()
            return {parts[0]}
        return set()
    if isinstance(value, list | tuple | set | frozenset):
        return set().union(*(_reference_provider_ids(item) for item in value), set())

    providers = _attribute_ids(value, _MUSIC_PROVIDER_ATTRIBUTES)
    uri = value.get("uri") if isinstance(value, Mapping) else getattr(value, "uri", None)
    if isinstance(uri, str):
        providers.update(_reference_provider_ids(uri))
    mappings = (
        value.get("provider_mappings")
        if isinstance(value, Mapping)
        else getattr(value, "provider_mappings", None)
    )
    if isinstance(mappings, list | tuple | set | frozenset):
        for mapping in mappings:
            providers.update(_reference_provider_ids(mapping))
    return providers


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
        raise InsufficientPermissions("Command target is not permitted for the current user")


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
            raise InsufficientPermissions("Command target is not permitted for the current user")
