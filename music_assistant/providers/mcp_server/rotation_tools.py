"""The tools to build, preview and change Rotations (playlists that follow a recipe of steps)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from music_assistant_models.auth import Scope

from music_assistant.providers.rotation_playlist import RotationPlaylistProvider
from music_assistant.providers.rotation_playlist.models import steps_json_schema

from .registry import ToolError, mcp_tool

if TYPE_CHECKING:
    from music_assistant_models.media_items import Playlist

    from music_assistant.mass import MusicAssistant

PROVIDER_DOMAIN = "rotation_playlist"
NOT_ENABLED = "Rotations are not enabled. Enable the Rotations plugin in Settings."
ROTATION_INSTRUCTIONS = (
    "A Rotation is a playlist built from a recipe of steps that plays through the library "
    "round by round. To make one, call describe_rotation_steps and list_rotation_recipes, "
    "check the steps with preview_rotation, then call create_rotation."
)


def rotations_enabled(mass: MusicAssistant) -> bool:
    """
    Return whether the Rotations plugin is loaded.

    :param mass: The Music Assistant instance.
    """
    return mass.get_provider(PROVIDER_DOMAIN, provider_type=RotationPlaylistProvider) is not None


@mcp_tool(
    "create_rotation",
    Scope.LIBRARY_WRITE,
    schema_overrides={"steps": steps_json_schema()},
    available=rotations_enabled,
)
async def create_rotation(
    mass: MusicAssistant, name: str, steps: list[dict[str, Any]]
) -> dict[str, Any]:
    """
    Create a Rotation: a playlist in the library that is built from a recipe of steps.

    A Rotation picks its music from the library, for example all albums of an artist, and
    puts it in an order, for example random albums with the tracks of each album in order.
    The playlist then plays through the music round by round and starts a new round when
    everything was played. Use describe_rotation_steps to learn what is possible and
    list_rotation_recipes for complete examples. Use preview_rotation before creating one.
    Example request: "albums by Die drei ??? in random order, each album in track order,
    leaving out tracks whose title contains 'Inhaltsangabe' or 'Titelmusik'" becomes these
    steps: [{"type":"source","media_type":"album","from":"library"},{"type":"filter","mode":"keep","match":"all","conditions":[{"field":"artist","op":"is","value":"Die drei ???"}]},{"type":"sort","keys":[{"field":"random"}]},{"type":"expand","to":"track","scope":"library"},{"type":"filter","mode":"remove","match":"any","conditions":[{"field":"title","op":"contains","value":"Inhaltsangabe"},{"field":"title","op":"contains","value":"Titelmusik"}]},{"type":"sort","keys":[{"field":"disc_number"},{"field":"track_number"}]}]
    Use describe_rotation_steps for the fields and operators that exist.

    :param name: The name of the Rotation, shown as the name of the playlist.
    :param steps: The steps that build the Rotation, in order.
    """
    playlist = await _provider(mass).create(name, steps)
    return _compact_playlist(playlist)


@mcp_tool(
    "update_rotation",
    Scope.LIBRARY_WRITE,
    schema_overrides={"steps": steps_json_schema()},
    available=rotations_enabled,
)
async def update_rotation(
    mass: MusicAssistant,
    rotation: str,
    name: str | None = None,
    steps: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    Change the name and/or the steps of a Rotation. Leave out what should stay as it is.

    Passing steps replaces all steps of the Rotation, so pass the complete list.
    Use preview_rotation first to check the new steps.

    :param rotation: The name of the Rotation, its id or the id of its playlist.
    :param name: The new name of the Rotation.
    :param steps: The complete new list of steps.
    """
    provider = _provider(mass)
    if name is None and steps is None:
        raise ToolError("Pass a name, steps or both")
    rotation_id = await _resolve_rotation(provider, rotation)
    playlist = await provider.update(rotation_id, name=name, steps=steps)
    return _compact_playlist(playlist)


