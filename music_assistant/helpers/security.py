"""Security utilities for input validation."""

from __future__ import annotations

import os


def is_safe_path(path: str) -> bool:
    """Check if path is free from path traversal components."""
    norm_path = os.path.normpath(path)
    return not (norm_path.startswith("..") or "/../" in norm_path or "\\..\\" in norm_path)


def is_safe_name(name: str) -> bool:
    """Check if name is safe for use (no path separators or traversal components)."""
    return not ("/" in name or "\\" in name or ".." in name)
