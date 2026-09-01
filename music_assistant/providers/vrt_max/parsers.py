"""Parsing helpers that turn VRT GraphQL payloads into typed objects."""

from __future__ import annotations

import base64
import json
import re
from datetime import datetime
from typing import Any

from music_assistant.helpers.datetime import from_iso_string

from .constants import _EPISODE_TILE_TYPES, _FAVOURITE_TILE_TYPES, IMAGE_RENDITION
from .models import VrtEpisode, VrtProgramTile, VrtSeason

# Matches the "orig" (full resolution) segment of a VRT image url.
_ORIG_RENDITION_RE = re.compile(r"^(https://images\.vrt\.be)/orig/")

# Map the GraphQL `brand` slug to a human channel name (fallback: prettified slug).
_BRAND_NAMES = {
    "radio1": "Radio 1",
    "radio2": "Radio 2",
    "klara": "Klara",
    "stubru": "Studio Brussel",
    "mnm": "MNM",
    "ketnet": "Ketnet",
    "vrtnws": "VRT NWS",
    "sporza": "Sporza",
}


def _brand_display_name(brand: Any) -> str | None:
    """Return a human channel name for a VRT brand slug."""
    if not isinstance(brand, str) or not brand:
        return None
    return _BRAND_NAMES.get(brand, brand.replace("-", " ").title())


# Media-kind labels that appear in a header meta breadcrumb but are not presenters.
_META_NON_PRESENTER = frozenset(
    {"radio", "podcast", "tv", "kijk", "luister", "fragment", "fragmenten", "clip"}
)


def _presenters_from_header(header: Any, channel_name: str | None) -> tuple[str, ...]:
    """
    Extract presenter names from a PageHeader's secondaryMeta breadcrumb.

    The breadcrumb is like [mediatype, channel, presenter]; drop the media-kind
    label, the channel name and season counts, keeping the presenter(s).
    """
    if not isinstance(header, dict):
        return ()
    names: list[str] = []
    for entry in header.get("secondaryMeta") or []:
        value = entry.get("value") if isinstance(entry, dict) else None
        if not isinstance(value, str) or not value:
            continue
        if value.lower() in _META_NON_PRESENTER or "seizoen" in value.lower():
            continue
        if channel_name and value == channel_name:
            continue
        if value not in names:
            names.append(value)
    return tuple(names)


def _collect_presenters(components: Any) -> tuple[str, ...]:
    """Collect presenter names from a program page's PresentersList components."""
    if not isinstance(components, list):
        return ()
    names: list[str] = []
    for comp in components:
        if not isinstance(comp, dict) or comp.get("__typename") != "ContainerNavigation":
            continue
        for item in comp.get("items") or []:
            if not isinstance(item, dict):
                continue
            for sub in item.get("components") or []:
                if not isinstance(sub, dict) or sub.get("__typename") != "PresentersList":
                    continue
                for presenter in sub.get("presenters") or []:
                    title = presenter.get("title") if isinstance(presenter, dict) else None
                    if isinstance(title, str) and title and title not in names:
                        names.append(title)
    return tuple(names)


def _collect_seasons(components: Any, seasons: list[VrtSeason]) -> None:
    """
    Recursively collect listen-back episode lists from a program page.

    Walks `ContainerNavigation` tabs (and the nested season-selector navigation
    used by multi-season podcasts), appending each PaginatedTileList that holds
    episode tiles. Scheduled/upcoming tabs are skipped.
    """
    if not isinstance(components, list):
        return
    for comp in components:
        if not isinstance(comp, dict):
            continue
        typename = comp.get("__typename")
        if typename == "ContainerNavigation":
            for item in comp.get("items") or []:
                if not isinstance(item, dict):
                    continue
                # Skip upcoming/scheduled broadcasts - not yet playable.
                if (item.get("title") or "").lower().startswith("gepland"):
                    continue
                _collect_seasons(item.get("components"), seasons)
        elif typename == "PaginatedTileList":
            if _first_node_type(comp.get("paginatedItems")) not in _EPISODE_TILE_TYPES:
                continue
            cid = comp.get("componentId")
            if isinstance(cid, str):
                seasons.append(VrtSeason(title=comp.get("title"), component_id=cid))


