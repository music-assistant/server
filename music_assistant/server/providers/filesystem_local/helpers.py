"""Some helpers for Filesystem based Musicproviders."""
from __future__ import annotations

import os

from music_assistant.server.helpers.compare import compare_strings


def get_parentdir(base_path: str, name: str) -> str | None:
    """Look for folder name in path (to find dedicated artist or album folder)."""
    parentdir = os.path.dirname(base_path)
    for _ in range(3):
        dirname = parentdir.rsplit(os.sep)[-1]
        if compare_strings(name, dirname, False):
            return parentdir
        parentdir = os.path.dirname(parentdir)
    return None


def get_relative_path(base_path: str, path: str) -> str:
    """Return the relative path string for a path."""
    if path.startswith(base_path):
        path = path.split(base_path)[1]
    for sep in ("/", "\\"):
        if path.startswith(sep):
            path = path[1:]
    return path


def get_absolute_path(base_path: str, path: str) -> str:
    """Return the absolute path string for a path."""
    if path.startswith(base_path):
        return path
    return os.path.join(base_path, path)
