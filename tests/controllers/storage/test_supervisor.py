"""Tests for network shares mounted by the Home Assistant Supervisor."""

from __future__ import annotations

import logging
import time
from typing import Any
from unittest.mock import MagicMock

import pytest
from music_assistant_models.auth import User, UserRole
from music_assistant_models.errors import ActionUnavailable, InvalidDataError, SetupFailedError

from music_assistant.constants import CONF_STORAGE_SHARES
from music_assistant.controllers.storage import StorageController, StorageKind
from music_assistant.controllers.storage import controller as controller_module
from music_assistant.controllers.storage.backends.base import BackendUnavailable
from music_assistant.controllers.storage.backends.supervisor import create_supervisor_mounter
from music_assistant.controllers.storage.models import MountBackend, NetworkShareSpec, ShareType
from music_assistant.controllers.webserver.helpers.auth_middleware import set_current_user
from tests.common import capture_log_records
from tests.controllers.storage.conftest import (
    FakeBackends,
    FakeMounter,
    FakeSupervisor,
    wait_until,
)

pytestmark = pytest.mark.usefixtures("probes")

NOT_REACHABLE = "Mount music is not reachable. Check the Supervisor logs for details"


@pytest.fixture
async def ready(storage: StorageController, supervisor: FakeSupervisor) -> FakeSupervisor:
    """
    Provide the fake Supervisor after the storage controller found it as its mount backend.

    :param storage: The storage controller.
    :param supervisor: The fake Supervisor.
    """
    await storage._probe_backends()
    supervisor.requests.clear()
    return supervisor


def _store(
    storage: StorageController,
    supervisor: FakeSupervisor,
    name: str,
    server: str = "nas.local",
    **kwargs: Any,
) -> None:
    """
    Store a share the Supervisor mounted, as an earlier add did.

    :param storage: The storage controller.
    :param supervisor: The fake Supervisor.
    :param name: The name of the share.
    :param server: The server of the share.
    :param kwargs: Other settings of the share.
    """
    spec = NetworkShareSpec(
        name=name,
        share_type=ShareType.CIFS,
        server=server,
        share=kwargs.pop("share", name),
        backend=MountBackend.SUPERVISOR,
        path=supervisor.path(name),
        **kwargs,
    )
    storage.mass.config.set(f"{CONF_STORAGE_SHARES}/{name}", spec.to_dict())


def _mutations(supervisor: FakeSupervisor) -> list[tuple[str, str]]:
    """Return the requests that changed a mount at the fake Supervisor."""
    return [request[:2] for request in supervisor.requests if request[0] != "GET"]


async def test_backend_found(storage: StorageController, supervisor: FakeSupervisor) -> None:
    """With access to the mounts the Supervisor is the backend, with its own protocol versions."""
    await storage._probe_backends()

    info = await storage.get_info()

    assert (info.can_mount_shares, info.mount_backend) == (True, MountBackend.SUPERVISOR)
    assert info.supported_share_types == [ShareType.CIFS, ShareType.NFS]
    assert info.supported_share_versions == {ShareType.CIFS: ["1.0", "2.0"], ShareType.NFS: []}
    assert supervisor.requests[0][:2] == ("GET", "/mounts")


async def test_backend_without_manager_role(
    storage: StorageController, supervisor: FakeSupervisor
) -> None:
    """A 403 means the app has no manager role: not a backend, and the reason is kept."""
    supervisor.refuse_access = True

    await storage._probe_backends()

    assert not (await storage.get_info()).can_mount_shares
    diagnostics = await storage.get_diagnostics()
    assert "403" in str(diagnostics["mount_backends"])
    assert "manager role" in str(diagnostics["mount_backends"])


async def test_no_supervisor(storage: StorageController, supervisor: FakeSupervisor) -> None:
    """Outside a Supervisor nothing is asked of it."""
    storage.mass.running_as_hass_addon = False

    await storage._probe_backends()

    assert not (await storage.get_info()).can_mount_shares
    assert supervisor.requests == []


