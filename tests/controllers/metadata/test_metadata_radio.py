"""Tests for the RadioArtworkMixin name-matching helpers on MetaDataController."""

import pytest
from music_assistant_models.enums import ImageType
from music_assistant_models.media_items import (
    Artist,
    ItemMapping,
    MediaItemImage,
    MediaItemMetadata,
)
from music_assistant_models.unique_list import UniqueList

from music_assistant.controllers.metadata import MetaDataController


def _artist(name: str) -> Artist | ItemMapping:
    """Build a minimal artist ItemMapping with the given name."""
    return ItemMapping(item_id=name, provider="library", name=name)


def _controller() -> MetaDataController:
    """Create a bare MetaDataController without running __init__."""
    return MetaDataController.__new__(MetaDataController)


class TestNormalizeRadioArtistName:
    """`normalize_radio_artist_name` flips "Last, First" while sparing edge cases."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Squier, Billy", "Billy Squier"),
            ("Beatles, The", "The Beatles"),
            ("Billy Joel", "Billy Joel"),
            ("Billy_Squier", "Billy Squier"),
            ("Lipps, Inc.", "Lipps, Inc."),
            ("Portugal, The Man", "Portugal, The Man"),
            ("Crosby, Stills & Nash", "Crosby, Stills & Nash"),
            ("Crosby, Stills and Nash", "Crosby, Stills and Nash"),
            ("hello, goodbye", "hello, goodbye"),
        ],
    )
    def test_normalize(self, raw: str, expected: str) -> None:
        """Each raw radio artist string normalizes to the expected display form."""
        assert MetaDataController.normalize_radio_artist_name(raw) == expected


class TestMatchArtistName:
    """`_match_artist_name` matches names directly and across "The" prefix variants."""

    def test_exact_match(self) -> None:
        """An exact name matches."""
        ctrl = _controller()
        assert ctrl._match_artist_name("Billy Squier", [_artist("Billy Squier")]) is True

    def test_the_prefix_on_library_artist(self) -> None:
        """A search without "The" matches a library artist that carries the prefix."""
        ctrl = _controller()
        assert ctrl._match_artist_name("Beatles", [_artist("The Beatles")]) is True

    def test_no_match(self) -> None:
        """Unrelated names do not match."""
        ctrl = _controller()
        assert ctrl._match_artist_name("Madonna", [_artist("Prince")]) is False

    def test_empty_artist_list(self) -> None:
        """No artists means no match."""
        ctrl = _controller()
        assert ctrl._match_artist_name("Anyone", []) is False


class TestGetThumbImage:
    """`_get_thumb_image` extracts only the THUMB image from metadata."""

    def test_returns_only_thumb(self) -> None:
        """A THUMB image is returned in isolation, dropping other image types."""
        ctrl = _controller()
        thumb = MediaItemImage(type=ImageType.THUMB, path="thumb.jpg", provider="theaudiodb")
        fanart = MediaItemImage(type=ImageType.FANART, path="fanart.jpg", provider="theaudiodb")
        metadata = MediaItemMetadata(images=UniqueList([fanart, thumb]))
        result = ctrl._get_thumb_image(metadata)
        assert result is not None
        assert result.images is not None
        assert list(result.images) == [thumb]

    def test_returns_none_without_thumb(self) -> None:
        """Metadata with only non-THUMB images yields None."""
        ctrl = _controller()
        fanart = MediaItemImage(type=ImageType.FANART, path="fanart.jpg", provider="theaudiodb")
        metadata = MediaItemMetadata(images=UniqueList([fanart]))
        assert ctrl._get_thumb_image(metadata) is None

    def test_returns_none_without_images(self) -> None:
        """Metadata without any images yields None."""
        ctrl = _controller()
        assert ctrl._get_thumb_image(MediaItemMetadata()) is None
