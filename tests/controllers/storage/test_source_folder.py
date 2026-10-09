"""Tests for the folder of a Local files source and the storage location holding it."""

from __future__ import annotations

from dataclasses import replace

import pytest
from music_assistant_models.auth import User, UserRole
from music_assistant_models.errors import InsufficientPermissions, InvalidDataError

from music_assistant.constants import CONF_PROVIDERS
from music_assistant.controllers.storage import StorageController, StorageKind
from music_assistant.controllers.webserver.helpers.auth_middleware import set_current_user
from tests.controllers.storage.conftest import FakeProbes, make_location, store_source

SOURCE = "filesystem_local--abc"
MEMBER = User(user_id="member", username="member", role=UserRole.USER)


async def test_the_most_specific_location_holds_the_folder(
    storage: StorageController, probes: FakeProbes
) -> None:
    """The folder comes with the innermost location around it, as last seen, without a probe."""
    nas = make_location("/mnt/nas", StorageKind.NETWORK_SHARE)
    music = replace(make_location("/mnt/nas/music"), used_by=["My music"])
    # no fresh probe answers: a probe would show up in the calls
    storage._locations = [nas, music]
    store_source(storage, "/mnt/nas/music/Rock/")

    result = await storage.get_source_folder(SOURCE)

    assert result.path == "/mnt/nas/music/Rock"
    assert result.location == music
    assert list(result.to_dict()) == ["path", "location"]
    assert probes.calls == []


async def test_a_folder_outside_every_location(storage: StorageController) -> None:
    """A folder no storage location holds comes without a location."""
    storage._locations = [make_location("/mnt/nas")]
    store_source(storage, "/srv/music")

    result = await storage.get_source_folder(SOURCE)

    assert (result.path, result.location) == ("/srv/music", None)


async def test_a_member_sees_the_location_without_its_private_details(
    storage: StorageController,
) -> None:
    """A member sees the location of its own source, not who uses it nor how it connects."""
    share = replace(
        make_location("/mnt/nas", StorageKind.NETWORK_SHARE, managed=True),
        share_name="nas",
        server="nas.local",
        share="music",
        used_by=["My music"],
    )
    storage._locations = [share]
    store_source(storage, "/mnt/nas/Rock")
    storage.mass.config.set(
        f"{CONF_PROVIDERS}/{SOURCE}/access", {"owner": MEMBER.user_id, "sharing": "private"}
    )
    set_current_user(MEMBER)

    result = await storage.get_source_folder(SOURCE)

    assert result.path == "/mnt/nas/Rock"
    assert result.location is not None
    assert result.location.path == "/mnt/nas"
    assert (result.location.share_name, result.location.server, result.location.share) == (
        None,
        None,
        None,
    )
    assert result.location.used_by == []


async def test_a_member_does_not_see_a_hidden_location(storage: StorageController) -> None:
    """On a host a member does not see a location Music Assistant did not set up."""
    storage._locations = [make_location("/media/disk", StorageKind.LOCAL_DISK)]
    store_source(storage, "/media/disk/Music")
    set_current_user(MEMBER)

    result = await storage.get_source_folder(SOURCE)

    assert (result.path, result.location) == ("/media/disk/Music", None)


async def test_a_member_may_not_ask_for_a_source_of_another_user(
    storage: StorageController,
) -> None:
    """The folder of a private source of another user stays hidden from a member."""
    store_source(storage, "/mnt/nas/Rock")
    storage.mass.config.set(
        f"{CONF_PROVIDERS}/{SOURCE}/access", {"owner": "someone_else", "sharing": "private"}
    )
    set_current_user(MEMBER)

    with pytest.raises(InsufficientPermissions):
        await storage.get_source_folder(SOURCE)


@pytest.mark.parametrize(
    ("domain", "has_folder"),
    [(None, False), ("spotify", True), ("filesystem_local", False)],
    ids=["unknown", "other_provider", "no_folder"],
)
async def test_only_a_local_files_source_has_a_folder(
    storage: StorageController, domain: str | None, has_folder: bool
) -> None:
    """
    An instance that is no Local files source reading a folder is refused.

    :param domain: The provider domain of the stored instance, None for no instance.
    :param has_folder: Whether a folder is stored with the instance.
    """
    if domain is not None:
        store_source(storage, "/mnt/nas", domain=domain)
        if not has_folder:
            storage.mass.config.set(f"{CONF_PROVIDERS}/{SOURCE}/setup_data", {})

    with pytest.raises(InvalidDataError) as exc_info:
        await storage.get_source_folder(SOURCE)

    assert exc_info.value.translation_key == "not_a_folder_source"
