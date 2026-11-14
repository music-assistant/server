"""Fixtures for the tests of the community catalog: a registry that serves a fixture index."""

from __future__ import annotations

import asyncio
import io
import tarfile
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

import pytest
from aiohttp import hdrs, web
from aiohttp.test_utils import TestServer

from music_assistant.controllers.community_catalog import index as index_module
from music_assistant.controllers.community_catalog import installer as installer_module

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Mapping, Sequence

ADDED = "2026-10-01T12:00:00Z"
RELEASED = "2026-11-01T12:00:00Z"
# the top-level folder of an archive GitHub serves
ARCHIVE_ROOT = "someone-repo-0123abcd"


@dataclass
class Registry:
    """What the fixture registry answers, and the requests it received."""

    document: dict[str, Any] | bytes
    status: int = 200
    etag: str = '"one"'
    delay: float = 0
    # the line and the If-None-Match header of each request
    requests: list[tuple[str, str | None]] = field(default_factory=list)


@dataclass
class GitHub:
    """What the fixture GitHub answers to a request for the archive of a commit."""

    # the archive of each commit
    archives: dict[str, bytes] = field(default_factory=dict)
    status: int = 200
    # send the start of the archive, then nothing until the fixture ends
    stall: bool = False
    release: asyncio.Event = field(default_factory=asyncio.Event)


def build_archive(
    files: Mapping[str, bytes],
    *,
    modes: Mapping[str, int] | None = None,
    extra: Sequence[tarfile.TarInfo] = (),
) -> bytes:
    """
    Return a repository as GitHub archives it: one top-level folder, each folder before its files.

    :param files: The content of each file, by path in the repository.
    :param modes: The mode of a file, by path in the repository; 0o644 when omitted.
    :param extra: Members added after the files as they are, named by their full path in the archive.
    """
    modes = modes or {}
    folders = {parent for path in files for parent in PurePosixPath(path).parents}
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for folder in sorted(folders, key=str):
            info = tarfile.TarInfo(str(PurePosixPath(ARCHIVE_ROOT, folder)))
            info.type, info.mode = tarfile.DIRTYPE, 0o755
            tar.addfile(info)
        for path, content in files.items():
            info = tarfile.TarInfo(f"{ARCHIVE_ROOT}/{path}")
            info.size, info.mode = len(content), modes.get(path, 0o644)
            tar.addfile(info, io.BytesIO(content))
        for info in extra:
            tar.addfile(info)
    return buffer.getvalue()


def catalog_index(line: str = "nightly", schema: int = 1) -> dict[str, Any]:
    """
    Return an index with one provider of each kind the list has to tell apart.

    :param line: The line of the index.
    :param schema: The format of the index.
    """
    return {
        "schema": schema,
        "server_line": line,
        "generated": "2026-11-14T08:00:00Z",
        "a_field_of_a_later_registry": True,
        "providers": {
            "listed_one": _entry("listed_one", "listed"),
            "blocked_one": _entry("blocked_one", "blocked", reason="Sends the library elsewhere"),
            "withdrawn_one": _entry("withdrawn_one", "withdrawn", reason="No longer maintained"),
            "broken_one": _entry("broken_one", "listed", manifest={"domain": "broken_one"}),
            "unreleased_one": _entry("unreleased_one", "listed", latest=None),
            "blocked_latest_one": _entry("blocked_latest_one", "listed")
            | {"blocked_versions": {"1.2.0": "Sends the library elsewhere"}},
        },
    }


@pytest.fixture
async def registry(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[Registry]:
    """Serve the fixture index at the address the server reads the index from."""
    registry = Registry(catalog_index())

    async def serve_index(request: web.Request) -> web.StreamResponse:
        if_none_match = request.headers.get(hdrs.IF_NONE_MATCH)
        registry.requests.append((request.match_info["line"], if_none_match))
        await asyncio.sleep(registry.delay)
        if registry.status != 200:
            return web.Response(status=registry.status)
        if if_none_match == registry.etag:
            return web.Response(status=304)
        if isinstance(registry.document, bytes):
            return web.Response(body=registry.document, headers={hdrs.ETAG: registry.etag})
        return web.json_response(registry.document, headers={hdrs.ETAG: registry.etag})

    app = web.Application()
    app.router.add_get("/v1/{line}.json", serve_index)
    async with TestServer(app) as server:
        url = f"http://{server.host}:{server.port}/v1/{{line}}.json"
        monkeypatch.setattr(index_module, "INDEX_URL", url)
        yield registry


@pytest.fixture
async def github(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[GitHub]:
    """Serve archives at the address the server downloads community providers from."""
    github = GitHub()

    async def serve_archive(request: web.Request) -> web.StreamResponse:
        if github.status != 200:
            return web.Response(status=github.status)
        archive = github.archives[request.match_info["commit"]]
        if not github.stall:
            return web.Response(body=archive)
        response = web.StreamResponse()
        await response.prepare(request)
        await response.write(archive[:100])
        await github.release.wait()
        return response

    app = web.Application()
    app.router.add_get("/{owner}/{name}/tar.gz/{commit}", serve_archive)
    async with TestServer(app) as server:
        url = f"http://{server.host}:{server.port}/{{repository}}/tar.gz/{{commit}}"
        monkeypatch.setattr(installer_module, "ARCHIVE_URL", url)
        yield github
        github.release.set()


def _entry(
    domain: str,
    status: str,
    *,
    reason: str | None = None,
    manifest: dict[str, Any] | None = None,
    latest: str | None = "1.2.0",
) -> dict[str, Any]:
    """
    Return a provider of the index.

    :param domain: The domain of the provider.
    :param status: Whether the catalog offers the provider.
    :param reason: Why the provider is blocked or withdrawn.
    :param manifest: The manifest snapshot, a plugin that claims to be built-in when omitted.
    :param latest: The newest version for the line.
    """
    version = {
        "commit": "0123abcd",
        "folder": domain,
        "content_hash": "e3b0c442",
        "requirements": ["some-package==1.0"],
        "released": RELEASED,
    }
    return {
        "status": status,
        "reason": reason,
        "reason_url": None,
        "repository": f"someone/{domain}",
        "maintainers": ["someone"],
        "icon_svg": "<svg/>",
        "added": ADDED,
        "manifest": manifest
        or {
            "type": "plugin",
            "domain": domain,
            "name": domain,
            "description": "test provider",
            "codeowners": ["@someone"],
            "tier": "core",
            "builtin": True,
            "allow_disable": False,
        },
        "latest": latest,
        "versions": {"1.2.0": version},
        "blocked_versions": {},
    }
