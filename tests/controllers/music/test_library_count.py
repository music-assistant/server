"""Tests that the library counts agree with the library listings."""

from __future__ import annotations

from unittest.mock import patch
from uuid import uuid4

import pytest
from music_assistant_models.auth import User, UserRole
from music_assistant_models.media_items import (
    Album,
    Artist,
    Audiobook,
    MediaItem,
    Playlist,
    Podcast,
    ProviderMapping,
    Radio,
    Track,
    UniqueList,
)

from music_assistant.mass import MusicAssistant

GET_CURRENT_USER = "music_assistant.controllers.music.media.base.get_current_user"
PROV = "prov_a_inst"


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
