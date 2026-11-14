"""
Local metadata provider for Music Assistant.

Reads artist metadata from a Kodi-style artist information folder: one subfolder per artist
holding an ``artist.nfo`` and artwork files.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import ImageType, ProviderFeature
from music_assistant_models.errors import ProviderUnavailableError
from music_assistant_models.media_items import Artist, MediaItemImage, MediaItemMetadata

from music_assistant.constants import CONF_PATH
from music_assistant.helpers.nfo import parse_artist_nfo, parse_nfo_root
from music_assistant.helpers.security import is_safe_path, real_path_of_fd
from music_assistant.models.metadata_provider import MetadataProvider

if TYPE_CHECKING:
    from collections.abc import Iterable

    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES = {ProviderFeature.ARTIST_METADATA}

NFO_FILENAME = "artist.nfo"
IMAGE_EXTENSIONS = ("jpg", "jpeg", "png", "gif")
# artwork file names (without extension) Kodi reads, in order of preference per type
IMAGE_STEMS: dict[str, ImageType] = {
    "folder": ImageType.THUMB,
    "thumb": ImageType.THUMB,
    "cover": ImageType.THUMB,
    "artist": ImageType.THUMB,
    "fanart": ImageType.FANART,
    "clearlogo": ImageType.LOGO,
    "logo": ImageType.LOGO,
    "banner": ImageType.BANNER,
    "clearart": ImageType.CLEARART,
    "landscape": ImageType.LANDSCAPE,
    "cutout": ImageType.CUTOUT,
    "back": ImageType.BACK,
    "discart": ImageType.DISCART,
}

# characters Windows does not allow in a file name, replaced the way Kodi does
RESERVED_CHARS = str.maketrans(dict.fromkeys('<>:"/\\|?*', "_"))
# seconds after which the folder list of the root is read again, also when its mtime is the same
SUBFOLDER_LIST_MAX_AGE = 60


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return LocalMetadataProvider(mass, manifest, config, SUPPORTED_FEATURES)


def artist_folder_name(name: str) -> str:
    """
    Return the folder name Kodi uses for an artist in its artist information folder.

    :param name: The artist name.
    """
    return name.translate(RESERVED_CHARS).rstrip(". ").replace(" _ ", "_")


class LocalMetadataProvider(MetadataProvider):
    """Metadata provider for a Kodi-style artist information folder."""

    _root: str
    # the root with its symlinks resolved at setup, which every read must stay inside
    _real_root: str
    # subfolders of the root by casefolded name, refreshed when the root's mtime changes or the
    # list is older than SUBFOLDER_LIST_MAX_AGE (a network share can report a stale mtime, so an
    # exact name is checked on disk first)
    _subfolders: dict[str, str]
    _root_mtime: int | None = None
    _listed_at: float = 0.0

    @property
    def priority(self) -> int:
        """Priority for this provider (lower = more preferred)."""
        return 5

    @property
    def is_local(self) -> bool:
        """Whether this provider reads metadata from local storage."""
        return True

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self._root = os.path.normpath(str(self.get_setup_value(CONF_PATH)))
        self._real_root = await asyncio.to_thread(os.path.realpath, self._root)
        self._subfolders = {}

    async def get_artist_metadata(self, artist: Artist) -> MediaItemMetadata | None:
        """
        Retrieve metadata for an artist from its folder in the artist information folder.

        :param artist: The library artist.
        :raises ProviderUnavailableError: When the artist information folder can not be read.
        """
        if not await self.mass.storage.is_available(self._root):
            msg = f"Artist information folder {self._root} is not available"
            raise ProviderUnavailableError(msg)
        if not (match := await self._find_artist_folder(artist)):
            return None
        folder, nfo_artist, files = match
        metadata = MediaItemMetadata()
        if nfo_artist:
            metadata.description = nfo_artist.metadata.description
            # an nfo has no language, so its bio counts as written in the user's language
            metadata.description_language = self.mass.metadata.preferred_language
            metadata.genres = nfo_artist.metadata.genres
            metadata.style = nfo_artist.metadata.style
            metadata.mood = nfo_artist.metadata.mood
            metadata.artist_entity_type = nfo_artist.metadata.artist_entity_type
            metadata.life_span = nfo_artist.metadata.life_span
        for file_name, image_type in _artwork_files(files):
            # the modification time in the path makes a replaced file a new image to the cache
            path = f"{folder}/{file_name}?v={files[file_name]}"
            metadata.add_image(self._local_image(image_type, path))
        return metadata

    async def resolve_image(self, path: str) -> bytes | None:
        """
        Return the content of an artwork file in the artist information folder.

        :param path: The file path, relative to the artist information folder.
        """
        # drop the version suffix appended to the image path
        path = path.split("?v=", 1)[0]
        # checked joined to the root, so a folder name starting with dots is not taken for ".."
        if not is_safe_path(os.path.join(self._root, path), self._root):
            return None
        return await asyncio.to_thread(_read_image, self._root, self._real_root, path)

    async def _find_artist_folder(
        self, artist: Artist
    ) -> tuple[str, Artist | None, dict[str, int]] | None:
        """Return the artist's folder name, its parsed artist.nfo and the mtime of its files."""
        if not (name := artist_folder_name(artist.name)):
            return None
        candidates = [f"{name}_{artist.mbid[:4]}", name] if artist.mbid else [name]
        for candidate in candidates:
            if (folder := await self._find_subfolder(candidate)) is None:
                continue
            try:
                nfo_name, nfo_root, files = await asyncio.to_thread(
                    _read_folder, self._root, self._real_root, folder
                )
            except FileNotFoundError, NotADirectoryError:
                self.logger.debug("Artist folder %s no longer exists", folder)
                # the folder list may be outdated, so it is read again on the next lookup
                self._root_mtime = None
                continue
            except OSError as err:
                msg = f"Artist folder {folder} can not be read: {err}"
                raise ProviderUnavailableError(msg) from err
            nfo_artist = (
                self._parse_nfo(artist, f"{folder}/{nfo_name}", nfo_root) if nfo_name else None
            )
            nfo_mbid = nfo_artist.mbid if nfo_artist else None
            # the folder of a namesake
            if nfo_mbid and artist.mbid and nfo_mbid != artist.mbid:
                self.logger.debug(
                    "Skipping folder %s for artist %s: its nfo MusicBrainz id %s does not match %s",
                    folder,
                    artist.name,
                    nfo_mbid,
                    artist.mbid,
                )
                continue
            return folder, nfo_artist, files
        return None

    async def _find_subfolder(self, name: str) -> str | None:
        """Return the actual name of a subfolder of the root, matched ignoring case."""
        if await asyncio.to_thread(os.path.isdir, os.path.join(self._root, name)):
            return name
        now = time.monotonic()
        known_mtime = self._root_mtime if now - self._listed_at < SUBFOLDER_LIST_MAX_AGE else None
        try:
            mtime, subfolders = await asyncio.to_thread(_list_subfolders, self._root, known_mtime)
        except OSError as err:
            msg = f"Artist information folder {self._root} can not be read: {err}"
            raise ProviderUnavailableError(msg) from err
        if subfolders is not None:
            self._root_mtime, self._subfolders, self._listed_at = mtime, subfolders, now
        return self._subfolders.get(name.casefold())

    def _parse_nfo(self, artist: Artist, source: str, root: dict[str, Any] | None) -> Artist | None:
        """Return the artist described by a parsed artist.nfo, or None when it was malformed."""
        if root is None:
            self.logger.warning("Failed to parse artist NFO file %s", source)
            return None
        nfo_artist = Artist(
            item_id=artist.item_id,
            provider=self.instance_id,
            name=artist.name,
            provider_mappings=set(),
        )
        parse_artist_nfo(nfo_artist, root, source)
        return nfo_artist

    def _local_image(self, image_type: ImageType, path: str) -> MediaItemImage:
        """Return the image entry of an artwork file in the artist information folder."""
        return MediaItemImage(
            type=image_type, path=path, provider=self.instance_id, remotely_accessible=False
        )


