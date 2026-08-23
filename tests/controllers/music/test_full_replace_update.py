"""Tests for the authoritative full_replace update mode used by the filesystem sidecar refresh."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Mapping

import pytest
from music_assistant_models.enums import ExternalID
from music_assistant_models.media_items import (
    Album,
    Artist,
    ProviderMapping,
    UniqueList,
)

from music_assistant.constants import DB_TABLE_ALBUMS, DB_TABLE_EXTERNAL_ID_LOOKUP
from music_assistant.controllers.music import MusicController
from music_assistant.mass import MusicAssistant

INSTANCE = "filesystem_local_1"
DOMAIN = "filesystem_local"
ALBUM_MBID = "aa11bb22-cc33-dd44-ee55-ff6677889900"
ARTIST_MBID = "11223344-5566-7788-99aa-bbccddeeff00"


@pytest.fixture
async def music(mass_minimal: MusicAssistant) -> AsyncGenerator[MusicController]:
    """Return a music controller with a real library database."""
    controller = MusicController(mass_minimal)
    mass_minimal.music = controller
    await controller._setup_database()
    yield controller
    if controller._database:
        await controller._database.close()


def _fs_album() -> Album:
    """Build a filesystem album carrying NFO-derived identity, ids, and metadata."""
    album = Album(
        item_id="Artist/Album",
        provider=INSTANCE,
        name="Album",
        version="Deluxe Edition",
        sort_name="Album",
        year=1999,
        external_ids={(ExternalID.MB_ALBUM, ALBUM_MBID), (ExternalID.BARCODE, "0724354283857")},
        provider_mappings={
            ProviderMapping(
                item_id="Artist/Album", provider_domain=DOMAIN, provider_instance=INSTANCE
            )
        },
        artists=UniqueList(
            [
                Artist(
                    item_id="Artist",
                    provider=INSTANCE,
                    name="Artist",
                    provider_mappings={
                        ProviderMapping(
                            item_id="Artist", provider_domain=DOMAIN, provider_instance=INSTANCE
                        )
                    },
                )
            ]
        ),
    )
    album.metadata.genres = {"Rock", "Pop"}
    album.metadata.description = "an nfo description"
    return album


async def _album_row(music: MusicController, db_id: int | str) -> Mapping[str, object]:
    """Return the stored album row."""
    row = await music.database.get_row(DB_TABLE_ALBUMS, {"item_id": int(db_id)})
    assert row is not None
    return row


async def _external_ids(music: MusicController, media_type: str, db_id: int | str) -> set[str]:
    """Return the external id types stored for a media item."""
    return {
        row["external_id_type"]
        for row in await music.database.get_rows(
            DB_TABLE_EXTERNAL_ID_LOOKUP, {"media_type": media_type, "item_id": int(db_id)}
        )
    }


async def test_full_replace_clears_album_nfo_owned_values(music: MusicController) -> None:
    """A full_replace album update clears version, year, external ids and empties metadata."""
    stored = await music.albums.add_item_to_library(_fs_album(), overwrite_existing=True)
    assert await _external_ids(music, "album", stored.item_id) == {
        str(ExternalID.MB_ALBUM),
        str(ExternalID.BARCODE),
    }

    # the NFO (and its barcode/mbid/version/year/genres) is removed: a full replace persists the
    # bare tag-only album verbatim, clearing every value the sidecar no longer provides
    cleared = Album(
        item_id="Artist/Album",
        provider=INSTANCE,
        name="Album",
        version="",
        sort_name=None,
        year=None,
        provider_mappings={
            ProviderMapping(
                item_id="Artist/Album", provider_domain=DOMAIN, provider_instance=INSTANCE
            )
        },
        artists=stored.artists,
    )
    await music.albums.update_item_in_library(stored.item_id, cleared, full_replace=True)

    row = await _album_row(music, stored.item_id)
    assert not row["version"]
    assert not row["year"]
    assert await _external_ids(music, "album", stored.item_id) == set()
    refreshed = await music.albums.get_library_item(stored.item_id)
    assert refreshed.external_ids == set()
    assert not refreshed.metadata.genres
    assert not refreshed.metadata.description


async def test_overwrite_without_full_replace_keeps_nfo_values(music: MusicController) -> None:
    """A plain overwrite update must not clear version, year, or external ids (existing behavior)."""
    stored = await music.albums.add_item_to_library(_fs_album(), overwrite_existing=True)
    cleared = Album(
        item_id="Artist/Album",
        provider=INSTANCE,
        name="Album",
        version="",
        year=None,
        provider_mappings={
            ProviderMapping(
                item_id="Artist/Album", provider_domain=DOMAIN, provider_instance=INSTANCE
            )
        },
        artists=stored.artists,
    )
    await music.albums.update_item_in_library(stored.item_id, cleared, overwrite=True)

    row = await _album_row(music, stored.item_id)
    assert row["version"] == "Deluxe Edition"
    assert row["year"] == 1999
    assert await _external_ids(music, "album", stored.item_id) == {
        str(ExternalID.MB_ALBUM),
        str(ExternalID.BARCODE),
    }


async def test_full_replace_clears_artist_nfo_owned_mbid(music: MusicController) -> None:
    """A full_replace artist update removes the final NFO-owned MusicBrainz id and sort name."""
    artist = Artist(
        item_id="Artist",
        provider=INSTANCE,
        name="Artist",
        sort_name="Artist, The",
        external_ids={(ExternalID.MB_ARTIST, ARTIST_MBID)},
        provider_mappings={
            ProviderMapping(item_id="Artist", provider_domain=DOMAIN, provider_instance=INSTANCE)
        },
    )
    artist.metadata.genres = {"Jazz"}
    stored = await music.artists.add_item_to_library(artist, overwrite_existing=True)
    assert await _external_ids(music, "artist", stored.item_id) == {str(ExternalID.MB_ARTIST)}

    cleared = Artist(
        item_id="Artist",
        provider=INSTANCE,
        name="Artist",
        sort_name=None,
        provider_mappings={
            ProviderMapping(item_id="Artist", provider_domain=DOMAIN, provider_instance=INSTANCE)
        },
    )
    await music.artists.update_item_in_library(stored.item_id, cleared, full_replace=True)

    assert await _external_ids(music, "artist", stored.item_id) == set()
    refreshed = await music.artists.get_library_item(stored.item_id)
    assert refreshed.mbid is None
    assert not refreshed.metadata.genres
