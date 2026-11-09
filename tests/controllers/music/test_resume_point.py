"""Tests for the rule picking between a provider's resume point and Music Assistant's own."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from music_assistant_models.auth import User, UserRole
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import Audiobook, ProviderMapping

from music_assistant.constants import DB_TABLE_MEDIA_PROGRESS
from music_assistant.controllers.music.helpers import ResumePoint, select_resume_point
from music_assistant.controllers.music.media.base import AudiobookSyncDetails
from music_assistant.controllers.webserver.helpers.auth_middleware import set_current_user
from music_assistant.mass import MusicAssistant

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
    mapping = ProviderMapping(
        item_id="book_1", provider_domain="test_prov", provider_instance="test_prov"
    )
    set_current_user(User(user_id="reader", username="reader", role=UserRole.USER))
    try:
        book = await music_mass.music.audiobooks.add_item_to_library(
            Audiobook(
                item_id="book_1", provider="test_prov", name="Book", provider_mappings={mapping}
            )
        )
        await music_mass.music.database.insert(
            DB_TABLE_MEDIA_PROGRESS,
            {
                "item_id": book.item_id,
                "provider": "library",
                "media_type": MediaType.AUDIOBOOK.value,
                "name": "Book",
                "userid": "reader",
                "seconds_played": 30,
                "fully_played": False,
                "timestamp": int(MID.timestamp()),
            },
        )

        updated = await music_mass.music.audiobooks.update_item_in_library(
            book.item_id,
            Audiobook(
                item_id="book_1",
                provider="test_prov",
                name="Book",
                provider_mappings={mapping},
                fully_played=False,
                resume_position_ms=provider_seconds * 1000,
                resume_position_timestamp=provider_timestamp,
            ),
        )
    finally:
        set_current_user(None)

    assert updated.resume_position_ms == expected_seconds * 1000