async def test_only_the_supervisor_mounts_under_a_supervisor(
    storage: StorageController,
    supervisor: FakeSupervisor,
    backends: FakeBackends,
    mount_table: Any,
) -> None:
    """
    While the Supervisor does not answer, nothing mounts: never the server itself.

    The app does not keep its mount privileges, so a share it mounted itself could not be
    mounted again later.
    """
    local = FakeMounter(mount_table)
    backends.available[MountBackend.LOCAL_MOUNT] = local
    supervisor.refuse_access = True
    await storage._probe_backends()

    with pytest.raises(ActionUnavailable) as exc_info:
        await storage.add_network_share(ShareType.CIFS, "nas.local", "music", "marcel", "pw")

    assert exc_info.value.translation_key == "supervisor_mounts_unavailable"
    assert local.calls == []
    assert backends.probes == 0
    assert storage.mass.config.get(CONF_STORAGE_SHARES) is None


async def test_info_under_a_supervisor_that_does_not_answer(
    storage: StorageController,
    supervisor: FakeSupervisor,
    backends: FakeBackends,
    mount_table: Any,
) -> None:
    """The info says nothing can mount while the Supervisor does not answer, then it can."""
    backends.available[MountBackend.LOCAL_MOUNT] = FakeMounter(mount_table)
    supervisor.refuse_access = True
    await storage._probe_backends()

    info = await storage.get_info()
    assert (info.can_mount_shares, info.mount_backend, info.supported_share_types) == (
        False,
        None,
        [],
    )

    supervisor.refuse_access = False
    info = await storage.get_info()
    assert (info.can_mount_shares, info.mount_backend) == (True, MountBackend.SUPERVISOR)


async def test_add(storage: StorageController, ready: FakeSupervisor) -> None:
    """A share becomes a media mount in the media folder, replacing what discovery sees there."""
    location = await storage.add_network_share(
        ShareType.CIFS, " nas.local ", "Music", username="marcel", password="secret", version="2.0"
    )

    # one look for a mount of the share, one for the names in use
    assert ready.requests == [
        ("GET", "/mounts", None),
        ("GET", "/mounts", None),
        (
            "POST",
            "/mounts",
            {
                "name": "music",
                "type": "cifs",
                "usage": "media",
                "server": "nas.local",
                "read_only": False,
                "share": "Music",
                "username": "marcel",
                "password": "secret",
                "version": "2.0",
            },
        ),
    ]
    path = ready.path("music")
    assert (location.path, location.kind, location.managed, location.available) == (
        path,
        StorageKind.NETWORK_SHARE,
        True,
        True,
    )
    assert (location.backend, location.mountpoint, location.fstype) == (
        MountBackend.SUPERVISOR,
        path,
        "cifs",
    )
    assert [loc.path for loc in storage.get_locations()].count(path) == 1
    stored = storage.mass.config.get(f"{CONF_STORAGE_SHARES}/music")
    assert (stored["backend"], stored["path"]) == ("supervisor", path)


async def test_add_as_guest(storage: StorageController, ready: FakeSupervisor) -> None:
    """Without credentials and version the Supervisor gets neither: it mounts as guest."""
    await storage.add_network_share(ShareType.CIFS, "nas.local", "music", password="unused")

    body = ready.requests[-1][2]
    assert body is not None
    assert not {"username", "password", "version"} & body.keys()


async def test_add_nfs(storage: StorageController, ready: FakeSupervisor) -> None:
    """An NFS export is sent as a path, named after its last part."""
    location = await storage.add_network_share(ShareType.NFS, "nas.local", "/volume1/My Music")

    assert ready.requests[-1][2] == {
        "name": "my_music",
        "type": "nfs",
        "usage": "media",
        "server": "nas.local",
        "read_only": False,
        "path": "/volume1/My Music",
    }
    assert location.path == ready.path("my_music")


@pytest.mark.parametrize(
    ("share_type", "version"),
    [(ShareType.CIFS, "3.0"), (ShareType.CIFS, "2.1"), (ShareType.NFS, "4")],
)
async def test_version_the_supervisor_can_not_pin(
    storage: StorageController, ready: FakeSupervisor, share_type: ShareType, version: str
) -> None:
    """A version the Supervisor can not pin is refused before anything is sent."""
    share = "music" if share_type == ShareType.CIFS else "/music"

    with pytest.raises(InvalidDataError) as exc_info:
        await storage.add_network_share(share_type, "nas.local", share, version=version)

    assert exc_info.value.translation_key == "share_version_not_supported"
    assert ready.requests == []


