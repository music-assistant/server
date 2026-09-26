"""Tests that a library sync stores changed audiobook authors/narrators, and then settles."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import ArtistType, ProviderType
from music_assistant_models.media_items import Artist, Audiobook, ProviderMapping

from music_assistant.constants import (
    CONF_LOG_LEVEL,
    DB_TABLE_ARTISTS,
    DB_TABLE_AUDIOBOOK_ARTISTS,
    DB_TABLE_AUDIOBOOKS,
)
from music_assistant.helpers.json import json_loads
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from music_assistant_models.enums import MediaType

    from music_assistant.mass import MusicAssistant

INSTANCE_A = "test--a"
INSTANCE_B = "test--b"


class _Provider(MusicProvider):
    """A provider whose library is whatever the test hands it."""

    books: list[Audiobook]
    artist_types: set[ArtistType]

    @property
    def supported_artist_types(self) -> set[ArtistType]:
        return self.artist_types

    async def get_library_audiobooks(self) -> AsyncGenerator[Audiobook]:
        for book in self.books:
            yield book


@pytest.fixture(autouse=True)
def strict_sync_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test when the sync loop swallows a per-item failure."""

    def _raise(
        self: MusicProvider, media_type: MediaType, item_ref: str | None, err: Exception
    ) -> None:
        del self, media_type
        raise AssertionError(f"sync swallowed a failure for {item_ref}: {err!r}")

    monkeypatch.setattr(MusicProvider, "_handle_sync_item_failure", _raise)


def _provider(mass: MusicAssistant, instance_id: str, linked: bool) -> _Provider:
    manifest = MagicMock()
    manifest.type = ProviderType.MUSIC
    manifest.domain = "test"
    config = MagicMock()
    config.instance_id = instance_id
    config.domain = "test"
    config.get_value.side_effect = lambda key, default=None: (
        "GLOBAL" if key == CONF_LOG_LEVEL else default
    )
    provider = _Provider(mass, manifest, config)
    provider.artist_types = {ArtistType.AUTHOR, ArtistType.NARRATOR} if linked else set()
    return provider


def _mapping(item_id: str, instance_id: str) -> set[ProviderMapping]:
    return {
        ProviderMapping(
            item_id=item_id,
            provider_domain="test",
            provider_instance=instance_id,
            in_library=True,
        )
    }


def _artist(item_id: str, name: str, artist_type: ArtistType, instance_id: str) -> Artist:
    return Artist(
        item_id=item_id,
        provider=instance_id,
        name=name,
        artist_type=artist_type,
        provider_mappings=_mapping(item_id, instance_id),
    )


def _book(
    authors: list[str | Artist] | None = None,
    narrators: list[str | Artist] | None = None,
    instance_id: str = INSTANCE_A,
) -> Audiobook:
    book_id = f"book-{instance_id}"
    book = Audiobook(
        item_id=book_id,
        provider=instance_id,
        name="Emma",
        provider_mappings=_mapping(book_id, instance_id),
    )
    book.authors.set(authors or [])
    book.narrators.set(narrators or [])
    return book


def _book_with(column: str, values: list[str | Artist]) -> Audiobook:
    return _book(authors=values) if column == "authors" else _book(narrators=values)


async def _sync(provider: _Provider, book: Audiobook) -> int:
    """Run the provider's audiobook sync over this one book and return its library id."""
    provider.books = [book]
    (db_id,) = await provider._sync_library_audiobooks()
    return db_id


async def _settled(mass: MusicAssistant, provider: _Provider, book: Audiobook) -> bool:
    """Return True when a further sync of the same book finds nothing to update."""
    details = await mass.music.audiobooks.get_library_item_sync_details(book.provider_mappings)
    assert details is not None
    return not provider._library_item_needs_update(details, book)


async def _linked(mass: MusicAssistant, db_id: int, artist_type: ArtistType) -> set[str]:
    rows = await mass.music.database.get_rows_from_query(
        f"SELECT {DB_TABLE_ARTISTS}.name FROM {DB_TABLE_AUDIOBOOK_ARTISTS} "
        f"JOIN {DB_TABLE_ARTISTS} "
        f"ON {DB_TABLE_ARTISTS}.item_id = {DB_TABLE_AUDIOBOOK_ARTISTS}.artist_id "
        "WHERE audiobook_id = :db_id AND artist_type = :artist_type",
        {"db_id": db_id, "artist_type": artist_type.value},
    )
    return {row["name"] for row in rows}


