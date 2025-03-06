"""iBroadcast support for MusicAssistant."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aiohttp import ClientSession
from ibroadcastaio import IBroadcastClient
from music_assistant_models.config_entries import ConfigEntry, ConfigValueType
from music_assistant_models.enums import (
    AlbumType,
    ConfigEntryType,
    ContentType,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import InvalidDataError, LoginFailed
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    ItemMapping,
    MediaItemImage,
    Playlist,
    ProviderMapping,
    Track,
    UniqueList,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.constants import (
    CONF_PASSWORD,
    CONF_USERNAME,
    UNKNOWN_ARTIST,
    VARIOUS_ARTISTS_MBID,
    VARIOUS_ARTISTS_NAME,
)
from music_assistant.helpers.util import parse_title_and_version
from music_assistant.models.music_provider import MusicProvider

SUPPORTED_FEATURES = {
    ProviderFeature.LIBRARY_ARTISTS,
    ProviderFeature.LIBRARY_TRACKS,
    ProviderFeature.LIBRARY_ALBUMS,
    ProviderFeature.LIBRARY_PLAYLISTS,
    ProviderFeature.BROWSE,
    ProviderFeature.ARTIST_ALBUMS,
}


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    if not config.get_value(CONF_USERNAME) or not config.get_value(CONF_PASSWORD):
        msg = "Invalid login credentials"
        raise LoginFailed(msg)
    return IBroadcastProvider(mass, manifest, config)


async def get_config_entries(
    mass: MusicAssistant,
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
    # ruff: noqa: ARG001
    return (
        ConfigEntry(
            key=CONF_USERNAME,
            type=ConfigEntryType.STRING,
            label="Username",
            required=True,
        ),
        ConfigEntry(
            key=CONF_PASSWORD,
            type=ConfigEntryType.SECURE_STRING,
            label="Password",
            required=True,
        ),
    )


class IBroadcastProvider(MusicProvider):
    """Provider for iBroadcast."""

    _user_id: str
    _client: IBroadcastClient
    _token: str

    async def handle_async_init(self) -> None:
        """Set up the iBroadcast provider."""
        async with ClientSession() as session:
            self._client = IBroadcastClient(session)
            status = await self._client.login(
                self.config.get_value(CONF_USERNAME),
                self.config.get_value(CONF_PASSWORD),
            )
            self._user_id = status["user"]["id"]
            self._token = status["user"]["token"]

            # temporary call to refresh library until ibroadcast provides a detailed api
            await self._client.refresh_library()

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return the features supported by this Provider."""
        return SUPPORTED_FEATURES

    async def get_library_albums(self) -> AsyncGenerator[Album, None]:
        """Retrieve library albums from ibroadcast."""
        for album in (await self._client.get_albums()).values():
            try:
                yield await self._parse_album(album)
            except (KeyError, TypeError, InvalidDataError, IndexError) as error:
                self.logger.debug("Parse album failed: %s", album, exc_info=error)
                continue

    async def get_album(self, prov_album_id: str) -> Album:
        """Get full album details by id."""
        album_obj = await self._client.get_album(int(prov_album_id))
        return await self._parse_album(album_obj)

    async def get_library_artists(self) -> AsyncGenerator[Artist, None]:
        """Retrieve all library artists from iBroadcast."""
        for artist in (await self._client.get_artists()).values():
            try:
                yield await self._parse_artist(artist)
            except (KeyError, TypeError, InvalidDataError, IndexError) as error:
                self.logger.debug("Parse artist failed: %s", artist, exc_info=error)
                continue

    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Get a list of albums for the given artist."""
        albums_objs = [
            album
            for album in (await self._client.get_albums()).values()
            if album["artist_id"] == int(prov_artist_id)
        ]
        albums = []
        for album in albums_objs:
            try:
                albums.append(await self._parse_album(album))
            except (KeyError, TypeError, InvalidDataError, IndexError) as error:
                self.logger.debug("Parse album failed: %s", album, exc_info=error)
                continue
        return albums

    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get album tracks for given album id."""
        album = await self._client.get_album(int(prov_album_id))
        return await self._get_tracks(album["tracks"])

    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        track_obj = await self._client.get_track(int(prov_track_id))
        return await self._parse_track(track_obj)

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get full artist details by id."""
        artist_obj = await self._client.get_artist(int(prov_artist_id))
        return await self._parse_artist(artist_obj)

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """Retrieve library tracks from iBroadcast."""
        for track in (await self._client.get_tracks()).values():
            try:
                yield await self._parse_track(track)
            except IndexError:
                continue
            except (KeyError, TypeError, InvalidDataError) as error:
                self.logger.debug("Parse track failed: %s", track, exc_info=error)
                continue

    def _get_artist_item_mapping(self, artist_id: str, artist_obj: dict[str, Any]) -> ItemMapping:
        if (not artist_id and artist_obj["name"] == "Various Artists") or artist_id == "0":
            artist_id = VARIOUS_ARTISTS_MBID
        return self._get_item_mapping(MediaType.ARTIST, artist_id, str(artist_obj.get("name")))

    def _get_item_mapping(self, media_type: MediaType, key: str, name: str) -> ItemMapping:
        return ItemMapping(
            media_type=media_type,
            item_id=key,
            provider=self.lookup_key,
            name=name,
        )

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Retrieve playlists from iBroadcast."""
        for playlist in (await self._client.get_playlists()).values():
            # Skip the auto generated playlist
            if playlist["type"] != "recently-played" and playlist["type"] != "thumbsup":
                yield await self._parse_playlist(playlist)

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get full playlist details by id."""
        playlist_obj = await self._client.get_playlist(int(prov_playlist_id))
        try:
            playlist = await self._parse_playlist(playlist_obj)
        except (KeyError, TypeError, InvalidDataError, IndexError) as error:
            self.logger.debug("Parse playlist failed: %s", playlist_obj, exc_info=error)
        return playlist

    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """Get playlist tracks."""
        tracks: list[Track] = []
        if page > 0:
            return tracks
        playlist_obj = await self._client.get_playlist(int(prov_playlist_id))
        if "tracks" not in playlist_obj:
            return tracks
        return await self._get_tracks(playlist_obj["tracks"], True)

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Return the content details for the given track when it will be streamed."""
        # How to buildup a stream url:
        # [streaming_server]/[url]?Expires=[now]&Signature=[user token]&file_id=[file ID]
        # &user_id=[user ID]&platform=[your app name]&version=[your app version]
        # See https://devguide.ibroadcast.com/?p=streaming-server
        url = await self._client.get_full_stream_url(int(item_id), "music-assistant")

        return StreamDetails(
            provider=self.lookup_key,
            item_id=item_id,
            audio_format=AudioFormat(
                content_type=ContentType.UNKNOWN,
            ),
            stream_type=StreamType.HTTP,
            path=url,
            can_seek=True,
            allow_seek=True,
        )

    async def _get_tracks(self, track_ids: list[int], is_playlist: bool = False) -> list[Track]:
        """Retrieve a list of tracks based on provided track IDs."""
        tracks = []
        for index, track_id in enumerate(track_ids, 1):
            track_obj = await self._client.get_track(track_id)
            if track_obj is not None:
                track = await self._parse_track(track_obj)
                if is_playlist:
                    track.position = index
                tracks.append(track)
        return tracks

    async def _parse_artist(self, artist_obj: dict[str, Any]) -> Artist:
        """Parse a iBroadcast user response to Artist model object."""
        artist_id = artist_obj["artist_id"]
        artist = Artist(
            item_id=artist_id,
            name=artist_obj["name"],
            provider=self.lookup_key,
            provider_mappings={
                ProviderMapping(
                    item_id=artist_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=f"https://media.ibroadcast.com/?view=container&container_id={artist_id}&type=artists",
                )
            },
        )
        # Artwork
        if "artwork_id" in artist_obj:
            artist.metadata.images = UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=await self._client.get_artist_artwork_url(artist_id),
                        provider=self.lookup_key,
                        remotely_accessible=True,
                    )
                ]
            )
        return artist

    async def _parse_album(self, album_obj: dict[str, Any]) -> Album:
        """Parse ibroadcast album object to generic layout."""
        album_id = album_obj["album_id"]
        name, version = parse_title_and_version(album_obj["name"])
        album = Album(
            item_id=album_id,
            provider=self.lookup_key,
            name=name,
            year=album_obj["year"],
            version=version,
            provider_mappings={
                ProviderMapping(
                    item_id=album_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    audio_format=AudioFormat(content_type=ContentType.MPEG),
                    url=f"https://media.ibroadcast.com/?view=container&container_id={album_id}&type=albums",
                )
            },
        )
        if album_obj["artist_id"] == 0:
            artist = Artist(
                item_id=VARIOUS_ARTISTS_MBID,
                name=VARIOUS_ARTISTS_NAME,
                provider=self.lookup_key,
                provider_mappings={
                    ProviderMapping(
                        item_id=VARIOUS_ARTISTS_MBID,
                        provider_domain=self.domain,
                        provider_instance=self.instance_id,
                    )
                },
            )
            album.artists.append(artist)
        else:
            artist_mapping = self._get_item_mapping(
                MediaType.ARTIST,
                album_obj["artist_id"],
                (await self._client.get_artist(album_obj["artist_id"]))["name"]
                if await self._client.get_artist(album_obj["artist_id"])
                else UNKNOWN_ARTIST,
            )
            album.artists.append(artist_mapping)

        if "rating" in album_obj and album_obj["rating"] == 5:
            album.favorite = True
        # iBroadcast doesn't seem to know album type
        album.album_type = AlbumType.UNKNOWN
        # There is only an artwork in the tracks, lets get the first track one
        artwork_url = await self._client.get_album_artwork_url(album_id)
        if artwork_url:
            album.metadata.images = UniqueList([self._get_artwork_object(artwork_url)])
        return album

    def _get_artwork_object(self, url: str) -> MediaItemImage:
        return MediaItemImage(
            type=ImageType.THUMB,
            path=url,
            provider=self.lookup_key,
            remotely_accessible=True,
        )

    async def _parse_track(self, track_obj: dict[str, Any]) -> Track:
        """Parse an iBroadcast track object to a Track model object."""
        track = Track(
            item_id=track_obj["track_id"],
            provider=self.lookup_key,
            name=track_obj["title"],
            provider_mappings={
                ProviderMapping(
                    item_id=track_obj["track_id"],
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    available=not track_obj["trashed"],
                    audio_format=AudioFormat(
                        content_type=ContentType.MPEG,
                    ),
                )
            },
        )
        if track_obj["album_id"]:
            album = await self._client.get_album(track_obj["album_id"])

        if "rating" in track_obj and track_obj["rating"] == 5:
            track.favorite = True
        if "length" in track_obj and str(track_obj["length"]).isdigit():
            track.duration = track_obj["length"]
        # use the disc number if available
        if album and album["disc"] > 0:
            track.disc_number = album["disc"]
            track.track_number = int(track_obj["track"])
        # otherwise, track number might look like 201, meaning, disc 2, track 1
        elif track_obj["track"] > 99:
            track.disc_number = int(str(track_obj["track"])[:1])
            track.track_number = int(str(track_obj["track"])[1:])
        # or just the track number and no disc number
        else:
            track.track_number = int(track_obj["track"])
        # Track artists
        if "artist_id" in track_obj:
            artist_id = track_obj["artist_id"]
            track.artists = UniqueList(
                [self._get_artist_item_mapping(artist_id, await self._client.get_artist(artist_id))]
            )
            # additional artists structure: 'artists_additional': [[artist id, phrase, type]]
            track.artists.extend(
                [
                    self._get_artist_item_mapping(
                        additional_artist[0],
                        await self._client.get_artist(additional_artist[0]),
                    )
                    for additional_artist in track_obj["artists_additional"]
                    if additional_artist[0]
                ]
            )
            # guard that track has valid artists
            if not track.artists:
                msg = "Track is missing artists"
                raise InvalidDataError(msg)

        # Artwork
        track.metadata.images = UniqueList(
            [
                self._get_artwork_object(
                    await self._client.get_track_artwork_url(track_obj["track_id"])
                )
            ]
        )
        # Genre
        genres: set[str] = set()
        if track_obj["genre"]:
            genres.add(track_obj["genre"])
        if track_obj["genres_additional"]:
            genres.add(track_obj["genres_additional"])
        track.metadata.genres = genres
        # album info
        if album:
            track.album = self._get_item_mapping(
                MediaType.ALBUM, track_obj["album_id"], album["name"]
            )
        return track

    async def _parse_playlist(self, playlist_obj: dict[str, Any]) -> Playlist:
        """Parse an iBroadcast Playlist response to a Playlist object."""
        playlist_id = str(playlist_obj["playlist_id"])
        playlist = Playlist(
            item_id=playlist_id,
            provider=self.lookup_key,
            name=playlist_obj["name"],
            provider_mappings={
                ProviderMapping(
                    item_id=playlist_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )
        # Can be supported in future, the API has options available
        playlist.is_editable = False
        playlist.metadata.images = UniqueList(
            [
                self._get_artwork_object(
                    await self._client.get_playlist_artwork_url(int(playlist_id))
                )
            ]
        )
        if "description" in playlist_obj:
            playlist.metadata.description = playlist_obj["description"]
        return playlist
