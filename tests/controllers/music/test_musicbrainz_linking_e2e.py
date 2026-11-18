"""End-to-end test: a library album is identified and linked through MusicBrainz."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, PropertyMock, patch

import pytest
from music_assistant_models.config_entries import ProviderConfig
from music_assistant_models.enums import (
    AlbumType,
    ArtistType,
    ExternalID,
    MediaType,
    ProviderType,
)
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import Album, Artist, ProviderMapping, Track, UniqueList
from music_assistant_models.provider import ProviderManifest

from music_assistant.constants import (
    DB_TABLE_ALBUM_TRACKS,
    DB_TABLE_EXTERNAL_ID_LOOKUP,
    DB_TABLE_PROVIDER_MAPPINGS,
)
from music_assistant.controllers.metadata import MetaDataController
from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers.musicbrainz.models import (
    MusicBrainzArtist,
    MusicBrainzMedia,
    MusicBrainzRecording,
    MusicBrainzRelation,
    MusicBrainzRelease,
    MusicBrainzReleaseGroup,
    MusicBrainzTrack,
    MusicBrainzUrl,
)
from music_assistant.providers.musicbrainz.provider import IdentifiedArtist, MusicbrainzProvider

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

RELEASE_ID = "5d9c6e8a-1c3b-4f2e-9a7d-2b8c4e6f0a11"
RELEASE_GROUP_ID = "7f1e2d3c-4b5a-4c6d-8e9f-0a1b2c3d4e55"
RECORDING_IDS = ("0c1d2e3f-4a5b-4c6d-9e8f-7a6b5c4d3e22", "9e8d7c6b-5a4f-4e3d-8c2b-1a0f9e8d7c33")
ISRCS = ("GBSTK0700001", "GBSTK0700002")
BARCODE = "0634904032463"
TIDAL_INSTANCE = "tidal--test"
TIDAL_ALBUM_ID = "79280548"
ARTIST_MBID = "0fe841c1-0d1a-4d2c-9a4e-3b7a1f6c2e10"


class _TidalStub(MusicProvider):
    """A loaded Tidal instance that lists the tracks of the one album MusicBrainz links."""

    def __init__(self, mass: MusicAssistant) -> None:
        manifest = ProviderManifest(
            type=ProviderType.MUSIC,
            domain="tidal",
            name="Tidal",
            description="Tidal",
            codeowners=["@music-assistant"],
        )
        config = ProviderConfig(
            values={},
            type=ProviderType.MUSIC,
            domain="tidal",
            instance_id=TIDAL_INSTANCE,
            name="Tidal",
        )
        super().__init__(mass, manifest, config)
        self.available = True

    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Return the album's tracks, carrying their ISRCs."""
        assert prov_album_id == TIDAL_ALBUM_ID
        return [
            Track(
                item_id=f"td-{number}",
                provider=TIDAL_INSTANCE,
                name=f"Track {number}",
                duration=200 + number,
                disc_number=1,
                track_number=number,
                external_ids={(ExternalID.ISRC, ISRCS[number - 1])},
                provider_mappings={
                    ProviderMapping(
                        item_id=f"td-{number}",
                        provider_domain="tidal",
                        provider_instance=TIDAL_INSTANCE,
                    )
                },
            )
            for number in (1, 2)
        ]


def _spotify(item_id: str) -> ProviderMapping:
    """Return the library mapping of an item that came in from Spotify."""
    return ProviderMapping(
        item_id=item_id,
        provider_domain="spotify",
        provider_instance="spotify--test",
        in_library=True,
    )


def _release() -> MusicBrainzRelease:
    """Return the resolved release: linked to Tidal, two tracks with recordings and ISRCs."""
    return MusicBrainzRelease(
        id=RELEASE_ID,
        title="In Rainbows",
        status="Official",
        date="2007-10-10",
        barcode=BARCODE,
        release_group=MusicBrainzReleaseGroup(
            id=RELEASE_GROUP_ID,
            title="In Rainbows",
            primary_type="Album",
            first_release_date="2007-10-10",
        ),
        relations=[
            MusicBrainzRelation(
                type="free streaming",
                url=MusicBrainzUrl(resource=f"https://tidal.com/album/{TIDAL_ALBUM_ID}"),
            )
        ],
        media=[
            MusicBrainzMedia(
                position=1,
                track_count=2,
                tracks=[
                    MusicBrainzTrack(
                        id=f"t-{number}",
                        number=str(number),
                        title=f"Track {number}",
                        length=(200 + number) * 1000,
                        position=number,
                        recording=MusicBrainzRecording(
                            id=RECORDING_IDS[number - 1],
                            title=f"Track {number}",
                            length=(200 + number) * 1000,
                            isrcs=[ISRCS[number - 1]],
                        ),
                    )
                    for number in (1, 2)
                ],
            )
        ],
    )


