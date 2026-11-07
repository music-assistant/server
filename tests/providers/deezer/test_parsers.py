"""Test parsing of Deezer metadata into Music Assistant items."""

from __future__ import annotations

import pytest
from deezer_python_gql.generated.enums import AlbumType as DeezerAlbumType
from deezer_python_gql.generated.fragments import AlbumFieldsSubtypes
from music_assistant_models.enums import AlbumType

from music_assistant.providers.deezer.parsers import map_album_type


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
