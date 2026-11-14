"""The code of installed community providers, downloaded exactly as the community catalog lists it."""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import stat
import tarfile
import zlib
from contextlib import suppress
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Final, NoReturn

import aiofiles
from aiohttp import ClientError, ClientResponseError, ClientTimeout
from music_assistant_models.errors import MusicAssistantError
from music_assistant_models.provider import ProviderManifest

if TYPE_CHECKING:
    import logging

    from music_assistant.controllers.community_catalog.models import IndexProvider, IndexVersion
    from music_assistant.mass import MusicAssistant

ARCHIVE_URL: Final = "https://codeload.github.com/{repository}/tar.gz/{commit}"
# a stalled connection fails, a large repository on a slow line still downloads
_TIMEOUT: Final = ClientTimeout(total=None, sock_connect=30, sock_read=60)
_CHUNK_SIZE: Final = 64 * 1024
_STAGING: Final = ".staging"
_PREVIOUS: Final = ".previous"


class InstallError(MusicAssistantError):
    """A version of a community provider could not be installed."""


def content_hash(folder: Path) -> str:
    """
    Return the content hash of a provider folder, as the community catalog lists it (blocking).

    Only the paths and the content of the files count: not their modes or times, not empty
    folders and not compiled Python files.

    :param folder: The provider folder.
    :raises OSError: The folder or a file in it cannot be read.
    """
    lines: list[tuple[bytes, bytes]] = []
    for root, dirs, files in os.walk(folder, onerror=_raise):
        dirs[:] = [name for name in dirs if name != "__pycache__"]
        for name in files:
            path = Path(root, name)
            if name.endswith(".pyc") or not stat.S_ISREG(path.lstat().st_mode):
                continue
            relative = path.relative_to(folder).as_posix().encode()
            with path.open("rb") as file:
                digest = hashlib.file_digest(file, "sha256").hexdigest()
            lines.append((relative, f"{digest}  ".encode() + relative + b"\n"))
    return hashlib.sha256(b"".join(line for _, line in sorted(lines))).hexdigest()


class Installer:
    """The live, previous and staged versions of the installed community providers."""

    def __init__(self, mass: MusicAssistant, folder: str, logger: logging.Logger) -> None:
        """
        Initialize the installer; nothing is touched until a method is called.

        :param mass: The Music Assistant instance.
        :param folder: The folder that holds the live provider folders.
        :param logger: The logger to report on.
        """
        self.mass = mass
        self.logger = logger
        self._folder = Path(folder)

    async def stage(self, domain: str, entry: IndexProvider, version: str) -> ProviderManifest:
        """
        Download a version of a provider next to the live one and check it is what the catalog lists.

        A version staged before is replaced. On failure nothing stays staged; the live and
        previous versions are never touched. Returns the manifest of the staged version.

        :param domain: The domain of the provider.
        :param entry: The provider in the index.
        :param version: The version to stage, one of the versions of the entry.
        :raises InstallError: The version cannot be downloaded or is not what the catalog lists.
        """
        if (release := entry.versions.get(version)) is None:
            msg = f"Version {version} of {domain} is not in the community catalog"
            raise InstallError(msg)
        staged = self._folders(domain)[2]
        try:
            return await self._stage(domain, version, entry.repository, release)
        except Exception:
            with suppress(OSError):
                await asyncio.to_thread(_discard, staged)
            raise

    async def activate(self, domain: str) -> None:
        """
        Make the staged version of a provider live, and keep the live one as the previous version.

        :param domain: The domain of the provider, staged before.
        :raises InstallError: There is no staged version, or the folders cannot be renamed.
        """
        try:
            await asyncio.to_thread(self._activate, domain)
        except OSError as err:
            msg = f"Could not activate {domain}: {err}"
            raise InstallError(msg) from err

    async def discard(self, domain: str) -> None:
        """
        Remove the staged version of a provider.

        :param domain: The domain of the provider.
        :raises InstallError: The staged version cannot be removed.
        """
        try:
            await asyncio.to_thread(_discard, self._folders(domain)[2])
        except OSError as err:
            msg = f"Could not remove the staged version of {domain}: {err}"
            raise InstallError(msg) from err

    async def remove(self, domain: str) -> None:
        """
        Remove every version of a provider: the live, the previous and the staged one.

        :param domain: The domain of the provider.
        :raises InstallError: A version cannot be removed.
        """
        try:
            await asyncio.to_thread(self._remove, domain)
        except OSError as err:
            msg = f"Could not remove {domain}: {err}"
            raise InstallError(msg) from err

    async def _stage(
        self, domain: str, version: str, repository: str, release: IndexVersion
    ) -> ProviderManifest:
        """
        Download, unpack and check a version of a provider; may leave a partly staged version.

        :param domain: The domain of the provider.
        :param version: The version to stage.
        :param repository: The repository of the provider on GitHub, as owner/name.
        :param release: The version in the index.
        """
        name = f"{domain} {version}"
        staged = self._folders(domain)[2]
        archive = _archive(staged)
        url = ARCHIVE_URL.format(repository=repository, commit=release.commit)
        try:
            # replaces the leftover of an interrupted stage
            await asyncio.to_thread(_discard, staged)
            await asyncio.to_thread(staged.parent.mkdir, parents=True, exist_ok=True)
            await self._download(url, archive)
            await asyncio.to_thread(_unpack, archive, release.folder, staged)
            return await asyncio.to_thread(_verify, staged, domain, release.content_hash)
        except TimeoutError as err:
            msg = f"Could not download {name}: GitHub did not answer in time"
            raise InstallError(msg) from err
        except ClientResponseError as err:
            msg = f"Could not download {name}: GitHub answered {err.status} {err.message}"
            raise InstallError(msg) from err
        except ClientError as err:
            msg = f"Could not download {name}: {err}"
            raise InstallError(msg) from err
        except ValueError as err:
            msg = f"Refusing {name}: {err}"
            raise InstallError(msg) from err
        except (tarfile.TarError, EOFError, zlib.error) as err:
            msg = f"Refusing {name}: the download is not a valid archive: {err}"
            raise InstallError(msg) from err
        except OSError as err:
            msg = f"Could not install {name}: {err}"
            raise InstallError(msg) from err
        finally:
            await asyncio.to_thread(archive.unlink, missing_ok=True)

    async def _download(self, url: str, path: Path) -> None:
        """
        Stream a download to a file.

        :param url: The address to download.
        :param path: The file to write.
        """
        self.logger.debug("Downloading %s", url)
        async with self.mass.http_session.get(url, timeout=_TIMEOUT) as response:
            response.raise_for_status()
            async with aiofiles.open(path, "wb") as file:
                async for chunk in response.content.iter_chunked(_CHUNK_SIZE):
                    await file.write(chunk)

    def _activate(self, domain: str) -> None:
        """Rename the staged version to the live one and the live one to the previous (blocking)."""
        live, previous, staged = self._folders(domain)
        if not staged.is_dir():
            msg = f"{domain} has no staged version"
            raise InstallError(msg)
        _delete(previous)
        previous.parent.mkdir(exist_ok=True)
        if live.exists():
            live.rename(previous)
        staged.rename(live)

    def _remove(self, domain: str) -> None:
        """Delete every version of a provider (blocking)."""
        live, previous, staged = self._folders(domain)
        _delete(live)
        _delete(previous)
        _discard(staged)

    def _folders(self, domain: str) -> tuple[Path, Path, Path]:
        """
        Return the live, previous and staged folder of a provider.

        :param domain: The domain of the provider.
        :raises InstallError: The domain is not a provider domain, so it could name another folder.
        """
        if not domain.isidentifier():
            msg = f"{domain!r} is not a provider domain"
            raise InstallError(msg)
        return (
            self._folder / domain,
            self._folder / _PREVIOUS / domain,
            self._folder / _STAGING / domain,
        )


