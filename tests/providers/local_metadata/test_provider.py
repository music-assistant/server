"""Tests for the local metadata provider."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import ImageType
from music_assistant_models.media_items import Artist, MediaItemImage
from music_assistant_models.media_items.metadata import MediaItemMetadata
from music_assistant_models.unique_list import UniqueList

from music_assistant.constants import CONF_LOG_LEVEL, CONF_PATH
from music_assistant.providers.local_metadata import (
    CONF_SAVE_ARTWORK,
    LocalMetadataProvider,
    artist_folder_name,
)

MBID = "53b106e7-0cc6-42cc-ac95-ed8d30a3a98e"
OTHER_MBID = "8f3a7d1c-1111-4222-8333-944444444444"
JPEG = b"\xff\xd8\xff\xe0jpeg"
PNG = b"\x89PNG\r\n\x1a\npng"
_GET_IMAGE_DATA = "music_assistant.providers.local_metadata.get_image_data"


async def _provider(
    root: Path, save: bool = False, library_names: list[str] | None = None
) -> LocalMetadataProvider:
    """
    Return a local metadata provider reading the given folder.

    :param root: The artist information folder.
    :param save: Whether saving artwork is enabled.
    :param library_names: Names of the library artists that share the tested artist's name.
    """
    mass = MagicMock()
    mass.metadata.preferred_language = "en"
    mass.storage.is_available = AsyncMock(side_effect=lambda path: Path(path).is_dir())
    mass.music.database.get_rows_from_query = AsyncMock(
        return_value=[{"name": name} for name in library_names or []]
    )
    # the folder is collected by the setup flow
    mass.config.get = MagicMock(return_value={CONF_PATH: str(root)})
    mass.config.decrypt_string = lambda value: value
    manifest = MagicMock()
    manifest.domain = "local_metadata"
    config = MagicMock()
    config.instance_id = "local_metadata"
    values: dict[str, Any] = {CONF_LOG_LEVEL: "GLOBAL", CONF_SAVE_ARTWORK: save}
    config.get_value = MagicMock(side_effect=values.get)
    provider = LocalMetadataProvider(mass, manifest, config, set())
    await provider.handle_async_init()
    return provider


def _artist(
    name: str, mbid: str | None = None, images: list[MediaItemImage] | None = None
) -> Artist:
    """Return a library artist."""
    artist = Artist(
        item_id="1",
        provider="library",
        name=name,
        provider_mappings=set(),
        metadata=MediaItemMetadata(images=UniqueList(images) if images else None),
    )
    if mbid:
        artist.mbid = mbid
    return artist


def _nfo(folder: Path, mbid: str | None = None, bio: str = "A bio") -> None:
    """Write an artist.nfo into an artist folder."""
    folder.mkdir(parents=True, exist_ok=True)
    mbid_tag = f"<musicbrainzartistid>{mbid}</musicbrainzartistid>" if mbid else ""
    (folder / "artist.nfo").write_text(
        f"<artist><name>{folder.name}</name>{mbid_tag}"
        f"<biography>{bio}</biography><genre>Rock</genre><genre>Pop</genre></artist>"
    )


def _remote(image_type: ImageType, url: str) -> MediaItemImage:
    """Return an online image."""
    return MediaItemImage(type=image_type, path=url, provider="fanarttv", remotely_accessible=True)


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
        (ImageType.THUMB, "the beatles/folder.jpg"),
        (ImageType.FANART, "the beatles/Fanart.PNG"),
        (ImageType.LOGO, "the beatles/clearlogo.png"),
        (ImageType.BANNER, "the beatles/banner.gif"),
    ]
    assert all(
        img.provider == "local_metadata" and not img.remotely_accessible
        for img in metadata.images or []
    )


async def test_artist_without_a_folder_gets_nothing(tmp_path: Path) -> None:
    """An artist without a folder gets no metadata."""
    provider = await _provider(tmp_path)

    assert await provider.get_artist_metadata(_artist("Nobody")) is None


async def test_namesakes_use_the_folder_with_the_mbid_suffix(tmp_path: Path) -> None:
    """With a namesake in the library the folder named after the artist's mbid is used."""
    _nfo(tmp_path / "John Williams", bio="Guitarist")
    _nfo(tmp_path / "John Williams_53b1", bio="Composer")
    provider = await _provider(tmp_path, library_names=["John Williams", "John Williams"])

    metadata = await provider.get_artist_metadata(_artist("John Williams", MBID))

    assert metadata is not None
    assert metadata.description == "Composer"


