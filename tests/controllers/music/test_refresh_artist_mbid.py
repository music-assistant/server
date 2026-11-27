"""Tests that refreshing an artist identifies it on MusicBrainz again."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

from music_assistant_models.enums import ExternalID
from music_assistant_models.media_items import Artist, ProviderMapping

from music_assistant.constants import VARIOUS_ARTISTS_MBID, VARIOUS_ARTISTS_NAME
from music_assistant.mass import MusicAssistant
from music_assistant.models.music_provider import MusicProvider

TIDAL = "tidal--test"
YTMUSIC = "ytmusic--test"
FS = "filesystem_local--test"
# MusicBrainz linked the Tidal artist of Georgia Satellites to Faces (support issue #7254)
FACES_MBID = "8c7a8406-f15a-408d-b0f6-23be813bdddc"
FACES_DISCOGS = "40425"
GEORGIA_SATELLITES_MBID = "8444928a-9292-4b69-ac50-3e9b44cfa8de"


def _mapping(instance_id: str, item_id: str = "15125") -> ProviderMapping:
    """Return a provider mapping of the artist on the given provider instance."""
    return ProviderMapping(
        item_id=item_id,
        provider_domain=instance_id.split("--", maxsplit=1)[0],
        provider_instance=instance_id,
    )


def _artist(
    instance_id: str = TIDAL,
    external_ids: set[tuple[ExternalID, str]] | None = None,
    name: str = "Georgia Satellites",
) -> Artist:
    """Return the artist as the given provider delivers it."""
    return Artist(
        item_id="15125",
        provider=instance_id,
        name=name,
        provider_mappings={_mapping(instance_id)},
        external_ids=external_ids or set(),
    )


def _provider(instance_id: str, is_streaming: bool) -> MagicMock:
    """Return a music provider of the given kind."""
    provider = MagicMock(spec=MusicProvider)
    provider.instance_id = instance_id
    provider.domain = instance_id.split("--", maxsplit=1)[0]
    provider.available = True
    type(provider).is_streaming_provider = PropertyMock(return_value=is_streaming)
    return provider


PROVIDERS = {
    TIDAL: _provider(TIDAL, is_streaming=True),
    YTMUSIC: _provider(YTMUSIC, is_streaming=True),
    FS: _provider(FS, is_streaming=False),
}


def _get_provider(instance_id: str, *_args: Any, **_kwargs: Any) -> MagicMock | None:
    """Return the test provider of the given instance id."""
    return PROVIDERS.get(instance_id)


async def _library_artist(mass: MusicAssistant, *instance_ids: str) -> Artist:
    """Add the artist with the wrong ids a lookup gave it, mapped to the given providers."""
    looked_up = {(ExternalID.MB_ARTIST, FACES_MBID), (ExternalID.DISCOGS, FACES_DISCOGS)}
    artist = await mass.music.artists.add_item_to_library(_artist(instance_ids[0], looked_up))
    if len(instance_ids) > 1:
        await mass.music.artists.add_provider_mappings(
            artist.item_id, [_mapping(x) for x in instance_ids[1:]]
        )
    return await mass.music.artists.get_library_item(artist.item_id)


async def _stored_ids(mass: MusicAssistant, item_id: str) -> set[tuple[ExternalID, str]]:
    """Return the external ids stored for a library artist."""
    return set((await mass.music.artists.get_library_item(item_id)).external_ids)


async def test_refresh_identifies_a_streaming_artist_again(music_mass: MusicAssistant) -> None:
    """A refresh hands the metadata update an artist without the ids a lookup gave it."""
    db_artist = await _library_artist(music_mass, TIDAL)
    seen_mbids: list[str | None] = []

    async def _update_metadata(item: Artist, **_kwargs: Any) -> Artist:
        seen_mbids.append(item.mbid)
        item.mbid = GEORGIA_SATELLITES_MBID
        await music_mass.music.artists.update_item_in_library(item.item_id, item)
        return item

    music_mass.metadata.update_metadata = AsyncMock(side_effect=_update_metadata)  # type: ignore[method-assign]
    with (
        patch.object(music_mass, "get_provider", side_effect=_get_provider),
        patch.object(music_mass.music, "_visible_provider_for", return_value=PROVIDERS[TIDAL]),
        patch.object(
            music_mass.music.artists, "get_provider_item", AsyncMock(return_value=_artist())
        ),
        patch.object(music_mass.music.artists, "match_providers", AsyncMock()),
        patch("music_assistant.controllers.music.controller.invalidate_listings", AsyncMock()),
    ):
        await music_mass.music.refresh_item(db_artist)

    assert seen_mbids == [None]
    assert await _stored_ids(music_mass, db_artist.item_id) == {
        (ExternalID.MB_ARTIST, GEORGIA_SATELLITES_MBID)
    }


async def test_streaming_artist_forgets_looked_up_ids(music_mass: MusicAssistant) -> None:
    """An artist only streaming services provide loses its MusicBrainz and Discogs ids."""
    db_artist = await _library_artist(music_mass, TIDAL, YTMUSIC)

    with patch.object(music_mass, "get_provider", side_effect=_get_provider):
        await music_mass.music.artists.forget_looked_up_ids(db_artist, _artist())

    assert db_artist.mbid is None
    assert db_artist.get_external_id(ExternalID.DISCOGS) is None
    assert await _stored_ids(music_mass, db_artist.item_id) == set()


async def test_artist_of_a_local_source_keeps_its_ids(music_mass: MusicAssistant) -> None:
    """An artist a local source provides may have its ids from tags, so they are kept."""
    db_artist = await _library_artist(music_mass, TIDAL, FS)
    stored = await _stored_ids(music_mass, db_artist.item_id)

    with patch.object(music_mass, "get_provider", side_effect=_get_provider):
        await music_mass.music.artists.forget_looked_up_ids(db_artist, _artist())

    assert db_artist.mbid == FACES_MBID
    assert await _stored_ids(music_mass, db_artist.item_id) == stored


async def test_artist_of_an_unknown_source_keeps_its_ids(music_mass: MusicAssistant) -> None:
    """An artist mapped to a provider that is not loaded keeps its ids, it may be a local one."""
    db_artist = await _library_artist(music_mass, TIDAL, "plex--gone")

    with patch.object(music_mass, "get_provider", side_effect=_get_provider):
        await music_mass.music.artists.forget_looked_up_ids(db_artist, _artist())

    assert db_artist.mbid == FACES_MBID


async def test_musicbrainz_id_the_provider_carries_is_kept(music_mass: MusicAssistant) -> None:
    """A MusicBrainz id the provider delivers itself keeps the artist's ids as they are."""
    db_artist = await _library_artist(music_mass, TIDAL)
    stored = await _stored_ids(music_mass, db_artist.item_id)
    provider_artist = _artist(external_ids={(ExternalID.MB_ARTIST, FACES_MBID)})

    with patch.object(music_mass, "get_provider", side_effect=_get_provider):
        await music_mass.music.artists.forget_looked_up_ids(db_artist, provider_artist)

    assert await _stored_ids(music_mass, db_artist.item_id) == stored


async def test_discogs_id_the_provider_carries_is_kept(music_mass: MusicAssistant) -> None:
    """A Discogs id the provider delivers itself stays when the MusicBrainz id goes."""
    db_artist = await _library_artist(music_mass, TIDAL)
    provider_artist = _artist(external_ids={(ExternalID.DISCOGS, FACES_DISCOGS)})

    with patch.object(music_mass, "get_provider", side_effect=_get_provider):
        await music_mass.music.artists.forget_looked_up_ids(db_artist, provider_artist)

    assert await _stored_ids(music_mass, db_artist.item_id) == {(ExternalID.DISCOGS, FACES_DISCOGS)}


async def test_various_artists_keeps_its_id(music_mass: MusicAssistant) -> None:
    """The Various Artists id is assigned by name, never by a lookup, so it stays."""
    artist = _artist(name=VARIOUS_ARTISTS_NAME)
    db_artist = await music_mass.music.artists.add_item_to_library(artist)

    with patch.object(music_mass, "get_provider", side_effect=_get_provider):
        await music_mass.music.artists.forget_looked_up_ids(db_artist, _artist())

    assert db_artist.mbid == VARIOUS_ARTISTS_MBID