def _unpack(archive: Path, folder: str, target: Path) -> None:
    """
    Write what lies below a folder of a GitHub archive to a new folder (blocking).

    :param archive: The archive, with the repository in one top-level folder.
    :param folder: The folder in the repository to take.
    :param target: The folder to write to, which must not exist.
    :raises ValueError: A member leaves the repository, the folder holds anything but regular
        files and folders, or the folder is not in the archive.
    """
    prefix = PurePosixPath(folder).parts
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar:
            name = PurePosixPath(member.name)
            parts = name.parts
            if name.is_absolute() or ".." in parts:
                msg = f"{member.name} lies outside the repository"
                raise ValueError(msg)
            # the first part is the top-level folder GitHub names after the repository and commit
            if parts[1 : len(prefix) + 1] != prefix:
                continue
            relative = parts[len(prefix) + 1 :]
            # compiled files are no part of the content hash, so they are not installed either
            if "__pycache__" in relative or name.suffix == ".pyc":
                continue
            path = target.joinpath(*relative)
            if member.isdir():
                path.mkdir(parents=True, exist_ok=True)
            elif member.isfile() and (source := tar.extractfile(member)) is not None:
                path.parent.mkdir(parents=True, exist_ok=True)
                with source, path.open("xb") as file:
                    shutil.copyfileobj(source, file)
                # of the mode only whether the file is executable is kept
                path.chmod(0o755 if member.mode & 0o111 else 0o644)
            else:
                msg = f"{PurePosixPath(*parts[1:])} is not a regular file or folder"
                raise ValueError(msg)
    if not target.is_dir():
        msg = f"the repository has no folder {folder}"
        raise ValueError(msg)


def _verify(folder: Path, domain: str, expected_hash: str) -> ProviderManifest:
    """
    Return the manifest of a staged provider, after checking it is what the catalog lists (blocking).

    :param folder: The staged provider folder.
    :param domain: The domain the provider must have.
    :param expected_hash: The content hash the catalog lists.
    :raises ValueError: The content differs from what the catalog lists, or the manifest is
        missing, invalid or of another domain.
    """
    if content_hash(folder) != expected_hash:
        msg = "its content is not what the community catalog lists"
        raise ValueError(msg)
    try:
        manifest = ProviderManifest.from_json(
            (folder / "manifest.json").read_text(encoding="utf-8")
        )
    except FileNotFoundError as err:
        msg = "it has no manifest.json"
        raise ValueError(msg) from err
    except (ValueError, LookupError) as err:
        msg = f"its manifest.json is invalid: {err}"
        raise ValueError(msg) from err
    if manifest.domain != domain:
        msg = f"its manifest is for {manifest.domain}"
        raise ValueError(msg)
    return manifest


def _archive(staged: Path) -> Path:
    """Return the file a staged version is downloaded to."""
    return staged.with_name(f"{staged.name}.tar.gz")


def _discard(staged: Path) -> None:
    """Delete a staged version and its download (blocking)."""
    _delete(staged)
    _archive(staged).unlink(missing_ok=True)


def _delete(folder: Path) -> None:
    """Delete a folder with everything in it, when it exists (blocking)."""
    with suppress(FileNotFoundError):
        shutil.rmtree(folder)


def _raise(err: OSError) -> NoReturn:
    """Raise an error of os.walk, which would skip what it cannot read."""
    raise err
