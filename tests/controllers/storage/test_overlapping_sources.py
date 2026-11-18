"""Tests for the music sources that read the files of a folder too."""

from __future__ import annotations

import pytest
from music_assistant_models.auth import User, UserRole
from music_assistant_models.config_entries import ProviderAccess
from music_assistant_models.enums import ProviderSharing

from music_assistant.constants import CONF_PROVIDERS
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


def test_only_sources_a_user_may_use_overlap(storage: StorageController) -> None:
    """For a user, only the sources of the household and those shared with it count."""
    store_source(storage, "/media", "filesystem_local--home", "Household")
    for instance_id, name, sharing in (
        ("filesystem_local--shared", "Shared", ProviderSharing.MEMBERS),
        ("filesystem_local--private", "Private", ProviderSharing.PRIVATE),
    ):
        store_source(storage, "/media", instance_id, name)
        storage.mass.config.set(
            f"{CONF_PROVIDERS}/{instance_id}/access",
            ProviderAccess(owner="other", sharing=sharing).to_dict(),
        )
    member = User(user_id="member", username="member", role=UserRole.USER)

    assert storage.get_overlapping_sources("/media/Music", user=member) == ["Household", "Shared"]
    assert storage.get_overlapping_sources("/media/Music") == ["Household", "Private", "Shared"]