async def test_stored_version_is_only_sent_when_the_supervisor_takes_it(
    storage: StorageController, ready: FakeSupervisor
) -> None:
    """A share stored with another version is mounted with the version negotiated."""
    _store(storage, ready, "music", version="3.0")

    await storage.reconcile()

    body = ready.requests[-1][2]
    assert body is not None
    assert "version" not in body
    assert "music" in ready.mounts


async def test_user_without_password_is_never_sent(
    storage: StorageController, ready: FakeSupervisor
) -> None:
    """A stored user without password mounts as guest instead of breaking the mount."""
    _store(storage, ready, "music", username="marcel")

    await storage.reconcile()

    body = ready.requests[-1][2]
    assert body is not None
    assert not {"username", "password"} & body.keys()
    assert "music" in ready.mounts


async def test_name_collision(storage: StorageController, ready: FakeSupervisor) -> None:
    """A name taken by a stored share, a Supervisor mount or the media folder gets a suffix."""
    _store(storage, ready, "music", server="other.local")
    ready.add_mount("music_2", type="cifs", server="other.local", share="music_2")
    (ready.media / "music_3").mkdir()

    location = await storage.add_network_share(ShareType.CIFS, "nas.local", "music")

    assert location.share_name == "music_4"
    assert location.path == ready.path("music_4")
    assert "music_4" in ready.mounts


async def test_folder_with_files_is_taken(
    storage: StorageController, ready: FakeSupervisor
) -> None:
    """A folder of the user in the media folder is left alone."""
    (ready.media / "music").mkdir()
    (ready.media / "music" / "song.flac").touch()

    location = await storage.add_network_share(ShareType.CIFS, "nas.local", "music")

    assert location.share_name == "music_2"
    assert (ready.media / "music" / "song.flac").exists()


async def test_failed_add_leaves_no_folder_behind(
    storage: StorageController, ready: FakeSupervisor
) -> None:
    """A first attempt that fails does not push the next one to another name."""
    ready.unreachable.add("nas.local")
    with pytest.raises(SetupFailedError):
        await storage.add_network_share(ShareType.CIFS, "nas.local", "music")
    assert not (ready.media / "music").exists()

    ready.unreachable.clear()
    location = await storage.add_network_share(ShareType.CIFS, "nas.local", "music")

    assert location.share_name == "music"


async def test_remove_and_add_again(storage: StorageController, ready: FakeSupervisor) -> None:
    """A share removed and added again gets its name back."""
    await storage.add_network_share(ShareType.CIFS, "nas.local", "music")
    await storage.remove_network_share("music")
    assert not (ready.media / "music").exists()

    location = await storage.add_network_share(ShareType.CIFS, "nas.local", "music")

    assert location.share_name == "music"


async def test_share_mounted_in_home_assistant_is_refused(
    storage: StorageController, ready: FakeSupervisor
) -> None:
    """
    A share Home Assistant mounted already is a storage location as it is.

    Nothing is stored for it and no credentials are sent: it is picked where discovery lists it.
    """
    ready.add_mount("nas_music", type="cifs", server="NAS.local", share="Music")
    ready.requests.clear()

    with pytest.raises(InvalidDataError) as exc_info:
        await storage.add_network_share(
            ShareType.CIFS, "nas.local", "music", username="marcel", password="secret"
        )

    assert exc_info.value.translation_key == "share_mounted_already"
    assert exc_info.value.translation_args == [ready.path("nas_music")]
    assert [request[:2] for request in ready.requests] == [("GET", "/mounts")]
    assert storage.mass.config.get(CONF_STORAGE_SHARES) is None


