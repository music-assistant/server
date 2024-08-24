from music_assistant.server.models.music_provider import MusicProvider
from music_assistant.common.models.media_items import (
    Album,
    Artist,
    Track,
    SearchResults,
    ProviderMapping,
    MediaItemImage,
    ImageType,
    AudioFormat,
    ItemMapping,
)
from music_assistant.common.models.enums import (
    MediaType,
    ContentType,
    StreamType,
    ProviderFeature,
)
from music_assistant.common.models.streamdetails import StreamDetails
from music_assistant.common.models.config_entries import ConfigEntry, ConfigValueType
from collections.abc import AsyncGenerator

from music_assistant.common.models.enums import (
    ConfigEntryType,
    ProviderFeature,
    StreamType,
)
from gql import gql, Client
from gql.transport.requests import RequestsHTTPTransport


# Define the async function to setup the provider instance
async def setup(mass, manifest, config) -> MusicProvider:
    """Initialize provider(instance) with given configuration."""
    return JewishMusicProvider(mass, manifest, config)


# Define the async function to get config entries for the provider
async def get_config_entries(
    mass,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    instance_id: id of an existing provider instance (None if new instance setup).
    action: [optional] action key called from config entries UI.
    values: the (intermediate) raw values for config entries sent with the action.
    """
    return (
        ConfigEntry(
            key="api_key",
            type=ConfigEntryType.STRING,
            label="API Key",
            required=True,
            description="Enter your API key for the Jewish Music provider.",
        ),
    )


# Define the JewishMusicProvider class
class JewishMusicProvider(MusicProvider):
    def __init__(self, mass, manifest, config):
        super().__init__(mass, manifest, config)
        self.api_url = "http://jewishmusic.fm:4000/graphql"
        self.client = Client(
            transport=RequestsHTTPTransport(url=self.api_url, verify=True, retries=3),
            fetch_schema_from_transport=True,
        )

    @property
    def supported_features(self) -> tuple:
        """Return the features supported by this Provider."""
        return (
            ProviderFeature.SEARCH,
            ProviderFeature.BROWSE,
            ProviderFeature.LIBRARY_ALBUMS,
            ProviderFeature.LIBRARY_ARTISTS,
            ProviderFeature.LIBRARY_TRACKS,
            ProviderFeature.ARTIST_ALBUMS,
        )

    @property
    def is_streaming_provider(self) -> bool:
        """Return True if the provider is a streaming provider."""
        return True

    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 5,
    ) -> SearchResults:
        """Perform search on the music provider."""
        query = gql(
            """
            query SearchByName($term: String!, $skip: Int!, $take: Int!) {
                artists(
                    where: {
                    OR: [
                        { enName: { contains: $term } },
                        { heName: { contains: $term } }
                    ]
                    },
                    skip: $skip,
                    take: $take
                ) {
                    id
                    enName
                    heName
                    images {
                    large
                    medium
                    small
                    }
                }
                
                albums(
                    where: {
                    OR: [
                        { enName: { contains: $term } },
                        { heName: { contains: $term } }
                    ]
                    },
                    skip: $skip,
                    take: $take
                ) {
                    id
                    enName
                    heName
                    releasedAt
                    artists {
                    id
                    enName
                    heName
                    }
                    images {
                    large
                    medium
                    small
                    }
                }
                
                tracks(
                    where: {
                    OR: [
                        { enName: { contains: $term } },
                        { heName: { contains: $term } }
                    ]
                    },
                    skip: $skip,
                    take: $take
                ) {
                    id
                    trackNumber
                    enName
                    heName
                    duration
                    file
                    album {
                        id
                        enName
                        heName
                    }
                    artists {
                        id
                        enName
                        heName
                    }
                    images
                }
                }




        """
        )

        params = {"term": search_query, "take": limit, "skip": 0}
        response = self.client.execute(query, variable_values=params)

        tracks = []
        for track in response["tracks"]:
            parsed_track = self._parse_track(track)
            tracks.append(parsed_track)

        albums = []
        for album in response["albums"]:
            parsed_album = self._parse_album(album)
            albums.append(parsed_album)

        artists = []
        for artist in response["artists"]:
            parsed_artist = self._parse_album(artist)
            artists.append(parsed_artist)

        return SearchResults(tracks=tracks, albums=albums, artists=artists)

    async def get_library_artists(self) -> list[Artist]:
        query = gql(
            """
            query GetCatArtists(
                $term: String!
                $skip: Int!
                $count: Int!
                $category: String!
                ) {
                __typename
                artists(
                    take: $count
                    skip: $skip
                    orderBy: { heName: asc }
                    where: {
                    OR: [
                        { enName: { contains: $term, mode: insensitive } }
                        { heName: { contains: $term, mode: insensitive } }
                    ]
                    categories: {
                        some: { enName: { contains: $category, mode: insensitive } }
                    }
                    }
                ) {
                    __typename
                    id
                    enName
                    heName
                    images {
                        __typename
                        small
                        medium
                        large
                    }
                }
                }
        """
        )
        params = {"term": "", "skip": 0, "count": 10, "category": "popular artist"}
        response = self.client.execute(query, variable_values=params)

        artists_obj = response["artists"]
        for artist in artists_obj:
            self._parse_artist(artist)

    async def get_library_albums(self) -> list[Album]:
        """Get full artist details by id."""
        query = gql(
            """
           query GetAllAlbums($skip: Int!, $take: Int!) {
                albums(
                    orderBy: { releasedAt: desc },
                    skip: $skip,
                    take: $take
                ) {
                    id
                    enName
                    heName
                    artists {
                        id
                        enName
                        heName
                    }
                    images {
                        large
                        small
                        medium
                    }
                }
            }
        """
        )
        params = {"skip": 0, "take": 10}
        response = self.client.execute(query, variable_values=params)

        albums_obj = response["albums"]
        albums_list = []

        for album in albums_obj:
            parsed_album = self._parse_album(album)
            albums_list.append(parsed_album)

        return albums_list

    async def get_library_tracks(self) -> list[Track]:
        query = gql(
            """
           query GetPopularTracks($count: Int!) {
                tracks(take: $count) {
                    id
                    enName
                    heName
                    file
                    duration
                    album {
                    id
                    enName
                    heName
                    artists {
                        id
                        heName
                        enName
                        images {
                            large
                            medium
                            small
                        }
                    }
                    images {
                        small
                        medium
                        large
                    }
                    }
                }
            }
        """
        )
        params = {"count": 10}
        response = self.client.execute(query, variable_values=params)

        tracks_obj = response["tracks"]
        for track in tracks_obj:
            self._parse_track(track)

        tracks_list = []

        for track in tracks_obj:
            parsed_track = self._parse_track(track)
            tracks_list.append(parsed_track)

        return tracks_list

    async def get_artist_albums(self, prov_artist_id) -> list[Album]:
        """Get a list of all albums for the given artist."""
        query = gql(
            """
                query GetAlbumsByArtist($artistId: Int!, $orderBy: [AlbumOrderByWithRelationInput!], $take: Int, $skip: Int) {
                    albums(where: { artists: { some: { id: { equals: $artistId } } } }, orderBy: $orderBy, take: $take, skip: $skip) {
                        id
                        enName
                        heName
                        artists {
                            id
                            enName
                            heName
                        }
                        images {
                            large
                            small
                            medium
                        }
                    }
                }

        """
        )
        params = {"term": "", "skip": 0, "count": 50, "artistId": prov_artist_id}
        response = self.client.execute(query, variable_values=params)

        albums_obj = response["albums"]

        albums_list = []

        for album in albums_obj:
            parsed_album = self._parse_album(album)
            albums_list.append(parsed_album)

        return albums_list

    async def get_album(self, prov_album_id) -> Album:
        """Get full album details by id."""
        query = gql(
            """
                query GetAlbumById($albumId: Int!) {
                        album(where: { id: $albumId }) {
                            id
                            enName
                            heName
                            enDesc
                            heDesc
                            artists {
                                id
                                enName
                                heName
                            }
                            images {
                                large
                                medium
                                small
                            }
                        }
                    }

        """
        )
        params = {"albumId": prov_album_id}
        response = self.client.execute(query, variable_values=params)

        album_data = response["album"]
        self._parse_album(album_data)

    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        query = gql(
            """
            query GetAlbumTracks($albumId: Int!) {
                album(where: { id: $albumId }) {
                    id
                    enName
                    heName
                    tracks {
                    id
                    trackNumber
                    enName
                    heName
                    duration
                    file
                    artists {
                        id
                        enName
                        heName
                    }
                    }
                }
            }

        """
        )
        params = {"albumId": prov_album_id}
        response = self.client.execute(query, variable_values=params)

        tracks_obj = response["album"]["tracks"]
        tracks = []
        for track in tracks_obj:
            parsed_track = self._parse_track(track)
            tracks.append(parsed_track)
        return tracks

    async def get_track(self, prov_track_id) -> Track:
        """Get full track details by id."""
        query = gql(
            """
            query GetTrackById($trackId: Int!) {
                track(where: { id: $trackId }) {
                    id
                    trackNumber
                    enName
                    heName
                    file
                    duration
                    album {
                    id
                    enName
                    heName
                    }
                    artists {
                    id
                    enName
                    heName
                    }
                    genres {
                    id
                    enName
                    heName
                    }
                    images
                }
                }

        """
        )
        params = {"trackId": int(prov_track_id)}
        response = self.client.execute(query, variable_values=params)
        track_data = response["track"]
        res = self._parse_track(track_data)
        return res

    async def get_artist(self, prov_artist_id) -> Artist:
        query = gql(
            """
            query GetArtistById($artistId: Int!) {
                artist(where: { id: $artistId }) {
                    id
                    enName
                    heName
                    images {
                    large
                    medium
                    small
                    }
                }
            }
        """
        )
        params = {"artistId": prov_artist_id}
        response = self.client.execute(query, variable_values=params)

        artist_obj = response["artist"]
        self._parse_artist(artist_obj)

    def _parse_artist(self, artist_obj: dict) -> Artist:
        """Parse a YT Artist response to Artist model object."""

        artist = Artist(
            item_id=artist_obj["id"],
            name=artist_obj["heName"] or artist_obj["enName"],
            provider=self.domain,
            provider_mappings={
                ProviderMapping(
                    item_id=artist_obj["id"],
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )

        if artist_obj.get("images"):
            artist.metadata.images = self._parse_thumbnails(artist_obj["images"])
        return artist

    def _parse_album(self, album_obj) -> Album:
        album = Album(
            item_id=album_obj["id"],
            name=album_obj["heName"] or album_obj["enName"],
            provider=self.domain,
            provider_mappings={
                ProviderMapping(
                    item_id=album_obj["id"],
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )
        if album_obj.get("artists"):
            album.artists = [
                self._get_artist_item_mapping(artist) for artist in album_obj["artists"]
            ]

        if album_obj.get("images"):
            album.metadata.images = self._parse_thumbnails(album_obj["images"])

        return album

    def _parse_track(self, track_obj: dict) -> Track:
        base_url = "https://jewishmusic.fm/wp-content/uploads/secretmusicfolder1/"
        track = Track(
            item_id=track_obj["id"],
            provider=self.domain,
            name=track_obj["heName"] or track_obj["enName"],
            provider_mappings={
                ProviderMapping(
                    item_id=track_obj["id"],
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    available=True,
                    url=track_obj["file"],
                    audio_format=AudioFormat(
                        content_type=ContentType.MP3,
                    ),
                )
            },
            disc_number=0,  # not supported on YTM?
            track_number=track_obj.get("trackNumber", 0),
        )

        if track_obj.get("artists"):
            track.artists = [
                self._get_artist_item_mapping(artist) for artist in track_obj["artists"]
            ]

        if (
            track_obj.get("album")
            and isinstance(track_obj.get("album"), dict)
            and track_obj["album"].get("id")
        ):
            album = track_obj["album"]
            track.album = self._get_item_mapping(
                MediaType.ALBUM, album["id"], album["heName"] or album["enName"]
            )

        if "duration" in track_obj and str(track_obj["duration"]).isdigit():
            track.duration = int(track_obj["duration"])

        return track

    def _parse_thumbnails(self, thumbnails_obj: dict) -> list[MediaItemImage]:
        """Parse and YTM thumbnails to MediaItemImage."""
        result: list[MediaItemImage] = []
        processed_images = set()

        # Assuming thumbnails_obj contains keys like 'small', 'medium', 'large', etc.
        for size_key, url in thumbnails_obj.items():
            # Dummy values for width and height based on the size_key.
            if size_key == "small":
                width, height = 150, 150
            elif size_key == "medium":
                width, height = 300, 300
            else:  # assuming "large" or any other size
                width, height = 600, 600

            image_ratio: float = width / height
            image_type = ImageType.LANDSCAPE if image_ratio > 2.0 else ImageType.THUMB

            # Base URL
            url_base = url

            if (url_base, image_type) in processed_images:
                continue

            processed_images.add((url_base, image_type))
            result.append(
                MediaItemImage(
                    type=image_type,
                    path=url,
                    provider=self.lookup_key,
                    remotely_accessible=True,
                )
            )

        return result

    def _get_item_mapping(
        self, media_type: MediaType, key: str, name: str
    ) -> ItemMapping:
        return ItemMapping(
            media_type=media_type,
            item_id=key,
            provider=self.instance_id,
            name=name,
        )

    def _get_artist_item_mapping(self, artist_obj: dict) -> ItemMapping:
        return self._get_item_mapping(
            MediaType.ARTIST,
            artist_obj["id"],
            artist_obj["heName"] or artist_obj["enName"],
        )
