"""End-to-end test: a library album is identified and linked through MusicBrainz."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, PropertyMock, patch

from music_assistant_models.config_entries import ProviderConfig
from music_assistant_models.enums import AlbumType, ExternalID, MediaType, ProviderType
from music_assistant_models.media_items import Album, Artist, ProviderMapping, Track, UniqueList
from music_assistant_models.provider import ProviderManifest

from music_assistant.constants import DB_TABLE_EXTERNAL_ID_LOOKUP, DB_TABLE_PROVIDER_MAPPINGS
from music_assistant.controllers.metadata import MetaDataController
from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers.musicbrainz.models import (
    MusicBrainzMedia,
    MusicBrainzRecording,
    MusicBrainzRelation,
    MusicBrainzRelease,
    MusicBrainzReleaseGroup,
    MusicBrainzTrack,
    MusicBrainzUrl,
)
from music_assistant.providers.musicbrainz.provider import MusicbrainzProvider

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

RELEASE_ID = "5d9c6e8a-1c3b-4f2e-9a7d-2b8c4e6f0a11"
RELEASE_GROUP_ID = "7f1e2d3c-4b5a-4c6d-8e9f-0a1b2c3d4e55"
RECORDING_IDS = ("0c1d2e3f-4a5b-4c6d-9e8f-7a6b5c4d3e22", "9e8d7c6b-5a4f-4e3d-8c2b-1a0f9e8d7c33")
ISRCS = ("GBSTK0700001", "GBSTK0700002")
BARCODE = "0634904032463"
TIDAL_INSTANCE = "tidal--test"
TIDAL_ALBUM_ID = "79280548"


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
