"""Test parsing of Deezer metadata into Music Assistant items."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from deezer_python_gql.generated.enums import AlbumType as DeezerAlbumType
from deezer_python_gql.generated.fragments import AlbumFieldsSubtypes
from music_assistant_models.enums import AlbumType, ExternalID, ImageType

from music_assistant.providers.deezer.parsers import (
    map_album_type,
    parse_artist,
    parse_audiobook,
)
from music_assistant.providers.deezer.provider import DeezerProvider

PICTURE = "https://cdn-images.dzcdn.net/images/artist/abc/500x500-000000-80-0-0.jpg"
HERO = "https://cdn-images.dzcdn.net/images/hero/def/1080x1920-000000-80-0-0.jpg"


def _subtypes(*, live: bool = False, compilation: bool = False) -> AlbumFieldsSubtypes:
    """Build the subtype flags Deezer sends for an album."""
    return AlbumFieldsSubtypes.model_validate(
        {
            "isStudio": not (live or compilation),
            "isLive": live,
            "isCompilation": compilation,
            "isKaraoke": False,
        }
    )


@pytest.mark.parametrize(
    ("deezer_type", "title", "version", "subtypes", "expected"),
    [
        (DeezerAlbumType.ALBUM, "Earth Inferno", "", _subtypes(live=True), AlbumType.LIVE),
        (
            DeezerAlbumType.ALBUM,
            "Best of Bowie",
            "",
            _subtypes(compilation=True),
            AlbumType.COMPILATION,
        ),
        (
            DeezerAlbumType.ALBUM,
            "Dune (Original Motion Picture Soundtrack)",
            "",
            _subtypes(compilation=True),
            AlbumType.SOUNDTRACK,
        ),
        (DeezerAlbumType.SINGLES, "Song", "", _subtypes(), AlbumType.SINGLE),
        # search results carry no subtypes
        (DeezerAlbumType.ALBUM, "Alive in Berlin", "Live", None, AlbumType.LIVE),
        (DeezerAlbumType.ALBUM, "Best of Bowie", "", None, AlbumType.ALBUM),
    ],
)
def test_map_album_type(
    deezer_type: DeezerAlbumType,
    title: str,
    version: str,
    subtypes: AlbumFieldsSubtypes | None,
    expected: AlbumType,
) -> None:
    """Deezer's live and compilation flags and the title set the album type."""
    assert map_album_type(deezer_type, title, version, subtypes) == expected


def test_artist_details_add_fanart(provider: DeezerProvider) -> None:
    """The artist details add the hero picture as fanart."""
    artist = SimpleNamespace(
        id="1",
        name="Example",
        picture=SimpleNamespace(urls=[PICTURE]),
        hero_picture=SimpleNamespace(urls=[HERO]),
    )
    parsed = parse_artist(provider, artist)  # type: ignore[arg-type]
    assert [(image.type, image.path) for image in parsed.metadata.images or []] == [
        (ImageType.THUMB, PICTURE),
        (ImageType.FANART, HERO),
    ]


def test_artist_from_a_list_has_no_fanart(provider: DeezerProvider) -> None:
    """Artists from lists and search carry no hero picture."""
    artist = SimpleNamespace(id="1", name="Example", picture=SimpleNamespace(urls=[PICTURE]))
    parsed = parse_artist(provider, artist)  # type: ignore[arg-type]
    assert [image.type for image in parsed.metadata.images or []] == [ImageType.THUMB]


def _audiobook(**details: str) -> SimpleNamespace:
    """Build an audiobook as Deezer returns it, with optional detail fields."""
    return SimpleNamespace(
        id="220381052",
        display_title="Weihnachten in Bullerbü",
        cover=None,
        is_explicit=False,
        description=None,
        fans_count=0,
        contributors=SimpleNamespace(edges=[]),
        duration=3600,
        publisher=None,
        is_favorite=False,
        **details,
    )


def test_audiobook_details_add_the_upc(provider: DeezerProvider) -> None:
    """The audiobook details add the UPC as barcode."""
    parsed = parse_audiobook(provider, _audiobook(upc="4064066253691"))  # type: ignore[arg-type]
    assert parsed.external_ids == {(ExternalID.BARCODE, "4064066253691")}


def test_audiobook_from_a_list_has_no_barcode(provider: DeezerProvider) -> None:
    """Audiobooks from lists carry no UPC."""
    parsed = parse_audiobook(provider, _audiobook())  # type: ignore[arg-type]
    assert parsed.external_ids == set()
