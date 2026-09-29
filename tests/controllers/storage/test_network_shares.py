"""Tests for the network shares the storage controller manages, whatever mounts them."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from functools import partial
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.api import ErrorResultMessage
from music_assistant_models.auth import User, UserRole
from music_assistant_models.errors import (
    ActionUnavailable,
    InvalidDataError,
    LoginFailed,
    SetupFailedError,
)
from music_assistant_models.translations import TRANSLATION_RESOLVER

from music_assistant.constants import CONF_STORAGE_SHARES, ENCRYPT_SUFFIX
from music_assistant.controllers.storage import StorageController, StorageKind
from music_assistant.controllers.storage import controller as controller_module
from music_assistant.controllers.storage.backends.local_mount import MOUNT_ROOT
from music_assistant.controllers.storage.constants import (
    PROBE_MAX_AGE,
    RECONCILE_TASK_ID,
    SHARES_DOCS_URL,
    SHARES_SETUP_TASK_ID,
    TRANSLATION_OWNER,
)
from music_assistant.controllers.storage.models import (
    MountBackend,
    NetworkShareSpec,
    ShareType,
    StorageInfo,
    StorageLocation,
    StorageUsage,
)
from music_assistant.controllers.translations import TranslationController
from music_assistant.controllers.webserver.helpers.auth_middleware import set_current_user
from music_assistant.helpers.json import json_dumps
from tests.controllers.storage.conftest import (
    FakeBackends,
    FakeMounter,
    FakeProbes,
    MountTable,
    mount_line,
    wait_until,
)

pytestmark = pytest.mark.usefixtures("probes")

MUSIC_PATH = f"{MOUNT_ROOT}/music"
SECRET = "s3cr3t,pass"


def _store(
    storage: StorageController,
    name: str,
    server: str = "nas.local",
    backend: MountBackend = MountBackend.LOCAL_MOUNT,
    **kwargs: Any,
) -> NetworkShareSpec:
    """
    Store a network share as an earlier add did.

    :param storage: The storage controller.
    :param name: The name of the share.
    :param server: The server of the share.
    :param backend: The backend that mounted the share.
    :param kwargs: Other settings of the share.
    """
    spec = NetworkShareSpec(
        name=name,
        share_type=kwargs.pop("share_type", ShareType.CIFS),
        server=server,
        share=kwargs.pop("share", name),
        backend=backend,
        path=f"{MOUNT_ROOT}/{name}",
        **kwargs,
    )
    storage.mass.config.set(f"{CONF_STORAGE_SHARES}/{name}", spec.to_dict())
    return spec


def _source(base_path: str) -> MagicMock:
    """
    Return a stand-in for a loaded music source reading its files from a path.

    :param base_path: The folder the source reads its files from.
    """
    source = MagicMock()
    source.domain = "filesystem_local"
    source.base_path = base_path
    source.name = "My music"
    return source


async def test_add(storage: StorageController, mounter: FakeMounter) -> None:
    """A share is mounted, stored and listed as a managed location on its own mountpoint."""
    location = await storage.add_network_share(
        ShareType.CIFS, "nas.local", "Music", username=" marcel ", password=SECRET
    )

    assert mounter.calls == [("add", "music", SECRET)]
    assert location == next(loc for loc in storage.get_locations() if loc.path == MUSIC_PATH)
    assert (location.kind, location.managed, location.available, location.error) == (
        StorageKind.NETWORK_SHARE,
        True,
        True,
        None,
    )
    assert (location.backend, location.mountpoint, location.fstype) == (
        MountBackend.LOCAL_MOUNT,
        MUSIC_PATH,
        "cifs",
    )
    assert (location.share_name, location.share_type, location.server, location.share) == (
        "music",
        ShareType.CIFS,
        "nas.local",
        "Music",
    )
    assert (location.username, location.version, location.read_only) == ("marcel", None, False)


@pytest.mark.usefixtures("mounter")
async def test_password_is_encrypted_at_rest_and_never_sent(storage: StorageController) -> None:
    """The password is only stored encrypted, and no API result carries it."""
    await storage.add_network_share(
        ShareType.CIFS, "nas.local", "music", username="marcel", password=SECRET
    )
    await storage.mass.config.async_save()

    stored = storage.mass.config.get(f"{CONF_STORAGE_SHARES}/music")
    assert stored["password"].startswith(ENCRYPT_SUFFIX)
    assert storage.mass.config.decrypt_string(stored["password"]) == SECRET
    settings_file = Path(storage.mass.storage_path, "settings.json").read_text()
    assert SECRET not in settings_file
    assert stored["password"] in settings_file
    info = await storage.get_info()
    for result in (info, await storage.reload_network_share("music")):
        serialized = json_dumps(result)
        assert SECRET not in serialized
        assert stored["password"] not in serialized
        assert '"password"' not in serialized


async def test_info_reports_the_backend(storage: StorageController, mounter: FakeMounter) -> None:
    """The info says what can mount a share, which types and which versions."""
    info = await storage.get_info()

    assert (info.can_mount_shares, info.mount_backend) == (True, MountBackend.LOCAL_MOUNT)
    assert info.supported_share_types == [ShareType.CIFS, ShareType.NFS]
    assert info.supported_share_versions == {ShareType.CIFS: ["2.0", "3.0"], ShareType.NFS: []}
    # a copy: a client can not change what the backend supports
    info.supported_share_versions[ShareType.CIFS].clear()
    assert mounter.supported_versions[ShareType.CIFS] == ["2.0", "3.0"]


@pytest.mark.parametrize(
    ("share_type", "server", "share", "version", "translation_key"),
    [
        (ShareType.CIFS, "nas.local", "music/albums", None, "share_name_invalid"),
        (ShareType.CIFS, "nas.local", "music\\albums", None, "share_name_invalid"),
        (ShareType.CIFS, "nas.local", "  ", None, "share_name_invalid"),
        # a comma would add options to the mount command
        (ShareType.CIFS, "nas.local", "music,uid=0", None, "share_name_invalid"),
        (ShareType.NFS, "nas.local", "volume1/music", None, "export_path_invalid"),
        (ShareType.NFS, "nas.local", "../volume1/music", None, "export_path_invalid"),
        (ShareType.NFS, "nas.local", "", None, "export_path_invalid"),
        (ShareType.CIFS, "nas.invalid", "music", None, "host_unresolvable"),
        (ShareType.CIFS, " ", "music", None, "host_unresolvable"),
        (ShareType.CIFS, "nas.local", "music", "1.0", "share_version_not_supported"),
        # an empty list: the version of an nfs share can not be chosen here
        (ShareType.NFS, "nas.local", "/volume1/music", "4", "share_version_not_supported"),
    ],
)
async def test_add_refuses_invalid_settings(
    storage: StorageController,
    mounter: FakeMounter,
    share_type: ShareType,
    server: str,
    share: str,
    version: str | None,
    translation_key: str,
) -> None:
    """Settings a backend can not mount are refused before anything is mounted or stored."""
    with pytest.raises(InvalidDataError) as exc_info:
        await storage.add_network_share(share_type, server, share, version=version)

    assert exc_info.value.translation_key == translation_key
    assert mounter.calls == []
    assert storage.mass.config.get(CONF_STORAGE_SHARES) is None


async def test_add_a_type_the_backend_can_not_mount(
    storage: StorageController, mounter: FakeMounter
) -> None:
    """A share type the backend can not mount is refused."""
    del mounter.supported_versions[ShareType.NFS]

    with pytest.raises(InvalidDataError) as exc_info:
        await storage.add_network_share(ShareType.NFS, "nas.local", "/volume1/music")

    assert exc_info.value.translation_key == "share_type_not_supported"


@pytest.mark.usefixtures("mounter")
async def test_add_the_same_share_twice(storage: StorageController) -> None:
    """The same share on the same server is one location; another share of it is not."""
    await storage.add_network_share(ShareType.CIFS, "nas.local", "music")

    with pytest.raises(InvalidDataError) as exc_info:
        await storage.add_network_share(ShareType.CIFS, "NAS.local", "Music")
    await storage.add_network_share(ShareType.CIFS, "nas.local", "music2")
    await storage.add_network_share(ShareType.CIFS, "nas2.local", "music")

    assert exc_info.value.translation_key == "share_already_added"
    assert sorted(storage.mass.config.get(CONF_STORAGE_SHARES)) == ["music", "music2", "music_2"]


@pytest.mark.usefixtures("mounter")
async def test_export_path_compares_as_a_path(storage: StorageController) -> None:
    """An export path with a trailing slash is the same export."""
    await storage.add_network_share(ShareType.NFS, "nas.local", "/volume1/music")

    with pytest.raises(InvalidDataError) as exc_info:
        await storage.add_network_share(ShareType.NFS, "nas.local", "/volume1//music/")

    assert exc_info.value.translation_key == "share_already_added"


async def test_nfs_share_has_no_credentials(
    storage: StorageController, mounter: FakeMounter
) -> None:
    """Credentials given for an NFS export are not kept: NFS has none."""
    location = await storage.add_network_share(
        ShareType.NFS, "nas.local", "/volume1/music/", username="marcel", password=SECRET
    )

    stored = storage.mass.config.get(f"{CONF_STORAGE_SHARES}/music")
    assert (stored["username"], stored["password"]) == (None, None)
    assert location.username is None
    assert mounter.calls == [("add", "music", None)]


async def test_add_without_a_backend(storage: StorageController, backends: FakeBackends) -> None:
    """Without anything that can mount, the backends are probed again first."""
    with pytest.raises(ActionUnavailable) as exc_info:
        await storage.add_network_share(ShareType.CIFS, "nas.local", "music")

    assert exc_info.value.translation_key == "no_mount_backend"
    assert exc_info.value.translation_args == [SHARES_DOCS_URL]
    assert backends.probes == 2


async def test_failed_mount_stores_nothing(
    storage: StorageController, mounter: FakeMounter
) -> None:
    """A share that does not mount is not kept, and its error reaches the caller."""
    mounter.failing["nas.local"] = LoginFailed("SMB mount failed with error: Permission denied")

    with pytest.raises(LoginFailed):
        await storage.add_network_share(ShareType.CIFS, "nas.local", "music", "marcel", "wrong")

    assert storage.mass.config.get(CONF_STORAGE_SHARES) is None
    assert all(loc.path != MUSIC_PATH for loc in storage.get_locations())


@pytest.mark.parametrize("answer", ["no answer", "not mounted"])
async def test_share_that_does_not_show_up_is_not_kept(
    storage: StorageController,
    mounter: FakeMounter,
    probes: FakeProbes,
    answer: str,
) -> None:
    """A mount the probe finds no share on is undone, and nothing is stored."""
    if answer == "no answer":
        probes.results[MUSIC_PATH] = None
    else:
        # the mount tool reported success but the table never showed the mount
        mounter.mount_table = MountTable()

    with pytest.raises(SetupFailedError) as exc_info:
        await storage.add_network_share(ShareType.CIFS, "nas.local", "music")

    assert exc_info.value.translation_key == "share_not_mounted"
    assert mounter.calls[-1][:2] == ("remove", "music")
    assert storage.mass.config.get(CONF_STORAGE_SHARES) is None


async def test_undo_that_fails_keeps_the_reason(
    storage: StorageController,
    mounter: FakeMounter,
    probes: FakeProbes,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A mount that can not be undone is logged; the caller still learns why the share failed."""
    probes.results[MUSIC_PATH] = None

    with (
        caplog.at_level(logging.WARNING),
        patch.object(mounter, "remove", AsyncMock(side_effect=OSError("busy"))),
        pytest.raises(SetupFailedError) as exc_info,
    ):
        await storage.add_network_share(ShareType.CIFS, "nas.local", "music")

    assert exc_info.value.translation_key == "share_not_mounted"
    assert "Unable to remove the mount of network share music: busy" in caplog.text
    assert storage.mass.config.get(CONF_STORAGE_SHARES) is None


