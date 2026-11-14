"""Tests for the dependency folder of community providers, against wheels built by the test."""

from __future__ import annotations

import base64
import hashlib
import importlib
import importlib.util
import sys
import zipfile
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from music_assistant.controllers.community_catalog import dependencies
from music_assistant.controllers.community_catalog.dependencies import (
    CommunityDependencies,
    DependenciesUnavailable,
    DependencyConflict,
    ResolvedDependencies,
)

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("uv") is None, reason="uv is not installed in this environment"
)

# a package of the server's own environment, and a version of it the server does not have
SERVER_PACKAGE = "orjson"
SERVER_VERSION = version(SERVER_PACKAGE)


@pytest.fixture(scope="module")
def wheels(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Return a folder with tiny wheels (and one sdist) that uv resolves against."""
    folder = tmp_path_factory.mktemp("wheels")
    _build_wheel(folder, SERVER_PACKAGE, SERVER_VERSION)
    _build_wheel(folder, SERVER_PACKAGE, "1.0")
    _build_wheel(folder, "uses-server", "1.0", SERVER_PACKAGE)
    _build_wheel(folder, "needs-old-server", "1.0", f"{SERVER_PACKAGE}==1.0")
    _build_wheel(folder, "picky", "1.0", SERVER_PACKAGE)
    _build_wheel(folder, "picky", "2.0", f"{SERVER_PACKAGE}==1.0")
    _build_wheel(folder, "newdep", "1.0")
    _build_wheel(folder, "newdep", "2.0")
    (folder / "sdist_only-1.0.tar.gz").write_bytes(b"")
    return folder


@pytest.fixture
def deps(tmp_path: Path, wheels: Path, monkeypatch: pytest.MonkeyPatch) -> CommunityDependencies:
    """Return a dependency folder in a fresh cache folder, resolving against the test wheels."""
    monkeypatch.setattr(dependencies, "_UV_INDEX_ARGS", ["--no-index", "--find-links", str(wheels)])
    return CommunityDependencies(Mock(cache_path=str(tmp_path)))


async def test_server_package_is_not_installed(deps: CommunityDependencies) -> None:
    """A requirement the server already has is resolved against it, not installed."""
    resolved = await deps.resolve({"one": ["uses-server"]})
    assert resolved.pins == ("uses-server==1.0",)

    await deps.sync(resolved)
    installed = {path.name for path in Path(deps.path).iterdir()}
    assert "uses_server" in installed
    assert not any(name.startswith(SERVER_PACKAGE) for name in installed)


async def test_clash_with_server(deps: CommunityDependencies) -> None:
    """A requirement that needs another version of a server package names the server."""
    with pytest.raises(DependencyConflict) as err:
        await deps.resolve({"one": ["uses-server"], "two": ["needs-old-server"]})
    assert (err.value.domain, err.value.culprit) == ("two", "server")
    assert SERVER_PACKAGE in str(err.value)


async def test_version_that_needs_another_server_package_is_passed_over(
    deps: CommunityDependencies,
) -> None:
    """The newest version of a package is not chosen when it does not fit the server's packages."""
    assert (await deps.resolve({"one": ["picky"]})).pins == ("picky==1.0",)


@pytest.mark.parametrize(
    "requirement",
    [
        "newdep @ https://example.com/newdep-1.0-py3-none-any.whl",
        "--find-links /tmp",
        "-r more.txt",
    ],
)
async def test_only_plain_requirements(deps: CommunityDependencies, requirement: str) -> None:
    """A requirement that is a URL or an option is refused and names the provider itself."""
    with pytest.raises(DependencyConflict) as err:
        await deps.resolve({"one": [requirement]})
    assert (err.value.domain, err.value.culprit) == ("one", "one")


async def test_clash_between_providers(deps: CommunityDependencies) -> None:
    """A clash between two providers refuses the later one and names the earlier one."""
    requirements = {"one": ["newdep<2"], "two": ["uses-server"], "three": ["newdep>=2"]}
    with pytest.raises(DependencyConflict) as err:
        await deps.resolve(requirements)
    assert (err.value.domain, err.value.culprit) == ("three", "one")


async def test_requirement_without_wheel_is_refused(deps: CommunityDependencies) -> None:
    """A requirement with only a source distribution names the provider itself."""
    with pytest.raises(DependencyConflict) as err:
        await deps.resolve({"one": ["sdist-only"]})
    assert (err.value.domain, err.value.culprit) == ("one", "one")


async def test_lock_keeps_pins(deps: CommunityDependencies, tmp_path: Path) -> None:
    """A pin of the installed set stays while the requirements allow it."""
    await deps.sync(await deps.resolve({"one": ["newdep<2"]}))

    assert (await deps.resolve({"one": ["newdep"]})).pins == ("newdep==1.0",)
    fresh = CommunityDependencies(Mock(cache_path=str(tmp_path / "fresh")))
    assert (await fresh.resolve({"one": ["newdep"]})).pins == ("newdep==2.0",)


async def test_sync_removes_what_the_lock_no_longer_has(deps: CommunityDependencies) -> None:
    """A package no requirement needs any more is removed from the folder."""
    await deps.sync(await deps.resolve({"one": ["newdep"], "two": ["uses-server"]}))
    assert (Path(deps.path) / "newdep").is_dir()

    await deps.sync(await deps.resolve({"two": ["uses-server"]}))
    assert not (Path(deps.path) / "newdep").exists()
    assert (Path(deps.path) / "uses_server").is_dir()
    assert (Path(deps.path) / "requirements.lock").read_text().splitlines()[2:] == [
        "uses-server==1.0"
    ]


async def test_unavailable_uv(
    deps: CommunityDependencies, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the package index, unchanged inputs still resolve and new ones are refused."""
    resolved = await deps.resolve({"one": ["newdep<2"]})
    await deps.sync(resolved)
    lock = (Path(deps.path) / "requirements.lock").read_text()
    missing = ["--no-index", "--find-links", str(tmp_path / "missing")]
    monkeypatch.setattr(dependencies, "_UV_INDEX_ARGS", missing)

    assert await deps.resolve({"one": ["newdep<2"]}) == resolved
    await deps.sync(resolved)
    with pytest.raises(DependenciesUnavailable):
        await deps.resolve({"one": ["newdep"]})
    with pytest.raises(DependenciesUnavailable):
        await deps.sync(ResolvedDependencies(("newdep==2.0",), "changed", resolved.environment))
    assert (Path(deps.path) / "requirements.lock").read_text() == lock


async def test_another_python_installs_the_folder_again(
    deps: CommunityDependencies, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a Python or platform change the same pins are installed again."""
    await deps.sync(await deps.resolve({"one": ["newdep<2"]}))
    module = Path(deps.path) / "newdep" / "__init__.py"
    module.write_text("installed for the previous Python")
    monkeypatch.setattr(dependencies, "_ENVIRONMENT", "another-python")

    resolved = await deps.resolve({"one": ["newdep<2"]})
    assert resolved.pins == ("newdep==1.0",)
    await deps.sync(resolved)

    assert module.read_text() == "VERSION = '1.0'\n"
    assert await deps.resolve({"one": ["newdep<2"]}) == resolved


def test_server_constraints_keep_the_installed_build(monkeypatch: pytest.MonkeyPatch) -> None:
    """A server package installed as a local build (`+cpu`) is pinned exactly as installed."""
    installed = [SimpleNamespace(name="Local_Build", version="1.0+cpu")]
    monkeypatch.setattr(dependencies, "distributions", lambda path: installed)  # noqa: ARG005

    assert dependencies._server_constraints() == ["local-build==1.0+cpu"]


def test_resolves_against_the_indexes_of_the_server() -> None:
    """The resolve uses the extra index and index strategy the server image is built with."""
    repo = Path(__file__).parents[3]
    extra_index = next(
        line.split()[1]
        for line in (repo / "requirements_all.txt").read_text().splitlines()
        if line.startswith("--extra-index-url")
    )

    assert [
        "--extra-index-url",
        extra_index,
        "--index-strategy",
        "unsafe-best-match",
    ] == dependencies._UV_INDEX_ARGS
    assert "--index-strategy unsafe-best-match" in (repo / "Dockerfile").read_text()


async def test_touches_imported(
    deps: CommunityDependencies, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only replacing or removing a package that is imported needs a restart."""
    await deps.sync(await deps.resolve({"one": ["newdep<2"], "two": ["uses-server"]}))
    monkeypatch.setattr(sys, "path", list(sys.path))
    deps.activate()
    deps.activate()
    assert sys.path.count(deps.path) == 1
    try:
        assert importlib.import_module("newdep").__file__.startswith(deps.path)  # type: ignore[union-attr]
        assert not await deps.touches_imported(["newdep==1.0", "uses-server==1.0"])
        assert not await deps.touches_imported(["newdep==1.0"])
        assert await deps.touches_imported(["newdep==2.0", "uses-server==1.0"])
        assert await deps.touches_imported([])
    finally:
        sys.modules.pop("newdep", None)


def _build_wheel(folder: Path, name: str, version: str, *requires: str) -> None:
    """Write a pure-Python wheel with one module named after the distribution."""
    module = name.replace("-", "_")
    dist_info = f"{module}-{version}.dist-info"
    metadata = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
    metadata += "".join(f"Requires-Dist: {req}\n" for req in requires)
    files = {
        f"{module}/__init__.py": f"VERSION = {version!r}\n",
        f"{dist_info}/METADATA": metadata,
        f"{dist_info}/WHEEL": "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    record = "".join(
        f"{path},sha256={_record_hash(content)},{len(content)}\n" for path, content in files.items()
    )
    files[f"{dist_info}/RECORD"] = f"{record}{dist_info}/RECORD,,\n"
    with zipfile.ZipFile(folder / f"{module}-{version}-py3-none-any.whl", "w") as wheel:
        for path, content in files.items():
            wheel.writestr(path, content)


def _record_hash(content: str) -> str:
    """Return the hash of a file as a wheel's RECORD writes it."""
    digest = hashlib.sha256(content.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