@mcp_tool(
    "preview_rotation",
    Scope.LIBRARY_READ,
    schema_overrides={"steps": steps_json_schema()},
    available=rotations_enabled,
)
async def preview_rotation(
    mass: MusicAssistant,
    limit: int,
    rotation: str | None = None,
    steps: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    Show the first tracks a Rotation or a list of steps would play, without saving anything.

    Pass exactly one of rotation (an existing Rotation) or steps (a draft). The preview always
    needs a limit (1-200). Tracks come in play order, grouped by album when the steps keep
    albums together. step_counts tells how many items each step left, which helps to find
    the step that removes too much. total_tracks is exact only when total_exact is true.
    A random order differs on every call.

    :param limit: The number of tracks to show, from 1 to 200.
    :param rotation: The name of the Rotation, its id or the id of its playlist.
    :param steps: The steps to preview, as for create_rotation.
    """
    provider = _provider(mass)
    result = await provider.preview(
        await _steps_for(provider, rotation, steps), limit, include_removed=False
    )
    return {
        "groups": [
            {
                "name": group["item"]["name"],
                "uri": group["item"]["uri"],
                "tracks": [_compact_entry(entry) for entry in group["tracks"]],
            }
            for group in result["groups"]
        ],
        "step_counts": result["step_counts"],
        "total_tracks": result["total_tracks"],
        "total_exact": result["total_exact"],
        "duration_seconds": result["duration_seconds"],
    }


@mcp_tool(
    "explain_rotation",
    Scope.LIBRARY_READ,
    schema_overrides={"steps": steps_json_schema()},
    available=rotations_enabled,
)
async def explain_rotation(
    mass: MusicAssistant,
    rotation: str | None = None,
    steps: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    Explain a Rotation or a list of steps in plain English, one sentence per step.

    Pass exactly one of rotation or steps. Use this to check that steps do what the user asked.

    :param rotation: The name of the Rotation, its id or the id of its playlist.
    :param steps: The steps to explain, as for create_rotation.
    """
    provider = _provider(mass)
    return {"steps": await provider.explain(await _steps_for(provider, rotation, steps))}


@mcp_tool("describe_rotation_steps", Scope.LIBRARY_READ, available=rotations_enabled)
async def describe_rotation_steps(
    mass: MusicAssistant, media_type: Literal["artist", "album", "track"] | None = None
) -> dict[str, Any]:
    """
    Describe the step types, their fields, the filter operators and sort keys of Rotations.

    Call this before building steps. It also lists the music providers of this install.

    :param media_type: Only describe what applies to this type of item.
    """
    return (await _provider(mass).describe_steps(media_type)).to_dict()


@mcp_tool("list_rotation_recipes", Scope.LIBRARY_READ, available=rotations_enabled)
async def list_rotation_recipes(mass: MusicAssistant) -> dict[str, Any]:
    """List ready-made Rotations as complete steps with a one line goal, to use as a start."""
    return {"recipes": [recipe.to_dict() for recipe in await _provider(mass).recipes()]}


def _provider(mass: MusicAssistant) -> RotationPlaylistProvider:
    """Return the Rotation provider, or raise when the plugin is not enabled."""
    provider = mass.get_provider(PROVIDER_DOMAIN, provider_type=RotationPlaylistProvider)
    if provider is None:
        raise ToolError(NOT_ENABLED)
    return provider


async def _resolve_rotation(provider: RotationPlaylistProvider, rotation: str) -> str:
    """Return the id of the Rotation with the given name, id or playlist id."""
    rotations = await provider.list_rotations()
    for entry in rotations:
        if rotation in (entry["id"], entry["playlist"]["item_id"]):
            return str(entry["id"])
    name = rotation.strip().lower()
    matches = [entry for entry in rotations if entry["name"].strip().lower() == name]
    if len(matches) == 1:
        return str(matches[0]["id"])
    available = ", ".join(entry["name"] for entry in rotations) or "none"
    if matches:
        raise ToolError(
            f"More than one Rotation is named {rotation!r}. Use its id instead. Rotations: "
            + ", ".join(f"{entry['name']} ({entry['id']})" for entry in matches)
        )
    raise ToolError(f"Unknown Rotation {rotation!r}. Available Rotations: {available}")


async def _steps_for(
    provider: RotationPlaylistProvider, rotation: str | None, steps: list[dict[str, Any]] | None
) -> list[dict[str, Any]]:
    """Return the steps of a Rotation or the given steps; exactly one of them must be passed."""
    if rotation is None:
        if steps is None:
            raise ToolError("Pass exactly one of rotation or steps")
        return steps
    if steps is not None:
        raise ToolError("Pass exactly one of rotation or steps")
    return (await provider.get(await _resolve_rotation(provider, rotation))).steps


def _compact_playlist(playlist: Playlist) -> dict[str, Any]:
    """Return the fields of a Rotation playlist that a model needs."""
    return {"uri": playlist.uri, "name": playlist.name, "playlist_id": playlist.item_id}


def _compact_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Return a preview track with the fields that a model needs."""
    track = entry["track"]
    album = track.get("album")
    return {
        "uri": track["uri"],
        "name": track["name"],
        "artists": [artist["name"] for artist in track.get("artists") or []],
        "album": album["name"] if album else None,
        "duration": track.get("duration") or None,
        "removed": entry["removed"],
    }
