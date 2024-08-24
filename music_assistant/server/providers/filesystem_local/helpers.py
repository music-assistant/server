"""Some helpers for Filesystem based Musicproviders."""

from __future__ import annotations

import os

from music_assistant.server.helpers.compare import compare_strings


def get_artist_dir(album_or_track_dir: str, artist_name: str) -> str | None:
    """Look for (Album)Artist directory in path of a track (or album)."""
    parentdir = os.path.dirname(album_or_track_dir)
    # account for disc or album sublevel by ignoring (max) 2 levels if needed
    matched_dir: str | None = None
    for _ in range(3):
        dirname = parentdir.rsplit(os.sep)[-1]
        if compare_strings(artist_name, dirname, False):
            # literal match
            # we keep hunting further down to account for the
            # edge case where the album name has the same name as the artist
            matched_dir = parentdir
        parentdir = os.path.dirname(parentdir)
    return matched_dir


def get_album_dir(track_dir: str, album_name: str) -> str | None:
    """Return album/parent directory of a track."""
    parentdir = track_dir
    # account for disc sublevel by ignoring 1 level if needed
    for _ in range(2):
        dirname = parentdir.rsplit(os.sep)[-1]
        if compare_strings(album_name, dirname, False):
            # literal match
            return parentdir
        if compare_strings(album_name, dirname.split(" - ")[-1], False):
            # account for ArtistName - AlbumName format in the directory name
            return parentdir
        if compare_strings(album_name, dirname.split("(")[0], False):
            # account for ArtistName - AlbumName (Version) format in the directory name
            return parentdir
        if compare_strings(album_name.split("(")[0], dirname, False):
            # account for AlbumName (Version) format in the album name
            return parentdir
        if compare_strings(album_name.split("(")[0], dirname.split(" - ")[-1], False):
            # account for ArtistName - AlbumName (Version) format
            return parentdir
        if len(album_name) > 8 and album_name in dirname:
            # dirname contains album name
            # (could potentially lead to false positives, hence the length check)
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
