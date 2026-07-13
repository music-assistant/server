"""Security utilities for input validation."""

from __future__ import annotations

import os
from pathlib import Path


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


def is_safe_name(name: str) -> bool:
    """Check if name is safe for use (no path separators or traversal components)."""
    return not ("/" in name or "\\" in name or ".." in name)