async def _add_spotify_album(mass: MusicAssistant) -> Album:
    """Store an album that came in from Spotify with its two tracks and return it."""
    artist = await mass.music.artists.add_item_to_library(
        Artist(
            item_id="0",
            provider="library",
            name="Radiohead",
            provider_mappings={_spotify("sp-artist")},
        )
    )
    album = await mass.music.albums.add_item_to_library(
        Album(
            item_id="0",
            provider="library",
            name="In Rainbows",
            album_type=AlbumType.ALBUM,
            artists=UniqueList([artist]),
            provider_mappings={_spotify("sp-album")},
            external_ids={(ExternalID.BARCODE, BARCODE)},
        )
    )
    for number in (1, 2):
        await mass.music.tracks.add_item_to_library(
            Track(
                item_id="0",
                provider="library",
                name=f"Track {number}",
                duration=200 + number,
                disc_number=1,
                track_number=number,
                artists=UniqueList([artist]),
                album=album,
                provider_mappings={_spotify(f"sp-{number}")},
            )
        )
    return album


async def test_album_is_identified_and_linked_through_musicbrainz(mass: MusicAssistant) -> None:
    """MusicBrainz ids land on the album and its tracks, and all of them get a Tidal mapping."""
    assert mass.get_provider("musicbrainz") is not None
    tidal = _TidalStub(mass)
    mass._providers[TIDAL_INSTANCE] = tidal
    album = await _add_spotify_album(mass)

    with (
        patch.object(MusicbrainzProvider, "resolve_release", AsyncMock(return_value=_release())),
        # the online metadata providers are not what is under test
        patch.object(MetaDataController, "providers", new_callable=PropertyMock, return_value=[]),
    ):
        await mass.metadata._update_album_metadata(album, force_refresh=True)

    album_id = int(album.item_id)
    album_mappings = await mass.music.database.get_rows(
        DB_TABLE_PROVIDER_MAPPINGS,
        {"media_type": MediaType.ALBUM.value, "item_id": album_id, "provider_domain": "tidal"},
    )
    assert [(row["provider_item_id"], row["in_library"]) for row in album_mappings] == [
        (TIDAL_ALBUM_ID, 0)
    ]
    album_ids = await mass.music.database.get_rows(
        DB_TABLE_EXTERNAL_ID_LOOKUP, {"media_type": MediaType.ALBUM.value, "item_id": album_id}
    )
    assert {(row["external_id_type"], row["external_id"]) for row in album_ids} >= {
        (ExternalID.MB_ALBUM.value, RELEASE_ID),
        (ExternalID.MB_RELEASEGROUP.value, RELEASE_GROUP_ID),
    }

    tracks = sorted(
        await mass.music.albums.get_library_album_tracks(album_id), key=lambda x: x.track_number
    )
    assert [track.mbid for track in tracks] == list(RECORDING_IDS)
    for track, isrc in zip(tracks, ISRCS, strict=True):
        assert (ExternalID.ISRC, isrc) in track.external_ids
    track_mappings = await mass.music.database.get_rows(
        DB_TABLE_PROVIDER_MAPPINGS,
        {"media_type": MediaType.TRACK.value, "provider_domain": "tidal"},
    )
    assert {(row["item_id"], row["provider_item_id"]) for row in track_mappings} == {
        (int(track.item_id), f"td-{track.track_number}") for track in tracks
    }


async def _add_artists_sharing_an_mbid(
    mass: MusicAssistant, newer_type: ArtistType = ArtistType.SINGER
) -> tuple[Artist, Artist]:
    """Store an artist tagged with the MusicBrainz id, then a newer one from Spotify without it."""
    older = await mass.music.artists.add_item_to_library(
        Artist(
            item_id="0",
            provider="library",
            name="Sydney Symphony Orchestra",
            provider_mappings={
                ProviderMapping(
                    item_id="sso",
                    provider_domain="filesystem_local",
                    provider_instance="filesystem_local--test",
                    in_library=True,
                )
            },
            external_ids={(ExternalID.MB_ARTIST, ARTIST_MBID)},
        )
    )
    newer = await mass.music.artists.add_item_to_library(
        Artist(
            item_id="0",
            provider="library",
            name="Sydney Symphony",
            artist_type=newer_type,
            provider_mappings={_spotify("4Z8W4fKeB5YxbusRsdQVPb")},
        )
    )
    return older, newer