async def test_export_mounted_in_home_assistant_is_refused(
    storage: StorageController, ready: FakeSupervisor
) -> None:
    """An export path compares the way the Supervisor stores it."""
    ready.add_mount("music", type="nfs", server="nas.local", path="/volume1/music")

    with pytest.raises(InvalidDataError) as exc_info:
        await storage.add_network_share(ShareType.NFS, "nas.local", "/volume1/music/")

    assert exc_info.value.translation_args == [ready.path("music")]


@pytest.mark.parametrize(
    "mount",
    [
        # not in the media folder
        {"type": "cifs", "server": "nas.local", "share": "music", "usage": "share"},
        {"type": "cifs", "server": "old.local", "share": "music"},
        {"type": "cifs", "server": "nas.local", "share": "movies"},
        {"type": "nfs", "server": "nas.local", "path": "music"},
    ],
)
async def test_other_mounts_in_home_assistant_are_no_reason_to_refuse(
    storage: StorageController, ready: FakeSupervisor, mount: dict[str, str]
) -> None:
    """Another usage, server, share or protocol is another share."""
    ready.add_mount("other", **mount)

    location = await storage.add_network_share(ShareType.CIFS, "nas.local", "music")

    assert (location.share_name, location.managed) == ("music", True)
    assert ready.requests[-1][:2] == ("POST", "/mounts")


async def test_update_into_a_share_mounted_in_home_assistant(
    storage: StorageController, ready: FakeSupervisor
) -> None:
    """A share can not be changed into one Home Assistant mounted; its own mount is fine."""
    await storage.add_network_share(ShareType.CIFS, "nas.local", "music")
    ready.add_mount("movies", type="cifs", server="nas.local", share="movies")

    with pytest.raises(InvalidDataError) as exc_info:
        await storage.update_network_share("music", "nas.local", "movies")
    location = await storage.update_network_share("music", "nas.local", "music", read_only=True)

    assert exc_info.value.translation_key == "share_mounted_already"
    assert ready.mounts["movies"]["share"] == "movies"
    assert (location.read_only, ready.mounts["music"]["read_only"]) == (True, True)


async def test_failed_activation_stores_nothing(
    storage: StorageController, ready: FakeSupervisor
) -> None:
    """A share the Supervisor could not mount is not kept, by the Supervisor or by us."""
    ready.unreachable.add("nas.local")

    with pytest.raises(SetupFailedError) as exc_info:
        await storage.add_network_share(ShareType.CIFS, "nas.local", "music")

    assert exc_info.value.translation_key == "mount_failed"
    assert exc_info.value.translation_args == [NOT_REACHABLE]
    assert ready.mounts == {}
    assert storage.mass.config.get(CONF_STORAGE_SHARES) is None


async def test_share_that_stays_behind_its_trigger_is_not_kept(
    storage: StorageController, ready: FakeSupervisor
) -> None:
    """The Supervisor's word is not enough: a share the probe could not wake is removed again."""
    ready.dormant.add("music")

    with pytest.raises(SetupFailedError) as exc_info:
        await storage.add_network_share(ShareType.CIFS, "nas.local", "music")

    assert exc_info.value.translation_key == "share_not_mounted"
    assert ready.requests[-1][:2] == ("DELETE", "/mounts/music")
    assert ready.mounts == {}
    assert not (ready.media / "music").exists()
    assert storage.mass.config.get(CONF_STORAGE_SHARES) is None


async def test_update(storage: StorageController, ready: FakeSupervisor) -> None:
    """New settings replace the mount at the Supervisor under the same name and path."""
    await storage.add_network_share(ShareType.CIFS, "nas.local", "music", "marcel", "secret")

    location = await storage.update_network_share("music", "nas2.local", "music", "marcel")

    assert ready.requests[-1][:2] == ("PUT", "/mounts/music")
    assert ready.mounts["music"]["server"] == "nas2.local"
    # the stored password is sent again
    assert ready.mounts["music"]["password"] == "secret"
    assert (location.server, location.path, location.available) == (
        "nas2.local",
        ready.path("music"),
        True,
    )


