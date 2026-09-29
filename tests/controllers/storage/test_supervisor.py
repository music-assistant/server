"""Tests for network shares mounted by the Home Assistant Supervisor."""

from __future__ import annotations

import pytest
from music_assistant_models.errors import InvalidDataError, SetupFailedError

from music_assistant.constants import CONF_STORAGE_SHARES
from music_assistant.controllers.storage import StorageController, StorageKind
from music_assistant.controllers.storage.backends.base import BackendUnavailable
from music_assistant.controllers.storage.backends.supervisor import create_supervisor_mounter
from music_assistant.controllers.storage.models import MountBackend, NetworkShareSpec, ShareType
from tests.controllers.storage.conftest import FakeSupervisor

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


def _store(storage: StorageController, name: str, server: str = "nas.local", **kwargs: str) -> None:
    """
    Store a share the Supervisor mounted, as an earlier add did.

    :param storage: The storage controller.
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
        path=f"/media/{name}",
        **kwargs,  # type: ignore[arg-type]
    )
    storage.mass.config.set(f"{CONF_STORAGE_SHARES}/{name}", spec.to_dict())


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


async def test_add(storage: StorageController, ready: FakeSupervisor) -> None:
    """A share becomes a media mount at /media/<name>, replacing what discovery sees there."""
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
    assert (location.path, location.kind, location.managed, location.available) == (
        "/media/music",
        StorageKind.NETWORK_SHARE,
        True,
        True,
    )
    assert (location.backend, location.mountpoint, location.fstype) == (
        MountBackend.SUPERVISOR,
        "/media/music",
        "cifs",
    )
    assert [loc.path for loc in storage.get_locations()].count("/media/music") == 1
    stored = storage.mass.config.get(f"{CONF_STORAGE_SHARES}/music")
    assert (stored["backend"], stored["path"]) == ("supervisor", "/media/music")


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
    assert location.path == "/media/my_music"


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
    _store(storage, "music", version="3.0")

    await storage.reconcile()

    body = ready.requests[-1][2]
    assert body is not None
    assert "version" not in body
    assert "music" in ready.mounts


async def test_name_collision(storage: StorageController, ready: FakeSupervisor) -> None:
    """A name taken by a stored share, a Supervisor mount or the media folder gets a suffix."""
    _store(storage, "music", server="other.local")
    ready.add_mount("music_2", type="cifs", server="other.local", share="music_2")
    ready.media_folder = {"music", "music_2", "music_3"}

    location = await storage.add_network_share(ShareType.CIFS, "nas.local", "music")

    assert location.share_name == "music_4"
    assert location.path == "/media/music_4"
    assert "music_4" in ready.mounts


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
    assert exc_info.value.translation_args == ["/media/nas_music"]
    assert [request[:2] for request in ready.requests] == [("GET", "/mounts")]
    assert storage.mass.config.get(CONF_STORAGE_SHARES) is None
    await storage.refresh()
    location = storage.get_location_for_path("/media/nas_music")
    assert location is not None
    assert (location.kind, location.managed) == (StorageKind.NETWORK_SHARE, False)


async def test_export_mounted_in_home_assistant_is_refused(
    storage: StorageController, ready: FakeSupervisor
) -> None:
    """An export path compares the way the Supervisor stores it."""
    ready.add_mount("music", type="nfs", server="nas.local", path="/volume1/music")

    with pytest.raises(InvalidDataError) as exc_info:
        await storage.add_network_share(ShareType.NFS, "nas.local", "/volume1/music/")

    assert exc_info.value.translation_args == ["/media/music"]


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
        "/media/music",
        True,
    )


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
    assert storage.get_location_for_path("/media/music").available  # type: ignore[union-attr]


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
    """Removing a share removes its mount from the Supervisor."""
    await storage.add_network_share(ShareType.CIFS, "nas.local", "music")

    await storage.remove_network_share("music")

    assert ready.mounts == {}
    assert storage.mass.config.get(CONF_STORAGE_SHARES) == {}
    assert storage.get_location_for_path("/media/music") is None


async def test_remove_a_mount_that_is_gone(
    storage: StorageController, ready: FakeSupervisor
) -> None:
    """A share whose mount was removed in Home Assistant already is removed without an error."""
    _store(storage, "music")

    await storage.remove_network_share("music")

    assert ready.requests[-1][:2] == ("DELETE", "/mounts/music")
    assert storage.mass.config.get(CONF_STORAGE_SHARES) == {}


async def test_reload_recreates_a_mount_that_is_gone(
    storage: StorageController, ready: FakeSupervisor
) -> None:
    """A share whose mount was removed in Home Assistant gets it back from its stored settings."""
    _store(storage, "music", username="marcel", password=storage.mass.config.encrypt_string("pw"))

    location = await storage.reload_network_share("music")

    assert [request[:2] for request in ready.requests] == [
        ("POST", "/mounts/music/reload"),
        ("POST", "/mounts"),
    ]
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

    location = storage.get_location_for_path("/media/music")
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
    _store(storage, "music")
    _store(storage, "movies")
    ready.add_mount("movies", type="cifs", server="nas.local", share="movies")
    ready.requests.clear()

    await storage.reconcile()

    assert [request[:2] for request in ready.requests] == [("GET", "/mounts"), ("POST", "/mounts")]
    assert set(ready.mounts) == {"music", "movies"}
    assert storage.get_location_for_path("/media/music").available  # type: ignore[union-attr]


async def test_mounter_without_supervisor_answers_is_unavailable(
    storage: StorageController, supervisor: FakeSupervisor
) -> None:
    """The probe of the backend itself refuses without access to the mounts."""
    supervisor.refuse_access = True

    with pytest.raises(BackendUnavailable, match="manager role"):
        await create_supervisor_mounter(storage.mass)