def _parse_iso(value: Any) -> datetime | None:
    """Parse a VRT ISO-8601 timestamp (e.g. '2026-08-09T10:00:00.000Z')."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return from_iso_string(value)
    except ValueError:
        return None


def _first_broadcast_start(player: Any) -> datetime | None:
    """Return the AudioPlayerMode broadcastStartDate from a player payload."""
    if not isinstance(player, dict):
        return None
    for mode in player.get("modes") or []:
        if isinstance(mode, dict) and mode.get("__typename") == "AudioPlayerMode":
            return _parse_iso(mode.get("broadcastStartDate"))
    return None


def _playlist_component_id(menu: Any) -> str | None:
    """Return the 'Playlist' tab componentId from an episode page menu."""
    if not isinstance(menu, dict):
        return None
    for item in menu.get("items") or []:
        if not isinstance(item, dict):
            continue
        if (item.get("title") or "").lower() == "playlist":
            component_id = item.get("componentId")
            if isinstance(component_id, str) and component_id:
                return component_id
    return None


def _song_list_component_id(component: Any) -> str | None:
    """Return the inner song PaginatedTileList componentId from a playlist tab."""
    if not isinstance(component, dict):
        return None
    for sub in component.get("components") or []:
        if not isinstance(sub, dict) or sub.get("__typename") != "PaginatedTileList":
            continue
        if sub.get("tileContentType") == "song":
            component_id = sub.get("componentId")
            if isinstance(component_id, str) and component_id:
                return component_id
    return None


def _search_list_id(entity_type: str, result_type: str, query: str) -> str:
    """Build the base64 `listId` for a faceted keyword search."""
    search = {
        "facets": [{"name": "entitytype", "values": [entity_type]}],
        "resultType": result_type,
        "q": query,
    }
    raw = f"o%14|{json.dumps(search)}|{result_type}%"
    return "$" + base64.b64encode(raw.encode()).decode()


def _favourite_id(node: Any) -> str | None:
    """Return the page id of a favourite tile if it maps to a Podcast, else None."""
    if not isinstance(node, dict) or node.get("__typename") not in _FAVOURITE_TILE_TYPES:
        return None
    return _link(node)


def _first_node_type(paginated_items: Any) -> str | None:
    """Return the __typename of the first node in a paginatedItems payload."""
    if not isinstance(paginated_items, dict):
        return None
    for edge in paginated_items.get("edges") or []:
        node = edge.get("node") if isinstance(edge, dict) else None
        if isinstance(node, dict):
            return node.get("__typename")
    return None


def _image_url(image: Any) -> str | None:
    """Extract a usable image URL from an Image object."""
    if not isinstance(image, dict):
        return None
    url = image.get("templateUrl")
    if not isinstance(url, str) or not url.startswith("http"):
        return None
    # Observed template urls are already fully-qualified 'orig' urls; drop any
    # leftover sizing placeholders defensively.
    url = re.sub(r"\{[^}]*\}", "", url)
    # 'orig' is the full-resolution original, several times the size of anything the
    # interface renders. Asking VRT's image CDN for a rendition instead keeps a browse
    # list from pulling megabytes of artwork it will only ever show as a thumbnail.
    return _ORIG_RENDITION_RE.sub(r"\1" + IMAGE_RENDITION, url, count=1)


def _link(node: dict[str, Any]) -> str | None:
    """Return the LinkAction target (page path) of a tile node."""
    action = node.get("action")
    if isinstance(action, dict) and action.get("__typename") == "LinkAction":
        link = action.get("link")
        if isinstance(link, str) and link:
            return link
    return None


def _header_meta(header: Any) -> list[Any]:
    """Return the primaryMeta list of a PageHeader."""
    if isinstance(header, dict):
        meta = header.get("primaryMeta")
        if isinstance(meta, list):
            return meta
    return []


def _first_meta(meta: list[Any]) -> str | None:
    """Return the first non-empty primaryMeta value."""
    for entry in meta:
        if isinstance(entry, dict):
            value = entry.get("value")
            if isinstance(value, str) and value:
                return value
    return None


def _parse_header(header: Any) -> tuple[str | None, str | None]:
    """Return (description, image_url) from a PageHeader object."""
    if not isinstance(header, dict):
        return None, None
    description = None
    rich = header.get("richDescription")
    if isinstance(rich, dict):
        text = rich.get("text")
        if isinstance(text, str) and text.strip():
            description = text.strip()
    return description, _image_url(header.get("image"))


def _parse_program_tile(node: dict[str, Any]) -> VrtProgramTile | None:
    """Parse a program/podcast tile node into a VrtProgramTile."""
    page_id = _link(node)
    title = node.get("title")
    if not page_id or not isinstance(title, str) or not title:
        return None
    description = node.get("description")
    if not isinstance(description, str) or not description:
        description = None
    return VrtProgramTile(
        page_id=page_id,
        title=title,
        description=description,
        image_url=_image_url(node.get("image")),
    )


def _parse_episode_tile(node: dict[str, Any]) -> VrtEpisode | None:
    """Parse an episode tile node into a VrtEpisode."""
    page_id = _link(node)
    title = node.get("title")
    if not page_id or not isinstance(title, str) or not title:
        return None
    description = node.get("description")
    if not isinstance(description, str) or not description:
        description = None
    date_label = _first_meta(node.get("primaryMeta") or [])
    progress = node.get("progress")
    fully_played = False
    resume_position = 0
    if isinstance(progress, dict):
        fully_played = bool(progress.get("completed"))
        pos = progress.get("progressInSeconds")
        resume_position = int(pos) if isinstance(pos, (int, float)) else 0
    return VrtEpisode(
        page_id=page_id,
        title=title,
        description=description,
        image_url=_image_url(node.get("image")),
        duration=_parse_duration(node.get("formattedDuration")),
        date_label=date_label,
        fully_played=fully_played,
        resume_position=resume_position,
    )


def _parse_duration(formatted: Any) -> int:
    """Parse a formatted duration like '60 min' or '1 u 5 min' into seconds."""
    if not isinstance(formatted, str):
        return 0
    seconds = 0
    hours = re.search(r"(\d+)\s*(?:u|uur|h)\b", formatted)
    if hours:
        seconds += int(hours.group(1)) * 3600
    minutes = re.search(r"(\d+)\s*min", formatted)
    if minutes:
        seconds += int(minutes.group(1)) * 60
    if seconds == 0:
        # bare number - assume minutes
        bare = re.fullmatch(r"\s*(\d+)\s*", formatted)
        if bare:
            seconds = int(bare.group(1)) * 60
    return seconds
