"""The decision whether an installed community provider may load."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from music_assistant.controllers.community_catalog.index import server_line
from music_assistant.controllers.community_catalog.models import IndexStatus

if TYPE_CHECKING:
    from music_assistant.controllers.community_catalog.models import (
        CatalogIndex,
        IndexProvider,
        InstalledProvider,
    )
    from music_assistant.controllers.community_catalog.store import CatalogStore
    from music_assistant.mass import MusicAssistant


class Verdict(StrEnum):
    """Whether an installed community provider loads, and why not when it does not."""

    LOAD = "load"
    # the files differ from what was installed: install the same version again first
    REINSTALL = "reinstall"
    # the installed version is not listed with its content: install the decision's version
    SWITCH = "switch"
    CATALOG_UNREACHABLE = "catalog_unreachable"
    BLOCKED = "blocked"
    NOT_LISTED = "not_listed"
    NOT_FOR_THIS_VERSION = "not_for_this_version"
    # decided while loading, never by evaluate
    REQUIREMENTS_CLASH = "requirements_clash"
    REINSTALL_FAILED = "reinstall_failed"
    WENT_BACK = "went_back"
    RESTART_TO_GO_BACK = "restart_to_go_back"
    RESTART_TO_UPDATE = "restart_to_update"
    # not installed: a moved provider that waits to be installed from the catalog
    MOVED = "moved"
    MOVE_FAILED = "move_failed"


@dataclass(frozen=True, slots=True)
class Decision:
    """The verdict for an installed community provider, with what explains it."""

    verdict: Verdict
    # the reason the catalog gives for a block, or what explains another verdict
    detail: str | None = None
    # where the catalog explains a block
    url: str | None = None
    # the version to switch to, also from a blocked version, or the update on offer for a
    # provider that loads
    version: str | None = None


# why an installed provider does not load, as the admin reads it; the texts of the
# translation keys community_provider_<verdict> in the errors of strings.json
_REASONS: Final[dict[Verdict, str]] = {
    Verdict.CATALOG_UNREACHABLE: (
        "The community catalog could not be reached to check this, so it does not run. "
        "Restart Music Assistant when your server is online again."
    ),
    Verdict.BLOCKED: "The community catalog blocked this ({0}), so it no longer runs. "
    "You can remove it.",
    Verdict.NOT_LISTED: (
        "This is no longer in the community catalog, so it no longer runs. You can remove it."
    ),
    Verdict.NOT_FOR_THIS_VERSION: (
        "This is not available for this version of Music Assistant, so it does not run. "
        "You can remove it."
    ),
    Verdict.REQUIREMENTS_CLASH: (
        "This needs versions of Python packages that clash with Music Assistant or with "
        "something else from the community catalog, so it does not run."
    ),
    Verdict.REINSTALL_FAILED: (
        "The files of this were changed on your server and could not be downloaded again "
        "({0}), so it does not run. Restart Music Assistant to try again."
    ),
    Verdict.WENT_BACK: (
        "Version {0} of this failed to load, and the version before it is no longer "
        "available, so it does not run."
    ),
    Verdict.RESTART_TO_GO_BACK: (
        "Version {0} of this failed to load. Restart Music Assistant to finish going back "
        "to the version before it."
    ),
    Verdict.RESTART_TO_UPDATE: (
        "Version {0} of this is ready. Restart Music Assistant to finish the update."
    ),
    Verdict.MOVED: (
        "This moved to the community catalog. Music Assistant installs it from there by itself "
        "as soon as the catalog offers it for this version and your server can reach it."
    ),
    Verdict.MOVE_FAILED: (
        "This moved to the community catalog, but Music Assistant could not install it from "
        "there ({0}). It tries again at every start and whenever it checks the catalog."
    ),
}


def evaluate(
    installed: InstalledProvider,
    folder_hash: str | None,
    entry: IndexProvider | None,
    *,
    has_index: bool,
    own_line: bool,
) -> Decision:
    """
    Return whether an installed community provider may load, or switches to another version.

    A provider that may load also learns of an update on offer.

    :param installed: What was installed.
    :param folder_hash: The content hash of the live folder, None when there is none.
    :param entry: The provider in the kept copy of the index, None when it is not in it.
    :param has_index: Whether the server has a kept copy of the index at all.
    :param own_line: Whether the kept copy is the index of this server's line.
    """
    if not has_index:
        return Decision(Verdict.CATALOG_UNREACHABLE)
    offered = entry.offered_version if entry is not None and own_line else None
    if offered == installed.failed_version:
        # a version the server went back from is not tried again
        offered = None
    if entry is not None and (
        entry.status == IndexStatus.BLOCKED or installed.version in entry.blocked_versions
    ):
        reason = (
            entry.reason
            if entry.status == IndexStatus.BLOCKED
            else entry.blocked_versions[installed.version]
        )
        # a blocked version switches to the version the catalog offers instead
        return Decision(Verdict.BLOCKED, reason, entry.reason_url, offered)
    if not own_line:
        # the versions of another line cannot be installed again here
        if folder_hash == installed.content_hash:
            return Decision(Verdict.LOAD)
        return Decision(Verdict.CATALOG_UNREACHABLE)
    if entry is None:
        return Decision(Verdict.NOT_LISTED)
    release = entry.versions.get(installed.version)
    if release is None or release.content_hash != installed.content_hash:
        if offered:
            return Decision(Verdict.SWITCH, version=offered)
        return Decision(Verdict.NOT_FOR_THIS_VERSION)
    if folder_hash != installed.content_hash:
        return Decision(Verdict.REINSTALL)
    if offered and offered != installed.version:
        return Decision(Verdict.LOAD, version=offered)
    return Decision(Verdict.LOAD)


class Gate:
    """Decides by the kept copy of the index whether the installed providers may load."""

    def __init__(self, mass: MusicAssistant, store: CatalogStore) -> None:
        """
        Initialize the gate.

        :param mass: The Music Assistant instance.
        :param store: The store that holds the kept copy of the index.
        """
        self.mass = mass
        self._store = store

    def evaluate(
        self, domain: str, installed: InstalledProvider, folder_hash: str | None
    ) -> Decision:
        """
        Return whether an installed provider may load by the kept copy of the index.

        :param domain: The domain of the provider.
        :param installed: What was installed.
        :param folder_hash: The content hash of its live folder.
        """
        index = self._store.index
        return evaluate(
            installed,
            folder_hash,
            index.providers.get(domain) if index is not None else None,
            has_index=index is not None,
            own_line=self.own_index() is not None,
        )

    def own_index(self) -> CatalogIndex | None:
        """Return the kept copy of the index when it is the one of this server's line."""
        index = self._store.index
        if index is None or index.server_line != server_line(self.mass.version):
            return None
        return index

    def own_entry(self, domain: str) -> IndexProvider | None:
        """
        Return a provider in the index of this server's line, None when it is not in it.

        :param domain: The domain of the provider.
        """
        index = self.own_index()
        return index.providers.get(domain) if index is not None else None
