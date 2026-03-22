"""Helpers for Apple Music playlist browsing."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import MediaType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import BrowseFolder, Playlist, ProviderMapping

if TYPE_CHECKING:
    from music_assistant.providers.apple_music import AppleMusicProvider

ROOT_PLAYLIST_FOLDER_ID = "p.playlistsroot"
# Apple exposes the entire playlist hierarchy under this synthetic root. We walk the
# tree lazily, fetching the exact branch the user opens instead of preloading.


@dataclass(slots=True)
class AppleMusicPlaylistFolder:
    """Lightweight representation of a folder node returned by Apple."""

    item_id: str
    name: str


def _folder_path_segment(name: str) -> str:
    """Return human-readable, path-safe breadcrumb text."""
    return (name.strip() or "Folder").replace("/", "-").replace("|", "-")


def _extract_playlist_folder_id(path_parts: list[str]) -> str | None:
    """Extract the active folder id from a playlist browse path."""
    if not path_parts:
        return None
    last_segment = path_parts[-1]
    if "|" in last_segment:
        return last_segment.rsplit("|", 1)[1]
    return last_segment


def _folder_nodes(
    provider: AppleMusicProvider,
    folders: list[AppleMusicPlaylistFolder],
    base_path: str,
) -> list[BrowseFolder]:
    """Convert folder metadata returned by the API into browse nodes."""
    normalized_base = base_path.rstrip("/")
    items: list[BrowseFolder] = []
    for folder in folders:
        folder_name = folder.name or "Folder"
        segment_name = _folder_path_segment(folder_name)
        segment = f"{segment_name}|{folder.item_id}"
        items.append(
            BrowseFolder(
                item_id=f"folder:{folder.item_id}",
                provider=provider.instance_id,
                path=f"{normalized_base}/{segment}",
                name=folder_name,
            )
        )
    return items


async def _fetch_playlist_folder_children(
    provider: AppleMusicProvider,
    folder_id: str | None = None,
) -> tuple[list[AppleMusicPlaylistFolder], list[Playlist]]:
    """Fetch folders/playlists for a single branch of the Apple Music tree."""
    apple_folder_id = folder_id or ROOT_PLAYLIST_FOLDER_ID
    endpoint = f"me/library/playlist-folders/{apple_folder_id}/children"
    try:
        children = await provider._get_all_items(endpoint)
    except MediaNotFoundError:
        children = []
    folders: list[AppleMusicPlaylistFolder] = []
    playlist_entries: list[dict[str, Any]] = []
    library_playlist_ids: list[str] = []
    for child in children:
        child_id = child.get("id")
        if not child_id:
            continue
        child_type = child.get("type")
        attributes = child.get("attributes") or {}
        if child_type == "library-playlist-folders":
            folders.append(
                AppleMusicPlaylistFolder(
                    item_id=child_id,
                    name=attributes.get("name") or "Folder",
                )
            )
        elif child_type == "library-playlists":
            playlist_entries.append(child)
            if provider.is_library_id(child_id):
                library_playlist_ids.append(child_id)
    ratings: dict[str, Any] = {}
    if library_playlist_ids:
        ratings = await provider._get_ratings(library_playlist_ids, MediaType.PLAYLIST)
    playlists: list[Playlist] = []
    for playlist_entry in playlist_entries:
        playlist_id = playlist_entry.get("id")
        is_favourite = ratings.get(playlist_id)
        attributes = playlist_entry.get("attributes") or {}
        play_params = attributes.get("playParams") or {}
        global_id = play_params.get("globalId")

        # Start with the original entry, potentially modify it below
        playlist_obj = playlist_entry

        if attributes.get("hasCatalog") and global_id and not provider.is_library_id(global_id):
            try:
                playlist = await provider.get_playlist(global_id, is_favourite)
            except MediaNotFoundError:
                provider.logger.debug(
                    "Catalog playlist %s not found, falling back to library metadata",
                    global_id,
                )
                playlist_obj = _playlist_without_global_id(playlist_obj)
            else:
                playlists.append(_apply_library_id(playlist, playlist_id, provider))
                continue
        playlists.append(provider._parse_playlist(playlist_obj, is_favourite))
    playlists.sort(key=lambda item: (item.name or "").casefold())
    folders.sort(key=lambda folder: folder.name.casefold())
    return folders, playlists


def _playlist_without_global_id(playlist_obj: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow copy without a catalog ID.

    Some folders report `hasCatalog=True` but their catalog playlist fetch fails.
    When that happens we strip the bogus `globalId` so downstream parsing sticks
    to the library ID (which *can* be resolved).
    """
    new_obj = dict(playlist_obj)
    attributes = dict(new_obj.get("attributes") or {})
    play_params = dict(attributes.get("playParams") or {})
    play_params.pop("globalId", None)
    attributes["playParams"] = play_params
    new_obj["attributes"] = attributes
    return new_obj


def _apply_library_id(
    playlist: Playlist, library_id: str, provider: AppleMusicProvider
) -> Playlist:
    """Return a copy of `playlist` that always points to the library endpoint.

    `get_playlist` is cached, so mutating the original object would leak those
    changes to other consumers of the cached catalog playlist.  Instead we clone
    the dataclass with `replace`, swap the ids for this provider instance, and
    keep the cached object untouched.
    """
    new_mappings: set[ProviderMapping] = set()
    for mapping in playlist.provider_mappings:
        if mapping.provider_instance == provider.instance_id:
            new_mappings.add(replace(mapping, item_id=library_id))
        else:
            new_mappings.add(mapping)
    return replace(
        playlist,
        item_id=library_id,
        provider=provider.instance_id,
        provider_mappings=new_mappings,
    )


async def browse_playlists(
    provider: AppleMusicProvider, path: str, path_parts: list[str]
) -> Sequence[BrowseFolder | Playlist]:
    """Handle playlist browsing for the Apple Music provider."""
    folder_id: str | None = None
    base_path = f"{provider.instance_id}://playlists"
    if len(path_parts) > 1:
        folder_id = _extract_playlist_folder_id(path_parts[1:])
        base_path = path.rstrip("/")
    folders, playlists = await _fetch_playlist_folder_children(provider, folder_id)
    folder_nodes = _folder_nodes(provider, folders, base_path)
    return [*folder_nodes, *playlists]
