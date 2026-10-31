"""Integration tests for externally dated audiobook progress on a real SQLite database."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from music_assistant_models.auth import User, UserRole
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import Audiobook, ProviderMapping

from music_assistant.constants import DB_TABLE_MEDIA_PROGRESS, DB_TABLE_PLAY_HISTORY
from music_assistant.mass import MusicAssistant


@pytest.fixture
def book() -> Audiobook:
    """Return a library book shared by two Audible accounts."""
    return Audiobook(
        item_id="42",
        provider="library",
        name="Shared book",
        duration=3600,
        provider_mappings={
            ProviderMapping(item_id="A", provider_domain="audible", provider_instance="audible_a"),
            ProviderMapping(item_id="B", provider_domain="audible", provider_instance="audible_b"),
        },
    )


async def test_import_is_user_scoped_and_preserves_newer_progress(
    music_mass: MusicAssistant,
    book: Audiobook,
) -> None:
    """Older/equal remote events cannot replace newer progress or another user's row."""
    mass = music_mass
    owner = User(user_id="owner", username="owner", role=UserRole.USER)
    played_at = datetime(2026, 10, 1, tzinfo=UTC)
    with patch.object(mass.music, "_get_user_for_provider", AsyncMock(return_value=owner)):
        assert await mass.music.import_audiobook_progress(book, "audible_a", played_at, 100)
        assert not await mass.music.import_audiobook_progress(book, "audible_a", played_at, 80)
        assert not await mass.music.import_audiobook_progress(
            book,
            "audible_a",
            played_at - timedelta(seconds=1),
            50,
        )
        row = await mass.music.database.get_row(DB_TABLE_MEDIA_PROGRESS, {"userid": "owner"})
        assert row is not None
        assert row["timestamp"] == played_at.timestamp()
        assert row["seconds_played"] == 100
        assert not row["fully_played"]
        assert await mass.music.database.get_rows(DB_TABLE_PLAY_HISTORY) == []
        with patch.object(
            mass.music,
            "_get_user_for_provider",
            AsyncMock(
                return_value=User(
                    user_id="other",
                    username="other",
                    role=UserRole.USER,
                )
            ),
        ):
            assert await mass.music.import_audiobook_progress(book, "audible_b", played_at, 200)
    rows = await mass.music.database.get_rows(DB_TABLE_MEDIA_PROGRESS)
    assert {row["userid"]: row["seconds_played"] for row in rows} == {"owner": 100, "other": 200}


async def test_import_keeps_local_speed_and_sticky_user_initiated(
    music_mass: MusicAssistant,
    book: Audiobook,
) -> None:
    """New external progress keeps user preferences and never forwards playback reports."""
    mass = music_mass
    owner = User(user_id="owner", username="owner", role=UserRole.USER)
    await mass.music.database.insert(
        DB_TABLE_MEDIA_PROGRESS,
        {
            "item_id": book.item_id,
            "provider": "library",
            "media_type": "audiobook",
            "userid": "owner",
            "name": book.name,
            "timestamp": 1,
            "fully_played": False,
            "seconds_played": 10,
            "playback_speed": 1.5,
            "user_initiated": True,
        },
    )
    with (
        patch.object(mass.music, "_get_user_for_provider", AsyncMock(return_value=owner)),
        patch.object(mass.music, "_forward_play_report", AsyncMock()) as forward,
    ):
        assert await mass.music.import_audiobook_progress(
            book,
            "audible_a",
            datetime(2026, 10, 1, tzinfo=UTC),
            20,
        )
        forward.assert_not_called()
    row = await mass.music.database.get_row(DB_TABLE_MEDIA_PROGRESS, {"userid": "owner"})
    assert row is not None
    assert row["playback_speed"] == 1.5
    assert row["user_initiated"] == 1
    assert row["seconds_played"] == 20


async def test_import_rejects_unknown_owner_and_foreign_provider(
    music_mass: MusicAssistant,
    book: Audiobook,
) -> None:
    """Unresolved ownership and unrelated provider mappings cannot create any rows."""
    played_at = datetime(2026, 10, 1, tzinfo=UTC)
    with (
        patch.object(music_mass.music, "_get_user_for_provider", AsyncMock(return_value=None)),
        pytest.raises(ValueError, match="resolved user"),
    ):
        await music_mass.music.import_audiobook_progress(book, "audible_a", played_at, 10)
    with pytest.raises(ValueError, match="reporting provider"):
        await music_mass.music.import_audiobook_progress(book, "audible_c", played_at, 10)
    with pytest.raises(ValueError, match="timezone-aware"):
        await music_mass.music.import_audiobook_progress(
            book,
            "audible_a",
            played_at.replace(tzinfo=None),
            10,
        )
    assert await music_mass.music.database.get_rows(DB_TABLE_MEDIA_PROGRESS) == []


