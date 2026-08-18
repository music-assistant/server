"""Tests for the Spotify Soloist shared helpers (binary manager + WebSocket client)."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import platform
import tarfile
import time
from collections.abc import AsyncGenerator, Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Self, cast

import pytest
from aiohttp import ClientError, WSMessage, WSMsgType

from music_assistant.providers.spotify_connect import soloist
from music_assistant.providers.spotify_connect.soloist import (
    BuildExpiredError,
    ConsentRequiredError,
    DownloadFailedError,
    InvalidArchiveError,
    SoloistAuthState,
    SoloistBinaryManager,
    SoloistClient,
    SoloistError,
    SoloistEvent,
    SoloistPlaybackState,
    SoloistPositionSync,
    SoloistQueueChanged,
    SoloistVolumeChanged,
    UnsupportedPlatformError,
)

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

_CDN_URL = "https://soloist-builds.spotifycdn.com/soloist_release_{arch}.tar.gz"
# build timestamp relative to now so the fake build never ages past the 90-day expiry
_VERSION_OUTPUT = (
    f"soloist version 1.2.3\nbuild {datetime.now(tz=UTC):%Y-%m-%dT%H:%M:%SZ} linux/x86_64"
).encode()


def _elf_binary(arch: str, marker: bytes = b"GOOD") -> bytes:
    """Return a minimal (fake) ELF executable for the given soloist architecture."""
    machine = {"arm64": 0xB7, "arm32": 0x28, "x86_64": 0x3E}[arch]
    header = bytearray(20)
    header[0:4] = b"\x7fELF"
    header[4] = 1 if arch == "arm32" else 2  # EI_CLASS
    header[5] = 1  # EI_DATA: little-endian
    header[6] = 1  # EI_VERSION
    header[16:18] = (2).to_bytes(2, "little")  # e_type: ET_EXEC
    header[18:20] = machine.to_bytes(2, "little")
    return bytes(header) + marker + b"\x00" * 64


def _build_archive(
    path: Path,
    files: dict[str, bytes] | None = None,
    *,
    symlink: tuple[str, str] | None = None,
) -> bytes:
    """Build a tar.gz archive with the given members and return its raw bytes."""
    with tarfile.open(path, "w:gz") as tar:
        for name, content in (files or {}).items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
        if symlink is not None:
            info = tarfile.TarInfo(symlink[0])
            info.type = tarfile.SYMTYPE
            info.linkname = symlink[1]
            tar.addfile(info)
    return path.read_bytes()


class _FakeContent:
    """Response body that hands out chunks like aiohttp's StreamReader."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    async def iter_chunked(self, n: int) -> AsyncGenerator[bytes]:
        """Yield the body in chunks of at most n bytes."""
        for i in range(0, len(self._body), n):
            await asyncio.sleep(0)
            yield self._body[i : i + n]


class _FakeResponse:
    """Stand-in for an aiohttp response context manager."""

    def __init__(
        self, status: int = 200, headers: dict[str, str] | None = None, body: bytes = b""
    ) -> None:
        self.status = status
        self.headers = headers or {}
        self.content = _FakeContent(body)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None


_Handler = Callable[[str, str], _FakeResponse]


class _FakeSession:
    """Fake aiohttp session that records every request it receives."""

    def __init__(self, handler: _Handler) -> None:
        self.handler = handler
        self.requests: list[tuple[str, str]] = []

    def get(self, url: str, **_kwargs: Any) -> _FakeResponse:
        """Issue a fake GET request."""
        return self._request("GET", url)

    def head(self, url: str, **_kwargs: Any) -> _FakeResponse:
        """Issue a fake HEAD request."""
        return self._request("HEAD", url)

    def _request(self, method: str, url: str) -> _FakeResponse:
        self.requests.append((method, url))
        return self.handler(method, url)


def _serve_archive(archive: bytes, etag: str = '"v1"') -> _Handler:
    """Return a request handler that serves the given archive bytes for any URL."""

    def handler(method: str, _url: str) -> _FakeResponse:
        if method == "HEAD":
            return _FakeResponse(headers={"ETag": etag})
        return _FakeResponse(headers={"ETag": etag}, body=archive)

    return handler


