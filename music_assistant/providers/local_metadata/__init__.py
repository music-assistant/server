"""
Local metadata provider for Music Assistant.

Reads artist metadata from a Kodi-style artist information folder: one subfolder per artist
holding an ``artist.nfo`` and artwork files. Optionally saves missing artwork into it.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType, ImageType, ProviderFeature
from music_assistant_models.errors import MediaNotFoundError, ProviderUnavailableError
from music_assistant_models.helpers import create_safe_string
from music_assistant_models.media_items import Artist, MediaItemImage, MediaItemMetadata

from music_assistant.constants import CONF_PATH, DB_TABLE_ARTISTS
from music_assistant.helpers.images import detect_image_content_format, get_image_data
from music_assistant.helpers.nfo import parse_artist_nfo, parse_nfo_root
from music_assistant.helpers.security import is_safe_path
from music_assistant.models.metadata_provider import MetadataProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES = {ProviderFeature.ARTIST_METADATA}

CONF_SAVE_ARTWORK = "save_artwork"

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
# the file name Kodi uses for each artwork type that is saved
SAVE_STEMS: dict[ImageType, str] = {
    ImageType.THUMB: "folder",
    ImageType.FANART: "fanart",
    ImageType.LOGO: "clearlogo",
    ImageType.BANNER: "banner",
    ImageType.CLEARART: "clearart",
    ImageType.LANDSCAPE: "landscape",
}
# characters Windows does not allow in a file name, replaced the way Kodi does
RESERVED_CHARS = str.maketrans(dict.fromkeys('<>:"/\\|?*', "_"))


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
    # subfolders of the root by casefolded name, refreshed when the root's mtime changes
    _subfolders: dict[str, str]
    _root_mtime: int | None = None
    _read_only_warned: bool = False

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to configure this provider."""
        return (
            ConfigEntry(key=CONF_SAVE_ARTWORK, type=ConfigEntryType.BOOLEAN, default_value=False),
        )

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
        self._subfolders = {}

    async def get_artist_metadata(self, artist: Artist) -> MediaItemMetadata | None:
        """Retrieve metadata for an artist from its folder in the artist information folder."""
        if not (match := await self._find_artist_folder(artist)):
            return None
        folder, nfo_artist, files = match
        metadata = MediaItemMetadata()
        if nfo_artist:
            metadata.description = nfo_artist.metadata.description
            # an nfo has no language, so its bio counts as written in the user's language
            metadata.description_language = self.mass.metadata.preferred_language
            metadata.genres = nfo_artist.metadata.genres
        for file_name, image_type in _artwork_files(files):
            metadata.add_image(self._local_image(image_type, f"{folder}/{file_name}"))
        return metadata

    async def save_artist_metadata(self, artist: Artist) -> None:
        """
        Save artwork types the artist's folder lacks, downloaded from the artist's online images.

        Existing files are never overwritten. Each saved file is added to the artist's images.

        :param artist: The library artist with its merged metadata.
        """
        images = artist.metadata.images
        if not self.config.get_value(CONF_SAVE_ARTWORK) or not images:
            return
        if not await self.mass.storage.is_available(self._root):
            return
        if not await asyncio.to_thread(os.access, self._root, os.W_OK):
            if not self._read_only_warned:
                self._read_only_warned = True
                self.logger.warning("Unable to save artwork, folder %s is read-only", self._root)
            return
        try:
            target = await self._save_folder(artist)
        except OSError as err:
            self.logger.warning("Unable to read the folder of artist %s: %s", artist.name, err)
            return
        if not target:
            return
        folder, files = target
        present = {image_type for _, image_type in _artwork_files(files)}
        for image_type, stem in SAVE_STEMS.items():
            if image_type in present:
                continue
            remote = next(
                (img for img in images if img.type == image_type and img.remotely_accessible),
                None,
            )
            if remote is None:
                continue
            try:
                file_name = await self._save_image(folder, stem, remote)
            except (OSError, MediaNotFoundError, ProviderUnavailableError) as err:
                self.logger.warning(
                    "Unable to save %s artwork of artist %s: %s", image_type, artist.name, err
                )
                continue
            if file_name:
                artist.metadata.add_image(self._local_image(image_type, f"{folder}/{file_name}"))

    async def resolve_image(self, path: str) -> bytes | None:
        """
        Return the content of an artwork file in the artist information folder.

        :param path: The file path, relative to the artist information folder.
        """
        if not is_safe_path(path, self._root):
            return None
        return await asyncio.to_thread(_read_file, os.path.join(self._root, path))

    async def _find_artist_folder(
        self, artist: Artist
    ) -> tuple[str, Artist | None, list[str]] | None:
        """Return the artist's folder name, its parsed artist.nfo and its file names."""
        if not (name := artist_folder_name(artist.name)):
            return None
        candidates = [f"{name}_{artist.mbid[:4]}", name] if artist.mbid else [name]
        for candidate in candidates:
            if (folder := await self._find_subfolder(candidate)) is None:
                continue
            nfo_data, files = await asyncio.to_thread(
                _read_folder, os.path.join(self._root, folder)
            )
            nfo_artist = self._parse_nfo(artist, f"{folder}/{NFO_FILENAME}", nfo_data)
            nfo_mbid = nfo_artist.mbid if nfo_artist else None
            if nfo_mbid and artist.mbid:
                if nfo_mbid == artist.mbid:
                    return folder, nfo_artist, files
                # the folder of a namesake
                continue
            # a plain name folder is ambiguous when the library holds a namesake
            if candidate != name or not await self._has_namesake(artist):
                return folder, nfo_artist, files
        return None

    async def _save_folder(self, artist: Artist) -> tuple[str, list[str]] | None:
        """Return the folder name to save the artist's artwork in and its file names."""
        if match := await self._find_artist_folder(artist):
            return match[0], match[2]
        if not (name := artist_folder_name(artist.name)):
            return None
        if await self._has_namesake(artist):
            if not artist.mbid:
                return None
            name = f"{name}_{artist.mbid[:4]}"
        # an existing folder that did not match belongs to a namesake
        if await self._find_subfolder(name) is not None:
            return None
        return name, []

    async def _find_subfolder(self, name: str) -> str | None:
        """Return the actual name of a subfolder of the root, matched ignoring case."""
        mtime, subfolders = await asyncio.to_thread(_list_subfolders, self._root, self._root_mtime)
        if subfolders is not None:
            self._root_mtime, self._subfolders = mtime, subfolders
        return self._subfolders.get(name.casefold())

    async def _save_image(self, folder: str, stem: str, image: MediaItemImage) -> str | None:
        """Download an image into the artist's folder and return its file name."""
        data = await get_image_data(self.mass, image.path, image.provider)
        ext = detect_image_content_format(data)
        if ext not in ("jpg", "png"):
            self.logger.debug("Not saving image %s of unsupported format %s", image.path, ext)
            return None
        file_name = f"{stem}.{ext}"
        await asyncio.to_thread(_write_new_file, os.path.join(self._root, folder), file_name, data)
        return file_name

    async def _has_namesake(self, artist: Artist) -> bool:
        """Return True when another library artist has the same name, ignoring case."""
        rows = await self.mass.music.database.get_rows_from_query(
            f"SELECT name FROM {DB_TABLE_ARTISTS} WHERE search_name = :search_name",
            {"search_name": create_safe_string(artist.name, True, True)},
        )
        name = artist.name.casefold()
        return sum(str(row["name"]).casefold() == name for row in rows) > 1

    def _parse_nfo(self, artist: Artist, source: str, data: bytes | None) -> Artist | None:
        """Return the artist described by an artist.nfo, or None when absent or malformed."""
        if data is None:
            return None
        if (root := parse_nfo_root(data, "artist")) is None:
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


