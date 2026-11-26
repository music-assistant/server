"""Test parsing of Deezer metadata into Music Assistant items."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from deezer_python_gql.generated.enums import AlbumType as DeezerAlbumType
from deezer_python_gql.generated.fragments import (
    AlbumFieldsSubtypes,
    TrackFieldsMediaEstimatedSizes,
)
from music_assistant_models.enums import AlbumType, ContentType, ExternalID, ImageType

from music_assistant.providers.deezer.parsers import (
    map_album_type,
    parse_artist,
    parse_audiobook,
    parse_audiobook_from_album,
    parse_track,
)
from music_assistant.providers.deezer.provider import DeezerProvider

PICTURE = "https://cdn-images.dzcdn.net/images/artist/abc/500x500-000000-80-0-0.jpg"
HERO = "https://cdn-images.dzcdn.net/images/hero/def/1080x1920-000000-80-0-0.jpg"
ACCOUNT_FORMATS = ["FLAC_24", "FLAC", "MP3_320", "MP3_128"]
ALL_SIZES = {"MP3_128": 1, "MP3_320": 1, "FLAC": 1, "FLAC_24": 1}


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
        release_date=details.pop("release_date", None),
        original_release_date=details.pop("original_release_date", None),
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


@pytest.mark.parametrize(
    ("dates", "expected"),
    [
        (
            {
                "original_release_date": "2003-01-01T00:00:00.000Z",
                "release_date": "2017-03-24T00:00:00.000Z",
            },
            datetime(2003, 1, 1, tzinfo=UTC),
        ),
        ({"release_date": "2017-03-24T00:00:00.000Z"}, datetime(2017, 3, 24, tzinfo=UTC)),
    ],
    ids=["original release", "no original release"],
)
def test_audiobook_has_its_release_date(
    provider: DeezerProvider, dates: dict[str, str], expected: datetime
) -> None:
    """The audiobook carries its original release date, else its release date."""
    parsed = parse_audiobook(provider, _audiobook(**dates))  # type: ignore[arg-type]
    assert parsed.metadata.release_date == expected


def test_audiobook_from_a_release_has_its_date_and_label(provider: DeezerProvider) -> None:
    """An audiobook found among releases carries the release date, and the label as publisher."""
    release = SimpleNamespace(
        id="575397181",
        display_title="Conni und ihr kleiner Bruder",
        cover=None,
        is_explicit=False,
        contributors=SimpleNamespace(edges=[]),
        release_date="2024-05-10T00:00:00.000Z",
        label="Karussell",
    )
    parsed = parse_audiobook_from_album(provider, release)  # type: ignore[arg-type]
    assert parsed.publisher == "Karussell"
    assert parsed.metadata.release_date == datetime(2024, 5, 10, tzinfo=UTC)


def _track(sizes: dict[str, int]) -> SimpleNamespace:
    """Build a track as Deezer lists it, with the file sizes it has."""
    no_files = dict.fromkeys(ALL_SIZES)
    return SimpleNamespace(
        id="1",
        title="Loser",
        duration=223,
        is_explicit=False,
        album=None,
        contributors=SimpleNamespace(edges=[]),
        media=SimpleNamespace(
            rights=SimpleNamespace(sub=SimpleNamespace(available=True)),
            estimated_sizes=TrackFieldsMediaEstimatedSizes.model_validate(no_files | sizes),
        ),
    )


@pytest.mark.parametrize(
    ("sizes", "account", "expected"),
    [
        (ALL_SIZES, ACCOUNT_FORMATS, (ContentType.FLAC, 24, None)),
        ({"MP3_128": 1, "MP3_320": 1, "FLAC": 1}, ACCOUNT_FORMATS, (ContentType.FLAC, 16, None)),
        (ALL_SIZES, ["MP3_320", "MP3_128"], (ContentType.MP3, 16, 320)),
        ({"MP3_128": 1}, ACCOUNT_FORMATS, (ContentType.MP3, 16, 128)),
        ({}, ACCOUNT_FORMATS, (ContentType.UNKNOWN, 16, None)),
    ],
)
def test_track_lists_the_best_format_it_has(
    provider: DeezerProvider,
    sizes: dict[str, int],
    account: list[str],
    expected: tuple[ContentType, int, int | None],
) -> None:
    """A track carries the best format it has among those the account asks for."""
    provider.gw_client = Mock(formats=[{"format": name} for name in account])
    track = parse_track(provider, _track(sizes))  # type: ignore[arg-type]

    audio_format = next(iter(track.provider_mappings)).audio_format
    assert (audio_format.content_type, audio_format.bit_depth, audio_format.bit_rate) == expected
