"""Security utilities for input validation."""

from __future__ import annotations

import fcntl
import os
import sys
from pathlib import Path
from typing import Any

from music_assistant.constants import ENCRYPT_SUFFIX


def is_safe_path(path: str, base_path: str | None = None) -> bool:
    """
    Check if path is free from path traversal components.

    :param path: The path to validate.
    :param base_path: If given, additionally require that path (resolved against
        base_path when relative) stays inside this base directory.
    """
    norm_path = os.path.normpath(path)
    if norm_path.startswith("..") or "/../" in norm_path or "\\..\\" in norm_path:
        return False
    if base_path is None:
        return True
    # Purely lexical containment check: no filesystem IO, so safe to call on the event loop.
    norm_base = os.path.normpath(base_path)
    if not Path(norm_path).is_absolute():
        norm_path = os.path.normpath(os.path.join(norm_base, norm_path))
    try:
        return os.path.commonpath((norm_base, norm_path)) == norm_base
    except ValueError:
        # commonpath raises for paths on different (Windows) drives
        return False


def real_path_of_fd(fd: int) -> str:
    """
    Return where the file of an open descriptor lies, with its symlinks resolved.

    Unlike os.path.realpath on its path, this names the file that was actually opened, also
    when its path was changed or re-pointed afterwards. NOT async friendly.

    :param fd: The open file descriptor.
    """
    if sys.platform == "darwin":
        # 1024 is MAXPATHLEN, the buffer size F_GETPATH writes the path into
        return os.fsdecode(fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024)).split(b"\0", 1)[0])
    return str(Path(f"/proc/self/fd/{fd}").readlink())


def has_control_chars(value: str) -> bool:
    """Check if value contains a CR, LF, NUL or any other C0 control character."""
    return any(ord(char) < 0x20 for char in value)


def contains_encrypted_value(value: Any) -> bool:
    """Check if value is, or holds in a nested list or dict, an encrypted config string."""
    if isinstance(value, str):
        return value.startswith(ENCRYPT_SUFFIX)
    if isinstance(value, dict):
        return any(contains_encrypted_value(item) for item in value.values())
    if isinstance(value, list):
        return any(contains_encrypted_value(item) for item in value)
    return False
