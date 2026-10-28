"""Tests for BookOrbit -> Music Assistant parsers."""

from typing import Any

import pytest
from music_assistant_models.enums import ArtistType, ImageType, MediaType
from music_assistant_models.errors import InvalidDataError

from music_assistant.providers.bookorbit.helpers import book_with_manifest_playback_order
from music_assistant.providers.bookorbit.parsers import (
    _parse_chapters,
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
    # Plain series index for UI display (HA / apps show this value).
    assert collection.sequence == "1"


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


def test_parse_audiobook_skips_name_only_people(book_audiobook: dict[str, Any]) -> None:
    """Name-only author/narrator dicts must not raise or invent artists."""
    book_audiobook["authors"] = [
        {"name": "Name Only"},
        {"id": 11, "name": "Frank Herbert", "sortName": "Herbert, Frank"},
    ]
    book_audiobook["audioMetadata"]["narrators"] = [
        {"name": "Narrator Only", "displayOrder": 0},
        {"id": 21, "name": "Scott Brick", "displayOrder": 1},
    ]
    ab = _parse(book_audiobook)
    assert [(a.item_id, a.name) for a in ab.authors] == [("author:11", "Frank Herbert")]
    assert [(n.item_id, n.name) for n in ab.narrators] == [("narrator:21", "Scott Brick")]


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


def test_parse_audiobook_sorts_out_of_order_embedded_chapters(
    book_audiobook: dict[str, Any],
) -> None:
    """Embedded chapters are sorted by startMs before deriving end offsets."""
    book_audiobook["audioMetadata"]["chapters"] = [
        {"title": "Later", "startMs": 120_000},
        {"title": "Earlier", "startMs": 0},
    ]
    chapters = _parse(book_audiobook).metadata.chapters
    assert [(c.position, c.name, c.start, c.end) for c in chapters] == [
        (1, "Earlier", 0, 120),
        (2, "Later", 120, 78000.5),
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


def test_parse_audiobook_unknown_duration_when_part_missing(
    book_multifile: dict[str, Any],
) -> None:
    """Incomplete part durations must not invent a short audiobook total."""
    book_multifile["files"][1]["durationSeconds"] = None
    book_multifile["audioMetadata"]["durationSeconds"] = None
    assert _parse(book_multifile).duration == 0


def test_parse_audiobook_omits_synthetic_chapters_when_duration_unknown(
    book_multifile: dict[str, Any],
) -> None:
    """Unknown part durations must not invent chapter seek offsets."""
    book_multifile["files"][0]["durationSeconds"] = None
    book_multifile["audioMetadata"]["durationSeconds"] = None
    assert list(_parse(book_multifile).metadata.chapters) == []


def test_parse_audiobook_multipart_prefers_embedded_chapters(
    book_multifile: dict[str, Any],
) -> None:
    """Authoritative chapters win over synthetic one-chapter-per-file timelines."""
    book_multifile["audioMetadata"]["chapters"] = [
        {"title": "Prologue", "startMs": 0},
        {"title": "Act Two", "startMs": 120_000},
    ]
    book_multifile["audioMetadata"]["durationSeconds"] = 301
    chapters = _parse(book_multifile).metadata.chapters
    assert [(c.position, c.name, c.start, c.end) for c in chapters] == [
        (1, "Prologue", 0, 120),
        (2, "Act Two", 120, 301),
    ]


def test_parse_audiobook_synthetic_chapters_follow_sort_order(
    book_multifile: dict[str, Any],
) -> None:
    """Synthetic chapters use playback sortOrder, not the raw files array order."""
    # Path/list order is reversed relative to playback order.
    book_multifile["files"] = [
        {
            "id": 772,
            "filename": "02.mp3",
            "format": "mp3",
            "durationSeconds": 200.5,
            "sortOrder": 1,
        },
        {
            "id": 771,
            "filename": "01.mp3",
            "format": "mp3",
            "durationSeconds": 100.5,
            "sortOrder": 0,
        },
        {"id": 773, "filename": "book.epub", "format": "epub", "durationSeconds": None},
    ]
    ab = _parse(book_multifile)
    assert [(c.position, c.name, c.start, c.end) for c in ab.metadata.chapters] == [
        (1, "01.mp3", 0, 100.5),
        (2, "02.mp3", 100.5, 301),
    ]


def test_parse_chapters_playback_files_independent_of_book_files(
    book_multifile: dict[str, Any],
) -> None:
    """Explicit playback parts (e.g. manifest sequence) own synthetic chapter order."""
    # Book files stay in their original order; playback list is deliberately reversed.
    playback_files = [
        {"id": 772, "filename": "02.mp3", "durationSeconds": 200.5},
        {"id": 771, "filename": "01.mp3", "durationSeconds": 100.5},
    ]
    chapters = _parse_chapters(book_multifile, duration=301.0, playback_files=playback_files)
    assert [(c.position, c.name, c.start, c.end) for c in chapters] == [
        (1, "02.mp3", 0, 200.5),
        (2, "01.mp3", 200.5, 301.0),
    ]


def test_parse_audiobook_uses_manifest_order_without_sort_order(
    book_multifile: dict[str, Any],
) -> None:
    """BookDetail omits sortOrder; manifest sequence must drive synthetic chapters."""
    # Path/id order is the reverse of playback sequence.
    book_multifile["files"] = [
        {
            "id": 772,
            "filename": "02.mp3",
            "format": "mp3",
            "durationSeconds": 200.5,
        },
        {
            "id": 771,
            "filename": "01.mp3",
            "format": "mp3",
            "durationSeconds": 100.5,
        },
        {"id": 773, "filename": "book.epub", "format": "epub", "durationSeconds": None},
    ]
    manifest = {
        "assets": [
            {"assetId": "a1", "fileId": 771, "sequence": 0, "durationMs": 100500},
            {"assetId": "a2", "fileId": 772, "sequence": 1, "durationMs": 200500},
        ]
    }
    ordered = book_with_manifest_playback_order(book_multifile, manifest)
    ab = parse_audiobook(ordered, provider_instance_id=INSTANCE, base_url=BASE_URL)
    chapters = ab.metadata.chapters or []
    assert [(c.position, c.name, c.start, c.end) for c in chapters] == [
        (1, "01.mp3", 0, 100.5),
        (2, "02.mp3", 100.5, 301),
    ]


def test_book_with_manifest_playback_order_requires_mappable_assets(
    book_multifile: dict[str, Any],
) -> None:
    """Unmappable manifests must not fall back to unordered BookDetail."""
    with pytest.raises(InvalidDataError, match="no mappable playback assets"):
        book_with_manifest_playback_order(
            book_multifile,
            {"assets": [{"assetId": "a1", "fileId": 99999, "sequence": 0}]},
        )


def test_book_with_manifest_playback_order_requires_all_audio_assets(
    book_multifile: dict[str, Any],
) -> None:
    """Partial manifest mapping must not invent leftover audio file order."""
    with pytest.raises(InvalidDataError, match="missing playback assets"):
        book_with_manifest_playback_order(
            book_multifile,
            {"assets": [{"assetId": "a1", "fileId": 772, "sequence": 0, "durationMs": 200500}]},
        )


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
    assert mapping.details == "Frank Herbert|Herbert, Frank||"
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
    assert mapping.details == f"Agatha Christie|Christie, Agatha|Queen of Crime|{image.path}"
    assert artist.metadata.description == "Queen of Crime"


def test_parse_author_fingerprint_includes_bio_and_sort_name() -> None:
    """Biography-only or sort-name-only edits must change the sync fingerprint."""
    base = {"id": 6, "name": "Agatha Christie", "sortName": "Christie, Agatha"}
    before = parse_author(base, provider_instance_id=INSTANCE, base_url=BASE_URL)
    bio_only = parse_author(
        {**base, "description": "Queen of Crime"},
        provider_instance_id=INSTANCE,
        base_url=BASE_URL,
    )
    sort_only = parse_author(
        {**base, "sortName": "Christie, A."},
        provider_instance_id=INSTANCE,
        base_url=BASE_URL,
    )
    (before_mapping,) = before.provider_mappings
    (bio_mapping,) = bio_only.provider_mappings
    (sort_mapping,) = sort_only.provider_mappings
    assert before_mapping.details != bio_mapping.details
    assert before_mapping.details != sort_mapping.details


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
