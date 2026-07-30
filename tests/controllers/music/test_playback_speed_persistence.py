"""
Tests for persisting the per-item playback speed of audiobooks/podcast episodes.

The playback speed a listener picks for an audiobook or podcast episode is stored
in the ``playlog`` table (alongside the resume position) so it can be restored when
the item is queued again later. These integration tests use the ``mass`` fixture
from ``tests/conftest.py`` which creates a full MusicAssistant instance with a real
SQLite database in a temporary directory.
"""

from __future__ import annotations

from uuid import uuid4

from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import Audiobook, ProviderMapping

from music_assistant.constants import DB_TABLE_PLAYLOG
from music_assistant.mass import MusicAssistant


def _library_mapping() -> set[ProviderMapping]:
    """Create a single library provider mapping with a unique provider item id."""
    return {
        ProviderMapping(
            item_id=uuid4().hex,
            provider_domain="library",
            provider_instance="library",
            in_library=True,
        )
    }


def _audiobook() -> Audiobook:
    """Create a minimal audiobook with a unique item id."""
    return Audiobook(
        item_id=uuid4().hex,
        provider="library",
        name="A Book",
        provider_mappings=_library_mapping(),
        duration=3600,
    )


async def test_playback_speed_defaults_to_normal(mass: MusicAssistant) -> None:
    """An item that never had a speed set reports the normal (1.0) speed."""
    user = await mass.webserver.auth.create_user("speeduser_default")
    book = _audiobook()
    assert await mass.music.get_playback_speed(book, userid=user.user_id) == 1.0


async def test_playback_speed_is_persisted(mass: MusicAssistant) -> None:
    """A speed supplied while reporting progress is stored and read back."""
    user = await mass.webserver.auth.create_user("speeduser_persist")
    book = _audiobook()

    await mass.music.mark_item_played(
        book,
        fully_played=False,
        seconds_played=100,
        userid=user.user_id,
        user_initiated=False,
        playback_speed=1.5,
    )

    assert await mass.music.get_playback_speed(book, userid=user.user_id) == 1.5


async def test_progress_sync_preserves_stored_speed(mass: MusicAssistant) -> None:
    """A speed-less progress write (e.g. a provider sync) must not reset the speed."""
    user = await mass.webserver.auth.create_user("speeduser_preserve")
    book = _audiobook()

    # listener sets a custom speed (as MA's own report path does)
    await mass.music.mark_item_played(
        book,
        fully_played=False,
        seconds_played=100,
        userid=user.user_id,
        user_initiated=False,
        playback_speed=1.5,
    )
    # a later progress update without a speed (e.g. Audiobookshelf/gpodder sync)
    await mass.music.mark_item_played(
        book,
        fully_played=False,
        seconds_played=200,
        userid=user.user_id,
        user_initiated=False,
    )

    # speed preserved, while the resume position still advanced
    assert await mass.music.get_playback_speed(book, userid=user.user_id) == 1.5
    row = await mass.music.database.get_row(
        DB_TABLE_PLAYLOG,
        {
            "item_id": book.item_id,
            "provider": "library",
            "media_type": MediaType.AUDIOBOOK.value,
            "userid": user.user_id,
        },
    )
    assert row is not None
    assert row["seconds_played"] == 200
