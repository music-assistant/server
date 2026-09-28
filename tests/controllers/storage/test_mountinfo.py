"""Tests for the discovery of media mounts in the mount table."""

from __future__ import annotations

from pathlib import Path

import pytest

from music_assistant.controllers.storage.backends import mountinfo
from music_assistant.controllers.storage.backends.mountinfo import (
    MediaMount,
    parse_mountinfo,
    parse_mountpoints,
    read_mountinfo,
)
from music_assistant.controllers.storage.models import StorageKind

FIXTURES = Path(__file__).parent / "fixtures"
CONTAINER_DATA_PATHS = ("/data", "/data/.cache")


def _fixture(name: str) -> str:
    """Return the contents of a captured mount table."""
    return (FIXTURES / f"{name}.mountinfo").read_text(encoding="utf-8")


def _line(mountpoint: str, fstype: str, options: str = "rw,relatime", source: str = "src") -> str:
    """Return a mountinfo line for a mount."""
    return f"100 1 8:1 / {mountpoint} {options} shared:1 - {fstype} {source} rw"


def _parse(
    *lines: str,
    in_container: bool = False,
    supervisor: bool = False,
    excluded_paths: tuple[str, ...] = (),
) -> dict[str, MediaMount]:
    """Parse a mount table made of the given lines, keyed by mountpoint."""
    return {
        mount.mountpoint: mount
        for mount in parse_mountinfo(
            "\n".join(lines),
            excluded_paths=excluded_paths,
            in_container=in_container,
            supervisor=supervisor,
        )
    }


def test_home_assistant_addon() -> None:
    """The add-on sees the media folder and each share the Supervisor set up below it."""
    mounts = parse_mountinfo(
        _fixture("haos_addon"),
        excluded_paths=CONTAINER_DATA_PATHS,
        in_container=True,
        supervisor=True,
    )

    assert mounts == [
        MediaMount("/media", "ext4", read_only=False, kind=StorageKind.BUILTIN_MEDIA),
        # accessed shares: the real mount sits on top of the automount trigger
        MediaMount("/media/nas", "cifs", read_only=False, kind=StorageKind.NETWORK_SHARE),
        MediaMount("/media/backup_nfs", "nfs4", read_only=True, kind=StorageKind.NETWORK_SHARE),
        # a share nobody accessed yet is only its automount trigger
        MediaMount("/media/archive", "autofs", read_only=False, kind=StorageKind.NETWORK_SHARE),
    ]


@pytest.mark.parametrize(
    "mountpoint", ["/proc/sys/fs/binfmt_misc", "/boot", "/efi", "/data/shares", "/"]
)
def test_automount_trigger_in_an_excluded_path_is_no_location(mountpoint: str) -> None:
    """An automount trigger only counts where a mount would count."""
    assert _parse(_line(mountpoint, "autofs"), excluded_paths=("/data",)) == {}


def test_home_assistant_addon_folders_are_never_media() -> None:
    """The add-on folders stay out even when the server paths are elsewhere."""
    mounts = _parse(*_fixture("haos_addon").splitlines(), in_container=True, supervisor=True)

    assert "/data" not in mounts
    assert "/ssl" not in mounts


def test_share_mounted_by_a_music_source_is_not_a_location() -> None:
    """The mount an SMB music source makes below /tmp for itself is not offered to anyone."""
    mounts = _parse(*_fixture("haos_addon").splitlines(), in_container=True, supervisor=True)

    assert not any(mountpoint.startswith("/tmp/") for mountpoint in mounts)  # noqa: S108


def test_docker_with_a_bind_volume() -> None:
    """A volume of a plain container is a container volume, also when it sits below /media."""
    mounts = parse_mountinfo(
        _fixture("docker_bind_volume"),
        excluded_paths=CONTAINER_DATA_PATHS,
        in_container=True,
        supervisor=False,
    )

    assert mounts == [
        MediaMount("/media/music", "ext4", read_only=True, kind=StorageKind.CONTAINER_VOLUME),
    ]