async def test_update_of_a_mount_removed_in_home_assistant(
    storage: StorageController, ready: FakeSupervisor
) -> None:
    """A share whose mount was deleted in Home Assistant is created with its new settings."""
    _store(storage, ready, "music")

    location = await storage.update_network_share("music", "nas2.local", "music")

    assert _mutations(ready) == [("PUT", "/mounts/music"), ("POST", "/mounts")]
    assert ready.mounts["music"]["server"] == "nas2.local"
    assert location.available


async def test_failed_update_restores_the_previous_mount(
    storage: StorageController, ready: FakeSupervisor
) -> None:
    """A failed update leaves the previous mount at the Supervisor, unmounted: a reload mounts it."""
    await storage.add_network_share(ShareType.CIFS, "nas.local", "music")
    ready.unreachable.add("dead.local")

    with pytest.raises(SetupFailedError):
        await storage.update_network_share("music", "dead.local", "music")

    assert [request[:2] for request in ready.requests[-2:]] == [
        ("PUT", "/mounts/music"),
        ("POST", "/mounts/music/reload"),
    ]
    assert ready.mounts["music"]["server"] == "nas.local"
    assert storage.mass.config.get(f"{CONF_STORAGE_SHARES}/music")["server"] == "nas.local"
    assert storage.get_location_for_path(ready.path("music")).available  # type: ignore[union-attr]


async def test_update_that_does_not_show_up_is_rolled_back(
    storage: StorageController, ready: FakeSupervisor
) -> None:
    """New settings the Supervisor accepted but that did not mount are replaced by the old ones."""
    await storage.add_network_share(ShareType.CIFS, "nas.local", "music")
    ready.dormant.add("music")

    with pytest.raises(SetupFailedError) as exc_info:
        await storage.update_network_share("music", "nas2.local", "music")

    assert exc_info.value.translation_key == "share_not_mounted"
    assert ready.requests[-1][:2] == ("PUT", "/mounts/music")
    assert ready.mounts["music"]["server"] == "nas.local"
    assert storage.mass.config.get(f"{CONF_STORAGE_SHARES}/music")["server"] == "nas.local"


async def test_supervisor_found_later(
    storage: StorageController, supervisor: FakeSupervisor
) -> None:
    """Under a Supervisor that did not answer at first, the next info asks it again."""
    supervisor.refuse_access = True
    await storage._probe_backends()
    supervisor.refuse_access = False

    info = await storage.get_info()

    assert (info.can_mount_shares, info.mount_backend) == (True, MountBackend.SUPERVISOR)


async def test_remove(storage: StorageController, ready: FakeSupervisor) -> None:
    """Removing a share removes its mount from the Supervisor, and its folder."""
    await storage.add_network_share(ShareType.CIFS, "nas.local", "music")

    await storage.remove_network_share("music")

    assert ready.mounts == {}
    assert storage.mass.config.get(CONF_STORAGE_SHARES) == {}
    assert storage.get_location_for_path(ready.path("music")) is None
    assert not (ready.media / "music").exists()


async def test_remove_a_mount_that_is_gone(
    storage: StorageController, ready: FakeSupervisor
) -> None:
    """A share whose mount was removed in Home Assistant already is removed without an error."""
    _store(storage, ready, "music")
    (ready.media / "music").mkdir()

    await storage.remove_network_share("music")

    assert ready.requests[-1][:2] == ("DELETE", "/mounts/music")
    assert storage.mass.config.get(CONF_STORAGE_SHARES) == {}
    assert not (ready.media / "music").exists()


async def test_remove_that_fails_keeps_the_share(
    storage: StorageController, ready: FakeSupervisor
) -> None:
    """A mount the Supervisor could not remove stays a managed share, and the user is told."""
    await storage.add_network_share(ShareType.CIFS, "nas.local", "music")
    ready.stuck.add("music")

    with pytest.raises(SetupFailedError) as exc_info:
        await storage.remove_network_share("music")

    assert exc_info.value.translation_key == "unmount_failed"
    assert "music" in storage.mass.config.get(CONF_STORAGE_SHARES)
    assert (ready.media / "music").exists()