async def _stored_names(mass: MusicAssistant, db_id: int, column: str) -> list[str]:
    row = await mass.music.database.get_row(DB_TABLE_AUDIOBOOKS, {"item_id": db_id})
    assert row is not None
    return list(json_loads(row[column]))


@pytest.mark.parametrize("column", ["authors", "narrators"])
async def test_corrected_plain_name_is_stored(mass: MusicAssistant, column: str) -> None:
    """The update without overwrite used to keep the stored names, on every sync."""
    provider = _provider(mass, INSTANCE_A, linked=False)
    db_id = await _sync(provider, _book_with(column, ["Jane Austin"]))

    corrected = _book_with(column, ["Jane Austen"])
    await _sync(provider, corrected)

    assert await _stored_names(mass, db_id, column) == ["Jane Austen"]
    assert await _settled(mass, provider, corrected)


@pytest.mark.parametrize(
    ("column", "artist_type"),
    [("authors", ArtistType.AUTHOR), ("narrators", ArtistType.NARRATOR)],
)
async def test_corrected_linked_artist_replaces_the_old_one(
    mass: MusicAssistant, column: str, artist_type: ArtistType
) -> None:
    """The old link used to stay next to the new one, so the names never matched again."""
    provider = _provider(mass, INSTANCE_A, linked=True)
    db_id = await _sync(
        provider, _book_with(column, [_artist("wrong", "Jane Austin", artist_type, INSTANCE_A)])
    )

    corrected = _book_with(column, [_artist("right", "Jane Austen", artist_type, INSTANCE_A)])
    await _sync(provider, corrected)

    assert await _linked(mass, db_id, artist_type) == {"Jane Austen"}
    assert await _settled(mass, provider, corrected)


async def test_library_side_artist_name_is_no_change(mass: MusicAssistant) -> None:
    """A library artist renamed by the user or a lookup must not count as a change forever."""
    provider = _provider(mass, INSTANCE_A, linked=True)
    book = _book(authors=[_artist("tolkien", "J. R. R. Tolkien", ArtistType.AUTHOR, INSTANCE_A)])
    db_id = await _sync(provider, book)
    (artist_id,) = [
        row["artist_id"]
        for row in await mass.music.database.get_rows(
            DB_TABLE_AUDIOBOOK_ARTISTS, {"audiobook_id": db_id}
        )
    ]
    await mass.music.database.update(
        DB_TABLE_ARTISTS, {"item_id": artist_id}, {"name": "J.R.R. Tolkien"}
    )

    assert await _settled(mass, provider, book)


async def test_linked_artists_replaced_by_plain_names(mass: MusicAssistant) -> None:
    """The old links used to stay, hiding the new names and flagging every sync."""
    db_id = await _sync(
        _provider(mass, INSTANCE_A, linked=True),
        _book(authors=[_artist("austen", "Jane Austen", ArtistType.AUTHOR, INSTANCE_A)]),
    )

    provider = _provider(mass, INSTANCE_A, linked=False)
    plain = _book(authors=["Jane Austen"])
    await _sync(provider, plain)

    assert await _linked(mass, db_id, ArtistType.AUTHOR) == set()
    assert (await mass.music.audiobooks.get_library_item(db_id)).authors == ["Jane Austen"]
    assert await _settled(mass, provider, plain)


async def test_plain_names_replaced_by_linked_artists(mass: MusicAssistant) -> None:
    """The one direction the old check handled keeps working."""
    db_id = await _sync(_provider(mass, INSTANCE_A, linked=False), _book(authors=["Jane Austen"]))

    provider = _provider(mass, INSTANCE_A, linked=True)
    linked = _book(authors=[_artist("austen", "Jane Austen", ArtistType.AUTHOR, INSTANCE_A)])
    await _sync(provider, linked)

    assert await _linked(mass, db_id, ArtistType.AUTHOR) == {"Jane Austen"}
    assert await _settled(mass, provider, linked)


