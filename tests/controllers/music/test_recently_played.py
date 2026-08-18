"""Tests for MusicController.recently_played playlog queries."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import Mock, patch

from music_assistant_models.enums import MediaType

from music_assistant.constants import DB_TABLE_PLAYLOG, DB_TABLE_PROVIDER_MAPPINGS
from music_assistant.mass import MusicAssistant

if TYPE_CHECKING:
    import pytest

GET_CURRENT_USER = "music_assistant.controllers.music.controller.get_current_user"


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
        DB_TABLE_PLAYLOG,
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


async def test_recently_played_combines_explicit_and_user_provider_filter(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user's provider_filter narrows an explicit filter that would otherwise pass."""
    # get_active_provider_instances already applies the user's provider_filter internally, so a
    # user restricted to local_1 never sees spotify_1 in the "active" provider set.
    monkeypatch.setattr(mass.music, "get_active_provider_instances", lambda: ["local_1"])
    await _add_provider_mapping(mass, item_id=1, provider_instance="spotify_1")
    await _add_provider_mapping(mass, item_id=1, provider_instance="local_1")
    await _add_playlog_track(mass, "1", timestamp=2000, provider="library")

    with patch(GET_CURRENT_USER, return_value=Mock(user_id="user-a", provider_filter=["local_1"])):
        result = await mass.music.recently_played(
            limit=0, media_types=[MediaType.TRACK], providers=["local_1"]
        )
    assert {item.item_id for item in result} == {"1"}

    # requesting a provider the user isn't permitted to use must not leak the item back
    # in, even though the item does have a (permission-restricted) mapping to it.
    with patch(GET_CURRENT_USER, return_value=Mock(user_id="user-a", provider_filter=["local_1"])):
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
