"""Some helpers for Filesystem based Musicproviders."""

from __future__ import annotations

import os

from music_assistant.server.helpers.compare import compare_strings


def get_artist_dir(album_path: str, artist_name: str) -> str | None:
    """Look for (Album)Artist directory in path of album."""
    parentdir = os.path.dirname(album_path)
    dirname = parentdir.rsplit(os.sep)[-1]
    if compare_strings(artist_name, dirname, False):
        return parentdir
    return None


def get_disc_dir(track_path: str, album_name: str, disc_number: int | None) -> str | None:
    """Look for disc directory in path of album/tracks."""
    parentdir = os.path.dirname(track_path)
    dirname = parentdir.rsplit(os.sep)[-1]
    dirname_lower = dirname.lower()
    if disc_number and compare_strings(f"disc {disc_number}", dirname, False):
        return parentdir
    if dirname_lower.startswith(album_name.lower()) and "disc" in dirname_lower:
        return parentdir
    if dirname_lower.startswith(album_name.lower()) and dirname_lower.endswith(str(disc_number)):
        return parentdir
    return None


def get_album_dir(track_path: str, album_name: str, disc_dir: str | None) -> str | None:
    """Return album/parent directory of a track."""
    parentdir = os.path.dirname(track_path)
    # account for disc sublevel by ignoring 1 level if needed
    for _ in range(2 if disc_dir else 1):
        dirname = parentdir.rsplit(os.sep)[-1]
        dirname_lower = dirname.lower()
        if compare_strings(album_name, dirname, False):
            return parentdir
        if album_name in dirname_lower:
            return parentdir
        if dirname_lower in album_name:
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
