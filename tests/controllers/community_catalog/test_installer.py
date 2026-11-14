"""Tests for the installer of community providers: download, check, activate and remove."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
import tarfile
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from aiohttp import ClientSession, ClientTimeout
from aiohttp.test_utils import unused_port

from music_assistant.controllers.community_catalog import installer as installer_module
from music_assistant.controllers.community_catalog.installer import (
    Installer,
    InstallError,
    content_hash,
)
from music_assistant.controllers.community_catalog.models import (
    IndexProvider,
    IndexStatus,
    IndexVersion,
)
from tests.controllers.community_catalog.conftest import (
    ADDED,
    ARCHIVE_ROOT,
    RELEASED,
    build_archive,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Mapping
    from pathlib import Path

    from tests.controllers.community_catalog.conftest import GitHub

LOGGER = logging.getLogger(__name__)
DOMAIN = "foo"
VERSION = "1.0.0"
COMMIT = "0123abcd"
FOLDER = "providers/foo"


def _manifest(domain: str = DOMAIN) -> bytes:
    """Return the manifest of a plugin provider."""
    return json.dumps(
        {
            "type": "plugin",
            "domain": domain,
            "name": domain,
            "description": "test provider",
            "codeowners": ["@someone"],
        }
    ).encode()


PROVIDER = {
    "manifest.json": _manifest(),
    "__init__.py": b"from .helpers.util import VALUE\n",
    "helpers/__init__.py": b"",
    "helpers/util.py": b"VALUE = 1\n",
}
# a fixed folder whose hash was computed with coreutils, in the folder:
# find . -type f ! -path '*/__pycache__/*' ! -name '*.pyc' | sed 's|^\./||' | LC_ALL=C sort
#   | xargs sha256sum | sha256sum
HASH_FOLDER = {
    "manifest.json": b'{"domain": "foo"}\n',
    "__init__.py": b"",
    "a.py": b"a\n",
    "a/b.py": b"b\n",
    "B.py": b"B\n",
    "é.txt": "é\n".encode(),
    "sub/deep/data.bin": bytes(range(256)),
}
HASH_FOLDER_HASH = "826ca411123465fad19e0b73dd160bd95dd150699eda49f13d4ac514bcf3ddf8"


@pytest.fixture
async def installer(tmp_path: Path) -> AsyncGenerator[Installer]:
    """Return an installer that keeps the providers in a temporary folder."""
    async with ClientSession() as session:
        mass = SimpleNamespace(http_session=session)
        yield Installer(mass, str(tmp_path / "community_providers"), LOGGER)  # type: ignore[arg-type]


@pytest.mark.parametrize("folder", ["foo", "providers/foo", "src/a/b/foo"])
async def test_stage_takes_only_the_provider_folder(
    tmp_path: Path, installer: Installer, github: GitHub, folder: str
) -> None:
    """Only the provider folder is staged, not a folder next to it whose name starts the same."""
    repository = {
        "README.md": b"readme\n",
        f"{folder}_bar/manifest.json": _manifest("foo_bar"),
        f"{folder}_bar/extra.py": b"",
        **_inside(PROVIDER, folder),
    }
    entry = _index_entry(github, build_archive(repository), folder=folder)

    manifest = await installer.stage(DOMAIN, entry, VERSION)

    root = tmp_path / "community_providers"
    assert manifest.domain == DOMAIN
    assert _read(root / ".staging" / DOMAIN) == PROVIDER
    assert _names(root / ".staging") == [DOMAIN]
    assert _names(root) == [".staging"]


async def test_stage_keeps_only_whether_a_file_is_executable(
    tmp_path: Path, installer: Installer, github: GitHub
) -> None:
    """An executable file stays executable; no other bit of an archived mode survives."""
    provider = {**PROVIDER, "run.sh": b"#!/bin/sh\n"}
    modes = {f"{FOLDER}/run.sh": 0o6777, f"{FOLDER}/__init__.py": 0o4666}
    entry = _index_entry(github, build_archive(_inside(provider, FOLDER), modes=modes), provider)

    await installer.stage(DOMAIN, entry, VERSION)

    staged = tmp_path / "community_providers" / ".staging" / DOMAIN
    assert stat.S_IMODE((staged / "run.sh").stat().st_mode) == 0o755
    assert stat.S_IMODE((staged / "__init__.py").stat().st_mode) == 0o644


@pytest.mark.parametrize(
    ("kind", "linkname"),
    [
        (tarfile.SYMTYPE, "../../README.md"),
        (tarfile.LNKTYPE, f"{ARCHIVE_ROOT}/README.md"),
        (tarfile.FIFOTYPE, ""),
    ],
    ids=["symlink", "hardlink", "fifo"],
)
async def test_stage_refuses_anything_but_files_and_folders(
    tmp_path: Path, installer: Installer, github: GitHub, kind: bytes, linkname: str
) -> None:
    """A link or a special file inside the provider folder refuses the install."""
    special = _member(f"{ARCHIVE_ROOT}/{FOLDER}/helpers/special", kind, linkname)
    archive = build_archive(_repository(), extra=[special])
    entry = _index_entry(github, archive)

    with pytest.raises(InstallError, match="helpers/special is not a regular file or folder"):
        await installer.stage(DOMAIN, entry, VERSION)

    assert _staged(tmp_path) == []


async def test_stage_ignores_links_outside_the_provider_folder(
    installer: Installer, github: GitHub
) -> None:
    """Links and special files elsewhere in the repository are not looked at."""
    extra = [
        _member(f"{ARCHIVE_ROOT}/docs/link", tarfile.SYMTYPE, "../README.md"),
        _member(f"{ARCHIVE_ROOT}/docs/hard", tarfile.LNKTYPE, f"{ARCHIVE_ROOT}/README.md"),
        _member(f"{ARCHIVE_ROOT}/docs/fifo", tarfile.FIFOTYPE),
    ]
    entry = _index_entry(github, build_archive(_repository(), extra=extra))

    assert (await installer.stage(DOMAIN, entry, VERSION)).domain == DOMAIN


@pytest.mark.parametrize(
    "name",
    [f"{ARCHIVE_ROOT}/{FOLDER}/../../../evil.py", "/providers/foo/evil.py"],
    ids=["parent", "absolute"],
)
async def test_stage_refuses_a_path_outside_the_repository(
    tmp_path: Path, installer: Installer, github: GitHub, name: str
) -> None:
    """A member that leaves the folder or has an absolute path refuses the install."""
    evil = tarfile.TarInfo(name)
    archive = build_archive(_repository(), extra=[evil])
    entry = _index_entry(github, archive)

    with pytest.raises(InstallError, match="lies outside the repository"):
        await installer.stage(DOMAIN, entry, VERSION)

    assert _staged(tmp_path) == []
    assert not (tmp_path / "evil.py").exists()


@pytest.mark.parametrize(
    ("provider", "content_hash_", "folder", "reason"),
    [
        (PROVIDER, "0" * 64, FOLDER, "its content is not what the community catalog lists"),
        ({**PROVIDER, "manifest.json": _manifest("bar")}, None, FOLDER, "its manifest is for bar"),
        ({"__init__.py": b""}, None, FOLDER, "it has no manifest.json"),
        (PROVIDER, None, "providers/other", "the repository has no folder providers/other"),
    ],
    ids=["content", "domain", "no manifest", "no folder"],
)
async def test_stage_refuses_what_the_catalog_does_not_list(
    tmp_path: Path,
    installer: Installer,
    github: GitHub,
    provider: dict[str, bytes],
    content_hash_: str | None,
    folder: str,
    reason: str,
) -> None:
    """A version that is not what the catalog lists is refused; the live version stays."""
    root = tmp_path / "community_providers"
    _write(root / DOMAIN, PROVIDER)
    _write(root / ".previous" / DOMAIN, {"old.py": b""})
    archive = build_archive(_repository(provider))
    entry = _index_entry(github, archive, provider, folder=folder, hash_=content_hash_)

    with pytest.raises(InstallError, match=re.escape(f"Refusing foo 1.0.0: {reason}")):
        await installer.stage(DOMAIN, entry, VERSION)

    assert _staged(tmp_path) == []
    assert _read(root / DOMAIN) == PROVIDER
    assert _read(root / ".previous" / DOMAIN) == {"old.py": b""}


@pytest.mark.parametrize("failure", ["not found", "refused", "stalled"])
async def test_stage_reports_a_failed_download(
    tmp_path: Path,
    installer: Installer,
    github: GitHub,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """A download that fails, or stalls, refuses the install and leaves nothing staged."""
    entry = _index_entry(github, build_archive(_repository()))
    if failure == "not found":
        github.status = 404
    elif failure == "refused":
        url = f"http://127.0.0.1:{unused_port()}/{{repository}}/tar.gz/{{commit}}"
        monkeypatch.setattr(installer_module, "ARCHIVE_URL", url)
    else:
        github.stall = True
        monkeypatch.setattr(installer_module, "_TIMEOUT", ClientTimeout(sock_read=0.2))

    with pytest.raises(InstallError, match=r"Could not download foo 1\.0\.0"):
        await installer.stage(DOMAIN, entry, VERSION)

    assert _staged(tmp_path) == []


async def test_stage_replaces_a_leftover(
    tmp_path: Path, installer: Installer, github: GitHub
) -> None:
    """What an interrupted install left staged does not break the next one."""
    staging = tmp_path / "community_providers" / ".staging"
    _write(staging / DOMAIN, {"stale.py": b"", "helpers/util.py": b"VALUE = 0\n"})
    (staging / f"{DOMAIN}.tar.gz").write_bytes(b"half a download")
    entry = _index_entry(github, build_archive(_repository()))

    await installer.stage(DOMAIN, entry, VERSION)

    assert _read(staging / DOMAIN) == PROVIDER
    assert _names(staging) == [DOMAIN]


async def test_stage_refuses_a_version_the_catalog_does_not_have(
    installer: Installer, github: GitHub
) -> None:
    """Only a version of the index can be staged."""
    entry = _index_entry(github, build_archive(_repository()))

    with pytest.raises(InstallError, match=r"Version 9\.9\.9 of foo is not in the community"):
        await installer.stage(DOMAIN, entry, "9.9.9")


def test_content_hash_matches_coreutils(tmp_path: Path) -> None:
    """The content hash is the one the shell pipeline in the comment of HASH_FOLDER gives."""
    _write(tmp_path, HASH_FOLDER)

    assert content_hash(tmp_path) == HASH_FOLDER_HASH


def test_content_hash_ignores_compiled_files_modes_times_and_empty_folders(tmp_path: Path) -> None:
    """Compiled Python files, modes, times and empty folders do not change the content hash."""
    _write(tmp_path, HASH_FOLDER)
    _write(tmp_path / "__pycache__", {"a.cpython-314.pyc": b"x", "notes.txt": b"x"})
    _write(tmp_path / "sub" / "__pycache__", {"x.pyc": b"x"})
    (tmp_path / "stray.pyc").write_bytes(b"x")
    (tmp_path / "empty" / "deeper").mkdir(parents=True)
    (tmp_path / "a.py").chmod(0o755)
    os.utime(tmp_path / "B.py", (0, 0))

    assert content_hash(tmp_path) == HASH_FOLDER_HASH


@pytest.mark.parametrize("change", ["rename", "content"])
def test_content_hash_changes_with_a_name_or_content(tmp_path: Path, change: str) -> None:
    """Renaming a file or changing its content changes the content hash."""
    _write(tmp_path, HASH_FOLDER)
    if change == "rename":
        (tmp_path / "a.py").rename(tmp_path / "c.py")
    else:
        (tmp_path / "a.py").write_bytes(b"c\n")

    assert content_hash(tmp_path) != HASH_FOLDER_HASH


async def test_activate_keeps_the_version_before(tmp_path: Path, installer: Installer) -> None:
    """Activating makes the staged version live and keeps the one before it as previous."""
    root = tmp_path / "community_providers"

    _write(root / ".staging" / DOMAIN, {"v1.py": b""})
    await installer.activate(DOMAIN)

    assert _read(root / DOMAIN) == {"v1.py": b""}
    assert not (root / ".previous" / DOMAIN).exists()

    _write(root / ".staging" / DOMAIN, {"v2.py": b""})
    await installer.activate(DOMAIN)

    assert _read(root / DOMAIN) == {"v2.py": b""}
    assert _read(root / ".previous" / DOMAIN) == {"v1.py": b""}

    _write(root / ".staging" / DOMAIN, {"v3.py": b""})
    await installer.activate(DOMAIN)

    assert _read(root / DOMAIN) == {"v3.py": b""}
    assert _read(root / ".previous" / DOMAIN) == {"v2.py": b""}
    assert _staged(tmp_path) == []


async def test_activate_without_a_staged_version(tmp_path: Path, installer: Installer) -> None:
    """Without a staged version activate refuses and the live version stays."""
    root = tmp_path / "community_providers"
    _write(root / DOMAIN, {"v1.py": b""})

    with pytest.raises(InstallError, match="foo has no staged version"):
        await installer.activate(DOMAIN)

    assert _read(root / DOMAIN) == {"v1.py": b""}


async def test_discard_and_remove(tmp_path: Path, installer: Installer) -> None:
    """Discard removes only the staged version; remove removes every version."""
    root = tmp_path / "community_providers"
    for folder in (root / DOMAIN, root / ".previous" / DOMAIN, root / ".staging" / DOMAIN):
        _write(folder, {"a.py": b""})
    _write(root / "bar", {"a.py": b""})

    await installer.discard(DOMAIN)

    assert _staged(tmp_path) == []
    assert (root / DOMAIN).is_dir()
    assert (root / ".previous" / DOMAIN).is_dir()

    _write(root / ".staging" / DOMAIN, {"a.py": b""})
    await installer.remove(DOMAIN)

    assert _staged(tmp_path) == []
    assert _names(root / ".previous") == []
    assert _names(root) == [".previous", ".staging", "bar"]


@pytest.mark.parametrize("domain", ["..", "foo/../..", ""])
async def test_a_domain_never_names_another_folder(
    tmp_path: Path, installer: Installer, domain: str
) -> None:
    """A domain that is not a provider domain is refused before any folder is touched."""
    _write(tmp_path / "community_providers", {"state.json": b"{}"})

    with pytest.raises(InstallError, match="is not a provider domain"):
        await installer.remove(domain)

    assert (tmp_path / "community_providers" / "state.json").exists()


def _index_entry(
    github: GitHub,
    archive: bytes,
    provider: Mapping[str, bytes] = PROVIDER,
    *,
    folder: str = FOLDER,
    hash_: str | None = None,
) -> IndexProvider:
    """
    Return an index entry for an archive, which the fixture GitHub serves.

    :param github: The fixture GitHub.
    :param archive: The archive of the repository at the listed commit.
    :param provider: The files of the provider, to compute the listed content hash from.
    :param folder: The folder of the provider in the repository.
    :param hash_: The listed content hash, when it should not match the provider.
    """
    github.archives[COMMIT] = archive
    version = IndexVersion(
        commit=COMMIT,
        folder=folder,
        content_hash=hash_ or _hash(provider),
        requirements=[],
        released=RELEASED,
    )
    return IndexProvider(
        status=IndexStatus.LISTED,
        repository="someone/repo",
        manifest={},
        added=ADDED,
        latest=VERSION,
        versions={VERSION: version},
    )


def _repository(provider: Mapping[str, bytes] = PROVIDER) -> dict[str, bytes]:
    """Return a repository with a provider in FOLDER and a file outside it."""
    return {"README.md": b"readme\n", **_inside(provider, FOLDER)}


def _inside(files: Mapping[str, bytes], folder: str) -> dict[str, bytes]:
    """Return files with their paths below a folder."""
    return {f"{folder}/{path}": content for path, content in files.items()}


def _member(name: str, kind: bytes, linkname: str = "") -> tarfile.TarInfo:
    """Return an archive member that is not a regular file or folder."""
    info = tarfile.TarInfo(name)
    info.type, info.linkname = kind, linkname
    return info


def _hash(files: Mapping[str, bytes]) -> str:
    """Return the content hash of files by their definition, independent of the installer."""
    lines = sorted(
        (path.encode(), f"{hashlib.sha256(content).hexdigest()}  {path}\n".encode())
        for path, content in files.items()
    )
    return hashlib.sha256(b"".join(line for _, line in lines)).hexdigest()


def _write(folder: Path, files: Mapping[str, bytes]) -> None:
    """Write files below a folder."""
    for name, content in files.items():
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def _read(folder: Path) -> dict[str, bytes]:
    """Return the content of every file below a folder, by POSIX path."""
    return {
        path.relative_to(folder).as_posix(): path.read_bytes()
        for path in folder.rglob("*")
        if path.is_file()
    }


def _staged(tmp_path: Path) -> list[str]:
    """Return what lies in the staging folder."""
    return _names(tmp_path / "community_providers" / ".staging")


def _names(folder: Path) -> list[str]:
    """Return the names of what lies in a folder, none when it does not exist."""
    return sorted(path.name for path in folder.iterdir()) if folder.exists() else []
