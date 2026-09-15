"""Tests for the bogus "None" provider mapping fix and its cleanup."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

from music_assistant_models.media_items import Artist, ItemMapping, ProviderMapping

from music_assistant.constants import DB_TABLE_PROVIDER_MAPPINGS

if TYPE_CHECKING:
    import pytest

    from music_assistant.mass import MusicAssistant


def test_artist_from_library_item_mapping_has_no_self_mapping(mass: MusicAssistant) -> None:
    """A library item mapping has no provider of its own, so it gets no provider mapping."""
    item = ItemMapping(item_id="42", provider="library", name="Test Artist")

    artist = mass.music.artists.artist_from_item_mapping(item)

    assert artist.provider == "library"
    assert artist.item_id == "42"
    assert artist.provider_mappings == set()


def test_album_from_library_item_mapping_has_no_self_mapping(mass: MusicAssistant) -> None:
    """A library item mapping has no provider of its own, so it gets no provider mapping."""
    item = ItemMapping(item_id="42", provider="library", name="Test Album")

    album = mass.music.albums.album_from_item_mapping(item)

    assert album.provider == "library"
    assert album.item_id == "42"
    assert album.provider_mappings == set()


def test_from_provider_item_mapping_keeps_mapping(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider item mapping is converted into a real, resolvable provider mapping."""
    monkeypatch.setattr(
        mass,
        "get_provider",
        lambda _: SimpleNamespace(domain="spotify", instance_id="spotify_1"),
    )
    expected = {
        ProviderMapping(item_id="abc", provider_domain="spotify", provider_instance="spotify_1")
    }

    artist_item = ItemMapping(item_id="abc", provider="spotify_1", name="Test Artist")
    album_item = ItemMapping(item_id="abc", provider="spotify_1", name="Test Album")

    assert mass.music.artists.artist_from_item_mapping(artist_item).provider_mappings == expected
    assert mass.music.albums.album_from_item_mapping(album_item).provider_mappings == expected


async def test_database_cleanup_removes_none_provider_mappings(mass: MusicAssistant) -> None:
    """The periodic cleanup drops the bogus "None" mapping and keeps the real one."""
    db_artist = await mass.music.artists.add_item_to_library(
        Artist(
            item_id="sp1",
            provider="spotify--EfGh",
            name="Test Artist",
            provider_mappings={
                ProviderMapping(
                    item_id="sp1", provider_domain="spotify", provider_instance="spotify--EfGh"
                )
            },
        )
    )
    await mass.music.database.insert(
        DB_TABLE_PROVIDER_MAPPINGS,
        {
            "media_type": "artist",
            "item_id": int(db_artist.item_id),
            "provider_domain": "None",
            "provider_instance": "None",
            "provider_item_id": db_artist.item_id,
        },
    )

    await mass.music._cleanup_database()

    rows = await mass.music.database.get_rows(
        DB_TABLE_PROVIDER_MAPPINGS,
        {"item_id": int(db_artist.item_id), "media_type": "artist"},
    )
    assert [r["provider_domain"] for r in rows] == ["spotify"]