async def test_provider_naming_nobody_leaves_the_stored_names_alone(
    mass: MusicAssistant,
) -> None:
    """A provider reporting nobody does not mean the book has nobody."""
    provider = _provider(mass, INSTANCE_A, linked=False)
    db_id = await _sync(provider, _book(authors=["Jane Austen"]))

    await _sync(provider, _book())

    assert await _stored_names(mass, db_id, "authors") == ["Jane Austen"]
    assert await _settled(mass, provider, _book())


async def test_each_provider_replaces_only_its_own_links(mass: MusicAssistant) -> None:
    """A merged book holds both providers' artists, so one provider's correction is its own."""
    provider_a = _provider(mass, INSTANCE_A, linked=True)
    provider_b = _provider(mass, INSTANCE_B, linked=True)
    book_a = _book(
        authors=[_artist("jane", "Jane Austen", ArtistType.AUTHOR, INSTANCE_A)],
        instance_id=INSTANCE_A,
    )
    db_id = await _sync(provider_a, book_a)
    # what the sync of the second provider does once it matched the book
    await mass.music.audiobooks.update_item_in_library(
        db_id,
        _book(
            authors=[_artist("wrong", "Cassandra Austin", ArtistType.AUTHOR, INSTANCE_B)],
            instance_id=INSTANCE_B,
        ),
    )

    corrected_b = _book(
        authors=[_artist("right", "Cassandra Austen", ArtistType.AUTHOR, INSTANCE_B)],
        instance_id=INSTANCE_B,
    )
    await _sync(provider_b, corrected_b)

    assert await _linked(mass, db_id, ArtistType.AUTHOR) == {"Jane Austen", "Cassandra Austen"}
    assert await _settled(mass, provider_a, book_a)
    assert await _settled(mass, provider_b, corrected_b)


async def test_plain_names_of_two_providers_do_not_take_turns(mass: MusicAssistant) -> None:
    """Plain names cannot be told apart per provider, so differing spellings must settle."""
    provider_a = _provider(mass, INSTANCE_A, linked=False)
    provider_b = _provider(mass, INSTANCE_B, linked=False)
    book_a = _book(authors=["Jane Austen"], instance_id=INSTANCE_A)
    book_b = _book(authors=["J. Austen"], instance_id=INSTANCE_B)
    db_id = await _sync(provider_a, book_a)
    await mass.music.audiobooks.update_item_in_library(db_id, book_b)

    await _sync(provider_b, book_b)
    await _sync(provider_a, book_a)

    assert await _stored_names(mass, db_id, "authors") == ["Jane Austen"]
    assert await _settled(mass, provider_a, book_a)
    assert await _settled(mass, provider_b, book_b)


async def test_same_name_under_a_new_id_settles(mass: MusicAssistant) -> None:
    """The new id is merged into the existing artist by name, which then holds both ids."""
    provider = _provider(mass, INSTANCE_A, linked=True)
    db_id = await _sync(
        provider, _book(authors=[_artist("old", "Jane Austen", ArtistType.AUTHOR, INSTANCE_A)])
    )

    reissued = _book(authors=[_artist("new", "Jane Austen", ArtistType.AUTHOR, INSTANCE_A)])
    await _sync(provider, reissued)

    assert await _linked(mass, db_id, ArtistType.AUTHOR) == {"Jane Austen"}
    assert await _settled(mass, provider, reissued)


async def test_one_id_for_both_roles_settles(mass: MusicAssistant) -> None:
    """One artist row serves both roles and is stored as author, so its narrator role is kept."""
    provider = _provider(mass, INSTANCE_A, linked=True)
    book = _book(
        authors=[_artist("fry", "Stephen Fry", ArtistType.AUTHOR, INSTANCE_A)],
        narrators=[_artist("fry", "Stephen Fry", ArtistType.NARRATOR, INSTANCE_A)],
    )
    db_id = await _sync(provider, book)
    await _sync(provider, book)

    assert await _linked(mass, db_id, ArtistType.AUTHOR) == {"Stephen Fry"}
    assert await _settled(mass, provider, book)
