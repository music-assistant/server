"""Stable authorization capabilities exposed by the provider."""

from enum import StrEnum


class Capability(StrEnum):
    """The 26 immutable Permissions & Confirmations v2 capabilities."""

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
    CONFIG_READ = "config:read"
    CONFIG_WRITE_PROVIDER = "config:write:provider"
    CONFIG_WRITE_CORE = "config:write:core"
    CONFIG_WRITE_PLAYER = "config:write:player"
    # Bandit B105: this fixed authorization capability is not a secret value.
    CONFIG_WRITE_SECRET = "config:write:secret"  # nosec B105
    SYSTEM_ADMIN = "system:admin"
