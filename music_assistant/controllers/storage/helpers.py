"""Helpers for the storage controller."""

from __future__ import annotations

import re
from collections.abc import Collection
from pathlib import Path, PurePosixPath

from music_assistant.controllers.storage.models import ShareType
from music_assistant.helpers.security import is_safe_path

_INVALID_NAME_CHARS = re.compile(r"[^A-Za-z0-9_]")


def is_within(path: str, base: str) -> bool:
    """
    Return whether an absolute path is a base path or lies below it, compared lexically.

    :param path: The path to check.
    :param base: The absolute base path.
    """
    return "\0" not in path and Path(path).is_absolute() and is_safe_path(path, base)


def share_key(share_type: str, server: str, share: str) -> tuple[str, str, str]:
    """
    Return what identifies a network share: the same share on the same server has the same key.

    Server names and cifs share names compare case-insensitively, nfs export paths as they are.

    :param share_type: The protocol of the share.
    :param server: The hostname or IP address of the server.
    :param share: The share name of a cifs share, the export path of an nfs share.
    """
    if share_type == ShareType.CIFS:
        share = share.casefold()
    return str(share_type), server.casefold(), share


def allocate_share_name(share_type: ShareType, share: str, taken: Collection[str]) -> str:
    """
    Return a free name for a new network share, derived from the share.

    The name holds letters, digits and underscores only, in lower case: the share name of a
    cifs share or the last part of the export path of an nfs share, ``share`` when nothing of it
    is left, with ``_2``, ``_3``, ... appended when the name is taken.

    :param share_type: The protocol of the share.
    :param share: The share name of a cifs share, the export path of an nfs share.
    :param taken: The names that are in use.
    """
    source = share if share_type == ShareType.CIFS else PurePosixPath(share).name
    base = _INVALID_NAME_CHARS.sub("_", source).strip("_").lower() or "share"
    name, counter = base, 1
    while name in taken:
        counter += 1
        name = f"{base}_{counter}"
    return name
