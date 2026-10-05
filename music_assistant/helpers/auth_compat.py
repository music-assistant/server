"""Compatibility shims for auth scope symbols.

Upstream introduced fine-grained auth scopes (`Scope`, `UserRole.SERVICE`) in
music-assistant-models 1.1.153+. This fork still pins music-assistant-models
1.1.119, which has neither, so the scope-aware auth endpoints vendor the missing
symbols here with their canonical upstream values.

When the fork syncs to models >= 1.1.153, import these from
music_assistant_models.auth instead and delete this module.
"""

from enum import StrEnum
from typing import Final

# Key used for the service role in role-scope tables. On models >= 1.1.153 this
# matches str(UserRole.SERVICE); on 1.1.119 the member does not exist, so the
# plain value is used. Serializes identically either way.
SERVICE_ROLE_KEY: Final[str] = "service"


class Scope(StrEnum):
    """Vendored Scope enum (canonical member set from music-assistant-models 1.1.214)."""

    ALL = "*"
    LIBRARY_READ = "library.read"
    LIBRARY_WRITE = "library.write"
    LIBRARY_MANAGE = "library.manage"
    PLAYERS_READ = "players.read"
    PLAYERS_CONTROL = "players.control"
    QUEUES_READ = "queues.read"
    QUEUES_CONTROL = "queues.control"
    PROVIDERS_READ = "providers.read"
    CONFIG_PLAYERS_READ = "config.players.read"
    CONFIG_PLAYERS_WRITE = "config.players.write"
    CONFIG_PROVIDERS_READ = "config.providers.read"
    CONFIG_PROVIDERS_WRITE = "config.providers.write"
    CONFIG_PROVIDERS_OWN = "config.providers.own"
    CONFIG_CORE_READ = "config.core.read"
    CONFIG_CORE_WRITE = "config.core.write"
    USERS_READ = "users.read"
    USERS_MANAGE = "users.manage"
    USERS_IMPERSONATE = "users.impersonate"
    USERS_INVITE = "users.invite"
    SYSTEM_READ = "system.read"
    SYSTEM_MANAGE = "system.manage"
    UNKNOWN = "unknown"

    @classmethod
    def _missing_(cls, value: object) -> "Scope":  # noqa: ARG003
        """Return UNKNOWN (which grants no access) if an unknown value is provided."""
        return cls.UNKNOWN
