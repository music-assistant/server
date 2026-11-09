"""Tests for the rule picking between a provider's resume point and Music Assistant's own."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.auth import User, UserRole
from music_assistant_models.enums import MediaType, ProviderType
from music_assistant_models.media_items import Audiobook, ProviderMapping

from music_assistant.constants import CONF_LOG_LEVEL, DB_TABLE_MEDIA_PROGRESS
from music_assistant.controllers.music.helpers import ResumePoint, select_resume_point
from music_assistant.controllers.music.media.base import AudiobookSyncDetails
from music_assistant.controllers.webserver.helpers.auth_middleware import set_current_user
from music_assistant.mass import MusicAssistant
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

OLD = datetime(2026, 1, 1, tzinfo=UTC)
MID = datetime(2026, 1, 15, tzinfo=UTC)
NEW = datetime(2026, 2, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    ("provider_point", "playlog_point", "expected"),
    [
        (ResumePoint(False, 1000, None), None, "provider"),
        (None, ResumePoint(False, 1000, OLD), "playlog"),
        # the newer one wins, also when it is the lower one
        (ResumePoint(False, 1000, NEW), ResumePoint(False, 5000, OLD), "provider"),
        (ResumePoint(False, 5000, OLD), ResumePoint(False, 1000, NEW), "playlog"),
        # without a provider timestamp, or on equal ones, the furthest wins, finished first
        (ResumePoint(False, 5000, None), ResumePoint(False, 1000, NEW), "provider"),
        (ResumePoint(True, 0, None), ResumePoint(False, 5000, NEW), "provider"),
        (ResumePoint(False, 5000, NEW), ResumePoint(False, 1000, NEW), "provider"),
        (ResumePoint(False, 1000, None), ResumePoint(False, 1000, NEW), "playlog"),
    ],
)
def test_select_resume_point(
    provider_point: ResumePoint | None, playlog_point: ResumePoint | None, expected: str
) -> None:
    """The newer resume point wins, otherwise the furthest one."""
    result = select_resume_point(provider_point, playlog_point)

    assert result is {"provider": provider_point, "playlog": playlog_point}[expected]


@pytest.mark.parametrize(
    ("provider_point", "stored_point", "expected"),
    [
        (None, ResumePoint(False, 1000, MID), False),
        (ResumePoint(False, 1000, None), None, True),
        # the same progress again, also under a newer timestamp, settles
        (ResumePoint(False, 1000, NEW), ResumePoint(False, 1000, MID), False),
        # the playlog keeps whole seconds, so a provider a few hundred ms ahead settles too
        (ResumePoint(False, 12345, None), ResumePoint(False, 12000, MID), False),
        (ResumePoint(False, 5000, NEW), ResumePoint(False, 1000, MID), True),
        # where the stored progress wins, the sync must not update the book again and again
        (ResumePoint(False, 5000, OLD), ResumePoint(False, 1000, MID), False),
        (ResumePoint(False, 500, None), ResumePoint(False, 1000, MID), False),
        (ResumePoint(False, 5000, None), ResumePoint(False, 1000, MID), True),
    ],
)
def test_sync_updates_only_a_winning_resume_point(
    provider_point: ResumePoint | None, stored_point: ResumePoint | None, expected: bool
) -> None:
    """A library sync updates a book only for a resume point that differs and wins."""
    mapping = ProviderMapping(
        item_id="book_1", provider_domain="test_prov", provider_instance="test_prov"
    )
    book = Audiobook(
        item_id="book_1",
        provider="test_prov",
        name="Book",
        provider_mappings={mapping},
        fully_played=provider_point.fully_played if provider_point else None,
        resume_position_ms=provider_point.position_ms if provider_point else None,
        resume_position_timestamp=provider_point.timestamp if provider_point else None,
    )
    sync_details = AudiobookSyncDetails(
        item_id=1,
        date_added=OLD,
        provider_mappings={mapping},
        artist_links=frozenset(),
        authors=(),
        narrators=(),
        fully_played=stored_point.fully_played if stored_point else None,
        resume_position_ms=stored_point.position_ms if stored_point else None,
        resume_position_timestamp=stored_point.timestamp if stored_point else None,
    )

    assert sync_details.resume_point_changed(book) is expected


@pytest.mark.parametrize(
    ("provider_timestamp", "provider_seconds", "expected_seconds"),
    [(OLD, 90, 30), (NEW, 10, 10), (None, 10, 30), (None, 90, 90)],
)
async def test_library_sync_only_writes_a_winning_resume_point(
    music_mass: MusicAssistant,
    provider_timestamp: datetime | None,
    provider_seconds: int,
    expected_seconds: int,
) -> None:
    """A library sync overwrites the user's progress only where the provider's wins."""
    set_current_user(User(user_id="reader", username="reader", role=UserRole.USER))
    try:
        book = await music_mass.music.audiobooks.add_item_to_library(_book())
        await _add_progress(music_mass, book.item_id, "reader", 30)

        updated = await music_mass.music.audiobooks.update_item_in_library(
            book.item_id,
            _book(
                fully_played=False,
                resume_position_ms=provider_seconds * 1000,
                resume_position_timestamp=provider_timestamp,
            ),
        )
    finally:
        set_current_user(None)

    assert updated.resume_position_ms == expected_seconds * 1000


async def test_library_sync_reaches_every_user(music_mass: MusicAssistant) -> None:
    """A user whose own progress wins does not keep the provider's from the next user."""
    cast("AsyncMock", music_mass.webserver.auth.list_users).return_value = [
        User(user_id=user_id, username=user_id, role=UserRole.USER)
        for user_id in ("reader_a", "reader_b")
    ]
    book = await music_mass.music.audiobooks.add_item_to_library(_book())
    await _add_progress(music_mass, book.item_id, "reader_a", 30)
    await _add_progress(music_mass, book.item_id, "reader_b", 5)

    await music_mass.music.audiobooks.update_item_in_library(
        book.item_id, _book(fully_played=False, resume_position_ms=10_000)
    )

    rows = await music_mass.music.database.get_rows(
        DB_TABLE_MEDIA_PROGRESS, {"item_id": book.item_id, "provider": "library"}
    )
    assert {row["userid"]: row["seconds_played"] for row in rows} == {
        "reader_a": 30,
        "reader_b": 10,
    }