def test_docker_without_volumes() -> None:
    """A container without volumes has no media location at all."""
    mounts = parse_mountinfo(
        _fixture("docker_no_volumes"),
        excluded_paths=CONTAINER_DATA_PATHS,
        in_container=True,
        supervisor=False,
    )

    assert mounts == []


def test_bare_metal_host() -> None:
    """A host offers its disks and network shares, never its system and pseudo filesystems."""
    mounts = parse_mountinfo(
        _fixture("bare_metal"),
        excluded_paths=("/home/marcel/.musicassistant", "/home/marcel/.musicassistant/.cache"),
        in_container=False,
        supervisor=False,
    )

    assert mounts == [
        MediaMount("/home", "ext4", read_only=False, kind=StorageKind.LOCAL_DISK),
        MediaMount("/mnt/nas music", "cifs", read_only=False, kind=StorageKind.NETWORK_SHARE),
    ]


@pytest.mark.parametrize(
    "mountpoint",
    ["/snapshots", "/variety", "/usrdata", "/tmpstore"],  # noqa: S108
)
def test_system_path_look_alikes_are_kept(mountpoint: str) -> None:
    """A mountpoint that only starts with the name of a system path is not a system path."""
    assert mountpoint in _parse(_line(mountpoint, "btrfs"))


def test_server_paths_and_everything_below_them_are_excluded() -> None:
    """The server's own data and cache directories are no media location."""
    mounts = _parse(
        _line("/srv/ma", "ext4"),
        _line("/srv/ma/cache", "ext4"),
        _line("/srv/ma2", "ext4"),
        excluded_paths=("/srv/ma/",),
    )

    assert list(mounts) == ["/srv/ma2"]


def test_add_on_folders_are_only_reserved_inside_a_container() -> None:
    """On a host, /share and /data are ordinary mountpoints (a NAS share, a data disk)."""
    lines = (_line("/share", "nfs4"), _line("/data", "xfs"), _line("/datasets", "ext4"))

    assert set(_parse(*lines)) == {"/share", "/data", "/datasets"}
    assert set(_parse(*lines, in_container=True)) == {"/datasets"}


@pytest.mark.parametrize(
    ("mountpoint", "fstype", "in_container", "supervisor", "kind"),
    [
        ("/media", "ext4", True, True, StorageKind.BUILTIN_MEDIA),
        ("/media", "ext4", True, False, StorageKind.CONTAINER_VOLUME),
        ("/media", "ext4", False, False, StorageKind.LOCAL_DISK),
        ("/media/SANDISK", "ext4", True, True, StorageKind.REMOVABLE),
        ("/media/marcel/USB", "ext4", False, False, StorageKind.REMOVABLE),
        ("/media/music", "ext4", True, False, StorageKind.CONTAINER_VOLUME),
        ("/mediafiles", "ext4", False, False, StorageKind.LOCAL_DISK),
        ("/media/usb", "vfat", True, False, StorageKind.REMOVABLE),
        ("/mnt/usb", "exfat", False, False, StorageKind.REMOVABLE),
        ("/mnt/nas", "smb3", False, False, StorageKind.NETWORK_SHARE),
        ("/media/nas", "nfs", True, True, StorageKind.NETWORK_SHARE),
        ("/mnt/pool", "fuse.mergerfs", False, False, StorageKind.LOCAL_DISK),
        ("/music", "virtiofs", True, False, StorageKind.CONTAINER_VOLUME),
        # a bind mount of Docker Desktop for Mac
        ("/media/music", "fakeowner", True, False, StorageKind.CONTAINER_VOLUME),
        ("/mnt/nas", "autofs", False, False, StorageKind.NETWORK_SHARE),
    ],
)
def test_kind(
    mountpoint: str, fstype: str, in_container: bool, supervisor: bool, kind: StorageKind
) -> None:
    """A mount is classified by its filesystem, its place and the install type."""
    mounts = _parse(_line(mountpoint, fstype), in_container=in_container, supervisor=supervisor)

    assert mounts[mountpoint].kind == kind