async def test_namesakes_use_a_plain_folder_only_when_its_mbid_matches(tmp_path: Path) -> None:
    """With a namesake in the library a plain name folder is used only on a matching nfo mbid."""
    _nfo(tmp_path / "John Williams", bio="Guitarist")
    provider = await _provider(tmp_path, library_names=["John Williams", "john williams"])

    assert await provider.get_artist_metadata(_artist("John Williams", MBID)) is None
    assert await provider.get_artist_metadata(_artist("John Williams")) is None

    _nfo(tmp_path / "John Williams", mbid=MBID, bio="Composer")
    metadata = await provider.get_artist_metadata(_artist("John Williams", MBID))
    assert metadata is not None
    assert metadata.description == "Composer"


async def test_folder_of_a_namesake_is_skipped(tmp_path: Path) -> None:
    """A folder whose nfo names another mbid belongs to a namesake and is skipped."""
    _nfo(tmp_path / "John Williams", mbid=OTHER_MBID)
    provider = await _provider(tmp_path, library_names=["John Williams"])

    assert await provider.get_artist_metadata(_artist("John Williams", MBID)) is None


async def test_artist_without_mbid_matches_by_name_without_namesake(tmp_path: Path) -> None:
    """Without a namesake in the library an artist without mbid matches on name alone."""
    _nfo(tmp_path / "AC_DC", mbid=OTHER_MBID, bio="Rock band")
    provider = await _provider(tmp_path, library_names=["AC/DC"])

    metadata = await provider.get_artist_metadata(_artist("AC/DC"))

    assert metadata is not None
    assert metadata.description == "Rock band"


async def test_save_writes_missing_artwork_and_adds_it_to_the_images(tmp_path: Path) -> None:
    """Missing artwork types are saved under their Kodi name and added next to the online image."""
    folder = tmp_path / "Radiohead"
    folder.mkdir()
    (folder / "thumb.jpg").write_bytes(b"own picture")
    thumb = _remote(ImageType.THUMB, "https://img/thumb.jpg")
    fanart = _remote(ImageType.FANART, "https://img/fanart")
    artist = _artist("Radiohead", images=[thumb, fanart])
    provider = await _provider(tmp_path, save=True, library_names=["Radiohead"])

    with patch(_GET_IMAGE_DATA, AsyncMock(return_value=PNG)) as get_image_data:
        await provider.save_artist_metadata(artist)

    get_image_data.assert_awaited_once_with(provider.mass, fanart.path, "fanarttv")
    assert (folder / "thumb.jpg").read_bytes() == b"own picture"
    assert not (folder / "folder.jpg").exists()
    assert (folder / "fanart.png").read_bytes() == PNG
    assert list(artist.metadata.images or []) == [
        thumb,
        fanart,
        MediaItemImage(
            type=ImageType.FANART, path="Radiohead/fanart.png", provider="local_metadata"
        ),
    ]


async def test_save_creates_the_folder_with_the_mbid_suffix_for_namesakes(
    tmp_path: Path,
) -> None:
    """A missing folder is created, named after the mbid when the library holds a namesake."""
    artist = _artist("John Williams", MBID, images=[_remote(ImageType.THUMB, "https://img/a")])
    provider = await _provider(tmp_path, save=True, library_names=["John Williams"] * 2)

    with patch(_GET_IMAGE_DATA, AsyncMock(return_value=JPEG)):
        await provider.save_artist_metadata(artist)

    assert (tmp_path / "John Williams_53b1" / "folder.jpg").read_bytes() == JPEG


async def test_save_never_writes_into_a_namesakes_folder(tmp_path: Path) -> None:
    """An existing folder of a namesake is left alone."""
    _nfo(tmp_path / "John Williams", mbid=OTHER_MBID)
    artist = _artist("John Williams", MBID, images=[_remote(ImageType.THUMB, "https://img/a")])
    provider = await _provider(tmp_path, save=True, library_names=["John Williams"])

    with patch(_GET_IMAGE_DATA, AsyncMock(return_value=JPEG)) as get_image_data:
        await provider.save_artist_metadata(artist)

    get_image_data.assert_not_awaited()
    assert not (tmp_path / "John Williams" / "folder.jpg").exists()


async def test_save_is_off_by_default(tmp_path: Path) -> None:
    """Nothing is saved unless the setting is on."""
    artist = _artist("Radiohead", images=[_remote(ImageType.THUMB, "https://img/a")])
    provider = await _provider(tmp_path, library_names=["Radiohead"])

    with patch(_GET_IMAGE_DATA, AsyncMock(return_value=JPEG)) as get_image_data:
        await provider.save_artist_metadata(artist)

    get_image_data.assert_not_awaited()
    assert not (tmp_path / "Radiohead").exists()