async def test_remove_while_the_supervisor_does_not_answer(
    storage: StorageController, supervisor: FakeSupervisor
) -> None:
    """A Supervisor that may only be busy keeps its mount, and so does the record."""
    _store(storage, supervisor, "music")
    supervisor.refuse_access = True

    with pytest.raises(ActionUnavailable) as exc_info:
        await storage.remove_network_share("music")

    assert exc_info.value.translation_key == "mount_backend_unavailable"
    assert "music" in storage.mass.config.get(CONF_STORAGE_SHARES)


async def test_remove_where_there_is_no_supervisor(
    storage: StorageController, supervisor: FakeSupervisor
) -> None:
    """A share of a Supervisor this installation no longer has is forgotten, and that is logged."""
    _store(storage, supervisor, "music")
    storage.mass.running_as_hass_addon = False

    with capture_log_records(storage.logger) as records:
        await storage.remove_network_share("music")

    assert storage.mass.config.get(CONF_STORAGE_SHARES) == {}
    assert any(
        record.levelno == logging.WARNING
        and record.getMessage().startswith("Forgetting network share music")
        for record in records
    )
    assert supervisor.requests == []


async def test_reload_recreates_a_mount_that_is_gone(
    storage: StorageController, ready: FakeSupervisor
) -> None:
    """A share whose mount was removed in Home Assistant gets it back from its stored settings."""
    _store(
        storage,
        ready,
        "music",
        username="marcel",
        password=storage.mass.config.encrypt_string("pw"),
    )

    location = await storage.reload_network_share("music")

    assert _mutations(ready) == [("POST", "/mounts/music/reload"), ("POST", "/mounts")]
    assert ready.mounts["music"]["password"] == "pw"
    assert location.available


async def test_reload_of_a_share_that_does_not_answer(
    storage: StorageController, ready: FakeSupervisor
) -> None:
    """A reload that fails shows why on the location, and the next reload can fix it."""
    await storage.add_network_share(ShareType.CIFS, "nas.local", "music")
    ready.unreachable.add("nas.local")

    with pytest.raises(SetupFailedError):
        await storage.reload_network_share("music")

    location = storage.get_location_for_path(ready.path("music"))
    assert location is not None
    assert (location.available, location.error_key, location.error_args) == (
        False,
        "mount_failed",
        [NOT_REACHABLE],
    )
    ready.unreachable.clear()
    assert (await storage.reload_network_share("music")).error is None


async def test_reconcile(storage: StorageController, ready: FakeSupervisor) -> None:
    """A missing mount is created again, a mount that is there is left alone."""
    _store(storage, ready, "music")
    _store(storage, ready, "movies")
    ready.add_mount("movies", type="cifs", server="nas.local", share="movies")
    ready.requests.clear()

    await storage.reconcile()

    assert [request[:2] for request in ready.requests] == [("GET", "/mounts"), ("POST", "/mounts")]
    assert set(ready.mounts) == {"music", "movies"}
    assert storage.get_location_for_path(ready.path("music")).available  # type: ignore[union-attr]


CHANGED_MOUNTS = [
    pytest.param({"type": "cifs", "server": "nas2.local", "share": "music"}, id="server"),
    pytest.param({"type": "cifs", "server": "nas.local", "share": "movies"}, id="share"),
    pytest.param({"type": "nfs", "server": "nas.local", "path": "/music"}, id="type"),
    pytest.param(
        {"type": "cifs", "server": "nas.local", "share": "music", "usage": "share"}, id="usage"
    ),
]