@pytest.mark.parametrize("password", [None, ""])
async def test_user_needs_a_password(
    storage: StorageController, mounter: FakeMounter, password: str | None
) -> None:
    """A user without a password is refused before anything is mounted."""
    with pytest.raises(InvalidDataError) as exc_info:
        await storage.add_network_share(
            ShareType.CIFS, "nas.local", "music", "marcel", password=password
        )

    assert exc_info.value.translation_key == "share_password_missing"
    assert mounter.calls == []


async def test_update_to_a_user_needs_a_password(
    storage: StorageController, mounter: FakeMounter
) -> None:
    """A guest share that gets a user needs a password; a share with a stored one keeps it."""
    await storage.add_network_share(ShareType.CIFS, "nas.local", "music")
    await storage.add_network_share(ShareType.CIFS, "nas.local", "movies", "marcel", SECRET)

    with pytest.raises(InvalidDataError) as exc_info:
        await storage.update_network_share("music", "nas.local", "music", "marcel")
    await storage.update_network_share("movies", "nas.local", "movies", "other")

    assert exc_info.value.translation_key == "share_password_missing"
    assert mounter.calls[-1] == ("update", "movies", SECRET)


@pytest.mark.parametrize("username", ["x,uid=0", "marcel,"])
async def test_user_name_with_a_comma(
    storage: StorageController, mounter: FakeMounter, username: str
) -> None:
    """A comma in a user name would add options to the mount command: it is refused."""
    await storage.add_network_share(ShareType.CIFS, "nas.local", "music", "marcel", SECRET)

    with pytest.raises(InvalidDataError) as add_error:
        await storage.add_network_share(ShareType.CIFS, "nas.local", "movies", username, "pw")
    with pytest.raises(InvalidDataError) as update_error:
        await storage.update_network_share("music", "nas.local", "music", username, "pw")

    assert add_error.value.translation_key == "share_username_invalid"
    assert update_error.value.translation_key == "share_username_invalid"
    assert [call[0] for call in mounter.calls] == ["add"]


