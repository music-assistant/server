"""Tests for BookOrbit -> Music Assistant parsers."""

from typing import Any

from music_assistant_models.enums import ArtistType, ImageType, MediaType

from music_assistant.providers.bookorbit.parsers import (
    parse_audiobook,
    parse_author,
    parse_narrator,
)

INSTANCE = "bo_1"
BASE_URL = "http://127.0.0.1:3000"


def _parse(book: dict[str, Any]) -> Any:
    return parse_audiobook(book, provider_instance_id=INSTANCE, base_url=BASE_URL)


def test_parse_audiobook_core_fields(book_audiobook: dict[str, Any]) -> None:
    """Core identity fields map to the MA Audiobook."""
    ab = _parse(book_audiobook)
    assert ab.media_type == MediaType.AUDIOBOOK
    assert ab.provider == INSTANCE
    assert ab.item_id == "42"
    assert ab.name == "Dune | Book One"
    assert ab.duration == 78000
    assert ab.publisher == "Macmillan Audio"
    (mapping,) = ab.provider_mappings
    assert mapping.item_id == "42"
    assert mapping.provider_domain == "bookorbit"
    assert mapping.provider_instance == INSTANCE
    assert mapping.details == book_audiobook["updatedAt"]
    assert mapping.is_unique is True


def test_parse_audiobook_metadata(book_audiobook: dict[str, Any]) -> None:
    """Description, language, genres, series and release date are mapped."""
    md = _parse(book_audiobook).metadata
    assert md.description == "A desert planet, a boy, and a spice."
    assert list(md.languages) == ["en"]
    assert md.genres == {"Science Fiction"}
    assert md.release_date is not None
    assert md.release_date.year == 2007
    (collection,) = md.collections
    assert collection.title == "Dune Chronicles"
    assert collection.sequence == 1


def test_parse_audiobook_cover_url(book_audiobook: dict[str, Any]) -> None:
    """Cover points at the BookOrbit cover endpoint with a cache-busting stamp."""
    ab = _parse(book_audiobook)
    (image,) = ab.metadata.images
    assert image.type == ImageType.THUMB
    assert image.provider == INSTANCE
    assert image.remotely_accessible is False
    assert image.path.startswith(f"{BASE_URL}/api/v1/books/42/cover?t=")


def test_parse_audiobook_without_cover(book_multifile: dict[str, Any]) -> None:
    """No cover source means no images."""
    assert not list(_parse(book_multifile).metadata.images)


def test_parse_audiobook_authors_and_narrators(book_audiobook: dict[str, Any]) -> None:
    """Authors and narrators are artists with namespaced ids."""
    ab = _parse(book_audiobook)
    assert [(a.item_id, a.name) for a in ab.authors] == [("author:11", "Frank Herbert")]
    assert [(n.item_id, n.name) for n in ab.narrators] == [
        ("narrator:21", "Scott Brick"),
        ("narrator:22", "Orlagh Cassidy"),
    ]


def test_parse_audiobook_single_file_uses_embedded_chapters(
    book_audiobook: dict[str, Any],
) -> None:
    """A single m4b uses its embedded chapters, ending at the next start/total duration."""
    chapters = _parse(book_audiobook).metadata.chapters
    assert [(c.position, c.name, c.start, c.end) for c in chapters] == [
        (1, "Opening", 0, 600),
        (2, "Part One", 600, 39000),
        (3, "Part Two", 39000, 78000.5),
    ]


def test_parse_audiobook_multifile_one_chapter_per_audio_file(
    book_multifile: dict[str, Any],
) -> None:
    """Multi-file audio yields one chapter per audio file; non-audio files are skipped."""
    ab = _parse(book_multifile)
    assert [(c.position, c.name, c.start, c.end) for c in ab.metadata.chapters] == [
        (1, "01.mp3", 0, 100.5),
        (2, "02.mp3", 100.5, 301),
    ]
    assert ab.duration == 301


def test_parse_audiobook_untitled_falls_back(book_multifile: dict[str, Any]) -> None:
    """Missing title gets a placeholder name."""
    book_multifile["title"] = None
    assert _parse(book_multifile).name == "UNKNOWN TITLE"


def test_parse_author() -> None:
    """Authors map to AUTHOR artists."""
    artist = parse_author(
        {"id": 11, "name": "Frank Herbert", "sortName": "Herbert, Frank"},
        provider_instance_id=INSTANCE,
        base_url=BASE_URL,
    )
    assert artist.item_id == "author:11"
    assert artist.provider == INSTANCE
    assert artist.name == "Frank Herbert"
    assert artist.sort_name == "Herbert, Frank"
    assert artist.artist_type == ArtistType.AUTHOR
    (mapping,) = artist.provider_mappings
    assert (mapping.item_id, mapping.provider_domain) == ("author:11", "bookorbit")
    assert mapping.details == ""
    assert not artist.metadata.images


def test_parse_author_image_url() -> None:
    """Author photos use the BookOrbit thumbnail path."""
    artist = parse_author(
        {
            "id": 6,
            "name": "Agatha Christie",
            "sortName": "Christie, Agatha",
            "imageUrl": "/api/v1/authors/6/thumbnail?t=1",
            "description": "Queen of Crime",
        },
        provider_instance_id=INSTANCE,
        base_url=BASE_URL,
    )
    assert artist.metadata.images is not None
    (image,) = artist.metadata.images
    assert image.type == ImageType.THUMB
    assert image.path == f"{BASE_URL}/api/v1/authors/6/thumbnail?t=1"
    assert image.remotely_accessible is False
    (mapping,) = artist.provider_mappings
    assert mapping.details == image.path
    assert artist.metadata.description == "Queen of Crime"


def test_parse_author_ignores_cover_book_id() -> None:
    """Book covers are not used as author thumbnails."""
    artist = parse_author(
        {"id": 11, "name": "Frank Herbert", "coverBookId": 42, "lastAddedAt": "t1"},
        provider_instance_id=INSTANCE,
        base_url=BASE_URL,
    )
    assert not artist.metadata.images


def test_parse_narrator_without_sort_name() -> None:
    """Narrators map to NARRATOR artists; sort name falls back to name."""
    artist = parse_narrator(
        {"id": 23, "name": "Ray Porter", "sortName": None, "displayOrder": 0},
        provider_instance_id=INSTANCE,
        base_url=BASE_URL,
    )
    assert artist.item_id == "narrator:23"
    assert artist.sort_name == "Ray Porter"
    assert artist.artist_type == ArtistType.NARRATOR
    assert not artist.metadata.images


def test_parse_audiobook_narrators_have_no_book_cover_thumbs(
    book_audiobook: dict[str, Any],
) -> None:
    """Narrators stay without images when BookOrbit has no narrator photos."""
    ab = _parse(book_audiobook)
    for narrator in ab.narrators:
        assert not narrator.metadata.images
