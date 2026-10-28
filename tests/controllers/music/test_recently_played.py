"""Tests for MusicController.recently_played playlog queries."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Mapping
from typing import Any
from unittest.mock import Mock, patch

import pytest
from music_assistant_models.auth import User, UserRole
from music_assistant_models.config_entries import ProviderAccess
from music_assistant_models.enums import MediaType, ProviderSharing, ProviderType
from music_assistant_models.media_items import Audiobook, ItemMapping, ProviderMapping

from music_assistant.constants import DB_TABLE_MEDIA_PROGRESS, DB_TABLE_PROVIDER_MAPPINGS
from music_assistant.mass import MusicAssistant
from tests.common import set_music_source_access

GET_CURRENT_USER = "music_assistant.controllers.music.controller.get_current_user"


@pytest.fixture(name="mass")
def mass_fixture(music_mass: MusicAssistant) -> MusicAssistant:
    """Run on a library-only instance: these tests only touch the library."""
    return music_mass


async def _add_playlog_track(
    mass: MusicAssistant,
    item_id: str,
    timestamp: int,
    *,
    fully_played: bool = True,
    userid: str = "user-a",
    provider: str = "library",
) -> None:
    """Insert a single track row into the playlog."""
    await mass.music.database.insert(
        DB_TABLE_MEDIA_PROGRESS,
        {
            "item_id": item_id,
            "provider": provider,
            "media_type": MediaType.TRACK.value,
            "name": f"Track {item_id}",
            "timestamp": timestamp,
            "fully_played": fully_played,
            "seconds_played": 180,
            "userid": userid,
            "user_initiated": True,
        },
    )


async def _add_provider_mapping(
    mass: MusicAssistant,
    item_id: int,
    provider_instance: str,
    media_type: MediaType = MediaType.TRACK,
    *,
    available: bool = True,
) -> None:
    """Insert a provider mapping row for a library item."""
    await mass.music.database.insert(
        DB_TABLE_PROVIDER_MAPPINGS,
        {
            "media_type": media_type.value,
            "item_id": item_id,
            "provider_domain": provider_instance.rstrip("0123456789_"),
            "provider_instance": provider_instance,
            "provider_item_id": f"{provider_instance}-{item_id}",
            "available": available,
            "in_library": True,
        },
    )


def _loaded_music_provider(instance_id: str) -> Mock:
    """Create a stand-in for a loaded, available music provider instance."""
    return Mock(instance_id=instance_id, type=ProviderType.MUSIC, available=True)


async def test_recently_played_filters_by_played_after_timestamp(mass: MusicAssistant) -> None:
    """Only entries with timestamp >= played_after_timestamp are returned."""
    await _add_playlog_track(mass, "recent", timestamp=2000)
    await _add_playlog_track(mass, "old", timestamp=1000)

    result = await mass.music.recently_played(
        limit=0,
        media_types=[MediaType.TRACK],
        userid="user-a",
        played_after_timestamp=1500,
    )

    item_ids = {item.item_id for item in result}
    assert "recent" in item_ids
    assert "old" not in item_ids


async def test_recently_played_without_timestamp_returns_all(mass: MusicAssistant) -> None:
    """Existing callers (no played_after_timestamp) still get every entry."""
    await _add_playlog_track(mass, "recent", timestamp=2000)
    await _add_playlog_track(mass, "old", timestamp=1000)

    result = await mass.music.recently_played(
        limit=0,
        media_types=[MediaType.TRACK],
        userid="user-a",
    )

    item_ids = {item.item_id for item in result}
    assert {"recent", "old"} <= item_ids


async def test_recently_played_filters_direct_provider_entries(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Direct (non-library) playlog entries are matched against their own provider."""
    monkeypatch.setattr(
        mass.music, "get_active_provider_instances", lambda: ["spotify_1", "local_1"]
    )
    await _add_playlog_track(mass, "spotify-track", timestamp=2000, provider="spotify_1")
    await _add_playlog_track(mass, "local-track", timestamp=1999, provider="local_1")

    result = await mass.music.recently_played(
        limit=0, media_types=[MediaType.TRACK], userid="user-a", providers=["local_1"]
    )

    assert {item.item_id for item in result} == {"local-track"}