async def test_info_for_a_member(
    storage: StorageController, mount_table: MountTable, backends: FakeBackends
) -> None:
    """
    A member sees a managed share without its connection details, and starts no probe.

    What the server knows about mounting is reported as it is; the reason a share is not
    available stays.
    """
    mount_table.set(mount_line("/", "ext4"))
    _store(storage, "music", username="marcel", version="3.0")
    set_current_user(User(user_id="member", username="member", role=UserRole.USER))

    info = await storage.get_info()

    assert backends.probes == 0
    assert RECONCILE_TASK_ID not in storage.mass._tracked_tasks
    assert (info.can_mount_shares, info.mount_backend) == (False, None)
    location = next(loc for loc in info.locations if loc.path == MUSIC_PATH)
    assert (location.share_name, location.server, location.share) == (None, None, None)
    assert (location.username, location.version) == (None, None)
    assert (location.share_type, location.managed, location.available) == (
        ShareType.CIFS,
        True,
        False,
    )
    assert location.error_key == "share_unavailable"
    # the stored share itself is untouched, and an admin gets every detail
    assert storage.get_location_for_path(MUSIC_PATH).server == "nas.local"  # type: ignore[union-attr]
    set_current_user(User(user_id="admin", username="admin", role=UserRole.ADMIN))
    await storage.get_info()
    assert backends.probes == 2


