"""Tests that refreshing an artist identifies it again once MusicBrainz links it elsewhere."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

from music_assistant_models.enums import ExternalID
from music_assistant_models.errors import ResourceTemporarilyUnavailable
from music_assistant_models.media_items import Artist, ProviderMapping, SearchResults

from music_assistant.constants import VARIOUS_ARTISTS_MBID, VARIOUS_ARTISTS_NAME
from music_assistant.controllers.music.media.artists import ArtistsController
from music_assistant.mass import MusicAssistant
from music_assistant.models.music_provider import MusicProvider

TIDAL = "tidal--test"
YTMUSIC = "ytmusic--test"
SPOTIFY = "spotify--test"
FS = "filesystem_local--test"
# MusicBrainz linked the Tidal artist of Georgia Satellites to Faces (support issue #7254)
FACES_MBID = "8c7a8406-f15a-408d-b0f6-23be813bdddc"
FACES_DISCOGS = "40425"
GEORGIA_SATELLITES_MBID = "8444928a-9292-4b69-ac50-3e9b44cfa8de"


def _mapping(instance_id: str, item_id: str = "15125", in_library: bool = False) -> ProviderMapping:
    """Return a provider mapping of the artist on the given provider instance."""
    return ProviderMapping(
        item_id=item_id,
        provider_domain=instance_id.split("--", maxsplit=1)[0],
        provider_instance=instance_id,
        in_library=in_library,
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
    SPOTIFY: _provider(SPOTIFY, is_streaming=True),
    FS: _provider(FS, is_streaming=False),
}


@contextmanager
def _musicbrainz(
    moved: bool | Exception = True, old_links: list[ProviderMapping] | None = None
) -> Iterator[MagicMock]:
    """
    Serve the test providers and a MusicBrainz that may link the Tidal artist elsewhere now.

    :param moved: Whether MusicBrainz now links the Tidal artist to another artist than the
        stored one, or the error MusicBrainz answers with.
    :param old_links: The provider mappings the stored (wrong) MusicBrainz artist links to.
    """
    musicbrainz = MagicMock()
    musicbrainz.get_artist_left_by_link = AsyncMock(
        side_effect=moved if isinstance(moved, Exception) else None,
        return_value=MagicMock(relations=[]) if moved is True else None,
    )
    providers = {**PROVIDERS, "musicbrainz": musicbrainz}
    with (
        patch.object(
            ArtistsController, "_linked_provider_mappings", AsyncMock(return_value=old_links or [])
        ),
        patch(
            "music_assistant.mass.MusicAssistant.get_provider",
            side_effect=lambda instance_id, *_args, **_kwargs: providers.get(instance_id),
        ),
    ):
        yield musicbrainz


async def _library_artist(mass: MusicAssistant, *mappings: ProviderMapping) -> Artist:
    """Add the artist with the wrong ids a lookup gave it, from Tidal and the given mappings."""
    looked_up = {(ExternalID.MB_ARTIST, FACES_MBID), (ExternalID.DISCOGS, FACES_DISCOGS)}
    artist = await mass.music.artists.add_item_to_library(_artist(TIDAL, looked_up))
    if mappings:
        await mass.music.artists.add_provider_mappings(artist.item_id, list(mappings))
    return await mass.music.artists.get_library_item(artist.item_id)


async def _stored_ids(mass: MusicAssistant, item_id: str) -> set[tuple[ExternalID, str]]:
    """Return the external ids stored for a library artist."""
    return set((await mass.music.artists.get_library_item(item_id)).external_ids)


async def test_refresh_identifies_a_streaming_artist_again(music_mass: MusicAssistant) -> None:
    """A refresh hands the metadata update an artist MusicBrainz no longer confirms, without ids."""
    db_artist = await _library_artist(music_mass)
    seen_mbids: list[str | None] = []

    async def _update_metadata(item: Artist, **_kwargs: Any) -> Artist:
        seen_mbids.append(item.mbid)
        item.mbid = GEORGIA_SATELLITES_MBID
        await music_mass.music.artists.update_item_in_library(item.item_id, item)
        return item

    music_mass.metadata.update_metadata = AsyncMock(side_effect=_update_metadata)  # type: ignore[method-assign]
    with (
        _musicbrainz(),
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


async def test_refresh_through_a_substitute_keeps_the_ids(music_mass: MusicAssistant) -> None:
    """A substitute found by name, when no own source answers, never resets the artist."""
    db_artist = await _library_artist(music_mass)
    substitute = _artist(YTMUSIC)
    forget = AsyncMock(return_value=False)

    music_mass.metadata.update_metadata = AsyncMock()  # type: ignore[method-assign]
    with (
        _musicbrainz(),
        patch.object(music_mass.music, "_visible_provider_for", return_value=None),
        patch.object(
            music_mass.music, "search", AsyncMock(return_value=SearchResults(artists=[substitute]))
        ),
        patch.object(
            music_mass.music.artists, "get_provider_item", AsyncMock(return_value=substitute)
        ),
        patch.object(music_mass.music.artists, "match_providers", AsyncMock()),
        patch.object(music_mass.music.artists, "forget_disputed_musicbrainz_id", forget),
        patch("music_assistant.controllers.music.controller.invalidate_listings", AsyncMock()),
    ):
        await music_mass.music.refresh_item(db_artist)

    forget.assert_not_called()


async def test_artist_musicbrainz_still_links_keeps_its_ids(music_mass: MusicAssistant) -> None:
    """While MusicBrainz does not link the provider artist elsewhere, nothing changes."""
    db_artist = await _library_artist(music_mass)
    stored = await _stored_ids(music_mass, db_artist.item_id)

    with _musicbrainz(moved=False) as musicbrainz:
        changed = await music_mass.music.artists.forget_disputed_musicbrainz_id(
            db_artist, _artist()
        )

    assert not changed
    assert await _stored_ids(music_mass, db_artist.item_id) == stored
    musicbrainz.get_artist_left_by_link.assert_awaited_once_with(_mapping(TIDAL), FACES_MBID)


async def test_relinked_artist_loses_ids_and_links_of_the_old_artist(
    music_mass_with_audio_analysis: MusicAssistant,
) -> None:
    """The ids go, and so do the links MusicBrainz gave from the old artist."""
    music_mass = music_mass_with_audio_analysis
    faces_spotify = _mapping(SPOTIFY, "faces-spotify")
    own_ytmusic = _mapping(YTMUSIC, "UCFtQ_ao2P8LoMcvJLX2qf3Q")
    db_artist = await _library_artist(music_mass, faces_spotify, own_ytmusic)

    cast("MagicMock", music_mass.metadata).link_providers_via_musicbrainz = True
    with _musicbrainz(old_links=[faces_spotify, _mapping(TIDAL)]):
        changed = await music_mass.music.artists.forget_disputed_musicbrainz_id(
            db_artist, _artist()
        )

    stored = await music_mass.music.artists.get_library_item(db_artist.item_id)
    assert changed
    assert stored.external_ids == set()
    assert {x.provider_instance for x in stored.provider_mappings} == {TIDAL, YTMUSIC}


async def test_link_in_a_library_is_kept(music_mass: MusicAssistant) -> None:
    """A link the user has in a provider's library stays, even when the old artist lists it."""
    spotify = _mapping(SPOTIFY, "faces-spotify", in_library=True)
    db_artist = await _library_artist(music_mass, spotify)

    with _musicbrainz(old_links=[spotify]):
        await music_mass.music.artists.forget_disputed_musicbrainz_id(db_artist, _artist())

    stored = await music_mass.music.artists.get_library_item(db_artist.item_id)
    assert stored.mbid is None
    assert {x.provider_instance for x in stored.provider_mappings} == {TIDAL, SPOTIFY}


