"""Soundcloud support for MusicAssistant."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from soundcloudpy import SoundcloudAsyncAPI

from music_assistant.common.helpers.util import parse_title_and_version
from music_assistant.common.models.config_entries import ConfigEntry, ConfigValueType
from music_assistant.common.models.enums import ConfigEntryType, ProviderFeature, StreamType
from music_assistant.common.models.errors import InvalidDataError, LoginFailed
from music_assistant.common.models.media_items import (
    Artist,
    AudioFormat,
    ContentType,
    ImageType,
    MediaItemImage,
    MediaType,
    Playlist,
    ProviderMapping,
    SearchResults,
    Track,
)
from music_assistant.common.models.streamdetails import StreamDetails
from music_assistant.server.models.music_provider import MusicProvider

CONF_CLIENT_ID = "client_id"
CONF_AUTHORIZATION = "authorization"

SUPPORTED_FEATURES = (
    ProviderFeature.LIBRARY_ARTISTS,
    ProviderFeature.LIBRARY_TRACKS,
    ProviderFeature.LIBRARY_PLAYLISTS,
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
    ProviderFeature.ARTIST_TOPTRACKS,
    ProviderFeature.SIMILAR_TRACKS,
)


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable

    from music_assistant.common.models.config_entries import ProviderConfig
    from music_assistant.common.models.provider import ProviderManifest
    from music_assistant.server import MusicAssistant
    from music_assistant.server.models import ProviderInstanceType


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    if not config.get_value(CONF_CLIENT_ID) or not config.get_value(CONF_AUTHORIZATION):
        msg = "Invalid login credentials"
        raise LoginFailed(msg)
    return SoundcloudMusicProvider(mass, manifest, config)


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
            key=CONF_CLIENT_ID,
            type=ConfigEntryType.SECURE_STRING,
            label="Client ID",
            required=True,
        ),
        ConfigEntry(
            key=CONF_AUTHORIZATION,
            type=ConfigEntryType.SECURE_STRING,
            label="Authorization",
            required=True,
        ),
    )


class SoundcloudMusicProvider(MusicProvider):
    """Provider for Soundcloud."""

    _headers = None
    _context = None
    _cookies = None
    _signature_timestamp = 0
    _cipher = None
    _user_id = None
    _soundcloud = None
    _me = None

    async def handle_async_init(self) -> None:
        """Set up the Soundcloud provider."""
        client_id = self.config.get_value(CONF_CLIENT_ID)
        auth_token = self.config.get_value(CONF_AUTHORIZATION)
        self._soundcloud = SoundcloudAsyncAPI(auth_token, client_id, self.mass.http_session)
        await self._soundcloud.login()
        self._me = await self._soundcloud.get_account_details()
        self._user_id = self._me["id"]

    @property
    def supported_features(self) -> tuple[ProviderFeature, ...]:
        """Return the features supported by this Provider."""
        return SUPPORTED_FEATURES

    @classmethod
    async def _run_async(cls, call: Callable, *args, **kwargs):  # noqa: ANN206
        return await asyncio.to_thread(call, *args, **kwargs)

    async def search(
        self, search_query: str, media_types=list[MediaType], limit: int = 10
    ) -> SearchResults:
        """Perform search on musicprovider.

        :param search_query: Search query.
        :param media_types: A list of media_types to include.
        :param limit: Number of items to return in the search (per type).
        """
        result = SearchResults()
        searchtypes = []
        if MediaType.ARTIST in media_types:
            searchtypes.append("artist")
        if MediaType.TRACK in media_types:
            searchtypes.append("track")
        if MediaType.PLAYLIST in media_types:
            searchtypes.append("playlist")

        media_types = [
            x for x in media_types if x in (MediaType.ARTIST, MediaType.TRACK, MediaType.PLAYLIST)
        ]
        if not media_types:
            return result

        searchresult = await self._soundcloud.search(search_query, limit)

        for item in searchresult["collection"]:
            media_type = item["kind"]
            if media_type == "user" and MediaType.ARTIST in media_types:
                result.artists.append(await self._parse_artist(item))
            elif media_type == "track" and MediaType.TRACK in media_types:
                if item.get("duration") == item.get("full_duration"):
                    # skip if it's a preview track (e.g. in case of free accounts)
                    result.tracks.append(await self._parse_track(item))
            elif media_type == "playlist" and MediaType.PLAYLIST in media_types:
                result.playlists.append(await self._parse_playlist(item))

        return result

    async def get_library_artists(self) -> AsyncGenerator[Artist, None]:
        """Retrieve all library artists from Soundcloud."""
        time_start = time.time()

        following = await self._soundcloud.get_following(self._user_id)
        self.logger.debug(
            "Processing Soundcloud library artists took %s seconds",
            round(time.time() - time_start, 2),
        )
        for artist in following["collection"]:
            try:
                yield await self._parse_artist(artist)
            except (KeyError, TypeError, InvalidDataError, IndexError) as error:
                self.logger.debug("Parse artist failed: %s", artist, exc_info=error)
                continue

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Retrieve all library playlists from Soundcloud."""
        time_start = time.time()
        async for item in self._soundcloud.get_account_playlists():
            try:
                raw_playlist = item["playlist"]
            except KeyError:
                self.logger.debug(
                    "Unexpected Soundcloud API response when parsing playlists: %s",
                    item,
                )
                continue

            try:
                playlist = await self._soundcloud.get_playlist_details(
                    playlist_id=raw_playlist["id"],
                )

                yield await self._parse_playlist(playlist)
            except (KeyError, TypeError, InvalidDataError, IndexError) as error:
                self.logger.debug(
                    "Failed to obtain Soundcloud playlist details: %s",
                    raw_playlist,
                    exc_info=error,
                )
                continue

        self.logger.debug(
            "Processing Soundcloud library playlists took %s seconds",
            round(time.time() - time_start, 2),
        )

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """Retrieve library tracks from Soundcloud."""
        time_start = time.time()
        async for item in self._soundcloud.get_tracks_liked():
            track = await self._soundcloud.get_track_details(item)
            try:
                yield await self._parse_track(track[0])
            except IndexError:
                continue
            except (KeyError, TypeError, InvalidDataError) as error:
                self.logger.debug("Parse track failed: %s", track, exc_info=error)
                continue

        self.logger.debug(
            "Processing Soundcloud library tracks took %s seconds",
            round(time.time() - time_start, 2),
        )

    async def get_artist(self, prov_artist_id) -> Artist:
        """Get full artist details by id."""
        artist_obj = await self._soundcloud.get_user_details(user_id=prov_artist_id)
        try:
            artist = await self._parse_artist(artist_obj=artist_obj) if artist_obj else None
        except (KeyError, TypeError, InvalidDataError, IndexError) as error:
            self.logger.debug("Parse artist failed: %s", artist_obj, exc_info=error)
        return artist

    async def get_track(self, prov_track_id) -> Track:
        """Get full track details by id."""
        track_obj = await self._soundcloud.get_track_details(track_id=prov_track_id)
        try:
            track = await self._parse_track(track_obj[0])
        except (KeyError, TypeError, InvalidDataError, IndexError) as error:
            self.logger.debug("Parse track failed: %s", track_obj, exc_info=error)
        return track

    async def get_playlist(self, prov_playlist_id) -> Playlist:
        """Get full playlist details by id."""
        playlist_obj = await self._soundcloud.get_playlist_details(playlist_id=prov_playlist_id)
        try:
            playlist = await self._parse_playlist(playlist_obj)
        except (KeyError, TypeError, InvalidDataError, IndexError) as error:
            self.logger.debug("Parse playlist failed: %s", playlist_obj, exc_info=error)
        return playlist

    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """Get playlist tracks."""
        result: list[Track] = []
        if page > 0:
            # TODO: soundcloud doesn't seem to support paging for playlist tracks ?!
            return result
        playlist_obj = await self._soundcloud.get_playlist_details(playlist_id=prov_playlist_id)
        if "tracks" not in playlist_obj:
            return result
        for index, item in enumerate(playlist_obj["tracks"], 1):
            # TODO: is it really needed to grab the entire track with an api call ?
            song = await self._soundcloud.get_track_details(item["id"])
            try:
                if track := await self._parse_track(song[0], index):
                    result.append(track)
            except (KeyError, TypeError, InvalidDataError, IndexError) as error:
                self.logger.debug("Parse track failed: %s", song, exc_info=error)
                continue
        return result

    async def get_artist_toptracks(self, prov_artist_id) -> list[Track]:
        """Get a list of 25 most popular tracks for the given artist."""
        tracks_obj = await self._soundcloud.get_popular_tracks_user(
            user_id=prov_artist_id, limit=25
        )
        tracks = []
        for item in tracks_obj["collection"]:
            song = await self._soundcloud.get_track_details(item["id"])
            try:
                track = await self._parse_track(song[0])
                tracks.append(track)
            except (KeyError, TypeError, InvalidDataError, IndexError) as error:
                self.logger.debug("Parse track failed: %s", song, exc_info=error)
                continue
        return tracks

    async def get_similar_tracks(self, prov_track_id, limit=25) -> list[Track]:
        """Retrieve a dynamic list of tracks based on the provided item."""
        tracks_obj = await self._soundcloud.get_recommended(track_id=prov_track_id, limit=limit)
        tracks = []
        for item in tracks_obj["collection"]:
            song = await self._soundcloud.get_track_details(item["id"])
            try:
                track = await self._parse_track(song[0])
                tracks.append(track)
            except (KeyError, TypeError, InvalidDataError, IndexError) as error:
                self.logger.debug("Parse track failed: %s", song, exc_info=error)
                continue

        return tracks

    async def get_stream_details(self, item_id: str) -> StreamDetails:
        """Return the content details for the given track when it will be streamed."""
        url: str = await self._soundcloud.get_stream_url(track_id=item_id)
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            # let ffmpeg work out the details itself as
            # soundcloud uses a mix of different content types and streaming methods
            audio_format=AudioFormat(
                content_type=ContentType.UNKNOWN,
            ),
            stream_type=StreamType.HLS
            if url.startswith("https://cf-hls-media.sndcdn.com")
            else StreamType.HTTP,
            path=url,
        )

    async def _parse_artist(self, artist_obj: dict) -> Artist:
        """Parse a Soundcloud user response to Artist model object."""
        artist_id = None
        permalink = artist_obj["permalink"]
        if artist_obj.get("id"):
            artist_id = artist_obj["id"]
        if not artist_id:
            msg = "Artist does not have a valid ID"
            raise InvalidDataError(msg)
        artist_id = str(artist_id)
        artist = Artist(
            item_id=artist_id,
            name=artist_obj["username"],
            provider=self.domain,
            provider_mappings={
                ProviderMapping(
                    item_id=str(artist_id),
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=f"https://soundcloud.com/{permalink}",
                )
            },
        )
        if artist_obj.get("description"):
            artist.metadata.description = artist_obj["description"]
        if artist_obj.get("avatar_url"):
            img_url = artist_obj["avatar_url"]
            artist.metadata.images = [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=img_url,
                    provider=self.lookup_key,
                    remotely_accessible=True,
                )
            ]
        return artist

    async def _parse_playlist(self, playlist_obj: dict) -> Playlist:
        """Parse a Soundcloud Playlist response to a Playlist object."""
        playlist_id = str(playlist_obj["id"])
        playlist = Playlist(
            item_id=playlist_id,
            provider=self.domain,
            name=playlist_obj["title"],
            provider_mappings={
                ProviderMapping(
                    item_id=playlist_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )
        playlist.is_editable = False
        if playlist_obj.get("description"):
            playlist.metadata.description = playlist_obj["description"]
        if playlist_obj.get("artwork_url"):
            playlist.metadata.images = [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=self._transform_artwork_url(playlist_obj["artwork_url"]),
                    provider=self.lookup_key,
                    remotely_accessible=True,
                )
            ]
        if playlist_obj.get("genre"):
            playlist.metadata.genres = playlist_obj["genre"]
        if playlist_obj.get("tag_list"):
            playlist.metadata.style = playlist_obj["tag_list"]
        return playlist

    async def _parse_track(self, track_obj: dict, playlist_position: int = 0) -> Track:
        """Parse a Soundcloud Track response to a Track model object."""
        name, version = parse_title_and_version(track_obj["title"])
        track_id = str(track_obj["id"])
        track = Track(
            item_id=track_id,
            provider=self.domain,
            name=name,
            version=version,
            duration=track_obj["duration"] / 1000,
            provider_mappings={
                ProviderMapping(
                    item_id=track_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    audio_format=AudioFormat(
                        content_type=ContentType.MP3,
                    ),
                    url=track_obj["permalink_url"],
                )
            },
            position=playlist_position,
        )
        user_id = track_obj["user"]["id"]
        user = await self._soundcloud.get_user_details(user_id)
        artist = await self._parse_artist(user)
        if artist and artist.item_id not in {x.item_id for x in track.artists}:
            track.artists.append(artist)

        if track_obj.get("artwork_url"):
            track.metadata.images = [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=self._transform_artwork_url(track_obj["artwork_url"]),
                    provider=self.lookup_key,
                    remotely_accessible=True,
                )
            ]
        if track_obj.get("description"):
            track.metadata.description = track_obj["description"]
        if track_obj.get("genre"):
            track.metadata.genres = track_obj["genre"]
        if track_obj.get("tag_list"):
            track.metadata.style = track_obj["tag_list"]
        return track

    def _transform_artwork_url(self, artwork_url: str) -> str:
        """Patch artwork URL to a high quality thumbnail."""
        # This is undocumented in their API docs, but was previously
        return artwork_url.replace("large", "t500x500")
