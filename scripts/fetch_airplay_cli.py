"""Download the pinned cliairplay binary for local development."""

from __future__ import annotations

import hashlib
import platform
import re
import stat
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import URLError
from urllib.request import Request, urlopen

# ruff: noqa: T201

RELEASE_BASE_URL = "https://github.com/music-assistant/airplay-cli/releases/download"
BINARY_DIR = Path("music_assistant/providers/airplay/bin")


def fetch_airplay_cli(root: Path) -> Path | None:
    """
    Download the container-pinned cliairplay binary when it is not present.

    :param root: Music Assistant server repository root.
    :return: Local binary path, or None when the current platform is unsupported.
    """
    system = platform.system()
    machine = platform.machine()
    asset_name = _asset_name(system, machine)
    if asset_name is None:
        print(
            f"No cliairplay release is available for {system.lower()}/{machine.lower()}; skipping."
        )
        return None

    destination = root / BINARY_DIR / asset_name
    if destination.exists():
        if not destination.is_file():
            msg = f"cliairplay destination is not a file: {destination}"
            raise RuntimeError(msg)
        print(f"Using existing AirPlay development binary: {destination}")
        return destination

    dockerfile = root / "Dockerfile"
    version = _read_docker_arg(dockerfile, "CLIAIRPLAY_VERSION")
    manifest_digest = _read_docker_arg(dockerfile, "CLIAIRPLAY_CHECKSUMS_SHA256")
    release_url = f"{RELEASE_BASE_URL}/{version}"

    manifest = _download(f"{release_url}/SHA256SUMS")
    actual_manifest_digest = hashlib.sha256(manifest).hexdigest()
    if actual_manifest_digest != manifest_digest:
        msg = (
            f"SHA256SUMS digest mismatch for cliairplay {version}: "
            f"expected {manifest_digest}, got {actual_manifest_digest}"
        )
        raise RuntimeError(msg)
    binary_digest = _binary_digest(manifest, asset_name)

    binary = _download(f"{release_url}/{asset_name}")
    actual_binary_digest = hashlib.sha256(binary).hexdigest()
    if actual_binary_digest != binary_digest:
        msg = (
            f"Checksum mismatch for {asset_name}: "
            f"expected {binary_digest}, got {actual_binary_digest}"
        )
        raise RuntimeError(msg)

    destination.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".cliairplay-", dir=destination.parent) as temp_dir:
        temp_binary = Path(temp_dir) / asset_name
        temp_binary.write_bytes(binary)
        temp_binary.chmod(temp_binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        temp_binary.replace(destination)

    print(f"Installed AirPlay development binary: {destination}")
    return destination


def main() -> int:
    """Download the local AirPlay development binary."""
    root = Path(__file__).resolve().parent.parent
    try:
        fetch_airplay_cli(root)
    except RuntimeError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 1
    return 0


def _asset_name(system: str, machine: str) -> str | None:
    """Return the release asset name for a platform."""
    normalized_system = system.lower().replace("darwin", "macos")
    normalized_machine = machine.lower()

    if normalized_machine in ("amd64", "x86_64"):
        architecture = "x86_64"
    elif normalized_machine in ("aarch64", "arm64"):
        architecture = "arm64" if normalized_system == "macos" else "aarch64"
    else:
        return None
    if normalized_system not in ("linux", "macos"):
        return None
    return f"cliairplay-{normalized_system}-{architecture}"


def _read_docker_arg(dockerfile: Path, name: str) -> str:
    """Read a pinned build argument from the Dockerfile."""
    match = re.search(
        rf"^ARG\s+{re.escape(name)}=(\S+)\s*$",
        dockerfile.read_text(encoding="utf-8"),
        flags=re.MULTILINE,
    )
    if match is None:
        msg = f"Unable to find {name} in {dockerfile}"
        raise RuntimeError(msg)
    return match.group(1)


def _binary_digest(manifest: bytes, asset_name: str) -> str:
    """Return the single checksum for an asset in SHA256SUMS."""
    try:
        lines = manifest.decode("utf-8").splitlines()
    except UnicodeDecodeError as err:
        msg = "Unable to decode cliairplay SHA256SUMS"
        raise RuntimeError(msg) from err

    matches = []
    for line in lines:
        fields = line.split()
        if len(fields) == 2 and fields[1].lstrip("*") == asset_name:
            matches.append(fields[0].lower())
    if len(matches) != 1 or re.fullmatch(r"[0-9a-f]{64}", matches[0]) is None:
        msg = f"SHA256SUMS must contain exactly one valid checksum for {asset_name}"
        raise RuntimeError(msg)
    return matches[0]


def _download(url: str) -> bytes:
    """Download a release file."""
    request = Request(  # noqa: S310
        url,
        headers={"User-Agent": "music-assistant-dev-setup"},
    )
    try:
        with urlopen(request, timeout=60) as response:  # noqa: S310
            return response.read()
    except URLError as err:
        msg = f"Unable to download {url}: {err}"
        raise RuntimeError(msg) from err


if __name__ == "__main__":
    sys.exit(main())