@pytest.mark.parametrize(
    ("username", "password", "expected_username", "expected_password"),
    [
        # the password is kept when none is given
        ("marcel", None, "marcel", SECRET),
        ("marcel", "", "marcel", SECRET),
        ("other", None, "other", SECRET),
        ("marcel", "new-pass", "marcel", "new-pass"),
        # without a user there is no password
        (None, None, None, None),
        (None, "new-pass", None, None),
        ("  ", "new-pass", None, None),
    ],
)
async def test_update_replaces_the_settings(
    storage: StorageController,
    mounter: FakeMounter,
    username: str | None,
    password: str | None,
    expected_username: str | None,
    expected_password: str | None,
) -> None:
    """Every setting is replaced; only the password is kept when none is given."""
    await storage.add_network_share(
        ShareType.CIFS, "nas.local", "music", "marcel", SECRET, version="3.0", read_only=True
    )

    location = await storage.update_network_share(
        "music", "nas2.local", "music", username=username, password=password
    )

    stored = NetworkShareSpec.from_dict(storage.mass.config.get(f"{CONF_STORAGE_SHARES}/music"))
    assert (stored.server, stored.username, stored.version, stored.read_only) == (
        "nas2.local",
        expected_username,
        None,
        False,
    )
    stored_password = storage._get_password(stored)
    assert stored_password == expected_password
    assert mounter.calls[-1] == ("update", "music", expected_password)
    assert (location.path, location.server, location.version, location.available) == (
        MUSIC_PATH,
        "nas2.local",
        None,
        True,
    )


async def test_failed_update_keeps_the_previous_settings(
    storage: StorageController, mounter: FakeMounter
) -> None:
    """Settings that do not mount are rolled back to the previous ones, which mount again."""
    await storage.add_network_share(ShareType.CIFS, "nas.local", "music", "marcel", SECRET)
    mounter.failing["dead.local"] = SetupFailedError("no route to host")

    with pytest.raises(SetupFailedError, match="no route"):
        await storage.update_network_share("music", "dead.local", "music", "marcel", "other")

    assert mounter.calls[-2:] == [
        ("update", "music", "other"),
        ("reload", "music", SECRET),
    ]
    stored = storage.mass.config.get(f"{CONF_STORAGE_SHARES}/music")
    assert stored["server"] == "nas.local"
    assert storage._get_password(NetworkShareSpec.from_dict(stored)) == SECRET
    location = storage.get_location_for_path(MUSIC_PATH)
    assert location is not None
    assert (location.available, location.error) == (True, None)