def _offline(_method: str, _url: str) -> _FakeResponse:
    """Request handler that behaves as if there is no network at all."""
    raise ClientError("no route to host")


def _make_manager(tmp_path: Path, handler: _Handler) -> tuple[SoloistBinaryManager, _FakeSession]:
    """Create a binary manager on a fake mass with the given request handler."""
    session = _FakeSession(handler)
    mass = SimpleNamespace(storage_path=str(tmp_path / "storage"), http_session=session)
    return SoloistBinaryManager(cast("MusicAssistant", mass)), session


def _install_dir(tmp_path: Path) -> Path:
    """Return the manager's install directory for the given tmp_path."""
    return tmp_path / "storage" / "soloist"


def _age_metadata(tmp_path: Path, days: float = 80.0) -> None:
    """
    Rewrite the persisted install metadata as if the build were days old.

    The default lands inside the update window (76 days) while staying short of
    the hard 90-day expiry, so the install counts as refreshable-but-valid.
    """
    meta_path = _install_dir(tmp_path) / "soloist.meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    aged = time.time() - days * 86400
    meta["installed_at"] = aged
    meta["build_timestamp"] = aged
    meta_path.write_text(json.dumps(meta), encoding="utf-8")


async def _fake_check_output(*args: str, **_kwargs: Any) -> tuple[int, bytes]:
    """Fake --version subprocess call whose result depends on markers in the binary."""
    content = Path(args[0]).read_bytes()
    if b"EXPIRED" in content:
        return (10, b"soloist build expired")
    if b"BROKEN" in content:
        return (1, b"crash")
    return (0, _VERSION_OUTPUT)


