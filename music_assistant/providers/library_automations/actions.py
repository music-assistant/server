"""Action registry and execution logic for the Library Automations plugin."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import MediaType
from music_assistant_models.errors import InvalidDataError, MusicAssistantError

from music_assistant.helpers.security import is_safe_name
from music_assistant.providers.library_automations.models import (
    ACTION_ADD_TO_PLAYLIST,
    ACTION_REMOVE_FROM_LIBRARY,
    ACTION_REMOVE_FROM_PLAYLIST,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from music_assistant_models.media_items import MediaItemType

    from music_assistant.providers.library_automations import LibraryAutomationsProvider
    from music_assistant.providers.library_automations.models import AutomationRule


@dataclass(frozen=True)
class ActionDefinition:
    """Static metadata describing an action type, surfaced via list_action_types()."""

    id: str
    label: str
    description: str


ACTION_TYPES: dict[str, ActionDefinition] = {
    ACTION_ADD_TO_PLAYLIST: ActionDefinition(
        id=ACTION_ADD_TO_PLAYLIST,
        label="Add to playlist",
        description=(
            "Add the triggering track (or all tracks of a triggering album/artist) to a "
            "playlist, creating it automatically if it doesn't exist yet."
        ),
    ),
    ACTION_REMOVE_FROM_PLAYLIST: ActionDefinition(
        id=ACTION_REMOVE_FROM_PLAYLIST,
        label="Remove from playlist",
        description="Remove the triggering track(s) from a playlist, if present.",
    ),
    ACTION_REMOVE_FROM_LIBRARY: ActionDefinition(
        id=ACTION_REMOVE_FROM_LIBRARY,
        label="Remove from library",
        description="Remove the triggering item from the library entirely.",
    ),
}


async def _resolve_track_uris(
    provider: LibraryAutomationsProvider, item: MediaItemType
) -> list[str]:
    """Resolve the item to track URIs: tracks pass through, albums/artists expand to their tracks."""
    if item.media_type == MediaType.TRACK:
        return [item.uri] if item.uri else []
    if item.media_type == MediaType.ALBUM:
        tracks = await provider.mass.music.albums.tracks(item.item_id, "library")
        return [t.uri for t in tracks if t.uri]
    if item.media_type == MediaType.ARTIST:
        tracks = await provider.mass.music.artists.tracks(item.item_id, "library")
        return [t.uri for t in tracks if t.uri]
    return []


async def _resolve_target_playlist(
    provider: LibraryAutomationsProvider, rule: AutomationRule
) -> int:
    """
    Get-or-create the playlist referenced by the rule's action params.

    The resolved db id is cached back onto ``rule.action.params["playlist_id"]`` and persisted,
    so subsequent matches skip the by-name lookup.
    """
    params = rule.action.params
    playlist_id = params.get("playlist_id")
    if playlist_id is not None:
        try:
            await provider.mass.music.playlists.get_library_item(playlist_id)
            return int(playlist_id)
        except MusicAssistantError:
            # stale reference (e.g. playlist was deleted): drop it and re-resolve by name below
            params.pop("playlist_id", None)

    name = params.get("playlist_name")
    if not name or not is_safe_name(name):
        msg = f"Invalid or missing playlist_name in action params: {name!r}"
        raise InvalidDataError(msg)

    existing = await provider.mass.music.playlists.library_items(search=name, summary=False)
    for playlist in existing:
        if playlist.name == name:
            params["playlist_id"] = playlist.item_id
            await provider.persist_rule(rule)
            return int(playlist.item_id)

    created = await provider.mass.music.playlists.create_playlist(name)
    params["playlist_id"] = created.item_id
    await provider.persist_rule(rule)
    return int(created.item_id)


async def _add_to_playlist(
    provider: LibraryAutomationsProvider, rule: AutomationRule, item: MediaItemType
) -> None:
    """Add the triggering item's track(s) to the configured playlist."""
    uris = await _resolve_track_uris(provider, item)
    if not uris:
        return
    db_playlist_id = await _resolve_target_playlist(provider, rule)
    await provider.mass.music.playlists._handle_add_playlist_tracks(db_playlist_id, uris)


async def _remove_from_playlist(
    provider: LibraryAutomationsProvider, rule: AutomationRule, item: MediaItemType
) -> None:
    """Remove the triggering item's track(s) from the configured playlist, if present."""
    uris = set(await _resolve_track_uris(provider, item))
    if not uris:
        return
    db_playlist_id = await _resolve_target_playlist(provider, rule)
    positions: list[int] = []
    idx = 0
    async for track in provider.mass.music.playlists.tracks(str(db_playlist_id), "library"):
        if track.uri in uris:
            positions.append(idx)
        idx += 1
    if positions:
        await provider.mass.music.playlists._handle_remove_playlist_tracks(
            db_playlist_id, tuple(positions)
        )


async def _remove_from_library(
    provider: LibraryAutomationsProvider, _rule: AutomationRule, item: MediaItemType
) -> None:
    """Remove the triggering item from the library entirely."""
    await provider.mass.music.remove_item_from_library(item.media_type, item.item_id)


ACTION_HANDLERS: dict[
    str,
    Callable[
        [LibraryAutomationsProvider, AutomationRule, MediaItemType],
        Coroutine[Any, Any, None],
    ],
] = {
    ACTION_ADD_TO_PLAYLIST: _add_to_playlist,
    ACTION_REMOVE_FROM_PLAYLIST: _remove_from_playlist,
    ACTION_REMOVE_FROM_LIBRARY: _remove_from_library,
}


async def execute_action(
    provider: LibraryAutomationsProvider, rule: AutomationRule, item: MediaItemType
) -> None:
    """Execute the action configured on a rule against the given triggering item."""
    handler = ACTION_HANDLERS.get(rule.action.type)
    if handler is None:
        provider.logger.warning("Unknown action type %r on rule %r", rule.action.type, rule.id)
        return
    try:
        await handler(provider, rule, item)
    except MusicAssistantError as exc:
        provider.logger.warning(
            "Action %r failed for rule %r (%s): %s", rule.action.type, rule.id, rule.name, exc
        )
