"""Tests that the library counts agree with the library listings."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from music_assistant_models.auth import User, UserRole
from music_assistant_models.media_items import (
    Album,
    Artist,
    Audiobook,
    Genre,
    MediaItem,
    Playlist,
    Podcast,
    ProviderMapping,
    Radio,
    Track,
    UniqueList,
)

from music_assistant.constants import DB_TABLE_GENRES
from music_assistant.mass import MusicAssistant

GET_CURRENT_USER = "music_assistant.controllers.music.media.base.get_current_user"
PROV = "prov_a_inst"
# the user whose likes the genre count test checks
FAN = User(user_id="genre-fan", username="genre-fan", role=UserRole.USER)


def _mapping(in_library: bool) -> ProviderMapping:
    """Create a provider mapping with a unique provider item id."""
    return ProviderMapping(
        item_id=uuid4().hex,
        provider_domain=PROV.removesuffix("_inst"),
        provider_instance=PROV,
        in_library=in_library,
    )


def _item[T: MediaItem](cls: type[T], name: str, in_library: bool) -> T:
    """Create a provider item of the given type with a single mapping."""
    mapping = _mapping(in_library)
    return cls(
        item_id=mapping.item_id,
        provider=PROV,
        name=name,
        provider_mappings={mapping},
    )


@pytest.fixture
async def library_mass(music_mass: MusicAssistant) -> MusicAssistant:
    """
    Return a database-only instance holding items in and out of the library.

    The albums and artists of the tracks are stored for those tracks only, as the
    library sync does, so they are not in the library themselves.
    """
    mass = music_mass
    music = mass.music
    await music.artists.add_item_to_library(_item(Artist, "Library Artist", True))
    await music.albums.add_item_to_library(_item(Album, "Library Album", True))
    for name, in_library in (("Library Track", True), ("Removed Track", False)):
        track = _item(Track, name, in_library)
        track.album = _item(Album, f"{name} Album", False)
        track.artists = UniqueList([_item(Artist, f"{name} Artist", False)])
        await music.tracks.add_item_to_library(track)
    for prefix, in_library in (("Library", True), ("Removed", False)):
        await music.playlists.add_item_to_library(_item(Playlist, f"{prefix} Playlist", in_library))
        await music.radio.add_item_to_library(_item(Radio, f"{prefix} Radio", in_library))
        await music.podcasts.add_item_to_library(_item(Podcast, f"{prefix} Podcast", in_library))
        await music.audiobooks.add_item_to_library(
            _item(Audiobook, f"{prefix} Audiobook", in_library)
        )
    await music.database.commit()
    return mass


@pytest.mark.parametrize(
    "media_type", ["tracks", "albums", "artists", "playlists", "radio", "podcasts", "audiobooks"]
)
@pytest.mark.parametrize(
    "user",
    [None, User(user_id="admin", username="admin", role=UserRole.ADMIN)],
    ids=["anonymous", "unrestricted"],
)
async def test_library_count_matches_listing(
    library_mass: MusicAssistant, media_type: str, user: User | None
) -> None:
    """The count only includes the items the library listing shows."""
    controller = getattr(library_mass.music, media_type)
    with patch(GET_CURRENT_USER, return_value=user):
        items = await controller.library_items(limit=500)
        count = await controller.library_count()
    # guard against a vacuous pass: the listing must hold exactly the one library item
    assert len(items) == 1
    assert items[0].name.startswith("Library ")
    assert count == len(items)


@pytest.mark.parametrize("favorite_only", [False, True])
async def test_genre_count_matches_default_listing(
    music_mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch, favorite_only: bool
) -> None:
    """The genre count leaves out the excluded genres and those the default listing hides."""
    # the database-only server runs no cache database
    monkeypatch.setattr(music_mass.cache, "delete", AsyncMock())
    genres = music_mass.music.genres
    shown, excluded = await genres.library_items(limit=2)
    custom = await genres.add_item_to_library(
        Genre(item_id="0", provider="library", name="Custom Genre", provider_mappings=set())
    )
    # the model derives a translation key from the name, so store the genre without one
    await music_mass.music.database.update(
        DB_TABLE_GENRES, {"item_id": int(custom.item_id)}, {"translation_key": None}
    )
    for genre in (shown, excluded, custom):
        await genres.set_favorite(genre.item_id, True, [FAN.user_id])
    await genres.remove_item_from_library(excluded.item_id)
    with patch(GET_CURRENT_USER, return_value=FAN):
        items = await genres.library_items(favorite=favorite_only or None, limit=1000)
        count = await genres.library_count(favorite_only=favorite_only)
    names = {item.name for item in items}
    # guard against a vacuous pass: only the listing filter keeps these two out
    assert shown.name in names
    assert excluded.name not in names
    assert custom.name not in names
    if favorite_only:
        assert len(items) == 1
    assert count == len(items)