def _mb_artist() -> MusicBrainzArtist:
    """Return the MusicBrainz artist both library artists are."""
    return MusicBrainzArtist(
        id=ARTIST_MBID, name="Sydney Symphony Orchestra", sort_name="Sydney Symphony Orchestra"
    )


async def _identify(mass: MusicAssistant, artist: Artist, entry_point: str) -> None:
    """Identify an artist on MusicBrainz through the link run or the metadata refresh."""
    if entry_point == "link":
        await mass.metadata.link_item_to_musicbrainz(artist)
        return
    # the online metadata providers are not what is under test
    with patch.object(MetaDataController, "providers", new_callable=PropertyMock, return_value=[]):
        await mass.metadata._update_artist_metadata(artist, force_refresh=True)


@pytest.mark.parametrize("entry_point", ["link", "refresh"])
async def test_artist_identified_by_streaming_link_merges_into_the_oldest(
    mass: MusicAssistant, entry_point: str
) -> None:
    """An artist whose Spotify link names another library artist's id merges into the oldest."""
    older, newer = await _add_artists_sharing_an_mbid(mass)

    with (
        patch.object(MusicbrainzProvider, "get_mbid_by_url", AsyncMock(return_value=ARTIST_MBID)),
        patch.object(
            MusicbrainzProvider, "get_artist_details", AsyncMock(return_value=_mb_artist())
        ),
    ):
        await _identify(mass, newer, entry_point)

    with pytest.raises(MediaNotFoundError):
        await mass.music.artists.get_library_item(newer.item_id)
    merged = await mass.music.artists.get_library_item(older.item_id)
    assert merged.name == "Sydney Symphony Orchestra"
    assert {mapping.provider_domain for mapping in merged.provider_mappings} == {
        "filesystem_local",
        "spotify",
    }


async def test_adding_an_artist_merged_by_its_metadata_update_returns_the_survivor(
    mass: MusicAssistant,
) -> None:
    """Adding an artist that its metadata update merges away returns the older artist."""
    older, newer = await _add_artists_sharing_an_mbid(mass)

    with (
        patch.object(MusicbrainzProvider, "get_mbid_by_url", AsyncMock(return_value=ARTIST_MBID)),
        patch.object(
            MusicbrainzProvider, "get_artist_details", AsyncMock(return_value=_mb_artist())
        ),
        patch.object(MetaDataController, "providers", new_callable=PropertyMock, return_value=[]),
        # only the metadata update of the add itself is under test
        patch.object(mass.metadata, "schedule_update_metadata"),
    ):
        added = await mass.music.add_item_to_library(newer)

    assert added.item_id == older.item_id
    assert "spotify" in {mapping.provider_domain for mapping in added.provider_mappings}


async def test_updating_metadata_of_an_artist_it_merges_returns_the_survivor(
    mass: MusicAssistant,
) -> None:
    """Updating the metadata of an artist that the update merges away returns the older artist."""
    older, newer = await _add_artists_sharing_an_mbid(mass)

    with (
        patch.object(MusicbrainzProvider, "get_mbid_by_url", AsyncMock(return_value=ARTIST_MBID)),
        patch.object(
            MusicbrainzProvider, "get_artist_details", AsyncMock(return_value=_mb_artist())
        ),
        patch.object(MetaDataController, "providers", new_callable=PropertyMock, return_value=[]),
    ):
        updated = await mass.metadata.update_metadata(newer, force_refresh=True)

    assert updated is not None
    assert updated.item_id == older.item_id


async def test_artist_identified_by_name_search_is_not_merged(mass: MusicAssistant) -> None:
    """An id only a name search found may belong to a namesake, so both artists stay."""
    older, newer = await _add_artists_sharing_an_mbid(mass)

    with (
        patch.object(
            MusicbrainzProvider,
            "identify_artist",
            AsyncMock(return_value=IdentifiedArtist(_mb_artist(), True)),
        ),
        patch.object(
            MusicbrainzProvider, "get_artist_details", AsyncMock(return_value=_mb_artist())
        ),
    ):
        await mass.metadata.link_item_to_musicbrainz(newer)

    assert (await mass.music.artists.get_library_item(older.item_id)).mbid == ARTIST_MBID
    assert (await mass.music.artists.get_library_item(newer.item_id)).mbid == ARTIST_MBID


