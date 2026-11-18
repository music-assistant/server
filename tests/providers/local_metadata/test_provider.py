"""Tests for the local metadata provider."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import ImageType
from music_assistant_models.errors import ProviderUnavailableError
from music_assistant_models.media_items import Artist

from music_assistant.constants import CONF_LOG_LEVEL, CONF_PATH
from music_assistant.providers.local_metadata import (
    SUBFOLDER_LIST_MAX_AGE,
    LocalMetadataProvider,
    artist_folder_name,
)

MBID = "53b106e7-0cc6-42cc-ac95-ed8d30a3a98e"
OTHER_MBID = "8f3a7d1c-1111-4222-8333-944444444444"
JPEG = b"\xff\xd8\xff\xe0jpeg"


async def _provider(root: Path) -> LocalMetadataProvider:
    """
    Return a local metadata provider reading the given folder.

    :param root: The artist information folder.
    """
    mass = MagicMock()
    mass.metadata.preferred_language = "en"
    mass.storage.is_available = AsyncMock(side_effect=lambda path: Path(path).is_dir())
    # the folder is collected by the setup flow
    mass.config.get = MagicMock(return_value={CONF_PATH: str(root)})
    mass.config.decrypt_string = lambda value: value
    manifest = MagicMock()
    manifest.domain = "local_metadata"
    config = MagicMock()
    config.instance_id = "local_metadata"
    values: dict[str, Any] = {CONF_LOG_LEVEL: "GLOBAL"}
    config.get_value = MagicMock(side_effect=values.get)
    provider = LocalMetadataProvider(mass, manifest, config, set())
    await provider.handle_async_init()
    return provider


def _artist(name: str, mbid: str | None = None) -> Artist:
    """Return a library artist."""
    artist = Artist(
        item_id="1",
        provider="library",
        name=name,
        provider_mappings=set(),
    )
    if mbid:
        artist.mbid = mbid
    return artist


def _mtime(path: Path) -> int:
    """Return the modification time of a file in nanoseconds."""
    return path.stat().st_mtime_ns


def _nfo(folder: Path, mbid: str | None = None, bio: str = "A bio") -> None:
    """Write an artist.nfo into an artist folder."""
    folder.mkdir(parents=True, exist_ok=True)
    mbid_tag = f"<musicbrainzartistid>{mbid}</musicbrainzartistid>" if mbid else ""
    (folder / "artist.nfo").write_text(
        f"<artist><name>{folder.name}</name>{mbid_tag}"
        f"<biography>{bio}</biography><genre>Rock</genre><genre>Pop</genre></artist>"
    )


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Radiohead", "Radiohead"),
        ("AC/DC", "AC_DC"),
        ('What? "Now": <a|b>*', "What_ _Now__ _a_b__"),
        ("Simon / Garfunkel", "Simon_Garfunkel"),
        ("M.I.A.", "M.I.A"),
        ("..", ""),
    ],
)
def test_artist_folder_name_follows_kodi(name: str, expected: str) -> None:
    """Reserved characters become underscores and a spaced underscore is collapsed."""
    assert artist_folder_name(name) == expected


async def test_reads_nfo_and_artwork_from_a_folder_matched_ignoring_case(tmp_path: Path) -> None:
    """The bio, genres and artwork of the artist's folder are returned, whatever its case."""
    folder = tmp_path / "the beatles"
    _nfo(folder, bio="Fab four")
    for file_name in ("folder.jpg", "Fanart.PNG", "clearlogo.png", "banner.gif", "notes.txt"):
        (folder / file_name).write_bytes(JPEG)
    provider = await _provider(tmp_path)

    metadata = await provider.get_artist_metadata(_artist("The Beatles"))

    assert metadata is not None
    assert metadata.description == "Fab four"
    assert metadata.description_language == "en"
    assert metadata.genres == {"Rock", "Pop"}
    assert [(img.type, img.path) for img in metadata.images or []] == [
        (ImageType.THUMB, f"the beatles/folder.jpg?v={_mtime(folder / 'folder.jpg')}"),
        (ImageType.FANART, f"the beatles/Fanart.PNG?v={_mtime(folder / 'Fanart.PNG')}"),
        (ImageType.LOGO, f"the beatles/clearlogo.png?v={_mtime(folder / 'clearlogo.png')}"),
        (ImageType.BANNER, f"the beatles/banner.gif?v={_mtime(folder / 'banner.gif')}"),
    ]
    assert all(
        img.provider == "local_metadata" and not img.remotely_accessible
        for img in metadata.images or []
    )


