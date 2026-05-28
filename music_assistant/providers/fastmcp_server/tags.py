"""FastMCP tag enum and config-to-tag mapping."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from .constants import (
    CONF_CONTROL_MEDIA,
    CONF_CONTROL_PLAYBACK,
    CONF_CONTROL_PLAYERS,
    CONF_CONTROL_VOLUME,
    CONF_DELETE_FAVORITES,
    CONF_DELETE_LIBRARY,
    CONF_DELETE_PLAYLISTS,
    CONF_DELETE_QUEUE,
    CONF_EDIT_FAVORITES,
    CONF_EDIT_LIBRARY,
    CONF_EDIT_PLAYLISTS,
    CONF_EDIT_QUEUE,
    CONF_QUERY_LIBRARY,
    CONF_QUERY_METADATA,
    CONF_QUERY_PLAYERS,
    CONF_QUERY_QUEUE,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig


class Tag(StrEnum):
    """Permission tags applied to FastMCP tools / resources / prompts."""

    QUERY_LIBRARY = "query:library"
    QUERY_QUEUE = "query:queue"
    QUERY_PLAYERS = "query:players"
    QUERY_METADATA = "query:metadata"
    CONTROL_PLAYBACK = "control:playback"
    CONTROL_VOLUME = "control:volume"
    CONTROL_PLAYERS = "control:players"
    CONTROL_MEDIA = "control:media"
    EDIT_LIBRARY = "edit:library"
    EDIT_QUEUE = "edit:queue"
    EDIT_PLAYLISTS = "edit:playlists"
    EDIT_FAVORITES = "edit:favorites"
    DELETE_LIBRARY = "delete:library"
    DELETE_QUEUE = "delete:queue"
    DELETE_PLAYLISTS = "delete:playlists"
    DELETE_FAVORITES = "delete:favorites"


CONFIG_TO_TAG: dict[str, Tag] = {
    CONF_QUERY_LIBRARY: Tag.QUERY_LIBRARY,
    CONF_QUERY_QUEUE: Tag.QUERY_QUEUE,
    CONF_QUERY_PLAYERS: Tag.QUERY_PLAYERS,
    CONF_QUERY_METADATA: Tag.QUERY_METADATA,
    CONF_CONTROL_PLAYBACK: Tag.CONTROL_PLAYBACK,
    CONF_CONTROL_VOLUME: Tag.CONTROL_VOLUME,
    CONF_CONTROL_PLAYERS: Tag.CONTROL_PLAYERS,
    CONF_CONTROL_MEDIA: Tag.CONTROL_MEDIA,
    CONF_EDIT_LIBRARY: Tag.EDIT_LIBRARY,
    CONF_EDIT_QUEUE: Tag.EDIT_QUEUE,
    CONF_EDIT_PLAYLISTS: Tag.EDIT_PLAYLISTS,
    CONF_EDIT_FAVORITES: Tag.EDIT_FAVORITES,
    CONF_DELETE_LIBRARY: Tag.DELETE_LIBRARY,
    CONF_DELETE_QUEUE: Tag.DELETE_QUEUE,
    CONF_DELETE_PLAYLISTS: Tag.DELETE_PLAYLISTS,
    CONF_DELETE_FAVORITES: Tag.DELETE_FAVORITES,
}


def enabled_tags(config: ProviderConfig) -> set[Tag]:
    """Return the set of permission tags that are enabled in the given config."""
    return {tag for cfg_key, tag in CONFIG_TO_TAG.items() if config.get_value(cfg_key)}