@pytest.mark.parametrize("mountpoint", ["/efi", "/efi/EFI"])
def test_efi_system_partition_is_excluded(mountpoint: str) -> None:
    """The EFI system partition mounted at /efi is no media location, although it is vfat."""
    assert _parse(_line(mountpoint, "vfat")) == {}


@pytest.mark.parametrize("fstype", ["overlay", "tmpfs", "squashfs", "proc", "cgroup2", "fuse"])
def test_filesystems_that_hold_no_media_are_excluded(fstype: str) -> None:
    """The container root and pseudo filesystems are never a media location."""
    assert _parse(_line("/music", fstype)) == {}


def test_last_mount_on_a_mountpoint_wins() -> None:
    """A later mount hides the earlier one on the same mountpoint, also when it holds no media."""
    shadowed = _parse(_line("/mnt/nas", "cifs"), _line("/mnt/nas", "tmpfs"))
    remounted = _parse(_line("/mnt/nas", "tmpfs"), _line("/mnt/nas", "nfs4"))

    assert shadowed == {}
    assert remounted["/mnt/nas"].fstype == "nfs4"


def test_read_only_from_mount_or_superblock_options() -> None:
    """A mount is read-only when either its own or its filesystem's options say so."""
    mounts = _parse(
        _line("/mnt/ro_mount", "ext4", options="ro,relatime"),
        "101 1 8:2 / /mnt/ro_filesystem rw,relatime - iso9660 /dev/sr0 ro,nojoliet",
        _line("/mnt/writable", "ext4"),
    )

    assert mounts["/mnt/ro_mount"].read_only
    assert mounts["/mnt/ro_filesystem"].read_only
    assert not mounts["/mnt/writable"].read_only


def test_several_optional_fields() -> None:
    """A mount that is shared and a slave at once has two optional fields before the dash."""
    mounts = _parse(
        "36 35 98:0 /mnt1 /mnt/music ro,noatime shared:5 master:1 - ext4 /dev/sdb1 rw",
        "37 35 98:1 / /mnt/more rw shared:6 master:2 propagate_from:3 unbindable - xfs /dev/sdc1 rw",
    )

    assert mounts == {
        "/mnt/music": MediaMount("/mnt/music", "ext4", read_only=True, kind=StorageKind.LOCAL_DISK),
        "/mnt/more": MediaMount("/mnt/more", "xfs", read_only=False, kind=StorageKind.LOCAL_DISK),
    }


def test_escaped_mountpoints_are_decoded() -> None:
    """Spaces, tabs and backslashes in a mountpoint come back as the characters themselves."""
    mounts = _parse(_line(r"/mnt/My\040Music\011Tab\134Slash", "ext4"))

    assert list(mounts) == ["/mnt/My Music\tTab\\Slash"]


def test_malformed_lines_are_skipped() -> None:
    """A truncated or garbled line does not break the rest of the table."""
    mounts = _parse(
        "",
        "garbage",
        "100 1 8:1 / /mnt/truncated rw,relatime shared:1",
        "100 1 8:1 / /mnt/no_source rw -",
        _line("/mnt/ok", "ext4"),
    )

    assert list(mounts) == ["/mnt/ok"]


def test_mountpoints_include_every_mount() -> None:
    """The mountpoints of a table include the mounts that hold no media."""
    mountpoints = parse_mountpoints(_fixture("haos_addon"))

    assert {"/", "/data", "/media", "/media/nas", "/media/backup_nfs", "/etc/hosts"} <= mountpoints


def test_dormant_automount_trigger_is_no_mount() -> None:
    """Only a trigger with the real mount on top counts as mounted."""
    dormant = parse_mountpoints(_line("/media/nas", "autofs"))
    woken = parse_mountpoints(
        "\n".join((_line("/media/nas", "autofs"), _line("/media/nas", "cifs")))
    )

    assert "/media/nas" not in dormant
    assert "/media/nas" in woken
    assert "/media/archive" not in parse_mountpoints(_fixture("haos_addon"))


def test_no_mount_table_outside_linux(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Without a mount table in procfs there is nothing to discover."""
    monkeypatch.setattr(mountinfo, "MOUNTINFO_PATH", str(tmp_path / "missing"))

    assert read_mountinfo() == ""