@pytest.mark.parametrize("mount", CHANGED_MOUNTS)
async def test_mount_changed_in_home_assistant_is_left_alone(
    storage: StorageController, ready: FakeSupervisor, mount: dict[str, str]
) -> None:
    """
    A mount under the name of a share that the user changed in Home Assistant is theirs now.

    It is neither mounted again nor changed: the share stays and its location says why it is not
    available, until the share is removed, which forgets it and leaves the mount where it is.
    """
    _store(storage, ready, "music")
    ready.add_mount("music", **mount)
    changed = {"name": "music", "usage": "media", "read_only": False, **mount}
    ready.requests.clear()

    await storage.reconcile()
    with pytest.raises(ActionUnavailable) as reload_error:
        await storage.reload_network_share("music")
    with pytest.raises(ActionUnavailable) as update_error:
        await storage.update_network_share("music", "nas.local", "music")

    assert reload_error.value.translation_key == "share_changed"
    assert update_error.value.translation_key == "share_changed"
    assert _mutations(ready) == []
    assert ready.mounts["music"] == changed
    location = storage.get_location_for_path(ready.path("music"))
    assert location is not None
    assert (location.managed, location.available, location.error_key) == (
        True,
        False,
        "share_changed",
    )

    await storage.remove_network_share("music")

    assert _mutations(ready) == []
    assert ready.mounts["music"] == changed
    assert (ready.media / "music" / "share.txt").exists()
    assert storage.mass.config.get(CONF_STORAGE_SHARES) == {}
    # whatever discovery shows there is no managed share any more
    assert not any(
        loc.managed for loc in storage.get_locations() if loc.path == ready.path("music")
    )


async def test_changed_mount_is_not_forgotten_while_in_use(
    storage: StorageController, ready: FakeSupervisor
) -> None:
    """A share a loaded music source reads from is not removed, whatever became of its mount."""
    _store(storage, ready, "music")
    ready.add_mount("music", type="cifs", server="nas2.local", share="music")
    source = MagicMock(domain="filesystem_local", base_path=ready.path("music"))
    source.name = "My music"
    storage.mass._providers["filesystem_local--abc"] = source

    with pytest.raises(ActionUnavailable) as exc_info:
        await storage.remove_network_share("music")

    assert exc_info.value.translation_key == "location_in_use"
    assert "music" in storage.mass.config.get(CONF_STORAGE_SHARES)


async def test_info_notes_a_mount_changed_in_home_assistant(
    storage: StorageController, ready: FakeSupervisor
) -> None:
    """Opening the storage page shows a share edited in Home Assistant meanwhile, and back again."""
    await storage.add_network_share(ShareType.CIFS, "nas.local", "music")
    ready.mounts["music"]["server"] = "nas2.local"
    ready.requests.clear()

    info = await storage.get_info()

    location = next(loc for loc in info.locations if loc.path == ready.path("music"))
    assert (location.available, location.error_key) == (False, "share_changed")
    assert _mutations(ready) == []
    ready.mounts["music"]["server"] = "NAS.local"
    location = next(
        loc for loc in (await storage.get_info()).locations if loc.path == ready.path("music")
    )
    assert (location.available, location.error) == (True, None)


async def test_member_info_does_not_ask_the_supervisor(
    storage: StorageController, ready: FakeSupervisor
) -> None:
    """A caller that does not manage every source makes the server ask the Supervisor nothing."""
    await storage.add_network_share(ShareType.CIFS, "nas.local", "music")
    ready.mounts["music"]["server"] = "nas2.local"
    ready.requests.clear()
    set_current_user(User(user_id="member", username="member", role=UserRole.USER))

    info = await storage.get_info()

    assert ready.requests == []
    location = next(loc for loc in info.locations if loc.path == ready.path("music"))
    assert (location.available, location.error) == (True, None)


@pytest.mark.parametrize("trouble", ["failing", "slow", "busy"])
async def test_info_while_the_share_states_are_unknown(
    storage: StorageController,
    ready: FakeSupervisor,
    monkeypatch: pytest.MonkeyPatch,
    trouble: str,
) -> None:
    """
    A Supervisor that fails or is slow, or a share command at work, leaves the list as it was.

    The info still answers, and quickly.
    """
    monkeypatch.setattr(controller_module, "SHARE_STATES_TIMEOUT", 0.2)
    _store(storage, ready, "music")
    _store(storage, ready, "movies")
    ready.add_mount("music", type="cifs", server="nas.local", share="music")
    ready.add_mount("movies", type="cifs", server="nas2.local", share="movies")
    await storage.reconcile()
    # edited back and forth in Home Assistant: the info can not know
    ready.mounts["music"]["server"] = "nas2.local"
    ready.mounts["movies"]["server"] = "nas.local"
    if trouble == "failing":
        ready.refuse_access = True
    elif trouble == "slow":
        ready.list_delay = 1

    started = time.monotonic()
    if trouble == "busy":
        async with storage._shares_lock:
            info = await storage.get_info()
    else:
        info = await storage.get_info()

    assert time.monotonic() - started < 0.8
    by_name = {loc.share_name: loc for loc in info.locations if loc.share_name}
    assert (by_name["music"].available, by_name["music"].error) == (True, None)
    assert (by_name["movies"].available, by_name["movies"].error_key) == (False, "share_changed")