async def test_recently_played_multiple_providers_or_semantics(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Requesting several providers matches entries from any one of them."""
    monkeypatch.setattr(
        mass.music, "get_active_provider_instances", lambda: ["spotify_1", "local_1", "tidal_1"]
    )
    await _add_playlog_track(mass, "spotify-track", timestamp=2000, provider="spotify_1")
    await _add_playlog_track(mass, "local-track", timestamp=1999, provider="local_1")
    await _add_playlog_track(mass, "tidal-track", timestamp=1998, provider="tidal_1")

    result = await mass.music.recently_played(
        limit=0,
        media_types=[MediaType.TRACK],
        userid="user-a",
        providers=["spotify_1", "local_1"],
    )

    assert {item.item_id for item in result} == {"spotify-track", "local-track"}


async def test_recently_played_library_item_or_matches_any_mapping(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A library item mapped to Spotify and local remains when only local is requested."""
    monkeypatch.setattr(
        mass.music, "get_active_provider_instances", lambda: ["spotify_1", "local_1"]
    )
    await _add_provider_mapping(mass, item_id=1, provider_instance="spotify_1")
    await _add_provider_mapping(mass, item_id=1, provider_instance="local_1")
    await _add_playlog_track(mass, "1", timestamp=2000, provider="library")

    result = await mass.music.recently_played(
        limit=0, media_types=[MediaType.TRACK], userid="user-a", providers=["local_1"]
    )

    assert {item.item_id for item in result} == {"1"}


async def test_recently_played_excludes_unavailable_mapping(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mapping that is no longer available on the requested provider cannot match."""
    monkeypatch.setattr(
        mass.music, "get_active_provider_instances", lambda: ["spotify_1", "local_1"]
    )
    await _add_provider_mapping(mass, item_id=1, provider_instance="local_1")
    await _add_provider_mapping(mass, item_id=1, provider_instance="spotify_1", available=False)
    await _add_playlog_track(mass, "1", timestamp=2000, provider="library")

    result = await mass.music.recently_played(
        limit=0, media_types=[MediaType.TRACK], userid="user-a", providers=["spotify_1"]
    )

    assert result == []


async def test_recently_played_explicit_empty_providers_returns_no_items(
    mass: MusicAssistant,
) -> None:
    """An explicit empty provider list returns no items, distinct from omitting the filter."""
    await _add_playlog_track(mass, "recent", timestamp=2000)

    result = await mass.music.recently_played(
        limit=0, media_types=[MediaType.TRACK], userid="user-a", providers=[]
    )

    assert result == []


async def test_recently_played_combines_explicit_and_user_music_sources(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user's music sources narrow an explicit filter that would otherwise pass."""
    # get_active_provider_instances derives the "active" provider set from the loaded music
    # sources the user may see, so seed both as loaded and let the access records narrow them.
    for instance_id in ("spotify_1", "local_1"):
        monkeypatch.setitem(mass._providers, instance_id, _loaded_music_provider(instance_id))
    set_music_source_access(
        mass,
        {
            "local_1": ProviderAccess(owner="user-a", sharing=ProviderSharing.PRIVATE),
            "spotify_1": ProviderAccess(owner="user-b", sharing=ProviderSharing.PRIVATE),
        },
    )
    user_a = User(user_id="user-a", username="user-a", role=UserRole.USER)
    await _add_provider_mapping(mass, item_id=1, provider_instance="spotify_1")
    await _add_provider_mapping(mass, item_id=1, provider_instance="local_1")
    await _add_playlog_track(mass, "1", timestamp=2000, provider="library")

    with patch(GET_CURRENT_USER, return_value=user_a):
        result = await mass.music.recently_played(
            limit=0, media_types=[MediaType.TRACK], providers=["local_1"]
        )
    assert {item.item_id for item in result} == {"1"}

    # requesting a music source the user may not use must not leak the item back
    # in, even though the item does have a (restricted) mapping to it.
    with patch(GET_CURRENT_USER, return_value=user_a):
        result = await mass.music.recently_played(
            limit=0, media_types=[MediaType.TRACK], providers=["spotify_1"]
        )
    assert result == []


async def test_recently_played_provider_filter_applied_before_limit(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The provider filter runs in SQL before LIMIT, so filtered-out rows can't starve results."""
    monkeypatch.setattr(
        mass.music, "get_active_provider_instances", lambda: ["spotify_1", "local_1"]
    )
    # newer spotify entries (excluded by the filter) outnumber and outrank the older
    # local entries; if the filter were applied after LIMIT, they would crowd it out.
    for i in range(10):
        await _add_playlog_track(mass, f"spotify-{i}", timestamp=3000 - i, provider="spotify_1")
    for i in range(5):
        await _add_playlog_track(mass, f"local-{i}", timestamp=2000 - i, provider="local_1")

    result = await mass.music.recently_played(
        limit=5, media_types=[MediaType.TRACK], userid="user-a", providers=["local_1"]
    )

    assert {item.item_id for item in result} == {f"local-{i}" for i in range(5)}


async def test_recently_played_merges_plays_of_one_library_item(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plays logged under a library id and under a provider id it maps to are one entry."""
    monkeypatch.setattr(mass.music, "get_active_provider_instances", lambda: ["ytmusic_1"])
    await _add_provider_mapping(mass, item_id=1, provider_instance="ytmusic_1")
    # item 1 was played from the library, then from a provider listing (logged under the
    # provider id); the merge must happen before LIMIT so item 2 still makes the cut
    await _add_playlog_track(mass, "1", timestamp=1000)
    await _add_playlog_track(mass, "2", timestamp=2000)
    await _add_playlog_track(mass, "ytmusic_1-1", timestamp=3000, provider="ytmusic_1")

    result = await mass.music.recently_played(
        limit=2, media_types=[MediaType.TRACK], userid="user-a"
    )

    assert [(item.provider, item.item_id) for item in result] == [
        ("library", "1"),
        ("library", "2"),
    ]


async def test_recently_played_keeps_provider_items_outside_the_library(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider play with no library mapping is returned under its own provider id."""
    monkeypatch.setattr(mass.music, "get_active_provider_instances", lambda: ["ytmusic_1"])
    await _add_provider_mapping(mass, item_id=1, provider_instance="ytmusic_1")
    await _add_playlog_track(mass, "1", timestamp=1000)
    await _add_playlog_track(mass, "ytmusic_1-9", timestamp=2000, provider="ytmusic_1")

    result = await mass.music.recently_played(
        limit=0, media_types=[MediaType.TRACK], userid="user-a"
    )

    assert [(item.provider, item.item_id) for item in result] == [
        ("ytmusic_1", "ytmusic_1-9"),
        ("library", "1"),
    ]


async def test_recently_played_merges_plays_past_the_limit_in_rows(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Merging keeps reading rows until the limit is met, well past limit rows."""
    monkeypatch.setattr(
        mass.music, "get_active_provider_instances", lambda: ["ytmusic_1", "ytmusic_2"]
    )
    # every item was played three times (from the library and from two providers), so
    # 25 items need 75 rows
    for item_id in range(1, 31):
        timestamp = 1000 * item_id
        await _add_playlog_track(mass, str(item_id), timestamp=timestamp)
        for offset, provider_instance in enumerate(("ytmusic_1", "ytmusic_2"), start=1):
            await _add_provider_mapping(mass, item_id=item_id, provider_instance=provider_instance)
            await _add_playlog_track(
                mass,
                f"{provider_instance}-{item_id}",
                timestamp=timestamp + offset,
                provider=provider_instance,
            )

    result = await mass.music.recently_played(
        limit=25, media_types=[MediaType.TRACK], userid="user-a"
    )

    assert [(item.provider, item.item_id) for item in result] == [
        ("library", str(item_id)) for item_id in range(30, 5, -1)
    ]


async def test_recently_played_stops_reading_once_the_limit_is_met(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rows are read in one pass that is closed once limit distinct items are found."""
    for item_id in range(1, 101):
        await _add_playlog_track(mass, str(item_id), timestamp=1000 * item_id)
    iter_rows = mass.music.database.iter_rows_from_query
    queries: list[str] = []
    rows_read = 0
    closed = False

    async def _counting_iter_rows(
        query: str, params: dict[str, Any] | None = None
    ) -> AsyncGenerator[Mapping[str, Any]]:
        nonlocal rows_read, closed
        queries.append(query)
        try:
            async for row in iter_rows(query, params):
                rows_read += 1
                yield row
        finally:
            closed = True

    monkeypatch.setattr(mass.music.database, "iter_rows_from_query", _counting_iter_rows)

    result = await mass.music.recently_played(
        limit=5, media_types=[MediaType.TRACK], userid="user-a"
    )

    assert [item.item_id for item in result] == ["100", "99", "98", "97", "96"]
    assert len(queries) == 1
    assert "OFFSET" not in queries[0]
    assert rows_read == 5
    assert closed


async def _add_library_audiobook(mass: MusicAssistant, provider_instance: str) -> str:
    """Add an audiobook with a single provider mapping to the library, return its library id."""
    db_book = await mass.music.audiobooks.add_item_to_library(
        Audiobook(
            item_id="book-001",
            provider=provider_instance,
            name="A Library Audiobook",
            provider_mappings={
                ProviderMapping(
                    item_id="book-001",
                    provider_domain=provider_instance.rstrip("0123456789_"),
                    provider_instance=provider_instance,
                )
            },
        )
    )
    return str(db_book.item_id)


async def _add_playlog_audiobook(
    mass: MusicAssistant, item_id: str, provider: str, timestamp: int, userid: str
) -> None:
    """Insert a single fully played audiobook row into the playlog."""
    await mass.music.database.insert(
        DB_TABLE_MEDIA_PROGRESS,
        {
            "item_id": item_id,
            "provider": provider,
            "media_type": MediaType.AUDIOBOOK.value,
            "name": "A Library Audiobook",
            "timestamp": timestamp,
            "fully_played": True,
            "seconds_played": 3600,
            "userid": userid,
            "user_initiated": True,
        },
    )


@pytest.mark.parametrize(
    "logged_under",
    [("provider",), ("library", "provider")],
    ids=["provider-only", "library-and-provider"],
)
async def test_recently_played_entry_can_be_marked_unplayed(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch, logged_under: tuple[str, ...]
) -> None:
    """Marking a recently played entry unplayed removes it, whichever ids its plays used."""
    monkeypatch.setattr(mass.music, "get_active_provider_instances", lambda: ["abs_1"])
    user = User(user_id="user-a", username="user-a", role=UserRole.USER)
    library_id = await _add_library_audiobook(mass, "abs_1")
    ids = {"library": (library_id, "library"), "provider": ("book-001", "abs_1")}
    for timestamp, key in enumerate(logged_under, start=1):
        await _add_playlog_audiobook(mass, *ids[key], timestamp=timestamp, userid=user.user_id)

    (entry,) = await mass.music.recently_played(
        limit=10, media_types=[MediaType.AUDIOBOOK], userid=user.user_id
    )
    assert (entry.provider, entry.item_id) == ("library", library_id)
    with patch(GET_CURRENT_USER, return_value=user):
        await mass.music.mark_item_unplayed(entry)

    assert not await mass.music.recently_played(
        limit=10, media_types=[MediaType.AUDIOBOOK], userid=user.user_id
    )


async def test_mark_unplayed_by_provider_id_clears_the_library_play(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unmarking an item by a provider id also clears the play logged under its library id."""
    monkeypatch.setattr(mass.music, "get_active_provider_instances", lambda: ["abs_1"])
    user = User(user_id="user-a", username="user-a", role=UserRole.USER)
    library_id = await _add_library_audiobook(mass, "abs_1")
    await _add_playlog_audiobook(mass, library_id, "library", timestamp=1, userid=user.user_id)

    with patch(GET_CURRENT_USER, return_value=user):
        await mass.music.mark_item_unplayed(
            ItemMapping(
                item_id="book-001",
                provider="abs_1",
                name="A Library Audiobook",
                media_type=MediaType.AUDIOBOOK,
            )
        )

    assert not await mass.music.database.get_rows(
        DB_TABLE_MEDIA_PROGRESS, {"media_type": MediaType.AUDIOBOOK.value}
    )