async def test_artists_of_different_roles_sharing_an_mbid_stay_apart(
    mass: MusicAssistant,
) -> None:
    """An author and a singer with one MusicBrainz id are left apart without an error."""
    older, newer = await _add_artists_sharing_an_mbid(mass, newer_type=ArtistType.AUTHOR)

    with (
        patch.object(MusicbrainzProvider, "get_mbid_by_url", AsyncMock(return_value=ARTIST_MBID)),
        patch.object(
            MusicbrainzProvider, "get_artist_details", AsyncMock(return_value=_mb_artist())
        ),
    ):
        await mass.metadata.link_item_to_musicbrainz(newer)

    assert await mass.music.artists.get_library_item(older.item_id)
    assert (await mass.music.artists.get_library_item(newer.item_id)).mbid == ARTIST_MBID


def _tidal(item_id: str) -> ProviderMapping:
    """Return the library mapping of an item that came in from Tidal."""
    return ProviderMapping(
        item_id=item_id,
        provider_domain="tidal",
        provider_instance=TIDAL_INSTANCE,
        in_library=True,
    )


async def _add_tidal_track(
    mass: MusicAssistant,
    album: Album,
    number: int,
    disc_number: int = 1,
    duration: int | None = None,
) -> Track:
    """Store a Tidal track of an album, titled by Tidal's own name, and return it."""
    return await mass.music.tracks.add_item_to_library(
        Track(
            item_id="0",
            provider="library",
            name=f"No. {number}: Track {number}",
            duration=duration or 200 + number,
            disc_number=disc_number,
            track_number=number,
            artists=UniqueList(album.artists),
            album=album,
            external_ids={(ExternalID.ISRC, ISRCS[number - 1])},
            provider_mappings={_tidal(f"td-{number}")},
        )
    )


async def _set_recording_id(mass: MusicAssistant, track: Track, recording_id: str) -> None:
    """Store a recording id on a library track, as an earlier link run would have."""
    track.mbid = recording_id
    await mass.music.tracks.update_item_in_library(track.item_id, track)


async def _link_track(mass: MusicAssistant, track: Track, recording_id: str) -> None:
    """Run the MusicBrainz link step on a library track that resolves to the recording."""
    recording = MusicBrainzRecording(id=recording_id, title="Track 1", length=201000)
    resolved = AsyncMock(return_value=recording)
    with patch.object(MusicbrainzProvider, "resolve_recording", resolved):
        await mass.metadata.link_item_to_musicbrainz(
            await mass.music.tracks.get_library_item(track.item_id)
        )


async def _track_domains(mass: MusicAssistant) -> list[set[str]]:
    """Return the provider domains of every library track."""
    tracks = await mass.music.tracks.library_items(limit=0)
    return [{x.provider_domain for x in track.provider_mappings} for track in tracks]


async def test_release_tracklist_joins_the_tracks_of_one_recording(mass: MusicAssistant) -> None:
    """Two tracks at one position of a merged album become one track mapped to both services."""
    mass._providers[TIDAL_INSTANCE] = _TidalStub(mass)
    album = await _add_spotify_album(mass)
    for number in (1, 2):
        await _add_tidal_track(mass, album, number)
    album_id = int(album.item_id)
    assert len(await mass.music.albums.get_library_album_tracks(album_id)) == 4

    await mass.music.albums.link_album_tracks(
        await mass.music.albums.get_library_item(album_id),
        await mass.music.albums.get_library_album_tracks(album_id),
        _release(),
        link_providers=False,
    )

    tracks = sorted(
        await mass.music.albums.get_library_album_tracks(album_id), key=lambda x: x.track_number
    )
    assert [track.mbid for track in tracks] == list(RECORDING_IDS)
    assert [{x.provider_domain for x in track.provider_mappings} for track in tracks] == [
        {"spotify", "tidal"},
        {"spotify", "tidal"},
    ]
    rows = await mass.music.database.get_rows(DB_TABLE_ALBUM_TRACKS, {"album_id": album_id})
    assert sorted((row["disc_number"], row["track_number"]) for row in rows) == [(1, 1), (1, 2)]


async def test_release_tracklist_skips_a_track_joined_earlier_in_the_run(
    mass: MusicAssistant,
) -> None:
    """A track merged into one listed before it is not written again."""
    album = await _add_spotify_album(mass)
    album_id = int(album.item_id)
    tidal_track = await _add_tidal_track(mass, album, 1)
    await _set_recording_id(mass, tidal_track, RECORDING_IDS[0])
    db_tracks = sorted(
        await mass.music.albums.get_library_album_tracks(album_id),
        key=lambda x: "tidal" in {m.provider_domain for m in x.provider_mappings},
    )

    await mass.music.albums.link_album_tracks(
        await mass.music.albums.get_library_item(album_id),
        db_tracks,
        _release(),
        link_providers=False,
    )

    assert sorted(await _track_domains(mass), key=len) == [{"spotify"}, {"spotify", "tidal"}]