async def test_mount_that_only_differs_in_case_is_the_same_share(
    storage: StorageController, ready: FakeSupervisor
) -> None:
    """Server and share names compare case-insensitively: the mount is still ours."""
    _store(storage, ready, "music", server="nas.local", share="Music")
    ready.add_mount("music", type="cifs", server="NAS.LOCAL", share="MUSIC")
    ready.requests.clear()

    await storage.reconcile()
    location = await storage.reload_network_share("music")

    assert _mutations(ready) == [("POST", "/mounts/music/reload")]
    assert (location.available, location.error) == (True, None)


async def test_mounter_without_supervisor_answers_is_unavailable(
    storage: StorageController, supervisor: FakeSupervisor
) -> None:
    """The probe of the backend itself refuses without access to the mounts."""
    supervisor.refuse_access = True

    with pytest.raises(BackendUnavailable, match="manager role"):
        await create_supervisor_mounter(storage.mass)


@pytest.mark.parametrize("state", ["failed", "inactive"])
async def test_reconcile_mounts_a_failed_mount_again(
    storage: StorageController, ready: FakeSupervisor, state: str
) -> None:
    """A mount the Supervisor reports as not working is reloaded once, and works again."""
    _store(storage, ready, "music")
    ready.add_mount("music", state=state, type="cifs", server="nas.local", share="music")
    ready.requests.clear()

    await storage.reconcile()

    assert _mutations(ready) == [("POST", "/mounts/music/reload")]
    location = storage.get_location_for_path(ready.path("music"))
    assert location is not None
    assert (location.available, location.error) == (True, None)
    ready.requests.clear()
    await storage.reconcile()
    assert _mutations(ready) == []


async def test_failed_mount_that_does_not_answer_stays_unavailable(
    storage: StorageController, ready: FakeSupervisor
) -> None:
    """A failed mount whose reload fails as well says why."""
    _store(storage, ready, "music")
    ready.add_mount("music", state="failed", type="cifs", server="nas.local", share="music")
    ready.unreachable.add("nas.local")
    ready.requests.clear()

    await storage.reconcile()

    assert _mutations(ready) == [("POST", "/mounts/music/reload")]
    location = storage.get_location_for_path(ready.path("music"))
    assert location is not None
    assert (location.available, location.error_key, location.error_args) == (
        False,
        "mount_failed",
        [NOT_REACHABLE],
    )


@pytest.mark.parametrize("dormant", [False, True])
async def test_mount_that_works_is_not_reloaded(
    storage: StorageController, ready: FakeSupervisor, dormant: bool
) -> None:
    """An active mount is left alone, also while its automount trigger is dormant."""
    _store(storage, ready, "music")
    if dormant:
        ready.dormant.add("music")
    ready.add_mount("music", type="cifs", server="nas.local", share="music")
    ready.requests.clear()

    await storage.reconcile()

    assert _mutations(ready) == []
    assert "music" not in storage._share_errors


async def test_failed_mount_is_reloaded_when_needed(
    storage: StorageController, ready: FakeSupervisor
) -> None:
    """A mount that did not work at start is reloaded once a music source needs the share."""
    _store(storage, ready, "music")
    ready.add_mount("music", state="inactive", type="cifs", server="nas.local", share="music")
    ready.unreachable.add("nas.local")
    await storage.reconcile()
    ready.unreachable.clear()
    ready.requests.clear()
    folder = ready.path("music")

    assert not await storage.is_available(folder)
    await wait_until(lambda: "storage_share_remount_music" not in storage.mass._tracked_tasks)

    assert _mutations(ready) == [("POST", "/mounts/music/reload")]
    assert await storage.is_available(folder)