async def test_links_stay_when_linking_through_musicbrainz_is_off(
    music_mass: MusicAssistant,
) -> None:
    """Without linking through MusicBrainz no link came from it, so only the ids go."""
    spotify = _mapping(SPOTIFY, "faces-spotify")
    db_artist = await _library_artist(music_mass, spotify)

    cast("MagicMock", music_mass.metadata).link_providers_via_musicbrainz = False
    with _musicbrainz(old_links=[spotify]):
        await music_mass.music.artists.forget_disputed_musicbrainz_id(db_artist, _artist())

    stored = await music_mass.music.artists.get_library_item(db_artist.item_id)
    assert stored.mbid is None
    assert {x.provider_instance for x in stored.provider_mappings} == {TIDAL, SPOTIFY}


async def test_musicbrainz_unreachable_keeps_everything(music_mass: MusicAssistant) -> None:
    """When MusicBrainz cannot be asked, the artist keeps its ids and links."""
    db_artist = await _library_artist(music_mass)
    stored = await _stored_ids(music_mass, db_artist.item_id)

    with _musicbrainz(ResourceTemporarilyUnavailable("down")):
        changed = await music_mass.music.artists.forget_disputed_musicbrainz_id(
            db_artist, _artist()
        )

    assert not changed
    assert await _stored_ids(music_mass, db_artist.item_id) == stored