async def test_artist_without_a_folder_gets_nothing(tmp_path: Path) -> None:
    """An artist without a folder gets no metadata."""
    provider = await _provider(tmp_path)

    assert await provider.get_artist_metadata(_artist("Nobody")) is None


async def test_folder_with_the_mbid_suffix_is_preferred(tmp_path: Path) -> None:
    """The folder named after the artist and its mbid is used before the plain name folder."""
    _nfo(tmp_path / "John Williams", bio="Guitarist")
    _nfo(tmp_path / "John Williams_53b1", bio="Composer")
    provider = await _provider(tmp_path)

    metadata = await provider.get_artist_metadata(_artist("John Williams", MBID))

    assert metadata is not None
    assert metadata.description == "Composer"


async def test_folder_of_a_namesake_is_skipped(tmp_path: Path) -> None:
    """A folder whose nfo names another mbid belongs to a namesake and is skipped."""
    _nfo(tmp_path / "John Williams", mbid=OTHER_MBID)
    provider = await _provider(tmp_path)

    assert await provider.get_artist_metadata(_artist("John Williams", MBID)) is None


async def test_artist_without_mbid_matches_by_name(tmp_path: Path) -> None:
    """An artist without mbid matches on name alone, whatever mbid its nfo holds."""
    _nfo(tmp_path / "AC_DC", mbid=OTHER_MBID, bio="Rock band")
    provider = await _provider(tmp_path)

    metadata = await provider.get_artist_metadata(_artist("AC/DC"))

    assert metadata is not None
    assert metadata.description == "Rock band"


async def test_resolve_image_stays_inside_the_folder(tmp_path: Path) -> None:
    """Artwork is read from inside the folder only."""
    (tmp_path / "root" / "Radiohead").mkdir(parents=True)
    (tmp_path / "root" / "Radiohead" / "folder.jpg").write_bytes(JPEG)
    (tmp_path / "secret.jpg").write_bytes(b"secret")
    provider = await _provider(tmp_path / "root")

    assert await provider.resolve_image("Radiohead/folder.jpg") == JPEG
    assert await provider.resolve_image("Radiohead/missing.jpg") is None
    assert await provider.resolve_image("../secret.jpg") is None
    assert await provider.resolve_image(str(tmp_path / "secret.jpg")) is None
    assert await provider.resolve_image("Radiohead") is None


async def test_artwork_of_a_folder_name_starting_with_dots_is_read(tmp_path: Path) -> None:
    """An artist folder whose name starts with dots serves its artwork."""
    name = "...And You Will Know Us by the Trail of Dead"
    (tmp_path / name).mkdir()
    (tmp_path / name / "folder.jpg").write_bytes(JPEG)
    provider = await _provider(tmp_path)

    metadata = await provider.get_artist_metadata(_artist(name))

    assert metadata is not None
    assert metadata.images
    assert await provider.resolve_image(metadata.images[0].path) == JPEG


async def test_artwork_linked_from_outside_the_folder_is_neither_listed_nor_read(
    tmp_path: Path,
) -> None:
    """A symlink in an artist folder that leads outside the folder is ignored."""
    root = tmp_path / "root"
    _nfo(root / "Radiohead")
    (root / "Radiohead" / "fanart.jpg").write_bytes(JPEG)
    (tmp_path / "secret.jpg").write_bytes(b"secret")
    (root / "Radiohead" / "folder.jpg").symlink_to(tmp_path / "secret.jpg")
    provider = await _provider(root)

    metadata = await provider.get_artist_metadata(_artist("Radiohead"))

    assert metadata is not None
    assert [img.type for img in metadata.images or []] == [ImageType.FANART]
    assert await provider.resolve_image("Radiohead/folder.jpg") is None