async def test_recording_link_joins_the_track_already_holding_it(mass: MusicAssistant) -> None:
    """A track identified by ISRC joins the library track that already has its recording."""
    album = await _add_spotify_album(mass)
    spotify_track = (await mass.music.albums.get_library_album_tracks(int(album.item_id)))[0]
    await _set_recording_id(mass, spotify_track, RECORDING_IDS[0])
    tidal_track = await _add_tidal_track(mass, album, 1)

    await _link_track(mass, tidal_track, RECORDING_IDS[0])

    assert await mass.music.tracks.get_library_items_by_external_id(
        RECORDING_IDS[0], ExternalID.MB_RECORDING, limit=None
    ) == [await mass.music.tracks.get_library_item(tidal_track.item_id)]
    assert sorted(await _track_domains(mass), key=len) == [{"spotify"}, {"spotify", "tidal"}]


@pytest.mark.parametrize("difference", ["disc", "second recording id", "duration"])
async def test_recording_link_keeps_tracks_of_one_recording_apart(
    mass: MusicAssistant, difference: str
) -> None:
    """
    Tracks of one recording stay apart on another disc or with durations that are not close.

    A library track carrying two recording ids is not joined on either of them.
    """
    album = await _add_spotify_album(mass)
    spotify_track = (await mass.music.albums.get_library_album_tracks(int(album.item_id)))[0]
    if difference == "second recording id":
        # a merge or an update adds a recording id next to the one a track holds
        spotify_track.external_ids.update((ExternalID.MB_RECORDING, x) for x in RECORDING_IDS)
        await mass.music.tracks.update_item_in_library(spotify_track.item_id, spotify_track)
    else:
        await _set_recording_id(mass, spotify_track, RECORDING_IDS[0])
    kwargs = {"disc": {"disc_number": 2}, "duration": {"duration": 278}}.get(difference, {})
    tidal_track = await _add_tidal_track(mass, album, 1, **kwargs)

    await _link_track(mass, tidal_track, RECORDING_IDS[0])

    holders = await mass.music.tracks.get_library_items_by_external_id(
        RECORDING_IDS[0], ExternalID.MB_RECORDING, limit=None
    )
    assert len(holders) == 2


async def _add_tidal_album_mapping(mass: MusicAssistant, album: Album) -> Album:
    """Map a library album to its Tidal album as well and return it."""
    await mass.music.albums.add_provider_mapping(
        album.item_id,
        ProviderMapping(
            item_id=TIDAL_ALBUM_ID, provider_domain="tidal", provider_instance=TIDAL_INSTANCE
        ),
    )
    return await mass.music.albums.get_library_item(album.item_id)


async def _link_album(
    mass: MusicAssistant, album: Album, release: MusicBrainzRelease | None
) -> None:
    """Link the tracks of a library album to its release and its providers' tracklists."""
    await mass.music.albums.link_album_tracks(
        album, await mass.music.albums.get_library_album_tracks(int(album.item_id)), release
    )


async def test_release_and_provider_tracklist_join_in_one_run(mass: MusicAssistant) -> None:
    """A track the release joins is not linked to the provider tracklist again."""
    mass._providers[TIDAL_INSTANCE] = _TidalStub(mass)
    album = await _add_tidal_album_mapping(mass, await _add_spotify_album(mass))
    for number in (1, 2):
        await _add_tidal_track(mass, album, number)

    await _link_album(mass, album, _release())

    assert sorted(await _track_domains(mass), key=len) == [
        {"spotify", "tidal"},
        {"spotify", "tidal"},
    ]


async def test_set_identifiers_joins_tracks_of_one_recording(mass: MusicAssistant) -> None:
    """A recording id set via set_identifiers merges the track that already holds it."""
    album = await _add_spotify_album(mass)
    spotify_track = (await mass.music.albums.get_library_album_tracks(int(album.item_id)))[0]
    await _set_recording_id(mass, spotify_track, RECORDING_IDS[0])
    tidal_track = await _add_tidal_track(mass, album, 1)

    await mass.music.tracks.set_identifiers(
        tidal_track.provider_mappings.pop().item_id,
        TIDAL_INSTANCE,
        mbid=RECORDING_IDS[0],
    )

    assert sorted(await _track_domains(mass), key=len) == [{"spotify"}, {"spotify", "tidal"}]
