"""The decision whether an installed community provider may load."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from music_assistant.controllers.community_catalog.models import IndexStatus

if TYPE_CHECKING:
    from music_assistant.controllers.community_catalog.models import (
        IndexProvider,
        InstalledProvider,
    )


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