@pytest.fixture
def fake_version_cmd(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the --version subprocess call with a marker-based fake."""
    monkeypatch.setattr(soloist, "check_output", _fake_check_output)


@pytest.fixture(autouse=True)
def _reset_verify_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate the module-level recent-verification stamp between tests."""
    monkeypatch.setattr(soloist, "_last_verified", None)


@pytest.fixture
def linux_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend to run on Linux x86_64 (tests run on macOS)."""
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")


@pytest.mark.usefixtures("fake_version_cmd")
@pytest.mark.parametrize(
    ("machine", "arch"),
    [
        ("aarch64", "arm64"),
        ("arm64", "arm64"),
        ("armv7l", "arm32"),
        ("armv8l", "arm32"),
        ("x86_64", "x86_64"),
        ("amd64", "x86_64"),
    ],
)
async def test_arch_maps_to_cdn_artifact(
    machine: str, arch: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each supported machine downloads the matching CDN artifact."""
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(platform, "machine", lambda: machine)
    archive = _build_archive(tmp_path / "a.tar.gz", {"soloist": _elf_binary(arch)})
    manager, session = _make_manager(tmp_path, _serve_archive(archive))

    path = await manager.ensure_binary(consent=True)

    assert path.is_file()
    assert session.requests == [("GET", _CDN_URL.format(arch=arch))]


async def test_non_linux_platform_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-Linux platform is rejected without any network access."""
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(platform, "machine", lambda: "arm64")
    manager, session = _make_manager(tmp_path, _offline)

    with pytest.raises(UnsupportedPlatformError):
        await manager.ensure_binary(consent=True)
    assert session.requests == []


async def test_unknown_machine_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An unknown machine architecture is rejected without any network access."""
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(platform, "machine", lambda: "mips64")
    manager, session = _make_manager(tmp_path, _offline)

    with pytest.raises(UnsupportedPlatformError):
        await manager.ensure_binary(consent=True)
    assert session.requests == []


@pytest.mark.usefixtures("linux_platform")
async def test_download_requires_consent(tmp_path: Path) -> None:
    """Without consent no download is attempted and no network call is made."""
    manager, session = _make_manager(tmp_path, _offline)

    with pytest.raises(ConsentRequiredError):
        await manager.ensure_binary(consent=False)
    assert session.requests == []


@pytest.mark.usefixtures("linux_platform", "fake_version_cmd")
async def test_installed_binary_returned_without_network(tmp_path: Path) -> None:
    """An already-installed valid binary is returned without consent or network."""
    install_dir = _install_dir(tmp_path)
    install_dir.mkdir(parents=True)
    (install_dir / "soloist").write_bytes(_elf_binary("x86_64"))
    manager, session = _make_manager(tmp_path, _offline)

    path = await manager.ensure_binary(consent=False)

    assert path == install_dir / "soloist"
    assert session.requests == []


@pytest.mark.usefixtures("linux_platform", "fake_version_cmd")
@pytest.mark.parametrize("redirect_host", ["evil.example.com", "evilspotifycdn.com"])
async def test_redirect_outside_allowlist_rejected(tmp_path: Path, redirect_host: str) -> None:
    """A redirect to a host outside Spotify's infrastructure aborts the download."""

    def handler(_method: str, _url: str) -> _FakeResponse:
        return _FakeResponse(
            status=302, headers={"Location": f"https://{redirect_host}/soloist.tar.gz"}
        )

    manager, session = _make_manager(tmp_path, handler)

    with pytest.raises(DownloadFailedError, match="untrusted host"):
        await manager.ensure_binary(consent=True)
    # only the initial request went out, the redirect was never followed
    assert len(session.requests) == 1


@pytest.mark.usefixtures("linux_platform", "fake_version_cmd")
async def test_redirect_within_allowlist_followed(tmp_path: Path) -> None:
    """A redirect within Spotify's infrastructure is followed and the download succeeds."""
    archive = _build_archive(tmp_path / "a.tar.gz", {"soloist": _elf_binary("x86_64")})
    redirect_url = "https://downloads.spotify.com/soloist_release_x86_64.tar.gz"

    def handler(_method: str, url: str) -> _FakeResponse:
        if url != redirect_url:
            return _FakeResponse(status=302, headers={"Location": redirect_url})
        return _FakeResponse(body=archive)

    manager, session = _make_manager(tmp_path, handler)

    path = await manager.ensure_binary(consent=True)

    assert path.is_file()
    assert session.requests[-1] == ("GET", redirect_url)


@pytest.mark.usefixtures("linux_platform", "fake_version_cmd")
@pytest.mark.parametrize(
    "files",
    [
        {"../soloist": b"payload"},  # path traversal
        {"/soloist": b"payload"},  # absolute path
        {"soloist": b"payload", "README": b"docs"},  # extra file
        {"README": b"docs"},  # no soloist binary at all
    ],
)
async def test_unsafe_or_unexpected_archive_rejected(
    tmp_path: Path, files: dict[str, bytes]
) -> None:
    """Archives with traversal, absolute paths, extra or missing files are rejected."""
    archive = _build_archive(tmp_path / "a.tar.gz", files)
    manager, _ = _make_manager(tmp_path, _serve_archive(archive))

    with pytest.raises(InvalidArchiveError):
        await manager.ensure_binary(consent=True)


@pytest.mark.usefixtures("linux_platform", "fake_version_cmd")
async def test_symlink_archive_rejected(tmp_path: Path) -> None:
    """An archive delivering soloist as a symlink is rejected."""
    archive = _build_archive(tmp_path / "a.tar.gz", symlink=("soloist", "/etc/passwd"))
    manager, _ = _make_manager(tmp_path, _serve_archive(archive))

    with pytest.raises(InvalidArchiveError):
        await manager.ensure_binary(consent=True)


@pytest.mark.usefixtures("linux_platform", "fake_version_cmd")
async def test_garbage_archive_rejected(tmp_path: Path) -> None:
    """A response that is not a tar.gz archive at all is rejected."""
    manager, _ = _make_manager(tmp_path, _serve_archive(b"this is not a tarball"))

    with pytest.raises(InvalidArchiveError):
        await manager.ensure_binary(consent=True)


@pytest.mark.usefixtures("linux_platform", "fake_version_cmd")
@pytest.mark.parametrize("content", [_elf_binary("arm64"), b"#!/bin/sh\necho not an elf\n"])
async def test_wrong_or_non_elf_binary_rejected(tmp_path: Path, content: bytes) -> None:
    """A binary for another architecture (or not an ELF at all) is rejected."""
    archive = _build_archive(tmp_path / "a.tar.gz", {"soloist": content})
    manager, _ = _make_manager(tmp_path, _serve_archive(archive))

    with pytest.raises(InvalidArchiveError):
        await manager.ensure_binary(consent=True)
    assert not manager.binary_path.exists()


@pytest.mark.usefixtures("linux_platform", "fake_version_cmd")
async def test_failed_validation_leaves_no_binary(tmp_path: Path) -> None:
    """A fresh install whose binary fails --version validation leaves nothing behind."""
    archive = _build_archive(
        tmp_path / "a.tar.gz", {"soloist": _elf_binary("x86_64", marker=b"BROKEN")}
    )
    manager, _ = _make_manager(tmp_path, _serve_archive(archive))

    with pytest.raises(InvalidArchiveError):
        await manager.ensure_binary(consent=True)
    assert not manager.binary_path.exists()


@pytest.mark.usefixtures("linux_platform", "fake_version_cmd")
async def test_fresh_download_of_expired_build(tmp_path: Path) -> None:
    """A freshly downloaded build that reports exit code 10 raises BuildExpiredError."""
    archive = _build_archive(
        tmp_path / "a.tar.gz", {"soloist": _elf_binary("x86_64", marker=b"EXPIRED")}
    )
    manager, _ = _make_manager(tmp_path, _serve_archive(archive))

    with pytest.raises(BuildExpiredError):
        await manager.ensure_binary(consent=True)
    assert not manager.binary_path.exists()


@pytest.mark.usefixtures("linux_platform", "fake_version_cmd")
async def test_rollback_restores_previous_binary(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A failed replacement is rolled back to the previously installed binary."""
    good = _build_archive(
        tmp_path / "good.tar.gz", {"soloist": _elf_binary("x86_64", marker=b"GOOD-BUILD-A")}
    )
    broken = _build_archive(
        tmp_path / "broken.tar.gz", {"soloist": _elf_binary("x86_64", marker=b"BROKEN")}
    )
    manager, session = _make_manager(tmp_path, _serve_archive(good, etag='"v1"'))
    await manager.ensure_binary(consent=True)
    _age_metadata(tmp_path)
    session.handler = _serve_archive(broken, etag='"v2"')

    with caplog.at_level(logging.WARNING):
        path = await manager.ensure_fresh(consent=True)

    assert path == manager.binary_path
    assert b"GOOD-BUILD-A" in path.read_bytes()
    assert not (_install_dir(tmp_path) / "soloist.prev").exists()
    assert manager.diagnostics()["etag"] == '"v1"'
    assert "keeping the current binary" in caplog.text


@pytest.mark.usefixtures("linux_platform", "fake_version_cmd")
async def test_refresh_installs_new_build(tmp_path: Path) -> None:
    """An aged install is replaced when the CDN serves a different build."""
    build_a = _build_archive(
        tmp_path / "a.tar.gz", {"soloist": _elf_binary("x86_64", marker=b"GOOD-BUILD-A")}
    )
    build_b = _build_archive(
        tmp_path / "b.tar.gz", {"soloist": _elf_binary("x86_64", marker=b"GOOD-BUILD-B")}
    )
    manager, session = _make_manager(tmp_path, _serve_archive(build_a, etag='"v1"'))
    await manager.ensure_binary(consent=True)
    _age_metadata(tmp_path)
    session.handler = _serve_archive(build_b, etag='"v2"')

    path = await manager.ensure_fresh(consent=True)

    assert b"GOOD-BUILD-B" in path.read_bytes()
    diag = manager.diagnostics()
    assert diag["etag"] == '"v2"'
    assert diag["sha256"] == hashlib.sha256(build_b).hexdigest()


@pytest.mark.usefixtures("linux_platform", "fake_version_cmd")
async def test_refresh_skipped_when_etag_unchanged(tmp_path: Path) -> None:
    """An aged install is kept when the CDN still serves the same build."""
    archive = _build_archive(tmp_path / "a.tar.gz", {"soloist": _elf_binary("x86_64")})
    manager, session = _make_manager(tmp_path, _serve_archive(archive, etag='"v1"'))
    await manager.ensure_binary(consent=True)
    _age_metadata(tmp_path)
    session.requests.clear()

    await manager.ensure_fresh(consent=True)

    assert session.requests == [("HEAD", _CDN_URL.format(arch="x86_64"))]


@pytest.mark.usefixtures("linux_platform", "fake_version_cmd")
async def test_offline_refresh_returns_valid_binary(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """When offline, a still-valid installed binary is returned with a warning."""
    archive = _build_archive(tmp_path / "a.tar.gz", {"soloist": _elf_binary("x86_64")})
    manager, session = _make_manager(tmp_path, _serve_archive(archive))
    await manager.ensure_binary(consent=True)
    _age_metadata(tmp_path)
    session.handler = _offline

    with caplog.at_level(logging.WARNING):
        path = await manager.ensure_fresh(consent=True)

    assert path == manager.binary_path
    assert "Unable to check for a soloist update" in caplog.text


@pytest.mark.usefixtures("linux_platform", "fake_version_cmd")
async def test_offline_with_expired_binary_raises(tmp_path: Path) -> None:
    """When offline and the installed build already expired, BuildExpiredError is raised."""
    install_dir = _install_dir(tmp_path)
    install_dir.mkdir(parents=True)
    (install_dir / "soloist").write_bytes(_elf_binary("x86_64", marker=b"EXPIRED"))
    manager, _ = _make_manager(tmp_path, _offline)

    with pytest.raises(BuildExpiredError):
        await manager.ensure_fresh(consent=True)


@pytest.mark.usefixtures("linux_platform", "fake_version_cmd")
async def test_recent_verification_shared_across_managers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Back-to-back ensure_fresh calls run the --version/CDN verification only once."""
    archive = _build_archive(tmp_path / "a.tar.gz", {"soloist": _elf_binary("x86_64")})
    manager1, session1 = _make_manager(tmp_path, _serve_archive(archive))
    await manager1.ensure_binary(consent=True)
    _age_metadata(tmp_path)  # old enough that a full verification also HEADs the CDN
    version_calls: list[str] = []

    async def _counting_check_output(*args: str, **kwargs: Any) -> tuple[int, bytes]:
        version_calls.append(args[0])
        return await _fake_check_output(*args, **kwargs)

    monkeypatch.setattr(soloist, "check_output", _counting_check_output)
    session1.requests.clear()
    manager2, session2 = _make_manager(tmp_path, _serve_archive(archive))

    path1 = await manager1.ensure_fresh(consent=True)
    path2 = await manager2.ensure_fresh(consent=True)

    assert path1 == path2 == manager1.binary_path
    # the second manager reuses the just-completed verification entirely
    assert len(version_calls) == 1
    assert session1.requests == [("HEAD", _CDN_URL.format(arch="x86_64"))]
    assert session2.requests == []


@pytest.mark.usefixtures("linux_platform", "fake_version_cmd")
async def test_concurrent_callers_share_one_download(tmp_path: Path) -> None:
    """Concurrent ensure_binary callers trigger exactly one download."""
    archive = _build_archive(tmp_path / "a.tar.gz", {"soloist": _elf_binary("x86_64")})
    manager, session = _make_manager(tmp_path, _serve_archive(archive))

    paths = await asyncio.gather(*(manager.ensure_binary(consent=True) for _ in range(5)))

    assert all(path == manager.binary_path for path in paths)
    assert [req for req in session.requests if req[0] == "GET"] == [
        ("GET", _CDN_URL.format(arch="x86_64"))
    ]


@pytest.mark.usefixtures("linux_platform", "fake_version_cmd")
async def test_diagnostics_contains_no_secrets(tmp_path: Path) -> None:
    """Diagnostics exposes install/build metadata only, never any key material."""
    archive = _build_archive(tmp_path / "a.tar.gz", {"soloist": _elf_binary("x86_64")})
    manager, _ = _make_manager(tmp_path, _serve_archive(archive, etag='"v1"'))
    assert manager.diagnostics() == {"installed": False}

    await manager.ensure_binary(consent=True)
    diag = manager.diagnostics()

    assert set(diag) == {
        "installed",
        "sha256",
        "etag",
        "version",
        "version_raw",
        "installed_at",
        "build_timestamp",
        "expires_at",
    }
    assert diag["installed"] is True
    assert diag["sha256"] == hashlib.sha256(archive).hexdigest()
    assert diag["etag"] == '"v1"'
    assert diag["version"] == "1.2.3"
    assert diag["expires_at"] == pytest.approx(diag["build_timestamp"] + 90 * 24 * 3600)


class _FakeWebSocket:
    """Fake events WebSocket: an async iterator fed from a queue, recording sent frames."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[WSMessage | None] = asyncio.Queue()
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self.closed = True

    def __aiter__(self) -> _FakeWebSocket:
        return self

    async def __anext__(self) -> WSMessage:
        msg = await self.queue.get()
        if msg is None:
            raise StopAsyncIteration
        return msg

    async def send_json(self, data: dict[str, Any]) -> None:
        """Record an outgoing JSON frame."""
        self.sent.append(data)

    def exception(self) -> BaseException | None:
        """Return the connection error (never set for this fake)."""
        return None


def _make_client(data_dir: Path, ws: _FakeWebSocket) -> SoloistClient:
    """Create a client for the given data dir whose session connects to the fake ws."""
    mass = SimpleNamespace(http_session=SimpleNamespace(ws_connect=lambda *_a, **_kw: ws))
    return SoloistClient(cast("MusicAssistant", mass), data_dir, logging.getLogger("test.soloist"))


def _publish_endpoint(data_dir: Path, addr: str = "127.0.0.1", port: str = "8765") -> None:
    """Write the ws.addr/ws.port endpoint files like the daemon does."""
    (data_dir / "ws.addr").write_text(f"{addr}\n", encoding="utf-8")
    (data_dir / "ws.port").write_text(f"{port}\n", encoding="utf-8")


def _text_msg(payload: dict[str, Any]) -> WSMessage:
    """Wrap an event payload in a WebSocket TEXT message."""
    return WSMessage(WSMsgType.TEXT, json.dumps(payload), None)


async def test_endpoint_discovery_polls_until_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Endpoint discovery keeps polling until both files exist and parse."""
    monkeypatch.setattr(soloist, "_ENDPOINT_POLL_INTERVAL", 0.01)
    client = _make_client(tmp_path, _FakeWebSocket())
    task = asyncio.create_task(client.wait_until_ready(timeout=5.0))

    await asyncio.sleep(0.05)
    assert not task.done()
    (tmp_path / "ws.addr").write_text("127.0.0.1\n", encoding="utf-8")
    (tmp_path / "ws.port").write_text("not-a-port\n", encoding="utf-8")
    await asyncio.sleep(0.05)
    assert not task.done()  # unparsable port: keep polling
    (tmp_path / "ws.port").write_text("8765\n", encoding="utf-8")

    assert await task is True


async def test_endpoint_discovery_times_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Endpoint discovery returns False when the files never appear."""
    monkeypatch.setattr(soloist, "_ENDPOINT_POLL_INTERVAL", 0.01)
    client = _make_client(tmp_path, _FakeWebSocket())

    assert await client.wait_until_ready(timeout=0.05) is False


async def test_event_dispatch_decodes_documented_payloads(tmp_path: Path) -> None:
    """Documented event payloads are decoded into their typed models."""
    _publish_endpoint(tmp_path)
    ws = _FakeWebSocket()
    client = _make_client(tmp_path, ws)
    playback_state = {
        "type": "playback_state",
        "status": "playing",
        "item": {
            "uri": "spotify:track:2JRo0gjbX4GrCqBYdRohoo",
            "entity_type": "track",
            "decorations": {
                "identity": {"name": "My Song"},
                "playback": {"duration_ms": 210000, "content_ratings": []},
            },
        },
        "context": {
            "uri": "spotify:playlist:37i9dQZF1DXcBWIGoYBM5M",
            "entity_type": "playlist",
            "decorations": {"identity": {"name": "Today's Top Hits"}},
        },
        "position": {"position_ms": 45000, "timestamp_ms": 1747654321000, "speed": 1.0},
        "volume": 65,
        "is_active": True,
        "options": {"shuffle": False, "repeat": "off", "playback_speed": 1.0, "modes": {}},
        "available_actions": {"pause": {}, "seek_forward": {"step_ms": 15000}},
    }
    for payload in (
        {"type": "auth_state", "logged_in": True, "is_active": True, "device_name": "Kitchen"},
        playback_state,
        {"type": "volume_changed", "volume": 42},
        {
            "type": "position_sync",
            "position": {"position_ms": 45000, "timestamp_ms": 1747654321000, "speed": 1.0},
        },
        {
            "type": "queue_changed",
            "previous": [],
            "upcoming": [
                {
                    "uid": "spotify:track:upcoming",
                    "source": "queue",
                    "item": {"uri": "spotify:track:upcoming", "entity_type": "track"},
                }
            ],
        },
    ):
        ws.queue.put_nowait(_text_msg(payload))
    ws.queue.put_nowait(None)
    events: list[SoloistEvent] = []

    async def on_event(event: SoloistEvent) -> None:
        events.append(event)

    await client.listen_events(on_event)

    assert [event.type for event in events] == [
        "auth_state",
        "playback_state",
        "volume_changed",
        "position_sync",
        "queue_changed",
    ]
    auth = events[0].data
    assert isinstance(auth, SoloistAuthState)
    assert auth.logged_in is True
    assert auth.device_name == "Kitchen"
    state = events[1].data
    assert isinstance(state, SoloistPlaybackState)
    assert state.status == "playing"
    assert state.item is not None
    assert state.item.uri == "spotify:track:2JRo0gjbX4GrCqBYdRohoo"
    assert state.item.decorations["identity"]["name"] == "My Song"
    assert state.options is not None
    assert state.options.repeat == "off"
    assert state.position is not None
    assert state.position.position_ms == 45000
    assert state.available_actions["seek_forward"]["step_ms"] == 15000
    volume = events[2].data
    assert isinstance(volume, SoloistVolumeChanged)
    assert volume.volume == 42
    sync = events[3].data
    assert isinstance(sync, SoloistPositionSync)
    assert sync.position.timestamp_ms == 1747654321000
    queue = events[4].data
    assert isinstance(queue, SoloistQueueChanged)
    assert queue.upcoming[0].source == "queue"
    assert queue.upcoming[0].item is not None
    assert queue.upcoming[0].item.uri == "spotify:track:upcoming"


async def test_event_dispatch_tolerates_malformed_and_unknown(tmp_path: Path) -> None:
    """Malformed frames are skipped, unknown event types pass through as raw events."""
    _publish_endpoint(tmp_path)
    ws = _FakeWebSocket()
    client = _make_client(tmp_path, ws)
    ws.queue.put_nowait(WSMessage(WSMsgType.TEXT, "not json", None))
    ws.queue.put_nowait(WSMessage(WSMsgType.TEXT, '["a", "list"]', None))
    ws.queue.put_nowait(_text_msg({"volume": 1}))  # no type field
    ws.queue.put_nowait(_text_msg({"type": "volume_changed"}))  # missing required field
    ws.queue.put_nowait(_text_msg({"type": "mystery_event", "foo": "bar"}))
    ws.queue.put_nowait(_text_msg({"type": "volume_changed", "volume": 7}))
    ws.queue.put_nowait(None)
    events: list[SoloistEvent] = []

    async def on_event(event: SoloistEvent) -> None:
        events.append(event)

    await client.listen_events(on_event)

    assert len(events) == 2
    assert events[0].type == "mystery_event"
    assert events[0].data is None
    assert events[0].raw == {"type": "mystery_event", "foo": "bar"}
    volume = events[1].data
    assert isinstance(volume, SoloistVolumeChanged)
    assert volume.volume == 7


async def _wait_connected(client: SoloistClient) -> None:
    """Wait until the client's events WebSocket is connected."""
    async with asyncio.timeout(5.0):
        while not client.connected:
            await asyncio.sleep(0.01)


async def test_commands_have_documented_shape(tmp_path: Path) -> None:
    """Command senders produce the documented wire frames (with clamped values)."""
    _publish_endpoint(tmp_path)
    ws = _FakeWebSocket()
    client = _make_client(tmp_path, ws)

    async def on_event(_event: SoloistEvent) -> None:
        return

    listen_task = asyncio.create_task(client.listen_events(on_event))
    await _wait_connected(client)

    await client.play("spotify:playlist:37i9dQZF1DXcBWIGoYBM5M")
    await client.resume()
    await client.pause()
    await client.skip_next()
    await client.seek(-100)
    await client.set_volume(150)
    await client.set_shuffle(True)
    await client.set_repeat_context(True)
    await client.add_to_queue("spotify:track:6rqhFgbbKwnb9MLmUQDhG6")
    await client.get_queue(5)
    ws.queue.put_nowait(None)
    await listen_task

    assert ws.sent == [
        {"type": "command", "command": "play", "uri": "spotify:playlist:37i9dQZF1DXcBWIGoYBM5M"},
        {"type": "command", "command": "play"},
        {"type": "command", "command": "pause"},
        {"type": "command", "command": "skip_next"},
        {"type": "command", "command": "seek", "position_ms": 0},
        {"type": "command", "command": "set_volume", "volume": 100},
        {"type": "command", "command": "set_shuffle", "enabled": True},
        {"type": "command", "command": "set_repeat_context", "enabled": True},
        {
            "type": "command",
            "command": "add_to_queue",
            "uri": "spotify:track:6rqhFgbbKwnb9MLmUQDhG6",
        },
        {"type": "command", "command": "get_queue", "limit": 5},
    ]


async def test_command_awaits_result(tmp_path: Path) -> None:
    """A command sent with await_result resolves once its command_result arrives."""
    _publish_endpoint(tmp_path)
    ws = _FakeWebSocket()
    client = _make_client(tmp_path, ws)
    events: list[SoloistEvent] = []

    async def on_event(event: SoloistEvent) -> None:
        events.append(event)

    listen_task = asyncio.create_task(client.listen_events(on_event))
    await _wait_connected(client)

    command_task = asyncio.create_task(client.activate(await_result=True))
    await asyncio.sleep(0)
    assert not command_task.done()
    ws.queue.put_nowait(_text_msg({"type": "command_result", "command": "activate"}))
    await asyncio.wait_for(command_task, timeout=1.0)
    ws.queue.put_nowait(None)
    await listen_task

    # the ack is also still dispatched to the event callback
    assert [event.type for event in events] == ["command_result"]


async def test_commands_require_connection(tmp_path: Path) -> None:
    """Sending a command without a connected WebSocket raises SoloistError."""
    client = _make_client(tmp_path, _FakeWebSocket())

    with pytest.raises(SoloistError, match="not connected"):
        await client.pause()


@pytest.mark.usefixtures("linux_platform", "fake_version_cmd")
async def test_expired_by_timestamp_is_replaced(tmp_path: Path) -> None:
    """An install past the 90-day build expiry is replaced even though it still runs."""
    build_a = _build_archive(
        tmp_path / "a.tar.gz", {"soloist": _elf_binary("x86_64", marker=b"GOOD-BUILD-A")}
    )
    build_b = _build_archive(
        tmp_path / "b.tar.gz", {"soloist": _elf_binary("x86_64", marker=b"GOOD-BUILD-B")}
    )
    manager, session = _make_manager(tmp_path, _serve_archive(build_a, etag='"v1"'))
    await manager.ensure_binary(consent=True)
    _age_metadata(tmp_path, days=100.0)
    session.handler = _serve_archive(build_b, etag='"v2"')

    path = await manager.ensure_fresh(consent=True)

    assert b"GOOD-BUILD-B" in path.read_bytes()


def test_parse_build_timestamp_reads_the_real_epoch_format() -> None:
    """The observed 1.3.7 --version output carries the build date as a unix epoch."""
    raw = "soloist 1.3.7.345 build 1787077868 (20260818) (gb24005ef46) (linux/aarch64)"
    assert soloist._parse_build_timestamp(raw) == 1787077868.0
    # an unrelated small number is not mistaken for a timestamp
    assert soloist._parse_build_timestamp("soloist 1.2.3 build 42") is None
