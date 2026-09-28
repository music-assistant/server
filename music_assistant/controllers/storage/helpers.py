"""Helpers for the storage controller."""

from __future__ import annotations

from pathlib import Path

from music_assistant.helpers.security import is_safe_path


def is_within(path: str, base: str) -> bool:
    """
    Return whether an absolute path is a base path or lies below it, compared lexically.

    :param path: The path to check.
    :param base: The absolute base path.
    """
    return "\0" not in path and Path(path).is_absolute() and is_safe_path(path, base)