async def test_library_sync_compares_with_the_owners_progress(
    music_mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another user's newer progress does not keep the provider's from its owner."""
    owner = User(user_id="reader_a", username="reader_a", role=UserRole.USER)
    monkeypatch.setattr(music_mass.music, "_get_user_for_provider", AsyncMock(return_value=owner))
    provider = _provider(music_mass)
    provider.books = [_book()]
    (db_id,) = await provider._sync_library_audiobooks()
    await _add_progress(music_mass, str(db_id), "reader_a", 5, OLD)
    await _add_progress(music_mass, str(db_id), "reader_b", 30, NEW)
    provider.books = [
        _book(fully_played=False, resume_position_ms=10_000, resume_position_timestamp=MID)
    ]

    await provider._sync_library_audiobooks()

    rows = await music_mass.music.database.get_rows(
        DB_TABLE_MEDIA_PROGRESS, {"item_id": str(db_id), "provider": "library"}
    )
    assert {row["userid"]: row["seconds_played"] for row in rows} == {
        "reader_a": 10,
        "reader_b": 30,
    }


class _Provider(MusicProvider):
    books: list[Audiobook]

    async def get_library_audiobooks(self) -> AsyncGenerator[Audiobook]:
        for book in self.books:
            yield book


def _provider(music_mass: MusicAssistant) -> _Provider:
    manifest = MagicMock()
    manifest.type = ProviderType.MUSIC
    manifest.domain = "test_prov"
    config = MagicMock()
    config.instance_id = "test_prov"
    config.domain = "test_prov"
    config.get_value.side_effect = lambda key, default=None: (
        "GLOBAL" if key == CONF_LOG_LEVEL else default
    )
    return _Provider(music_mass, manifest, config)


def _book(**resume: Any) -> Audiobook:
    return Audiobook(
        item_id="book_1",
        provider="test_prov",
        name="Book",
        provider_mappings={
            ProviderMapping(
                item_id="book_1", provider_domain="test_prov", provider_instance="test_prov"
            )
        },
        **resume,
    )


async def _add_progress(
    music_mass: MusicAssistant,
    item_id: str,
    userid: str,
    seconds_played: int,
    timestamp: datetime = MID,
) -> None:
    await music_mass.music.database.insert(
        DB_TABLE_MEDIA_PROGRESS,
        {
            "item_id": item_id,
            "provider": "library",
            "media_type": MediaType.AUDIOBOOK.value,
            "name": "Book",
            "userid": userid,
            "seconds_played": seconds_played,
            "fully_played": False,
            "timestamp": int(timestamp.timestamp()),
        },
    )