async def test_artist_of_a_local_source_keeps_its_ids(music_mass: MusicAssistant) -> None:
    """An artist a local source provides may have its ids from tags, so they are kept."""
    db_artist = await _library_artist(music_mass, _mapping(FS))
    stored = await _stored_ids(music_mass, db_artist.item_id)

    with _musicbrainz() as musicbrainz:
        await music_mass.music.artists.forget_disputed_musicbrainz_id(db_artist, _artist())

    assert await _stored_ids(music_mass, db_artist.item_id) == stored
    musicbrainz.get_artist_left_by_link.assert_not_called()


async def test_artist_of_an_unknown_source_keeps_its_ids(music_mass: MusicAssistant) -> None:
    """An artist mapped to a provider that is not loaded keeps its ids, it may be a local one."""
    db_artist = await _library_artist(music_mass, _mapping("plex--gone"))
    stored = await _stored_ids(music_mass, db_artist.item_id)

    with _musicbrainz() as musicbrainz:
        await music_mass.music.artists.forget_disputed_musicbrainz_id(db_artist, _artist())

    assert await _stored_ids(music_mass, db_artist.item_id) == stored
    musicbrainz.get_artist_left_by_link.assert_not_called()


async def test_musicbrainz_id_the_provider_carries_is_kept(music_mass: MusicAssistant) -> None:
    """A MusicBrainz id the provider delivers itself keeps the artist's ids as they are."""
    db_artist = await _library_artist(music_mass)
    stored = await _stored_ids(music_mass, db_artist.item_id)
    provider_artist = _artist(external_ids={(ExternalID.MB_ARTIST, FACES_MBID)})

    with _musicbrainz():
        await music_mass.music.artists.forget_disputed_musicbrainz_id(db_artist, provider_artist)

    assert await _stored_ids(music_mass, db_artist.item_id) == stored


async def test_discogs_id_the_provider_carries_is_kept(music_mass: MusicAssistant) -> None:
    """A Discogs id the provider delivers itself stays when the MusicBrainz id goes."""
    db_artist = await _library_artist(music_mass)
    provider_artist = _artist(external_ids={(ExternalID.DISCOGS, FACES_DISCOGS)})

    with _musicbrainz():
        await music_mass.music.artists.forget_disputed_musicbrainz_id(db_artist, provider_artist)

    assert await _stored_ids(music_mass, db_artist.item_id) == {(ExternalID.DISCOGS, FACES_DISCOGS)}


async def test_various_artists_keeps_its_id(music_mass: MusicAssistant) -> None:
    """The Various Artists id is assigned by name, never by a lookup, so it stays."""
    artist = _artist(name=VARIOUS_ARTISTS_NAME)
    db_artist = await music_mass.music.artists.add_item_to_library(artist)

    with _musicbrainz() as musicbrainz:
        await music_mass.music.artists.forget_disputed_musicbrainz_id(db_artist, _artist())

    stored = await music_mass.music.artists.get_library_item(db_artist.item_id)
    assert stored.mbid == VARIOUS_ARTISTS_MBID
    musicbrainz.get_artist_left_by_link.assert_not_called()
