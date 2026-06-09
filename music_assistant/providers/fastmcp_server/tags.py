"""FastMCP tag enum and config-to-tag mapping."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from .constants import (
    CONF_CONFIG_READ,
    CONF_CONFIG_WRITE_CORE,
    CONF_CONFIG_WRITE_PLAYER,
    CONF_CONFIG_WRITE_PROVIDER,
    CONF_CONFIG_WRITE_SECRET,
    CONF_CONTROL_MEDIA,
    CONF_CONTROL_PLAYBACK,
    CONF_CONTROL_PLAYERS,
    CONF_CONTROL_VOLUME,
    CONF_DEBUG_EVENTS,
    CONF_DEBUG_INSPECT,
    CONF_DEBUG_LOGS,
    CONF_DEBUG_PROVIDERS,
    CONF_DEBUG_RELOAD,
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
    DEBUG_INSPECT = "debug:inspect"
    DEBUG_LOGS = "debug:logs"
    DEBUG_EVENTS = "debug:events"
    DEBUG_PROVIDERS = "debug:providers"
    DEBUG_RELOAD = "debug:reload"
    CONFIG_READ = "config:read"
    CONFIG_WRITE_PROVIDER = "config:write:provider"
    CONFIG_WRITE_CORE = "config:write:core"
    CONFIG_WRITE_PLAYER = "config:write:player"
    CONFIG_WRITE_SECRET = "config:write:secret"


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
    CONF_DEBUG_INSPECT: Tag.DEBUG_INSPECT,
    CONF_DEBUG_LOGS: Tag.DEBUG_LOGS,
    CONF_DEBUG_EVENTS: Tag.DEBUG_EVENTS,
    CONF_DEBUG_PROVIDERS: Tag.DEBUG_PROVIDERS,
    CONF_DEBUG_RELOAD: Tag.DEBUG_RELOAD,
    CONF_CONFIG_READ: Tag.CONFIG_READ,
    CONF_CONFIG_WRITE_PROVIDER: Tag.CONFIG_WRITE_PROVIDER,
    CONF_CONFIG_WRITE_CORE: Tag.CONFIG_WRITE_CORE,
    CONF_CONFIG_WRITE_PLAYER: Tag.CONFIG_WRITE_PLAYER,
    CONF_CONFIG_WRITE_SECRET: Tag.CONFIG_WRITE_SECRET,
}


def enabled_tags(config: ProviderConfig) -> set[Tag]:
    """Return the set of permission tags that are enabled in the given config."""
    return {tag for cfg_key, tag in CONFIG_TO_TAG.items() if config.get_value(cfg_key)}