@pytest.mark.parametrize("swapped", ["file", "folder"])
async def test_artwork_swapped_for_a_link_leading_outside_is_not_read(
    tmp_path: Path, swapped: str
) -> None:
    """
    Artwork listed inside the folder is not read once it, or its folder, leads outside.

    The swap happens right before the file is opened, after every check on its path.

    :param swapped: Whether the artwork file or its artist folder becomes a symlink.
    """
    root = tmp_path / "root"
    folder = root / "Radiohead"
    _nfo(folder)
    (folder / "folder.jpg").write_bytes(JPEG)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "folder.jpg").write_bytes(b"secret")
    provider = await _provider(root)
    metadata = await provider.get_artist_metadata(_artist("Radiohead"))
    assert metadata is not None
    image_path = (metadata.images or [])[0].path
    real_open = os.open

    def _swap_then_open(path: str, flags: int) -> int:
        if swapped == "file":
            (folder / "folder.jpg").unlink()
            (folder / "folder.jpg").symlink_to(outside / "folder.jpg")
        else:
            folder.rename(tmp_path / "moved")
            folder.symlink_to(outside, target_is_directory=True)
        return real_open(path, flags)

    with patch("music_assistant.providers.local_metadata.os.open", _swap_then_open):
        assert await provider.resolve_image(image_path) is None