def _artwork_files(files: Iterable[str]) -> list[tuple[str, ImageType]]:
    """Return the artwork files among the file names with their type, in order of preference."""
    by_stem: dict[str, str] = {}
    for file_name in sorted(files):
        stem, _, ext = file_name.rpartition(".")
        if stem and ext.lower() in IMAGE_EXTENSIONS:
            by_stem.setdefault(stem.casefold(), file_name)
    return [
        (by_stem[stem], image_type) for stem, image_type in IMAGE_STEMS.items() if stem in by_stem
    ]


def _list_subfolders(root: str, known_mtime: int | None) -> tuple[int, dict[str, str] | None]:
    """Return the root's mtime and its subfolders by casefolded name, or None when unchanged."""
    mtime = Path(root).stat().st_mtime_ns
    if mtime == known_mtime:
        return mtime, None
    with os.scandir(root) as entries:
        return mtime, {entry.name.casefold(): entry.name for entry in entries if entry.is_dir()}


def _read_folder(
    root: str, real_root: str, folder: str
) -> tuple[str | None, dict[str, Any] | None, dict[str, int]]:
    """Return a subfolder's artist.nfo name and parsed root, if any, and the mtime of its files."""
    path = os.path.join(root, folder)
    with os.scandir(path) as entries:
        files = {
            entry.name: entry.stat().st_mtime_ns
            for entry in entries
            if entry.is_file() and _is_inside(entry.path, real_root)
        }
    nfo_name = next((name for name in files if name.casefold() == NFO_FILENAME), None)
    if nfo_name and (nfo_data := _read_file(real_root, os.path.join(path, nfo_name))) is not None:
        return nfo_name, parse_nfo_root(nfo_data, "artist"), files
    return None, None, files


def _read_image(root: str, real_root: str, path: str) -> bytes | None:
    """Return the content of a file of the root, or None when it is not a readable file in it."""
    try:
        return _read_file(real_root, os.path.join(root, path))
    except PermissionError, IsADirectoryError:
        return None


def _read_file(real_root: str, path: str) -> bytes | None:
    """Return the content of a file, or None when it does not exist or lies outside the real root."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return None
    # checked on the opened file, so a path re-pointed after the check is not read
    with os.fdopen(fd, "rb") as file:
        if not is_safe_path(real_path_of_fd(fd), real_root):
            return None
        return file.read()


def _is_inside(path: str, real_root: str) -> bool:
    """Return whether the real path of a file lies inside the real root (blocking)."""
    return is_safe_path(os.path.realpath(path), real_root)