async def test_save_failure_is_logged_and_keeps_the_online_image(tmp_path: Path) -> None:
    """A failed download is logged, the online image stays and other types are still saved."""
    thumb = _remote(ImageType.THUMB, "https://img/thumb")
    fanart = _remote(ImageType.FANART, "https://img/fanart")
    artist = _artist("Radiohead", images=[thumb, fanart])
    provider = await _provider(tmp_path, save=True, library_names=["Radiohead"])

    async def _get_image_data(_mass: Any, path: str, _provider: str) -> bytes:
        if path == thumb.path:
            raise FileNotFoundError(path)
        return JPEG

    with (
        patch(_GET_IMAGE_DATA, _get_image_data),
        patch.object(provider, "logger") as logger,
    ):
        await provider.save_artist_metadata(artist)

    logger.warning.assert_called_once()
    assert (artist.metadata.images or [])[0] == thumb
    assert (tmp_path / "Radiohead" / "fanart.jpg").read_bytes() == JPEG


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


async def test_artist_without_a_folder_name_gets_and_saves_nothing(tmp_path: Path) -> None:
    """An artist whose name leaves no folder name never reaches outside the folder."""
    root = tmp_path / "root"
    _nfo(root, bio="Root bio")
    artist = _artist("..", images=[_remote(ImageType.THUMB, "https://img/a")])
    provider = await _provider(root, save=True, library_names=[".."])

    with patch(_GET_IMAGE_DATA, AsyncMock(return_value=JPEG)) as get_image_data:
        assert await provider.get_artist_metadata(artist) is None
        await provider.save_artist_metadata(artist)

    get_image_data.assert_not_awaited()
    assert not (root / "folder.jpg").exists()
    assert not (tmp_path / "folder.jpg").exists()


async def test_save_skips_an_unavailable_folder(tmp_path: Path) -> None:
    """Nothing is saved while the folder is not available, such as an unmounted share."""
    artist = _artist("Radiohead", images=[_remote(ImageType.THUMB, "https://img/a")])
    provider = await _provider(tmp_path, save=True, library_names=["Radiohead"])
    cast("MagicMock", provider.mass).storage.is_available = AsyncMock(return_value=False)

    with patch(_GET_IMAGE_DATA, AsyncMock(return_value=JPEG)) as get_image_data:
        await provider.save_artist_metadata(artist)

    get_image_data.assert_not_awaited()
    assert not (tmp_path / "Radiohead").exists()


async def test_save_into_a_read_only_folder_warns_once(tmp_path: Path) -> None:
    """A read-only folder is reported once and nothing is downloaded."""
    artist = _artist("Radiohead", images=[_remote(ImageType.THUMB, "https://img/a")])
    provider = await _provider(tmp_path, save=True, library_names=["Radiohead"])

    with (
        patch(_GET_IMAGE_DATA, AsyncMock(return_value=JPEG)) as get_image_data,
        patch("music_assistant.providers.local_metadata.os.access", return_value=False),
        patch.object(provider, "logger") as logger,
    ):
        await provider.save_artist_metadata(artist)
        await provider.save_artist_metadata(artist)

    get_image_data.assert_not_awaited()
    logger.warning.assert_called_once()


async def test_root_is_listed_again_only_when_it_changes(tmp_path: Path) -> None:
    """The folder list is reused until a subfolder is added, such as by saving artwork."""
    _nfo(tmp_path / "Radiohead")
    provider = await _provider(tmp_path, save=True, library_names=["Muse"])
    scandir = MagicMock(wraps=os.scandir)

    with patch("music_assistant.providers.local_metadata.os.scandir", scandir):
        assert await provider.get_artist_metadata(_artist("Radiohead")) is not None
        assert await provider.get_artist_metadata(_artist("Muse")) is None
        root_listings = [call for call in scandir.call_args_list if call.args[0] == str(tmp_path)]
        assert len(root_listings) == 1

        muse = _artist("Muse", images=[_remote(ImageType.THUMB, "https://img/a")])
        with patch(_GET_IMAGE_DATA, AsyncMock(return_value=JPEG)):
            await provider.save_artist_metadata(muse)
        metadata = await provider.get_artist_metadata(_artist("Muse"))

    assert metadata is not None
    assert [img.path for img in metadata.images or []] == ["Muse/folder.jpg"]
