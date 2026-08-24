"""Tests for the local cliairplay download helper."""

from __future__ import annotations

import hashlib
import stat
from pathlib import Path

import pytest

from scripts import fetch_airplay_cli


def test_fetch_airplay_cli_installs_verified_platform_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Install the matching binary after verifying both release checksums.

    The Dockerfile remains the single source for the development and container pins.
    """
    binary = b"cliairplay test binary"
    asset_name = "cliairplay-macos-arm64"
    manifest = f"{hashlib.sha256(binary).hexdigest()}  {asset_name}\n".encode()
    _write_dockerfile(tmp_path, manifest)
    downloads: list[str] = []

    def download(url: str) -> bytes:
        downloads.append(url)
        return manifest if url.endswith("/SHA256SUMS") else binary

    monkeypatch.setattr("scripts.fetch_airplay_cli.platform.system", lambda: "Darwin")
    monkeypatch.setattr("scripts.fetch_airplay_cli.platform.machine", lambda: "arm64")
    monkeypatch.setattr(fetch_airplay_cli, "_download", download)

    result = fetch_airplay_cli.fetch_airplay_cli(tmp_path)

    expected = tmp_path / fetch_airplay_cli.BINARY_DIR / asset_name
    assert result == expected
    assert expected.read_bytes() == binary
    assert expected.stat().st_mode & stat.S_IXUSR
    assert downloads == [
        f"{fetch_airplay_cli.RELEASE_BASE_URL}/v0.1.0/SHA256SUMS",
        f"{fetch_airplay_cli.RELEASE_BASE_URL}/v0.1.0/{asset_name}",
    ]


def test_fetch_airplay_cli_preserves_existing_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Preserve an existing local binary without contacting the release service.

    Developers may intentionally replace the pinned executable with a local build.
    """
    destination = tmp_path / fetch_airplay_cli.BINARY_DIR / "cliairplay-linux-x86_64"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"local build")
    _write_dockerfile(tmp_path, b"unused manifest")
    monkeypatch.setattr("scripts.fetch_airplay_cli.platform.system", lambda: "Linux")
    monkeypatch.setattr("scripts.fetch_airplay_cli.platform.machine", lambda: "x86_64")
    monkeypatch.setattr(fetch_airplay_cli, "_read_binary_version", lambda _path: None)
    monkeypatch.setattr(
        fetch_airplay_cli,
        "_download",
        lambda _url: pytest.fail("existing binaries must not trigger a download"),
    )

    assert fetch_airplay_cli.fetch_airplay_cli(tmp_path) == destination
    assert destination.read_bytes() == b"local build"


def test_fetch_airplay_cli_replaces_older_release_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replace a recognized older release binary with the pinned release."""
    binary = b"new release"
    asset_name = "cliairplay-macos-arm64"
    manifest = f"{hashlib.sha256(binary).hexdigest()}  {asset_name}\n".encode()
    _write_dockerfile(tmp_path, manifest)
    destination = tmp_path / fetch_airplay_cli.BINARY_DIR / asset_name
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"old release")
    downloads: list[str] = []

    def download(url: str) -> bytes:
        downloads.append(url)
        return manifest if url.endswith("/SHA256SUMS") else binary

    monkeypatch.setattr("scripts.fetch_airplay_cli.platform.system", lambda: "Darwin")
    monkeypatch.setattr("scripts.fetch_airplay_cli.platform.machine", lambda: "arm64")
    monkeypatch.setattr(fetch_airplay_cli, "_read_binary_version", lambda _path: (0, 0, 9))
    monkeypatch.setattr(fetch_airplay_cli, "_download", download)

    assert fetch_airplay_cli.fetch_airplay_cli(tmp_path) == destination
    assert destination.read_bytes() == binary
    assert downloads == [
        f"{fetch_airplay_cli.RELEASE_BASE_URL}/v0.1.0/SHA256SUMS",
        f"{fetch_airplay_cli.RELEASE_BASE_URL}/v0.1.0/{asset_name}",
    ]


def test_fetch_airplay_cli_rejects_checksum_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Reject a binary that differs from the pinned release checksum.

    A failed verification must not leave an executable in the provider directory.
    """
    asset_name = "cliairplay-linux-aarch64"
    manifest = f"{hashlib.sha256(b'expected').hexdigest()}  {asset_name}\n".encode()
    _write_dockerfile(tmp_path, manifest)
    monkeypatch.setattr("scripts.fetch_airplay_cli.platform.system", lambda: "Linux")
    monkeypatch.setattr("scripts.fetch_airplay_cli.platform.machine", lambda: "arm64")
    monkeypatch.setattr(
        fetch_airplay_cli,
        "_download",
        lambda url: manifest if url.endswith("/SHA256SUMS") else b"unexpected",
    )

    with pytest.raises(RuntimeError, match="Checksum mismatch"):
        fetch_airplay_cli.fetch_airplay_cli(tmp_path)

    assert not (tmp_path / fetch_airplay_cli.BINARY_DIR / asset_name).exists()