def _artwork_files(files: list[str]) -> list[tuple[str, ImageType]]:
    """Return the artwork files among the file names with their type, in order of preference."""
    by_stem: dict[str, str] = {}
    for file_name in sorted(files):
        stem, _, ext = file_name.rpartition(".")
        if stem and ext.lower() in IMAGE_EXTENSIONS:
            by_stem.setdefault(stem.casefold(), file_name)
    return [
        (by_stem[stem], image_type) for stem, image_type in IMAGE_STEMS.items() if stem in by_stem
    ]


def _list_subfolders(
    root: str, known_mtime: int | None
) -> tuple[int | None, dict[str, str] | None]:
    """Return the root's mtime and its subfolders by casefolded name, or None when unchanged."""
    try:
        mtime = Path(root).stat().st_mtime_ns
    except FileNotFoundError:
        return None, {}
    if mtime == known_mtime:
        return mtime, None
    with os.scandir(root) as entries:
        return mtime, {entry.name.casefold(): entry.name for entry in entries if entry.is_dir()}


def _read_folder(path: str) -> tuple[bytes | None, list[str]]:
    """Return the content of a folder's artist.nfo, if any, and the folder's file names."""
    with os.scandir(path) as entries:
        files = [entry.name for entry in entries if entry.is_file()]
    nfo_name = next((name for name in files if name.casefold() == NFO_FILENAME), None)
    nfo_data = _read_file(os.path.join(path, nfo_name)) if nfo_name else None
    return nfo_data, files


def _read_file(path: str) -> bytes | None:
    """Return the content of a file, or None when it does not exist."""
    try:
        with open(path, "rb") as file:
            return file.read()
    except FileNotFoundError:
        return None


def _write_new_file(folder: str, file_name: str, data: bytes) -> None:
    """Write a new file, creating its folder when needed; an existing file is never replaced."""
    Path(folder).mkdir(exist_ok=True)
    with open(os.path.join(folder, file_name), "xb") as file:
        file.write(data)
