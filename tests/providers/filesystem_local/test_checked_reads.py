"""Tests that the filesystem provider reads the very file it checked to lie inside its folder."""

import io
import os
import shutil
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from music_assistant_models.errors import MediaNotFoundError
from mutagen.id3 import APIC, ID3
from PIL import Image

from music_assistant.providers.filesystem_local import LocalFileSystemProvider
from music_assistant.providers.filesystem_local.helpers import open_real_path

FIXTURES = Path(__file__).parent.parent.parent / "fixtures"
INSIDE_AUDIO = FIXTURES / "MultipleArtists.flac"
OUTSIDE_AUDIO = FIXTURES / "MyArtist - MyTitle.mp3"
UNTAGGED_MP3 = FIXTURES / "MyArtist - MyTitle without Tags.mp3"

RED = (255, 0, 0)
BLUE = (0, 0, 255)

type Repoint = Callable[[str, str], None]


def _make_provider(base_path: Path) -> LocalFileSystemProvider:
    provider = LocalFileSystemProvider.__new__(LocalFileSystemProvider)
    provider.base_path = str(base_path)
    provider.logger = MagicMock()
    provider.write_access = False
    provider.media_content_type = "music"
    provider.config = MagicMock(instance_id="filesystem_local--test")
    provider.manifest = MagicMock(domain="filesystem_local")
    return provider


def _png(color: tuple[int, int, int]) -> bytes:
    data = io.BytesIO()
    Image.new("RGB", (16, 16), color).save(data, "PNG")
    return data.getvalue()


def _mp3_with_cover(path: Path, color: tuple[int, int, int]) -> None:
    shutil.copy(UNTAGGED_MP3, path)
    tags = ID3()  # type: ignore[no-untyped-call]
    cover = APIC(encoding=3, mime="image/png", type=3, desc="cover", data=_png(color))  # type: ignore[no-untyped-call]
    tags.add(cover)  # type: ignore[no-untyped-call]
    tags.save(path)


def _point(link: Path, target: Path) -> None:
    """Re-point a symlink in one step, as a rename over the old link does."""
    new_link = link.with_name(f".{link.name}.new")
    new_link.symlink_to(target)
    new_link.replace(link)


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """
    Create a folder holding symlinks to files and a folder inside it, and files outside it.

    Each symlink name has an outside counterpart of the same kind for it to be re-pointed to.
    """
    base = tmp_path / "music"
    outside = tmp_path / "outside"
    (base / "Albums Real").mkdir(parents=True)
    (outside / "Elsewhere").mkdir(parents=True)
    shutil.copy(INSIDE_AUDIO, base / "inside.flac")
    shutil.copy(OUTSIDE_AUDIO, outside / "track.flac")
    _mp3_with_cover(base / "inside.mp3", RED)
    _mp3_with_cover(outside / "covered.mp3", BLUE)
    (base / "inside.jpg").write_bytes(_png(RED))
    (outside / "cover.jpg").write_bytes(_png(BLUE))
    (base / "inside.nfo").write_bytes(b"inside")
    (outside / "album.nfo").write_bytes(b"outside")
    (base / "Albums Real" / "inside album").mkdir()
    (outside / "Elsewhere" / "outside album").mkdir()
    (base / "track.flac").symlink_to("inside.flac")
    (base / "covered.mp3").symlink_to("inside.mp3")
    (base / "cover.jpg").symlink_to("inside.jpg")
    (base / "album.nfo").symlink_to("inside.nfo")
    (base / "Albums").symlink_to("Albums Real", target_is_directory=True)
    return base