def test_fetch_airplay_cli_rejects_manifest_digest_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Reject a checksum manifest that differs from the Dockerfile pin.

    A modified manifest must fail before the executable asset is downloaded.
    """
    asset_name = "cliairplay-linux-x86_64"
    manifest = f"{hashlib.sha256(b'binary').hexdigest()}  {asset_name}\n".encode()
    _write_dockerfile(tmp_path, manifest, manifest_digest="0" * 64)
    monkeypatch.setattr("scripts.fetch_airplay_cli.platform.system", lambda: "Linux")
    monkeypatch.setattr("scripts.fetch_airplay_cli.platform.machine", lambda: "x86_64")
    downloads: list[str] = []

    def download(url: str) -> bytes:
        downloads.append(url)
        return manifest

    monkeypatch.setattr(fetch_airplay_cli, "_download", download)

    with pytest.raises(RuntimeError, match="SHA256SUMS digest mismatch"):
        fetch_airplay_cli.fetch_airplay_cli(tmp_path)

    assert downloads == [f"{fetch_airplay_cli.RELEASE_BASE_URL}/v0.1.0/SHA256SUMS"]


def test_read_docker_arg_reports_missing_file(tmp_path: Path) -> None:
    """Report an unreadable Dockerfile as a clean setup error."""
    dockerfile = tmp_path / "Dockerfile"

    with pytest.raises(RuntimeError, match=f"Unable to read {dockerfile}"):
        fetch_airplay_cli._read_docker_arg(dockerfile, "CLIAIRPLAY_VERSION")


def test_main_reports_filesystem_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Return a clean failure instead of a traceback for filesystem errors."""
    monkeypatch.setattr(
        fetch_airplay_cli,
        "fetch_airplay_cli",
        lambda _root: (_ for _ in ()).throw(PermissionError("read-only filesystem")),
    )

    assert fetch_airplay_cli.main() == 1
    assert capsys.readouterr().err == "ERROR: read-only filesystem\n"


@pytest.mark.parametrize(
    ("system", "machine", "expected"),
    [
        ("Darwin", "aarch64", "cliairplay-macos-arm64"),
        ("Darwin", "x86_64", "cliairplay-macos-x86_64"),
        ("Linux", "arm64", "cliairplay-linux-aarch64"),
        ("Linux", "amd64", "cliairplay-linux-x86_64"),
        ("Windows", "AMD64", None),
        ("Linux", "riscv64", None),
    ],
)
def test_asset_name(system: str, machine: str, expected: str | None) -> None:
    """Map supported development platforms to their release asset."""
    assert fetch_airplay_cli._asset_name(system, machine) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("v0.3.0", (0, 3, 0)),
        ("1.2.3", (1, 2, 3)),
        ("0.3", None),
        ("development", None),
    ],
)
def test_parse_release_version(value: str, expected: tuple[int, int, int] | None) -> None:
    """Parse supported release versions without treating local labels as releases."""
    assert fetch_airplay_cli._parse_release_version(value) == expected


@pytest.mark.parametrize(
    ("current", "pinned", "expected"),
    [
        ((0, 2, 0), "v0.3.0", True),
        ((0, 3, 0), "v0.3.0", False),
        ((0, 3, 1), "v0.3.0", False),
        (None, "v0.3.0", False),
    ],
)
def test_binary_is_outdated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    current: tuple[int, int, int] | None,
    pinned: str,
    expected: bool,
) -> None:
    """Only older recognized releases are replaced."""
    binary_path = tmp_path / "cliairplay"
    monkeypatch.setattr(fetch_airplay_cli, "_read_binary_version", lambda _path: current)

    assert fetch_airplay_cli._binary_is_outdated(binary_path, pinned) is expected


def _write_dockerfile(root: Path, manifest: bytes, *, manifest_digest: str | None = None) -> None:
    """Write the release pins consumed by the helper."""
    if manifest_digest is None:
        manifest_digest = hashlib.sha256(manifest).hexdigest()
    root.joinpath("Dockerfile").write_text(
        f"ARG CLIAIRPLAY_VERSION=v0.1.0\nARG CLIAIRPLAY_CHECKSUMS_SHA256={manifest_digest}\n",
        encoding="utf-8",
    )
