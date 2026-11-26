"""Fixtures for the tests of the community catalog: a registry that serves a fixture index."""

from __future__ import annotations

import asyncio
import datetime
import io
import ipaddress
import ssl
import tarfile
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

import pytest
from aiohttp import hdrs, web
from aiohttp.test_utils import TestServer
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from music_assistant.controllers.community_catalog import index as index_module
from music_assistant.controllers.community_catalog import installer as installer_module
from music_assistant.helpers import ssl as ssl_util

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Mapping, Sequence

    from music_assistant_models.errors import MusicAssistantError

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
    # the channel and the If-None-Match header of each request
    requests: list[tuple[str, str | None]] = field(default_factory=list)
    # the address of the index over https, and the same index over plain http
    url: str = ""
    http_url: str = ""


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


def assert_refused(
    refused: pytest.ExceptionInfo[MusicAssistantError], key: str, *args: str
) -> None:
    """
    Assert that a command of the community catalog failed with one of its translatable texts.

    :param refused: The raised error.
    :param key: The key of the text, without its community_catalog_ prefix.
    :param args: What the text names.
    """
    assert refused.value.translation_key == f"community_catalog_{key}"
    assert refused.value.translation_args == list(args)


def catalog_index(channel: str = "nightly", schema: int = 1) -> dict[str, Any]:
    """
    Return an index with one provider of each kind the list has to tell apart.

    :param channel: The channel of the index.
    :param schema: The format of the index.
    """
    return {
        "schema": schema,
        "channel": channel,
        "generated": "2026-11-14T08:00:00Z",
        "a_field_of_a_later_registry": True,
        "providers": {
            "listed_one": _entry("listed_one", "listed"),
            "blocked_one": _entry("blocked_one", "blocked", reason="Sends the library elsewhere"),
            "withdrawn_one": _entry("withdrawn_one", "withdrawn", reason="No longer maintained"),
            "broken_one": _entry("broken_one", "listed", manifest={"domain": "broken_one"}),
            "misnamed_one": _entry("misnamed_one", "listed")
            | {"manifest": _entry("another_one", "listed")["manifest"]},
            "unreleased_one": _entry("unreleased_one", "listed", latest=None),
            "blocked_latest_one": _entry("blocked_latest_one", "listed")
            | {"blocked_versions": {"1.2.0": "Sends the library elsewhere"}},
        },
    }


@dataclass
class Tls:
    """The SSL contexts of a certificate for 127.0.0.1 that only these tests trust."""

    server: ssl.SSLContext
    client: ssl.SSLContext


@pytest.fixture(scope="session")
def tls(tmp_path_factory: pytest.TempPathFactory) -> Tls:
    """Return the SSL contexts the fixture servers and their clients use for https."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "community catalog tests")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    folder = tmp_path_factory.mktemp("tls")
    (folder / "cert.pem").write_bytes(cert_pem)
    (folder / "key.pem").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    server = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    server.load_cert_chain(folder / "cert.pem", folder / "key.pem")
    client = ssl_util.create_client_context()
    client.load_verify_locations(cadata=cert_pem.decode())
    return Tls(server, client)


@pytest.fixture(autouse=True)
def trust_tls(monkeypatch: pytest.MonkeyPatch, tls: Tls) -> None:
    """Let the Music Assistant of each test trust the certificate of the fixture servers."""
    monkeypatch.setattr(ssl_util, "client_context", lambda *_: tls.client)


@asynccontextmanager
async def serve(app: web.Application, ssl_context: ssl.SSLContext | None) -> AsyncIterator[str]:
    """
    Serve an app on 127.0.0.1 and yield its root address.

    :param app: The app to serve.
    :param ssl_context: The context to serve https with, plain http when None.
    """
    server = TestServer(app)
    await server.start_server(ssl=ssl_context)
    try:
        yield f"{server.scheme}://{server.host}:{server.port}"
    finally:
        await server.close()


@pytest.fixture
async def registry(monkeypatch: pytest.MonkeyPatch, tls: Tls) -> AsyncGenerator[Registry]:
    """Serve the fixture index over https at the address the server reads the index from."""
    registry = Registry(catalog_index())

    async def serve_index(request: web.Request) -> web.StreamResponse:
        if_none_match = request.headers.get(hdrs.IF_NONE_MATCH)
        registry.requests.append((request.match_info["channel"], if_none_match))
        await asyncio.sleep(registry.delay)
        if registry.status != 200:
            return web.Response(status=registry.status)
        if if_none_match == registry.etag:
            return web.Response(status=304)
        if isinstance(registry.document, bytes):
            return web.Response(body=registry.document, headers={hdrs.ETAG: registry.etag})
        return web.json_response(registry.document, headers={hdrs.ETAG: registry.etag})

    https_app, http_app = web.Application(), web.Application()
    for app in (https_app, http_app):
        app.router.add_get("/v1/{channel}.json", serve_index)
    async with serve(https_app, tls.server) as https_root, serve(http_app, None) as http_root:
        registry.url = f"{https_root}/v1/{{channel}}.json"
        registry.http_url = f"{http_root}/v1/{{channel}}.json"
        monkeypatch.setattr(index_module, "INDEX_URL", registry.url)
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
    :param latest: The newest version for the channel.
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
