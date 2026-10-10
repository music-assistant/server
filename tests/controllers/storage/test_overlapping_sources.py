"""Tests for the music sources that read the files of a folder too."""

from __future__ import annotations

import pytest

from music_assistant.controllers.storage import StorageController
from tests.controllers.storage.conftest import store_source


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/media/nas_music", ["Everything", "Share"]),
        ("/media/nas_music/Albums", ["Everything", "Share"]),
        ("/media", ["Everything", "live", "Share"]),
        ("/media/nas", ["Everything"]),
        ("/mnt/usb", []),
    ],
    ids=["same_folder", "inside", "holding", "look_alike", "elsewhere"],
)
def test_sources_around_or_inside_a_folder_overlap(
    storage: StorageController, path: str, expected: list[str]
) -> None:
    """
    A source whose folder holds the folder, is it or lies inside it reads files of it too.

    :param path: The folder to check.
    :param expected: The names of the overlapping sources, sorted regardless of case.
    """
    store_source(storage, "/media", "filesystem_local--all", "Everything")
    store_source(storage, "/media/nas_music", "filesystem_local--nas", "Share")
    store_source(storage, "/media/Live", "filesystem_local--live", "live")

    assert storage.get_overlapping_sources(path) == expected


def test_left_out_sources_do_not_overlap(storage: StorageController) -> None:
    """The excluded source, a disabled source and a source of another kind are left out."""
    store_source(storage, "/media", "filesystem_local--self", "Itself")
    store_source(storage, "/media", "filesystem_local--off", "Disabled", enabled=False)
    store_source(storage, "/media", "other--abc", "Other", domain="other")
    store_source(storage, "/media/nas_music", "filesystem_local--nas", "Share")

    assert storage.get_overlapping_sources("/media", exclude="filesystem_local--self") == ["Share"]
