"""Tests that overwriting an audiobook rewrites its author/narrator links."""

from __future__ import annotations

from music_assistant_models.enums import ArtistType
from music_assistant_models.media_items import Artist, Audiobook, ProviderMapping

from music_assistant.constants import DB_TABLE_ARTISTS, DB_TABLE_AUDIOBOOK_ARTISTS
from music_assistant.mass import MusicAssistant

INSTANCE_ID = "test--1"
BOOK_ID = "book_1"


def _mapping(item_id: str) -> set[ProviderMapping]:
    """Return a single provider mapping for the given provider item id."""
    return {
        ProviderMapping(
            item_id=item_id,
            provider_domain="test",
            provider_instance=INSTANCE_ID,
            in_library=True,
        )
    }


def _artist(name: str, artist_type: ArtistType) -> Artist:
    """Return an author or narrator as a provider reports it."""
    return Artist(
        item_id=name,
        provider=INSTANCE_ID,
        name=name,
        artist_type=artist_type,
        provider_mappings=_mapping(name),
    )


def _book(authors: list[Artist] | None = None, narrators: list[Artist] | None = None) -> Audiobook:
    """Return an audiobook as a provider reports it."""
    book = Audiobook(
        item_id=BOOK_ID, provider=INSTANCE_ID, name="A Book", provider_mappings=_mapping(BOOK_ID)
    )
    book.authors.set(authors or [])
    book.narrators.set(narrators or [])
    return book


async def _linked(mass: MusicAssistant, db_id: int, artist_type: ArtistType) -> set[str]:
    """Return the names linked to the audiobook as the given artist type."""
    rows = await mass.music.database.get_rows_from_query(
        f"SELECT {DB_TABLE_ARTISTS}.name FROM {DB_TABLE_AUDIOBOOK_ARTISTS} "
        f"JOIN {DB_TABLE_ARTISTS} "
        f"ON {DB_TABLE_ARTISTS}.item_id = {DB_TABLE_AUDIOBOOK_ARTISTS}.artist_id "
        "WHERE audiobook_id = :db_id AND artist_type = :artist_type",
        {"db_id": db_id, "artist_type": artist_type.value},
    )
    return {row["name"] for row in rows}


async def _add(mass: MusicAssistant, book: Audiobook) -> int:
    """Add the audiobook to the library and return its database id."""
    return int((await mass.music.audiobooks.add_item_to_library(book)).item_id)


async def test_overwrite_drops_a_narrator_the_provider_stopped_naming(
    mass: MusicAssistant,
) -> None:
    """The stale link used to survive, leaving the book under both narrators."""
    db_id = await _add(mass, _book(narrators=[_artist("Old Voice", ArtistType.NARRATOR)]))

    await mass.music.audiobooks.update_item_in_library(
        db_id, _book(narrators=[_artist("New Voice", ArtistType.NARRATOR)]), overwrite=True
    )

    assert await _linked(mass, db_id, ArtistType.NARRATOR) == {"New Voice"}


async def test_overwriting_one_type_keeps_the_other(mass: MusicAssistant) -> None:
    """Both types share one table, so clearing all rows would take the authors too."""
    db_id = await _add(
        mass,
        _book(
            authors=[_artist("Jane Austen", ArtistType.AUTHOR)],
            narrators=[_artist("Old Voice", ArtistType.NARRATOR)],
        ),
    )

    await mass.music.audiobooks.update_item_in_library(
        db_id, _book(narrators=[_artist("New Voice", ArtistType.NARRATOR)]), overwrite=True
    )

    assert await _linked(mass, db_id, ArtistType.AUTHOR) == {"Jane Austen"}
    assert await _linked(mass, db_id, ArtistType.NARRATOR) == {"New Voice"}


async def test_a_type_the_update_omits_is_left_alone(mass: MusicAssistant) -> None:
    """An empty list says nothing about what is stored."""
    db_id = await _add(mass, _book(narrators=[_artist("Old Voice", ArtistType.NARRATOR)]))

    await mass.music.audiobooks.update_item_in_library(db_id, _book(), overwrite=True)

    assert await _linked(mass, db_id, ArtistType.NARRATOR) == {"Old Voice"}


async def test_without_overwrite_nothing_is_removed(mass: MusicAssistant) -> None:
    """Only an overwrite replaces links, a plain update never drops one."""
    db_id = await _add(mass, _book(narrators=[_artist("Old Voice", ArtistType.NARRATOR)]))

    await mass.music.audiobooks.update_item_in_library(
        db_id, _book(narrators=[_artist("New Voice", ArtistType.NARRATOR)])
    )

    assert "Old Voice" in await _linked(mass, db_id, ArtistType.NARRATOR)


async def test_overwrite_keeps_an_artist_whose_role_changed(mass: MusicAssistant) -> None:
    """The narrator pass used to remove the link of a narrator who became the author."""
    db_id = await _add(mass, _book(narrators=[_artist("Kate Voice", ArtistType.NARRATOR)]))

    await mass.music.audiobooks.update_item_in_library(
        db_id,
        _book(
            authors=[_artist("Kate Voice", ArtistType.AUTHOR)],
            narrators=[_artist("New Voice", ArtistType.NARRATOR)],
        ),
        overwrite=True,
    )

    assert await _linked(mass, db_id, ArtistType.AUTHOR) == {"Kate Voice"}
    assert await _linked(mass, db_id, ArtistType.NARRATOR) == {"New Voice"}