async def test_update_does_not_wait_for_a_probe_of_the_old_mount(
    storage: StorageController,
    mounter: FakeMounter,
    probes: FakeProbes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A probe hanging on the mount of a server that is gone does not decide the new mount."""
    monkeypatch.setattr(controller_module, "PROBE_TIMEOUT", 0.2)
    await storage.add_network_share(ShareType.CIFS, "nas.local", "music")
    # the server is gone: the next probe of the share hangs
    storage._probes[MUSIC_PATH].answered_at = time.monotonic() - PROBE_MAX_AGE
    probes.block(MUSIC_PATH)
    await storage.get_info()
    old_probe = storage._probes[MUSIC_PATH].probe
    assert old_probe is not None
    gate = probes.blocked.pop(MUSIC_PATH)
    try:
        location = await storage.update_network_share("music", "nas2.local", "music")
    finally:
        # the hanging probe fails in the end, about the old mount
        probes.results[MUSIC_PATH] = None
        gate.set()

    assert [call[0] for call in mounter.calls] == ["add", "update"]
    assert (location.server, location.available) == ("nas2.local", True)
    assert storage.mass.config.get(f"{CONF_STORAGE_SHARES}/music")["server"] == "nas2.local"
    await wait_until(old_probe.done)
    await storage.refresh()
    assert storage.get_location_for_path(MUSIC_PATH) == location


async def test_failed_update_and_rollback(storage: StorageController, mounter: FakeMounter) -> None:
    """When the previous settings no longer mount either, the location shows why."""
    await storage.add_network_share(ShareType.CIFS, "nas.local", "music")
    mounter.failing["nas.local"] = SetupFailedError("gone", translation_key="mount_failed")
    mounter.failing["nas2.local"] = SetupFailedError("gone too")

    with pytest.raises(SetupFailedError, match="gone too"):
        await storage.update_network_share("music", "nas2.local", "music")

    location = storage.get_location_for_path(MUSIC_PATH)
    assert location is not None
    assert (location.available, location.error, location.error_key) == (
        False,
        "gone",
        "mount_failed",
    )
    assert storage.mass.config.get(f"{CONF_STORAGE_SHARES}/music")["server"] == "nas.local"


@pytest.mark.usefixtures("mounter")
async def test_update_refuses_a_share_that_is_added_already(storage: StorageController) -> None:
    """A share can not be changed into another share that is a location already."""
    await storage.add_network_share(ShareType.CIFS, "nas.local", "music")
    await storage.add_network_share(ShareType.CIFS, "nas.local", "movies")

    with pytest.raises(InvalidDataError) as exc_info:
        await storage.update_network_share("movies", "nas.local", "MUSIC")
    # the same share with other settings is fine
    await storage.update_network_share("music", "nas.local", "music", read_only=True)

    assert exc_info.value.translation_key == "share_already_added"


@pytest.mark.usefixtures("mounter")
@pytest.mark.parametrize("command", ["update", "remove", "reload"])
async def test_unknown_share(storage: StorageController, command: str) -> None:
    """A command for a share that does not exist is refused."""
    commands: dict[str, Callable[[], Awaitable[object]]] = {
        "update": lambda: storage.update_network_share("music", "nas.local", "music"),
        "remove": lambda: storage.remove_network_share("music"),
        "reload": lambda: storage.reload_network_share("music"),
    }

    with pytest.raises(InvalidDataError) as exc_info:
        await commands[command]()

    assert exc_info.value.translation_key == "share_not_found"
    assert exc_info.value.translation_args == ["music"]


async def test_remove(storage: StorageController, mounter: FakeMounter) -> None:
    """A removed share is unmounted, forgotten and no longer listed."""
    await storage.add_network_share(ShareType.CIFS, "nas.local", "music")
    # a source on a look-alike path does not use the share
    storage.mass._providers["filesystem_local--abc"] = _source(f"{MUSIC_PATH}2")

    await storage.remove_network_share("music")

    assert mounter.calls[-1][:2] == ("remove", "music")
    assert storage.mass.config.get(CONF_STORAGE_SHARES) == {}
    assert storage.get_location_for_path(MUSIC_PATH) is None


@pytest.mark.parametrize("base_path", [MUSIC_PATH, f"{MUSIC_PATH}/Albums"])
async def test_remove_refused_while_in_use(
    storage: StorageController, mounter: FakeMounter, base_path: str
) -> None:
    """A share a loaded music source reads from can not be removed."""
    await storage.add_network_share(ShareType.CIFS, "nas.local", "music")
    storage.mass._providers["filesystem_local--abc"] = _source(base_path)

    with pytest.raises(ActionUnavailable) as exc_info:
        await storage.remove_network_share("music")

    assert exc_info.value.translation_key == "location_in_use"
    assert exc_info.value.translation_args == ["My music"]
    assert mounter.calls[-1][0] == "add"
    assert "music" in storage.mass.config.get(CONF_STORAGE_SHARES)


@pytest.mark.usefixtures("mount_table", "backends")
@pytest.mark.parametrize(
    ("backend", "under_supervisor"),
    [
        (MountBackend.SUPERVISOR, False),
        (MountBackend.LOCAL_MOUNT, False),
        # the server never mounts itself under a Supervisor
        (MountBackend.LOCAL_MOUNT, True),
    ],
)
async def test_remove_without_its_backend(
    storage: StorageController,
    caplog: pytest.LogCaptureFixture,
    backend: MountBackend,
    under_supervisor: bool,
) -> None:
    """A share of a backend this installation does not have is forgotten, and that is logged."""
    storage.mass.running_as_hass_addon = under_supervisor
    _store(storage, "music", backend=backend)

    with caplog.at_level(logging.WARNING):
        await storage.remove_network_share("music")

    assert storage.mass.config.get(CONF_STORAGE_SHARES) == {}
    assert "Forgetting network share music" in caplog.text


async def test_reconcile(storage: StorageController, mounter: FakeMounter) -> None:
    """Of two stored shares one mounts and one fails: no error escapes, and it says why."""
    _store(storage, "music", username="marcel", password=storage.mass.config.encrypt_string(SECRET))
    _store(storage, "movies", server="dead.local")
    mounter.failing["dead.local"] = SetupFailedError(
        "timed out", translation_key="mount_failed", translation_args=["timed out"]
    )

    await storage.reconcile()

    assert mounter.calls == [("add", "music", SECRET), ("add", "movies", None)]
    by_name = {loc.share_name: loc for loc in storage.get_locations() if loc.share_name}
    assert by_name["music"].available
    assert (by_name["movies"].available, by_name["movies"].error_key) == (False, "mount_failed")
    assert by_name["movies"].error_args == ["timed out"]
    # a second pass only tries the share that is not mounted
    mounter.calls.clear()
    await storage.reconcile()
    assert mounter.calls == [("add", "movies", None)]
    # an error of a share that turns out to be mounted is outdated
    storage._share_errors["music"] = SetupFailedError("old")
    await storage.reconcile()
    assert "music" not in storage._share_errors


async def test_reconcile_survives_anything(
    storage: StorageController, mounter: FakeMounter, caplog: pytest.LogCaptureFixture
) -> None:
    """Whatever goes wrong while the shares are mounted is logged, never raised."""
    _store(storage, "music")

    with (
        caplog.at_level(logging.ERROR),
        patch.object(storage, "refresh", AsyncMock(side_effect=RuntimeError("boom"))),
    ):
        await storage.reconcile()

    assert "Failed to mount the network shares" in caplog.text
    assert "music" in mounter.mounted


@pytest.mark.usefixtures("mounter")
async def test_reconcile_never_raises(storage: StorageController) -> None:
    """An unreadable password or an unexpected failure only fails that share."""
    _store(storage, "music", username="marcel", password=f"{ENCRYPT_SUFFIX}not-a-token")

    await storage.reconcile()

    location = storage.get_location_for_path(f"{MOUNT_ROOT}/music")
    assert location is not None
    assert not location.available
    assert location.error_key == "invalid_data"


@pytest.mark.usefixtures("backends")
async def test_reconcile_without_the_backend_of_a_share(
    storage: StorageController, mount_table: MountTable
) -> None:
    """A share whose backend is not available stays listed, saying why it is not mounted."""
    mount_table.set(mount_line("/", "ext4"))
    _store(storage, "music", backend=MountBackend.SUPERVISOR)
    storage.mass.config.set(f"{CONF_STORAGE_SHARES}/broken", {"name": "broken"})

    await storage.reconcile()

    location = storage.get_location_for_path(f"{MOUNT_ROOT}/music")
    assert location is not None
    assert (location.available, location.error_key) == (False, "mount_backend_unavailable")
    assert [loc.share_name for loc in storage.get_locations() if loc.share_name] == ["music"]


async def test_backend_that_becomes_available_mounts_the_shares(
    storage: StorageController, mount_table: MountTable, backends: FakeBackends
) -> None:
    """A share command that finds a backend again mounts the shares that waited for it."""
    mount_table.set(mount_line("/", "ext4"))
    _store(storage, "movies")
    await storage._probe_backends()
    mounter = FakeMounter(mount_table)
    backends.available[mounter.backend] = mounter

    await storage.add_network_share(ShareType.CIFS, "nas.local", "music")
    await wait_until(lambda: RECONCILE_TASK_ID not in storage.mass._tracked_tasks)

    assert sorted(mounter.mounted) == ["movies", "music"]


@pytest.mark.usefixtures("backends")
async def test_managed_share_locations(storage: StorageController, mount_table: MountTable) -> None:
    """
    A managed share is listed from its record wherever it is mounted.

    Below /tmp discovery sees nothing, so an own mount is only listed through its record, and
    a share that is not mounted is listed as unavailable with a reason, never as the folder it
    leaves behind.
    """
    _store(storage, "music", username="marcel", version="3.0", read_only=True)
    _store(storage, "movies", share_type=ShareType.NFS, share="/volume1/movies")
    mount_table.set(
        mount_line("/", "ext4"), mount_line(MUSIC_PATH, "cifs"), mount_line("/mnt/nas", "cifs")
    )

    await storage.get_info()

    by_path = {loc.path: loc for loc in storage.get_locations()}
    music = by_path[MUSIC_PATH]
    assert (music.available, music.mountpoint, music.fstype, music.read_only) == (
        True,
        MUSIC_PATH,
        "cifs",
        True,
    )
    assert (music.share_type, music.username, music.version) == (ShareType.CIFS, "marcel", "3.0")
    movies = by_path[f"{MOUNT_ROOT}/movies"]
    assert (movies.available, movies.mountpoint, movies.fstype) == (
        False,
        f"{MOUNT_ROOT}/movies",
        None,
    )
    assert (movies.share_type, movies.share, movies.error_key) == (
        ShareType.NFS,
        "/volume1/movies",
        "share_unavailable",
    )
    # a discovered share nobody added is no managed share
    assert (by_path["/mnt/nas"].managed, by_path["/mnt/nas"].share_name) == (False, None)


def test_error_is_localized_on_the_wire() -> None:
    """An API result carries the error in the language of the client, never its key."""
    location = StorageLocation(
        path=MUSIC_PATH,
        name="music",
        usage=StorageUsage.MEDIA,
        kind=StorageKind.NETWORK_SHARE,
        available=False,
        error="SMB mount failed with error: mount error(112): Host is down",
        error_key="mount_failed",
        error_args=["mount error(112): Host is down"],
    )
    asked: list[tuple[str, str | None, list[str] | None]] = []

    def resolve(key: str, owner: str | None = None, params: list[str] | None = None) -> str:
        asked.append((key, owner, params))
        return f"Kan niet verbinden: {params[0] if params else ''}"

    token = TRANSLATION_RESOLVER.set(resolve)
    try:
        data = location.to_dict()
    finally:
        TRANSLATION_RESOLVER.reset(token)

    assert data["error"] == "Kan niet verbinden: mount error(112): Host is down"
    assert asked == [("errors.mount_failed", TRANSLATION_OWNER, ["mount error(112): Host is down"])]
    assert "error_key" not in data
    assert "error_args" not in data
    # without a resolver the English message stays
    assert location.to_dict()["error"] == location.error
    assert (
        "error_key"
        not in StorageInfo([location], False, None, [], {}, False).to_dict()["locations"][0]
    )


async def test_close_unmounts_nothing(storage: StorageController, mounter: FakeMounter) -> None:
    """Stopping the server leaves the shares mounted."""
    await storage.add_network_share(ShareType.CIFS, "nas.local", "music")

    await storage.close()

    assert [call[0] for call in mounter.calls] == ["add"]
    assert "music" in mounter.mounted


async def test_setup_mounts_the_stored_shares(
    storage: StorageController, mounter: FakeMounter, backends: FakeBackends
) -> None:
    """The server probes the backends in the background at start, then mounts its shares."""
    _store(storage, "music")
    storage._mounters = {}

    await storage.setup(MagicMock())
    await wait_until(lambda: SHARES_SETUP_TASK_ID not in storage.mass._tracked_tasks)

    assert backends.probes == 2
    assert "music" in mounter.mounted


@pytest.mark.usefixtures("mounter")
async def test_diagnostics_hold_no_names_or_paths(storage: StorageController) -> None:
    """The report says how shares are mounted and how locations are, never where or what."""
    await storage._probe_backends()
    await storage.add_network_share(ShareType.CIFS, "nas.local", "private_music", "marcel", "pw")

    diagnostics = await storage.get_diagnostics()

    report = json_dumps(diagnostics)
    for private in ("nas.local", "private_music", "marcel", MOUNT_ROOT, storage.mass.storage_path):
        assert private not in report
    assert diagnostics["mount_backend"] == "local_mount"
    assert diagnostics["mount_backends"] == {
        "supervisor": "supervisor is not available in this test",
        "local_mount": "available",
    }
    locations = diagnostics["locations"]
    assert isinstance(locations, list)
    assert [loc for loc in locations if isinstance(loc, dict) and loc["managed"]] == [
        {
            "kind": "network_share",
            "usage": "media",
            "fstype": "cifs",
            "available": True,
            "managed": True,
            "backend": "local_mount",
            "error": None,
        }
    ]


def _store_at(storage: StorageController, path: Path) -> None:
    """
    Store a network share mounted on a folder that exists, as an earlier add did.

    :param storage: The storage controller.
    :param path: The folder the share is mounted on.
    """
    spec = NetworkShareSpec(
        name=path.name,
        share_type=ShareType.CIFS,
        server="nas.local",
        share=path.name,
        backend=MountBackend.LOCAL_MOUNT,
        path=str(path),
    )
    storage.mass.config.set(f"{CONF_STORAGE_SHARES}/{spec.name}", spec.to_dict())


@pytest.mark.usefixtures("mounter")
async def test_share_that_never_mounted_is_not_its_folder(
    storage: StorageController, mount_table: MountTable, tmp_path: Path
) -> None:
    """The folder a share is mounted on is not the share: the mount table decides."""
    share = tmp_path / "music"
    (share / "Albums").mkdir(parents=True)
    _store_at(storage, share)

    await storage.get_info()

    location = storage.get_location_for_path(str(share))
    assert location is not None
    assert (location.available, location.mountpoint) == (False, str(share))
    assert not await storage.is_available(str(share / "Albums"))
    mount_table.mount(str(share))
    await storage.refresh()
    assert await storage.is_available(str(share / "Albums"))


@pytest.mark.usefixtures("mounter")
async def test_share_that_is_gone_is_not_its_folder(
    storage: StorageController, mount_table: MountTable, tmp_path: Path
) -> None:
    """A share that was mounted and is not any more is not available, its folder left behind."""
    share = tmp_path / "music"
    (share / "Albums").mkdir(parents=True)
    _store_at(storage, share)
    mount_table.mount(str(share))
    await storage.get_info()
    assert await storage.is_available(str(share / "Albums"))

    mount_table.unmount(str(share))

    assert not await storage.is_available(str(share / "Albums"))


async def test_share_without_a_mount_table(
    storage: StorageController, mount_table: MountTable, tmp_path: Path
) -> None:
    """Without a mount table (macOS) a share must still be mounted on its folder to be used."""
    share = tmp_path / "music"
    (share / "Albums").mkdir(parents=True)
    _store_at(storage, share)
    mount_table.set()

    await storage.get_info()

    # the probe finds the folder, but it is no mountpoint
    assert not await storage.is_available(str(share / "Albums"))


async def test_errors_with_common_keys_are_translated(
    storage: StorageController, mounter: FakeMounter
) -> None:
    """
    An error of this controller with a key of the common strings reads in the client's language.

    Goes the real way: the translations of the server, in a language other than English, for an
    error a command raised and for the error a stored share carries.
    """
    translations = TranslationController(storage.mass)
    await translations.setup(MagicMock())
    await translations.ensure_locale_loaded("nl")
    with pytest.raises(InvalidDataError) as exc_info:
        await storage.add_network_share(ShareType.CIFS, "nas.invalid", "music")
    raised = exc_info.value
    _store(storage, "music")
    mounter.failing["nas.local"] = SetupFailedError(
        "SMB mount failed with error: mount error(112): Host is down",
        translation_key="mount_failed",
        translation_args=["mount error(112): Host is down"],
    )
    await storage.reconcile()
    location = storage.get_location_for_path(MUSIC_PATH)
    assert location is not None

    token = TRANSLATION_RESOLVER.set(partial(translations.get_translation, locale="nl"))
    try:
        message = ErrorResultMessage(
            "1",
            raised.error_code,
            str(raised),
            translation_key=raised.translation_key,
            translation_args=raised.translation_args,
            translation_owner=raised.translation_owner,
        ).to_dict()
        serialized = location.to_dict()
    finally:
        TRANSLATION_RESOLVER.reset(token)

    assert raised.translation_owner == TRANSLATION_OWNER
    assert message["details"] == translations.get_translation(
        "common.errors.host_unresolvable", locale="nl", params=["nas.invalid"]
    )
    assert message["details"].startswith("Het bereiken van nas.invalid")
    assert serialized["error"] == (
        "Er kan geen verbinding worden gemaakt met de externe locatie: "
        "mount error(112): Host is down"
    )


async def test_member_gets_no_detail_of_a_mount_error(
    storage: StorageController, mounter: FakeMounter
) -> None:
    """The error of a mount can name the server and the export: only an admin gets it."""
    detail = "mount.nfs: access denied by server while mounting nas.local:/volume1/music"
    _store(storage, "music")
    mounter.failing["nas.local"] = SetupFailedError(
        f"NFS mount failed with error: {detail}",
        translation_key="mount_failed",
        translation_args=[detail],
    )
    await storage.reconcile()

    admin = next(loc for loc in (await storage.get_info()).locations if loc.path == MUSIC_PATH)
    set_current_user(User(user_id="member", username="member", role=UserRole.USER))
    member = next(loc for loc in (await storage.get_info()).locations if loc.path == MUSIC_PATH)

    assert (admin.error_key, admin.error_args) == ("mount_failed", [detail])
    assert admin.error is not None
    assert "nas.local" in admin.error
    assert (member.available, member.error_key, member.error_args) == (
        False,
        "share_unavailable",
        [],
    )
    assert "nas.local" not in json_dumps(member)
