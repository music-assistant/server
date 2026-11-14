"""Models of the community catalog: the index the registry publishes and the API results."""

from __future__ import annotations

from base64 import b64encode
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from mashumaro import DataClassDictMixin
from music_assistant_models.provider import (
    ProviderManifest,  # noqa: TC002 - mashumaro resolves field types at runtime
)

from music_assistant.controllers.community_catalog.constants import STATE_SCHEMA


class IndexStatus(StrEnum):
    """Whether the catalog offers a provider."""

    LISTED = "listed"
    # offered no longer; what is installed keeps loading
    WITHDRAWN = "withdrawn"
    # may not load at all
    BLOCKED = "blocked"


class CommunityProviderState(StrEnum):
    """Where a community provider stands on this server."""

    AVAILABLE = "available"
    INSTALLED = "installed"
    UPDATE_AVAILABLE = "update_available"
    RESTART_REQUIRED = "restart_required"
    BLOCKED = "blocked"
    UNAVAILABLE = "unavailable"
    WITHDRAWN = "withdrawn"


@dataclass
class IndexVersion(DataClassDictMixin):
    """A released version of a provider in the index."""

    commit: str
    # the folder of the provider in its repository
    folder: str
    content_hash: str
    requirements: list[str]
    released: str


@dataclass
class IndexProvider(DataClassDictMixin):
    """A provider in the index."""

    status: IndexStatus
    # owner/name of the repository on GitHub
    repository: str
    # the manifest of the latest version, parsed only where it is used
    manifest: dict[str, Any]
    # when the provider entered the catalog
    added: str
    reason: str | None = None
    reason_url: str | None = None
    maintainers: list[str] = field(default_factory=list)
    icon_svg: str | None = None
    latest: str | None = None
    versions: dict[str, IndexVersion] = field(default_factory=dict)
    # the reason, by version
    blocked_versions: dict[str, str] = field(default_factory=dict)

    @property
    def icon(self) -> str | None:
        """Return the icon as a data URI, which a client can show as an image but not as markup."""
        if not self.icon_svg:
            return None
        return f"data:image/svg+xml;base64,{b64encode(self.icon_svg.encode()).decode('ascii')}"


@dataclass
class CatalogIndex(DataClassDictMixin):
    """The index of the community catalog for one server line."""

    schema: int
    server_line: str
    generated: str
    providers: dict[str, IndexProvider] = field(default_factory=dict)


@dataclass
class CatalogState(DataClassDictMixin):
    """What the server keeps about the community catalog next to the kept copy of the index."""

    schema: int = STATE_SCHEMA
    index_etag: str | None = None


@dataclass
class CommunityProvider(DataClassDictMixin):
    """A provider of the community catalog, as the app shows it."""

    domain: str
    # owner/name of the repository on GitHub
    repository: str
    maintainers: list[str]
    # a data URI for an image element, like providers/icon returns
    icon: str | None
    # the manifest of the installed version, else the one of the latest version in the index
    manifest: ProviderManifest
    state: CommunityProviderState
    installed_version: str | None = None
    latest_version: str | None = None
    # the version that becomes active at the next start of the server
    pending_version: str | None = None
    # when the provider entered the catalog
    added: str | None = None
    # when latest_version was released
    released: str | None = None
    reason: str | None = None
    reason_url: str | None = None
    notice: str | None = None