async def test_recently_played_opt_in_includes_partial_books_only(
    music_mass: MusicAssistant,
    book: Audiobook,
) -> None:
    """The opt-in includes positive audiobook progress without admitting partial tracks."""
    for item_id, media_type, seconds in [
        (book.item_id, MediaType.AUDIOBOOK, 100),
        ("zero", MediaType.AUDIOBOOK, 0),
        ("track", MediaType.TRACK, 100),
    ]:
        await music_mass.music.database.insert(
            DB_TABLE_MEDIA_PROGRESS,
            {
                "item_id": item_id,
                "provider": "library",
                "media_type": media_type.value,
                "userid": "owner",
                "name": item_id,
                "timestamp": 1,
                "fully_played": False,
                "seconds_played": seconds,
            },
        )
    assert await music_mass.music.recently_played(userid="owner") == []
    results = await music_mass.music.recently_played(
        userid="owner",
        include_in_progress_audiobooks=True,
    )
    assert [item.item_id for item in results] == [book.item_id]


async def test_import_respects_newer_direct_provider_progress(
    music_mass: MusicAssistant,
    book: Audiobook,
) -> None:
    """A newer local play through provider browsing also guards the library identity."""
    played_at = datetime(2026, 10, 1, tzinfo=UTC)
    await music_mass.music.database.insert(
        DB_TABLE_MEDIA_PROGRESS,
        {
            "item_id": "A",
            "provider": "audible_a",
            "media_type": "audiobook",
            "userid": "owner",
            "name": book.name,
            "timestamp": played_at.timestamp() + 1,
            "seconds_played": 200,
            "fully_played": False,
        },
    )
    with patch.object(
        music_mass.music,
        "_get_user_for_provider",
        AsyncMock(
            return_value=User(user_id="owner", username="owner", role=UserRole.USER),
        ),
    ):
        assert not await music_mass.music.import_audiobook_progress(
            book,
            "audible_a",
            played_at,
            100,
        )
    rows = await music_mass.music.database.get_rows(DB_TABLE_MEDIA_PROGRESS)
    assert len(rows) == 1
    assert rows[0]["provider"] == "audible_a"
    assert rows[0]["seconds_played"] == 200


@pytest.mark.parametrize(
    ("stored_provider", "stored_item"), [("library", "42"), ("audible_a", "A")]
)
async def test_remote_echo_preserves_completion_but_earlier_replay_clears_it(
    music_mass: MusicAssistant,
    book: Audiobook,
    stored_provider: str,
    stored_item: str,
) -> None:
    """A newer Audible echo must not undo completion; an earlier-position replay may."""
    played_at = datetime(2026, 10, 1, tzinfo=UTC)
    await music_mass.music.database.insert(
        DB_TABLE_MEDIA_PROGRESS,
        {
            "item_id": stored_item,
            "provider": stored_provider,
            "media_type": "audiobook",
            "userid": "owner",
            "name": book.name,
            "timestamp": played_at.timestamp() - 1,
            "fully_played": True,
            "seconds_played": 100,
        },
    )
    with (
        patch.object(
            music_mass.music,
            "_get_user_for_provider",
            AsyncMock(
                return_value=User(user_id="owner", username="owner", role=UserRole.USER),
            ),
        ),
        patch.object(music_mass.music, "_signal_playlog_updated") as signal,
    ):
        assert await music_mass.music.import_audiobook_progress(book, "audible_a", played_at, 100)
        row = await music_mass.music.database.get_row(
            DB_TABLE_MEDIA_PROGRESS, {"userid": "owner", "provider": "library"}
        )
        assert row is not None
        assert row["fully_played"]
        assert signal.call_args.kwargs["fully_played"] is True
        assert await music_mass.music.import_audiobook_progress(
            book,
            "audible_a",
            played_at + timedelta(seconds=1),
            10,
        )
        row = await music_mass.music.database.get_row(
            DB_TABLE_MEDIA_PROGRESS, {"userid": "owner", "provider": "library"}
        )
        assert row is not None
        assert not row["fully_played"]
        assert signal.call_args.kwargs["fully_played"] is False


@pytest.mark.parametrize(("position", "expected"), [(3539, False), (3540, True), (3600, True)])
async def test_import_recognizes_external_completion(
    music_mass: MusicAssistant, book: Audiobook, position: int, expected: bool
) -> None:
    """External completion uses the same final-minute rule as local audiobooks."""
    owner = User(user_id="owner", username="owner", role=UserRole.USER)
    with patch.object(music_mass.music, "_get_user_for_provider", AsyncMock(return_value=owner)):
        assert await music_mass.music.import_audiobook_progress(
            book, "audible_a", datetime(2026, 10, 1, tzinfo=UTC), position
        )
    row = await music_mass.music.database.get_row(DB_TABLE_MEDIA_PROGRESS, {"userid": "owner"})
    assert row is not None
    assert bool(row["fully_played"]) is expected
    assert await music_mass.music.database.get_rows(DB_TABLE_PLAY_HISTORY) == []