async def test_replaced_artwork_gets_a_new_image_path(tmp_path: Path) -> None:
    """The image path carries the file's version, which reading the image ignores."""
    folder = tmp_path / "Radiohead"
    _nfo(folder)
    (folder / "folder.jpg").write_bytes(JPEG)
    provider = await _provider(tmp_path)
    metadata = await provider.get_artist_metadata(_artist("Radiohead"))
    assert metadata is not None
    old_path = (metadata.images or [])[0].path

    (folder / "folder.jpg").write_bytes(b"new")
    stat = (folder / "folder.jpg").stat()
    os.utime(folder / "folder.jpg", ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    metadata = await provider.get_artist_metadata(_artist("Radiohead"))
    assert metadata is not None
    new_path = (metadata.images or [])[0].path

    assert new_path != old_path
    assert new_path.startswith("Radiohead/folder.jpg?v=")
    assert await provider.resolve_image(new_path) == b"new"


async def test_an_unavailable_folder_is_reported_as_temporarily_unavailable(
    tmp_path: Path,
) -> None:
    """A folder on storage that is not there, or that can not be listed, raises."""
    _nfo(tmp_path / "Radiohead")
    provider = await _provider(tmp_path)
    storage = cast("MagicMock", provider.mass.storage)
    storage.is_available = AsyncMock(return_value=False)

    with pytest.raises(ProviderUnavailableError):
        await provider.get_artist_metadata(_artist("Radiohead"))

    storage.is_available = AsyncMock(return_value=True)
    with (
        patch(
            "music_assistant.providers.local_metadata.os.scandir",
            side_effect=PermissionError("denied"),
        ),
        pytest.raises(ProviderUnavailableError),
    ):
        await provider.get_artist_metadata(_artist("Muse"))


async def test_artist_without_a_folder_name_gets_nothing(tmp_path: Path) -> None:
    """An artist whose name leaves no folder name never reaches outside the folder."""
    root = tmp_path / "root"
    _nfo(root, bio="Root bio")
    provider = await _provider(root)

    assert await provider.get_artist_metadata(_artist("..")) is None


async def test_folder_list_is_reused_for_names_that_differ_in_case(tmp_path: Path) -> None:
    """A folder whose name only differs in case is found through the folder list, read once."""
    _nfo(tmp_path / "RadioHead")
    provider = await _provider(tmp_path)
    scandir = MagicMock(wraps=os.scandir)

    with patch("music_assistant.providers.local_metadata.os.scandir", scandir):
        assert await provider.get_artist_metadata(_artist("Radiohead")) is not None
        assert await provider.get_artist_metadata(_artist("Muse")) is None
        root_listings = [call for call in scandir.call_args_list if call.args[0] == str(tmp_path)]

    assert len(root_listings) == 1


async def test_new_folder_is_found_when_the_root_mtime_is_stale(tmp_path: Path) -> None:
    """A folder added or renamed is found even when the root still reports its old mtime."""
    _nfo(tmp_path / "Radiohead")
    provider = await _provider(tmp_path)
    assert await provider.get_artist_metadata(_artist("Muse")) is None

    # a network share can keep reporting the old mtime of the root
    stat = tmp_path.stat()
    (tmp_path / "Radiohead").rename(tmp_path / "Muse")
    os.utime(tmp_path, ns=(stat.st_atime_ns, stat.st_mtime_ns))

    metadata = await provider.get_artist_metadata(_artist("Muse"))
    assert metadata is not None
    assert metadata.description == "A bio"
    assert await provider.get_artist_metadata(_artist("Radiohead")) is None


async def test_folder_list_is_read_again_once_it_is_too_old(tmp_path: Path) -> None:
    """A new folder that differs in case is found once the folder list expires, mtime or not."""
    _nfo(tmp_path / "Radiohead")
    provider = await _provider(tmp_path)
    clock = MagicMock()
    clock.monotonic.return_value = 1000.0

    with patch("music_assistant.providers.local_metadata.time", clock):
        assert await provider.get_artist_metadata(_artist("Muse")) is None
        # a network share can keep reporting the old mtime of the root
        stat = tmp_path.stat()
        _nfo(tmp_path / "MUSE")
        os.utime(tmp_path, ns=(stat.st_atime_ns, stat.st_mtime_ns))

        clock.monotonic.return_value = 1000.0 + SUBFOLDER_LIST_MAX_AGE - 1
        assert await provider.get_artist_metadata(_artist("Muse")) is None

        clock.monotonic.return_value = 1000.0 + SUBFOLDER_LIST_MAX_AGE
        metadata = await provider.get_artist_metadata(_artist("Muse"))

    assert metadata is not None
    assert metadata.description == "A bio"


async def test_unreadable_artist_folder_is_reported_as_temporarily_unavailable(
    tmp_path: Path,
) -> None:
    """An artist folder that exists but can not be read raises instead of reporting nothing."""
    _nfo(tmp_path / "Radiohead")
    provider = await _provider(tmp_path)
    folder = str(tmp_path / "Radiohead")
    real_scandir = os.scandir

    def _scandir(path: str) -> Any:
        if path == folder:
            raise PermissionError("denied")
        return real_scandir(path)

    with (
        patch("music_assistant.providers.local_metadata.os.scandir", side_effect=_scandir),
        pytest.raises(ProviderUnavailableError),
    ):
        await provider.get_artist_metadata(_artist("Radiohead"))


async def test_artist_folder_removed_while_looking_it_up_gets_nothing(tmp_path: Path) -> None:
    """An artist folder that disappears while it is looked up is treated as not there."""
    _nfo(tmp_path / "Radiohead")
    provider = await _provider(tmp_path)

    with patch(
        "music_assistant.providers.local_metadata.os.scandir",
        side_effect=FileNotFoundError("gone"),
    ):
        assert await provider.get_artist_metadata(_artist("Radiohead")) is None


async def test_malformed_nfo_still_gives_the_artwork(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A malformed artist.nfo is skipped with a warning, the artwork of the folder is kept."""
    folder = tmp_path / "Radiohead"
    folder.mkdir()
    (folder / "artist.nfo").write_text("not xml at all <<<")
    (folder / "folder.jpg").write_bytes(JPEG)
    provider = await _provider(tmp_path)

    metadata = await provider.get_artist_metadata(_artist("Radiohead"))

    assert metadata is not None
    assert metadata.description is None
    assert [image.type for image in metadata.images or []] == [ImageType.THUMB]
    assert "Failed to parse artist NFO file Radiohead/artist.nfo" in caplog.text