@pytest.fixture
def repoint_after_check(monkeypatch: pytest.MonkeyPatch, tree: Path) -> Repoint:
    """
    Return a function that arms a symlink to be re-pointed right after it passed the check.

    This is the earliest moment another process changing the folder could act.
    """
    armed: dict[str, Path] = {}

    def _open_then_repoint(real_base_path: str, path: str, flags: int = os.O_RDONLY) -> int:
        fd = open_real_path(real_base_path, path, flags)
        if (target := armed.pop(Path(path).name, None)) is not None:
            _point(Path(path), target)
        return fd

    monkeypatch.setattr(
        "music_assistant.providers.filesystem_local.open_real_path", _open_then_repoint
    )

    def _arm(link_name: str, outside_name: str) -> None:
        armed[link_name] = tree.parent / "outside" / outside_name

    return _arm


def _color_of(image: bytes) -> tuple[int, int, int]:
    pixel = Image.open(io.BytesIO(image)).convert("RGB").getpixel((8, 8))
    assert isinstance(pixel, tuple)
    return (pixel[0], pixel[1], pixel[2])


def _is_close(color: tuple[int, int, int], expected: tuple[int, int, int]) -> bool:
    return all(abs(a - b) < 40 for a, b in zip(color, expected, strict=True))


async def test_links_inside_the_folder_are_read(tree: Path) -> None:
    """Tags and images are read through symlinks that stay inside the folder."""
    provider = _make_provider(tree)

    tags = await provider._parse_tags(await provider.resolve("track.flac"))
    cover = await provider.resolve_image("cover.jpg")
    embedded = await provider.resolve_image("covered.mp3")

    assert tags.format == "flac"
    assert cover == _png(RED)
    assert isinstance(embedded, bytes)
    assert _is_close(_color_of(embedded), RED)


async def test_tags_are_refused_for_a_link_leading_outside(tree: Path) -> None:
    """A file whose real location lies outside the folder is not parsed."""
    (tree / "elsewhere.flac").symlink_to(tree.parent / "outside" / "track.flac")
    provider = _make_provider(tree)
    item = MagicMock(absolute_path=str(tree / "elsewhere.flac"), file_size=None)

    with pytest.raises(MediaNotFoundError):
        await provider._parse_tags(item)


async def test_read_file_reads_the_checked_file(tree: Path, repoint_after_check: Repoint) -> None:
    """A file read (NFO, CUE, playlist, lyrics) returns the file that passed the check."""
    provider = _make_provider(tree)
    repoint_after_check("album.nfo", "album.nfo")

    assert await provider._read_file("album.nfo") == b"inside"


async def test_tags_are_read_from_the_checked_file(
    tree: Path, repoint_after_check: Repoint
) -> None:
    """The tags come from the file that passed the check."""
    provider = _make_provider(tree)
    item = await provider.resolve("track.flac")
    repoint_after_check("track.flac", "track.flac")

    tags = await provider._parse_tags(item)

    assert tags.format == "flac"
    assert tags.title == "Test Track"


async def test_image_is_read_from_the_checked_file(
    tree: Path, repoint_after_check: Repoint
) -> None:
    """An image file comes from the file that passed the check."""
    provider = _make_provider(tree)
    repoint_after_check("cover.jpg", "cover.jpg")

    assert await provider.resolve_image("cover.jpg") == _png(RED)


async def test_embedded_image_is_read_from_the_checked_file(
    tree: Path, repoint_after_check: Repoint
) -> None:
    """An image embedded in an audio file comes from the file that passed the check."""
    provider = _make_provider(tree)
    repoint_after_check("covered.mp3", "covered.mp3")

    image = await provider.resolve_image("covered.mp3")

    assert isinstance(image, bytes)
    assert _is_close(_color_of(image), RED)


async def test_folder_listing_is_of_the_checked_folder(
    tree: Path, repoint_after_check: Repoint
) -> None:
    """A folder listing shows the folder that passed the check."""
    provider = _make_provider(tree)
    repoint_after_check("Albums", "Elsewhere")

    items = await provider._scandir("Albums")

    assert [item.filename for item in items] == ["inside album"]
    assert items[0].relative_path == "Albums/inside album"
