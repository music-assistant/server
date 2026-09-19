"""Tests that a library sync notices when an audiobook's authors/narrators change."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

from music_assistant_models.enums import ArtistType, ProviderType
from music_assistant_models.media_items import Artist, Audiobook, MediaItemType, ProviderMapping

from music_assistant.constants import CONF_LOG_LEVEL
from music_assistant.controllers.music.media.base import AudiobookSyncDetails
from music_assistant.models.music_provider import MusicProvider

INSTANCE_ID = "test--1"
BOOK_ID = "book_1"


def _mapping() -> ProviderMapping:
    """Return the one mapping both the library item and the provider item carry."""
    return ProviderMapping(
        item_id=BOOK_ID,
        provider_domain="test",
        provider_instance=INSTANCE_ID,
        in_library=True,
        is_unique=True,
    )


def _provider() -> MusicProvider:
    """Return a provider instance."""
    manifest = MagicMock()
    manifest.type = ProviderType.MUSIC
    manifest.domain = "test"
    config = MagicMock()
    config.instance_id = INSTANCE_ID
    config.domain = "test"
    config.get_value.side_effect = lambda key, default=None: (
        "GLOBAL" if key == CONF_LOG_LEVEL else default
    )
    return MusicProvider(MagicMock(), manifest, config)


def _stored(
    authors: tuple[str, ...] = (),
    narrators: tuple[str, ...] = (),
    author_is_str: bool = True,
    narrator_is_str: bool = True,
) -> AudiobookSyncDetails:
    """Return the sync snapshot of a library audiobook."""
    return AudiobookSyncDetails(
        item_id=1,
        favorite=False,
        date_added=datetime.fromtimestamp(0, tz=UTC),
        provider_mappings={_mapping()},
        authors=authors,
        narrators=narrators,
        author_is_str=author_is_str,
        narrator_is_str=narrator_is_str,
        fully_played=None,
        resume_position_ms=None,
    )


def _reported(
    authors: list[str | Artist] | None = None, narrators: list[str | Artist] | None = None
) -> Audiobook:
    """Return the audiobook as the provider currently reports it."""
    book = Audiobook(
        item_id=BOOK_ID, provider=INSTANCE_ID, name="Book", provider_mappings={_mapping()}
    )
    book.authors.set(authors or [])
    book.narrators.set(narrators or [])
    return book


def _author(name: str) -> Artist:
    """Return an author as a provider supporting artist types reports it."""
    return Artist(
        item_id=f"author:{name}",
        provider=INSTANCE_ID,
        name=name,
        artist_type=ArtistType.AUTHOR,
        provider_mappings={_mapping()},
    )


def _needs_update(stored: AudiobookSyncDetails, reported: MediaItemType) -> bool:
    """Return what the sync decides about this pair."""
    return _provider()._library_item_needs_update(stored, reported)


def test_corrected_author_is_picked_up() -> None:
    """A corrected author moves neither the provider mappings nor date_added."""
    assert _needs_update(_stored(authors=("Jane Austin",)), _reported(authors=["Jane Austen"]))


def test_corrected_narrator_is_picked_up() -> None:
    """Narrators are compared as well as authors."""
    assert _needs_update(_stored(narrators=("Joe Read",)), _reported(narrators=["Jo Reed"]))


def test_same_names_in_another_order_are_no_change() -> None:
    """Linked artist records have no order of their own, so order alone means nothing."""
    stored = _stored(authors=("Ann Writer", "Bob Penn"))
    assert not _needs_update(stored, _reported(authors=["Bob Penn", "Ann Writer"]))


def test_provider_naming_nobody_leaves_the_stored_names_alone() -> None:
    """A provider reporting nobody does not mean the book has nobody."""
    assert not _needs_update(_stored(authors=("Jane Austen",)), _reported())


def test_plain_names_replaced_by_artist_items_are_a_change() -> None:
    """Plain names and Artist items are stored in different places."""
    stored = _stored(authors=("Jane Austen",))
    assert _needs_update(stored, _reported(authors=[_author("Jane Austen")]))


def test_artist_items_stay_put_once_linked() -> None:
    """Once the switch happened the names come from linked records, so it must not repeat."""
    stored = _stored(authors=("Jane Austen",), author_is_str=False)
    assert not _needs_update(stored, _reported(authors=[_author("Jane Austen")]))
