"""Spotify musicprovider support for MusicAssistant."""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlencode

from music_assistant_models.config_entries import ConfigEntry, ConfigValueType
from music_assistant_models.enums import (
    AlbumType,
    ConfigEntryType,
    ContentType,
    ExternalID,
    ImageType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import (
    AudioError,
    LoginFailed,
    MediaNotFoundError,
    ResourceTemporarilyUnavailable,
    SetupFailedError,
)
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    MediaItemImage,
    MediaItemType,
    MediaType,
    Playlist,
    ProviderMapping,
    SearchResults,
    Track,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.helpers.app_vars import app_var
from music_assistant.helpers.auth import AuthenticationHelper
from music_assistant.helpers.json import json_loads
from music_assistant.helpers.process import AsyncProcess, check_output
from music_assistant.helpers.throttle_retry import ThrottlerManager, throttle_with_retries
from music_assistant.helpers.util import lock, parse_title_and_version
from music_assistant.models.music_provider import MusicProvider

from .helpers import get_librespot_binary

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant import MusicAssistant
    from music_assistant.models import ProviderInstanceType

CONF_CLIENT_ID = "client_id"
CONF_ACTION_AUTH = "auth"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_ACTION_CLEAR_AUTH = "clear_auth"
SCOPE = [
    "playlist-read",
    "playlist-read-private",
    "playlist-read-collaborative",
    "playlist-modify-public",
    "playlist-modify-private",
    "user-follow-modify",
    "user-follow-read",
    "user-library-read",
    "user-library-modify",
    "user-read-private",
    "user-read-email",
    "user-top-read",
    "app-remote-control",
    "streaming",
    "user-read-playback-state",
    "user-modify-playback-state",
    "user-read-currently-playing",
    "user-modify-private",
    "user-modify",
    "user-read-playback-position",
    "user-read-recently-played",
]

CALLBACK_REDIRECT_URL = "https://music-assistant.io/callback"

LIKED_SONGS_FAKE_PLAYLIST_ID_PREFIX = "liked_songs"
SUPPORTED_FEATURES = {
    ProviderFeature.LIBRARY_ARTISTS,
    ProviderFeature.LIBRARY_ALBUMS,
    ProviderFeature.LIBRARY_TRACKS,
    ProviderFeature.LIBRARY_PLAYLISTS,
    ProviderFeature.LIBRARY_ARTISTS_EDIT,
    ProviderFeature.LIBRARY_ALBUMS_EDIT,
    ProviderFeature.LIBRARY_PLAYLISTS_EDIT,
    ProviderFeature.LIBRARY_TRACKS_EDIT,
    ProviderFeature.PLAYLIST_TRACKS_EDIT,
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
    ProviderFeature.ARTIST_ALBUMS,
    ProviderFeature.ARTIST_TOPTRACKS,
    ProviderFeature.SIMILAR_TRACKS,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    if config.get_value(CONF_REFRESH_TOKEN) in (None, ""):
        msg = "Re-Authentication required"
        raise SetupFailedError(msg)
    return SpotifyProvider(mass, manifest, config)


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

    if action == CONF_ACTION_AUTH:
        # spotify PKCE auth flow
        # https://developer.spotify.com/documentation/web-api/tutorials/code-pkce-flow
        import pkce

        code_verifier, code_challenge = pkce.generate_pkce_pair()
        async with AuthenticationHelper(mass, cast("str", values["session_id"])) as auth_helper:
            params = {
                "response_type": "code",
                "client_id": values.get(CONF_CLIENT_ID) or app_var(2),
                "scope": " ".join(SCOPE),
                "code_challenge_method": "S256",
                "code_challenge": code_challenge,
                "redirect_uri": CALLBACK_REDIRECT_URL,
                "state": auth_helper.callback_url,
            }
            query_string = urlencode(params)
            url = f"https://accounts.spotify.com/authorize?{query_string}"
            result = await auth_helper.authenticate(url)
            authorization_code = result["code"]
        # now get the access token
        params = {
            "grant_type": "authorization_code",
            "code": authorization_code,
            "redirect_uri": CALLBACK_REDIRECT_URL,
            "client_id": values.get(CONF_CLIENT_ID) or app_var(2),
            "code_verifier": code_verifier,
        }
        async with mass.http_session.post(
            "https://accounts.spotify.com/api/token", data=params
        ) as response:
            result = await response.json()
            values[CONF_REFRESH_TOKEN] = result["refresh_token"]

    # handle action clear authentication
    if action == CONF_ACTION_CLEAR_AUTH:
        assert values
        values[CONF_REFRESH_TOKEN] = None

    auth_required = values.get(CONF_REFRESH_TOKEN) in (None, "")

    if auth_required:
        values[CONF_CLIENT_ID] = None
        label_text = (
            "You need to authenticate to Spotify. Click the authenticate button below "
            "to start the authentication process which will open in a new (popup) window, "
            "so make sure to disable any popup blockers.\n\n"
            "Also make sure to perform this action from your local network"
        )
    elif action == CONF_ACTION_AUTH:
        label_text = "Authenticated to Spotify. Press save to complete setup."
    else:
        label_text = "Authenticated to Spotify. No further action required."

    return (
        ConfigEntry(
            key="label_text",
            type=ConfigEntryType.LABEL,
            label=label_text,
        ),
        ConfigEntry(
            key=CONF_REFRESH_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            label=CONF_REFRESH_TOKEN,
            hidden=True,
            required=True,
            value=values.get(CONF_REFRESH_TOKEN) if values else None,
        ),
        ConfigEntry(
            key=CONF_CLIENT_ID,
            type=ConfigEntryType.SECURE_STRING,
            label="Client ID (optional)",
            description="By default, a generic client ID is used which is (heavy) rate limited. "
            "To speedup performance, it is advised that you create your own Spotify Developer "
            "account and use that client ID here, but this comes at the cost of some features "
            "due to Spotify policies. For example Radio mode/recommendations and featured playlists"
            "will not work with a custom client ID. \n\n"
            f"Use {CALLBACK_REDIRECT_URL} as callback URL.",
            required=False,
            value=values.get(CONF_CLIENT_ID) if values else None,
            hidden=not auth_required,
        ),
        ConfigEntry(
            key=CONF_ACTION_AUTH,
            type=ConfigEntryType.ACTION,
            label="Authenticate with Spotify",
            description="This button will redirect you to Spotify to authenticate.",
            action=CONF_ACTION_AUTH,
            hidden=not auth_required,
        ),
        ConfigEntry(
            key=CONF_ACTION_CLEAR_AUTH,
            type=ConfigEntryType.ACTION,
            label="Clear authentication",
            description="Clear the current authentication details.",
            action=CONF_ACTION_CLEAR_AUTH,
            action_label="Clear authentication",
            required=False,
            hidden=auth_required,
        ),
    )


class SpotifyProvider(MusicProvider):
    """Implementation of a Spotify MusicProvider."""

    _auth_info: str | None = None
    _sp_user: dict[str, Any] | None = None
    _librespot_bin: str | None = None
    custom_client_id_active: bool = False
    throttler: ThrottlerManager

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self.cache_dir = os.path.join(self.mass.cache_path, self.instance_id)
        self.throttler = ThrottlerManager(rate_limit=1, period=2)
        if self.config.get_value(CONF_CLIENT_ID):
            # loosen the throttler a bit when a custom client id is used
            self.throttler.rate_limit = 45
            self.throttler.period = 30
            self.custom_client_id_active = True
        # check if we have a librespot binary for this arch
        self._librespot_bin = await get_librespot_binary()
        # try login which will raise if it fails
        await self.login()

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return the features supported by this Provider."""
        base = {
            ProviderFeature.LIBRARY_ARTISTS,
            ProviderFeature.LIBRARY_ALBUMS,
            ProviderFeature.LIBRARY_TRACKS,
            ProviderFeature.LIBRARY_PLAYLISTS,
            ProviderFeature.LIBRARY_ARTISTS_EDIT,
            ProviderFeature.LIBRARY_ALBUMS_EDIT,
            ProviderFeature.LIBRARY_PLAYLISTS_EDIT,
            ProviderFeature.LIBRARY_TRACKS_EDIT,
            ProviderFeature.PLAYLIST_TRACKS_EDIT,
            ProviderFeature.PLAYLIST_CREATE,
            ProviderFeature.BROWSE,
            ProviderFeature.SEARCH,
            ProviderFeature.ARTIST_ALBUMS,
            ProviderFeature.ARTIST_TOPTRACKS,
        }
        if not self.custom_client_id_active:
            # Spotify has killed the similar tracks api for developers
            # https://developer.spotify.com/blog/2024-11-27-changes-to-the-web-api
            base.add(ProviderFeature.SIMILAR_TRACKS)
        return base

    @property
    def instance_name_postfix(self) -> str | None:
        """Return a (default) instance name postfix for this provider instance."""
        if self._sp_user:
            return self._sp_user["display_name"]
        return None

    async def search(
        self, search_query: str, media_types=list[MediaType], limit: int = 5
    ) -> SearchResults:
        """Perform search on musicprovider.

        :param search_query: Search query.
        :param media_types: A list of media_types to include.
        :param limit: Number of items to return in the search (per type).
        """
        searchresult = SearchResults()
        searchtypes = []
        if MediaType.ARTIST in media_types:
            searchtypes.append("artist")
        if MediaType.ALBUM in media_types:
            searchtypes.append("album")
        if MediaType.TRACK in media_types:
            searchtypes.append("track")
        if MediaType.PLAYLIST in media_types:
            searchtypes.append("playlist")
        if not searchtypes:
            return searchresult
        searchtype = ",".join(searchtypes)
        search_query = search_query.replace("'", "")
        offset = 0
        page_limit = min(limit, 50)
        while True:
            items_received = 0
            api_result = await self._get_data(
                "search", q=search_query, type=searchtype, limit=page_limit, offset=offset
            )
            if "artists" in api_result:
                searchresult.artists += [
                    self._parse_artist(item)
                    for item in api_result["artists"]["items"]
                    if (item and item["id"] and item["name"])
                ]
                items_received += len(api_result["artists"]["items"])
            if "albums" in api_result:
                searchresult.albums += [
                    self._parse_album(item)
                    for item in api_result["albums"]["items"]
                    if (item and item["id"])
                ]
                items_received += len(api_result["albums"]["items"])
            if "tracks" in api_result:
                searchresult.tracks += [
                    self._parse_track(item)
                    for item in api_result["tracks"]["items"]
                    if (item and item["id"])
                ]
                items_received += len(api_result["tracks"]["items"])
            if "playlists" in api_result:
                searchresult.playlists += [
                    self._parse_playlist(item)
                    for item in api_result["playlists"]["items"]
                    if (item and item["id"])
                ]
                items_received += len(api_result["playlists"]["items"])
            offset += page_limit
            if offset >= limit:
                break
            if items_received < page_limit:
                break
        return searchresult

    async def get_library_artists(self) -> AsyncGenerator[Artist, None]:
        """Retrieve library artists from spotify."""
        endpoint = "me/following"
        while True:
            spotify_artists = await self._get_data(
                endpoint,
                type="artist",
                limit=50,
            )
            for item in spotify_artists["artists"]["items"]:
                if item and item["id"]:
                    yield self._parse_artist(item)
            if spotify_artists["artists"]["next"]:
                endpoint = spotify_artists["artists"]["next"]
                endpoint = endpoint.replace("https://api.spotify.com/v1/", "")
            else:
                break

    async def get_library_albums(self) -> AsyncGenerator[Album, None]:
        """Retrieve library albums from the provider."""
        async for item in self._get_all_items("me/albums"):
            if item["album"] and item["album"]["id"]:
                yield self._parse_album(item["album"])

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """Retrieve library tracks from the provider."""
        async for item in self._get_all_items("me/tracks"):
            if item and item["track"]["id"]:
                yield self._parse_track(item["track"])

    def _get_liked_songs_playlist_id(self) -> str:
        return f"{LIKED_SONGS_FAKE_PLAYLIST_ID_PREFIX}-{self.instance_id}"

    async def _get_liked_songs_playlist(self) -> Playlist:
        liked_songs = Playlist(
            item_id=self._get_liked_songs_playlist_id(),
            provider=self.lookup_key,
            name=f"Liked Songs {self._sp_user['display_name']}",  # TODO to be translated
            owner=self._sp_user["display_name"],
            provider_mappings={
                ProviderMapping(
                    item_id=self._get_liked_songs_playlist_id(),
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url="https://open.spotify.com/collection/tracks",
                )
            },
        )

        liked_songs.is_editable = False  # TODO Editing requires special endpoints

        liked_songs.metadata.images = [
            MediaItemImage(
                type=ImageType.THUMB,
                path="https://misc.scdn.co/liked-songs/liked-songs-64.png",
                provider=self.lookup_key,
                remotely_accessible=True,
            )
        ]

        liked_songs.cache_checksum = str(time.time())

        return liked_songs

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Retrieve playlists from the provider."""
        yield await self._get_liked_songs_playlist()
        async for item in self._get_all_items("me/playlists"):
            if item and item["id"]:
                yield self._parse_playlist(item)

    async def get_artist(self, prov_artist_id) -> Artist:
        """Get full artist details by id."""
        artist_obj = await self._get_data(f"artists/{prov_artist_id}")
        return self._parse_artist(artist_obj)

    async def get_album(self, prov_album_id) -> Album:
        """Get full album details by id."""
        album_obj = await self._get_data(f"albums/{prov_album_id}")
        return self._parse_album(album_obj)

    async def get_track(self, prov_track_id) -> Track:
        """Get full track details by id."""
        track_obj = await self._get_data(f"tracks/{prov_track_id}")
        return self._parse_track(track_obj)

    async def get_playlist(self, prov_playlist_id) -> Playlist:
        """Get full playlist details by id."""
        if prov_playlist_id == self._get_liked_songs_playlist_id():
            return await self._get_liked_songs_playlist()

        playlist_obj = await self._get_data(f"playlists/{prov_playlist_id}")
        return self._parse_playlist(playlist_obj)

    async def get_album_tracks(self, prov_album_id) -> list[Track]:
        """Get all album tracks for given album id."""
        return [
            self._parse_track(item)
            async for item in self._get_all_items(f"albums/{prov_album_id}/tracks")
            if item["id"]
        ]

    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """Get playlist tracks."""
        result: list[Track] = []
        uri = (
            "me/tracks"
            if prov_playlist_id == self._get_liked_songs_playlist_id()
            else f"playlists/{prov_playlist_id}/tracks"
        )
        page_size = 50
        offset = page * page_size
        spotify_result = await self._get_data(uri, limit=page_size, offset=offset)
        for index, item in enumerate(spotify_result["items"], 1):
            if not (item and item["track"] and item["track"]["id"]):
                continue
            # use count as position
            track = self._parse_track(item["track"])
            track.position = offset + index
            result.append(track)
        return result

    async def get_artist_albums(self, prov_artist_id) -> list[Album]:
        """Get a list of all albums for the given artist."""
        return [
            self._parse_album(item)
            async for item in self._get_all_items(
                f"artists/{prov_artist_id}/albums?include_groups=album,single,compilation"
            )
            if (item and item["id"])
        ]

    async def get_artist_toptracks(self, prov_artist_id) -> list[Track]:
        """Get a list of 10 most popular tracks for the given artist."""
        artist = await self.get_artist(prov_artist_id)
        endpoint = f"artists/{prov_artist_id}/top-tracks"
        items = await self._get_data(endpoint)
        return [
            self._parse_track(item, artist=artist)
            for item in items["tracks"]
            if (item and item["id"])
        ]

    async def library_add(self, item: MediaItemType):
        """Add item to library."""
        if item.media_type == MediaType.ARTIST:
            await self._put_data("me/following", {"ids": [item.item_id]}, type="artist")
        elif item.media_type == MediaType.ALBUM:
            await self._put_data("me/albums", {"ids": [item.item_id]})
        elif item.media_type == MediaType.TRACK:
            await self._put_data("me/tracks", {"ids": [item.item_id]})
        elif item.media_type == MediaType.PLAYLIST:
            await self._put_data(f"playlists/{item.item_id}/followers", data={"public": False})
        return True

    async def library_remove(self, prov_item_id, media_type: MediaType):
        """Remove item from library."""
        if media_type == MediaType.ARTIST:
            await self._delete_data("me/following", {"ids": [prov_item_id]}, type="artist")
        elif media_type == MediaType.ALBUM:
            await self._delete_data("me/albums", {"ids": [prov_item_id]})
        elif media_type == MediaType.TRACK:
            await self._delete_data("me/tracks", {"ids": [prov_item_id]})
        elif media_type == MediaType.PLAYLIST:
            await self._delete_data(f"playlists/{prov_item_id}/followers")
        return True

    async def add_playlist_tracks(self, prov_playlist_id: str, prov_track_ids: list[str]):
        """Add track(s) to playlist."""
        track_uris = [f"spotify:track:{track_id}" for track_id in prov_track_ids]
        data = {"uris": track_uris}
        await self._post_data(f"playlists/{prov_playlist_id}/tracks", data=data)

    async def remove_playlist_tracks(
        self, prov_playlist_id: str, positions_to_remove: tuple[int, ...]
    ) -> None:
        """Remove track(s) from playlist."""
        track_uris = []
        for pos in positions_to_remove:
            uri = f"playlists/{prov_playlist_id}/tracks"
            spotify_result = await self._get_data(uri, limit=1, offset=pos - 1)
            for item in spotify_result["items"]:
                if not (item and item["track"] and item["track"]["id"]):
                    continue
                track_uris.append({"uri": f"spotify:track:{item['track']['id']}"})
        data = {"tracks": track_uris}
        await self._delete_data(f"playlists/{prov_playlist_id}/tracks", data=data)

    async def create_playlist(self, name: str) -> Playlist:
        """Create a new playlist on provider with given name."""
        data = {"name": name, "public": False}
        new_playlist = await self._post_data(f"users/{self._sp_user['id']}/playlists", data=data)
        self._fix_create_playlist_api_bug(new_playlist)
        return self._parse_playlist(new_playlist)

    async def get_similar_tracks(self, prov_track_id, limit=25) -> list[Track]:
        """Retrieve a dynamic list of tracks based on the provided item."""
        endpoint = "recommendations"
        items = await self._get_data(endpoint, seed_tracks=prov_track_id, limit=limit)
        return [self._parse_track(item) for item in items["tracks"] if (item and item["id"])]

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Return the content details for the given track when it will be streamed."""
        return StreamDetails(
            item_id=item_id,
            provider=self.lookup_key,
            audio_format=AudioFormat(
                content_type=ContentType.OGG,
            ),
            stream_type=StreamType.CUSTOM,
            allow_seek=True,
            can_seek=True,
        )

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes, None]:
        """Return the audio stream for the provider item."""
        spotify_uri = f"spotify://track:{streamdetails.item_id}"
        self.logger.log(VERBOSE_LOG_LEVEL, f"Start streaming {spotify_uri} using librespot")
        args = [
            self._librespot_bin,
            "--cache",
            self.cache_dir,
            "--disable-audio-cache",
            "--passthrough",
            "--bitrate",
            "320",
            "--backend",
            "pipe",
            "--single-track",
            spotify_uri,
            "--disable-discovery",
            "--dither",
            "none",
        ]
        if seek_position:
            args += ["--start-position", str(int(seek_position))]

        # we retry twice in case librespot fails to start
        for attempt in (1, 2):
            async with AsyncProcess(
                args,
                stdout=True,
                stderr=None if self.logger.isEnabledFor(VERBOSE_LOG_LEVEL) else False,
                name="librespot",
            ) as librespot_proc:
                # get first chunk with timeout, to catch the issue where librespot is not starting
                # which seems to happen from time to time (but rarely)
                try:
                    chunk = await asyncio.wait_for(librespot_proc.read(64000), timeout=5 * attempt)
                    if not chunk:
                        raise AudioError
                    yield chunk
                except (TimeoutError, AudioError):
                    err_mesg = "No audio received from librespot within timeout"
                    if attempt == 2:
                        raise AudioError(err_mesg)
                    self.logger.warning("%s - will retry once", err_mesg)
                    continue

                # keep yielding chunks until librespot is done
                async for chunk in librespot_proc.iter_chunked():
                    yield chunk

                # if we reach this point, streaming succeeded and we can break the loop
                break

    def _parse_artist(self, artist_obj):
        """Parse spotify artist object to generic layout."""
        artist = Artist(
            item_id=artist_obj["id"],
            provider=self.lookup_key,
            name=artist_obj["name"] or artist_obj["id"],
            provider_mappings={
                ProviderMapping(
                    item_id=artist_obj["id"],
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=artist_obj["external_urls"]["spotify"],
                )
            },
        )
        if "genres" in artist_obj:
            artist.metadata.genres = set(artist_obj["genres"])
        if artist_obj.get("images"):
            for img in artist_obj["images"]:
                img_url = img["url"]
                if "2a96cbd8b46e442fc41c2b86b821562f" not in img_url:
                    artist.metadata.images = [
                        MediaItemImage(
                            type=ImageType.THUMB,
                            path=img_url,
                            provider=self.lookup_key,
                            remotely_accessible=True,
                        )
                    ]
                    break
        return artist

    def _parse_album(self, album_obj: dict):
        """Parse spotify album object to generic layout."""
        name, version = parse_title_and_version(album_obj["name"])
        album = Album(
            item_id=album_obj["id"],
            provider=self.lookup_key,
            name=name,
            version=version,
            provider_mappings={
                ProviderMapping(
                    item_id=album_obj["id"],
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    audio_format=AudioFormat(content_type=ContentType.OGG, bit_rate=320),
                    url=album_obj["external_urls"]["spotify"],
                )
            },
        )
        if "external_ids" in album_obj and album_obj["external_ids"].get("upc"):
            album.external_ids.add((ExternalID.BARCODE, "0" + album_obj["external_ids"]["upc"]))
        if "external_ids" in album_obj and album_obj["external_ids"].get("ean"):
            album.external_ids.add((ExternalID.BARCODE, album_obj["external_ids"]["ean"]))

        for artist_obj in album_obj["artists"]:
            if not artist_obj.get("name") or not artist_obj.get("id"):
                continue
            album.artists.append(self._parse_artist(artist_obj))

        with contextlib.suppress(ValueError):
            album.album_type = AlbumType(album_obj["album_type"])

        if "genres" in album_obj:
            album.metadata.genre = set(album_obj["genres"])
        if album_obj.get("images"):
            album.metadata.images = [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=album_obj["images"][0]["url"],
                    provider=self.lookup_key,
                    remotely_accessible=True,
                )
            ]
        if "label" in album_obj:
            album.metadata.label = album_obj["label"]
        if album_obj.get("release_date"):
            album.year = int(album_obj["release_date"].split("-")[0])
        if album_obj.get("copyrights"):
            album.metadata.copyright = album_obj["copyrights"][0]["text"]
        if album_obj.get("explicit"):
            album.metadata.explicit = album_obj["explicit"]
        return album

    def _parse_track(
        self,
        track_obj: dict[str, Any],
        artist=None,
    ) -> Track:
        """Parse spotify track object to generic layout."""
        name, version = parse_title_and_version(track_obj["name"])
        track = Track(
            item_id=track_obj["id"],
            provider=self.lookup_key,
            name=name,
            version=version,
            duration=track_obj["duration_ms"] / 1000,
            provider_mappings={
                ProviderMapping(
                    item_id=track_obj["id"],
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    audio_format=AudioFormat(
                        content_type=ContentType.OGG,
                        bit_rate=320,
                    ),
                    url=track_obj["external_urls"]["spotify"],
                    available=not track_obj["is_local"] and track_obj["is_playable"],
                )
            },
            disc_number=track_obj.get("disc_number", 0),
            track_number=track_obj.get("track_number", 0),
        )
        if isrc := track_obj.get("external_ids", {}).get("isrc"):
            track.external_ids.add((ExternalID.ISRC, isrc))

        if artist:
            track.artists.append(artist)
        for track_artist in track_obj.get("artists", []):
            if not track_artist.get("name") or not track_artist.get("id"):
                continue
            artist = self._parse_artist(track_artist)
            if artist and artist.item_id not in {x.item_id for x in track.artists}:
                track.artists.append(artist)

        track.metadata.explicit = track_obj["explicit"]
        if "preview_url" in track_obj:
            track.metadata.preview = track_obj["preview_url"]
        if "album" in track_obj:
            track.album = self._parse_album(track_obj["album"])
            if track_obj["album"].get("images"):
                track.metadata.images = [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=track_obj["album"]["images"][0]["url"],
                        provider=self.lookup_key,
                        remotely_accessible=True,
                    )
                ]
        if track_obj.get("copyright"):
            track.metadata.copyright = track_obj["copyright"]
        if track_obj.get("explicit"):
            track.metadata.explicit = True
        if track_obj.get("popularity"):
            track.metadata.popularity = track_obj["popularity"]
        return track

    def _parse_playlist(self, playlist_obj):
        """Parse spotify playlist object to generic layout."""
        is_editable = (
            playlist_obj["owner"]["id"] == self._sp_user["id"] or playlist_obj["collaborative"]
        )
        playlist = Playlist(
            item_id=playlist_obj["id"],
            provider=self.instance_id if is_editable else self.lookup_key,
            name=playlist_obj["name"],
            owner=playlist_obj["owner"]["display_name"],
            provider_mappings={
                ProviderMapping(
                    item_id=playlist_obj["id"],
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=playlist_obj["external_urls"]["spotify"],
                )
            },
            is_editable=is_editable,
        )
        if playlist_obj.get("images"):
            playlist.metadata.images = [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=playlist_obj["images"][0]["url"],
                    provider=self.lookup_key,
                    remotely_accessible=True,
                )
            ]
        if playlist.owner is None:
            playlist.owner = self._sp_user["display_name"]
        playlist.cache_checksum = str(playlist_obj["snapshot_id"])
        return playlist

    @lock
    async def login(self, force_refresh: bool = False) -> dict:
        """Log-in Spotify and return Auth/token info."""
        # return existing token if we have one in memory
        if (
            not force_refresh
            and self._auth_info
            and (self._auth_info["expires_at"] > (time.time() - 600))
        ):
            return self._auth_info
        # request new access token using the refresh token
        if not (refresh_token := self.config.get_value(CONF_REFRESH_TOKEN)):
            raise LoginFailed("Authentication required")

        client_id = self.config.get_value(CONF_CLIENT_ID) or app_var(2)
        params = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        }
        for _ in range(2):
            async with self.mass.http_session.post(
                "https://accounts.spotify.com/api/token", data=params
            ) as response:
                if response.status != 200:
                    err = await response.text()
                    if "revoked" in err:
                        err_msg = f"Failed to refresh access token: {err}"
                        # clear refresh token if it's invalid
                        self.update_config_value(CONF_REFRESH_TOKEN, None)
                        if self.available:
                            # If we're already loaded, we need to unload and set an error
                            self.unload_with_error(err_msg)
                        raise LoginFailed(err_msg)
                    # the token failed to refresh, we allow one retry
                    await asyncio.sleep(2)
                    continue
                # if we reached this point, the token has been successfully refreshed
                auth_info = await response.json()
                auth_info["expires_at"] = int(auth_info["expires_in"] + time.time())
                self.logger.debug("Successfully refreshed access token")
                break
        else:
            if self.available:
                self.mass.create_task(self.mass.unload_provider_with_error(self.instance_id))
            raise LoginFailed(f"Failed to refresh access token: {err}")

        # make sure that our updated creds get stored in memory + config
        self._auth_info = auth_info
        self.update_config_value(CONF_REFRESH_TOKEN, auth_info["refresh_token"], encrypted=True)
        # check if librespot still has valid auth
        args = [
            self._librespot_bin,
            "--cache",
            self.cache_dir,
            "--check-auth",
        ]
        ret_code, stdout = await check_output(*args)
        if ret_code != 0:
            # cached librespot creds are invalid, re-authenticate
            # we can use the check-token option to send a new token to librespot
            # librespot will then get its own token from spotify (somehow) and cache that.
            args += [
                "--access-token",
                auth_info["access_token"],
            ]
            ret_code, stdout = await check_output(*args)
            if ret_code != 0:
                # this should not happen, but guard it just in case
                err = stdout.decode("utf-8").strip()
                raise LoginFailed(f"Failed to verify credentials on Librespot: {err}")

        # get logged-in user info
        if not self._sp_user:
            self._sp_user = userinfo = await self._get_data("me", auth_info=auth_info)
            self.mass.metadata.set_default_preferred_language(userinfo["country"])
            self.logger.info("Successfully logged in to Spotify as %s", userinfo["display_name"])
        return auth_info

    async def _get_all_items(
        self, endpoint, key="items", **kwargs
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Get all items from a paged list."""
        limit = 50
        offset = 0
        while True:
            kwargs["limit"] = limit
            kwargs["offset"] = offset
            result = await self._get_data(endpoint, **kwargs)
            offset += limit
            if not result or key not in result or not result[key]:
                break
            for item in result[key]:
                yield item
            if len(result[key]) < limit:
                break

    @throttle_with_retries
    async def _get_data(self, endpoint, **kwargs) -> dict[str, Any]:
        """Get data from api."""
        url = f"https://api.spotify.com/v1/{endpoint}"
        kwargs["market"] = "from_token"
        kwargs["country"] = "from_token"
        if not (auth_info := kwargs.pop("auth_info", None)):
            auth_info = await self.login()
        headers = {"Authorization": f"Bearer {auth_info['access_token']}"}
        locale = self.mass.metadata.locale.replace("_", "-")
        language = locale.split("-")[0]
        headers["Accept-Language"] = f"{locale}, {language};q=0.9, *;q=0.5"
        async with (
            self.mass.http_session.get(
                url, headers=headers, params=kwargs, ssl=True, timeout=120
            ) as response,
        ):
            # handle spotify rate limiter
            if response.status == 429:
                backoff_time = int(response.headers["Retry-After"])
                raise ResourceTemporarilyUnavailable(
                    "Spotify Rate Limiter", backoff_time=backoff_time
                )
            # handle temporary server error
            if response.status in (502, 503):
                raise ResourceTemporarilyUnavailable(backoff_time=30)

            # handle token expired, raise ResourceTemporarilyUnavailable
            # so it will be retried (and the token refreshed)
            if response.status == 401:
                self._auth_info = None
                raise ResourceTemporarilyUnavailable("Token expired", backoff_time=0.05)

            # handle 404 not found, convert to MediaNotFoundError
            if response.status == 404:
                raise MediaNotFoundError(f"{endpoint} not found")
            response.raise_for_status()
            return await response.json(loads=json_loads)

    @throttle_with_retries
    async def _delete_data(self, endpoint, data=None, **kwargs) -> None:
        """Delete data from api."""
        url = f"https://api.spotify.com/v1/{endpoint}"
        auth_info = kwargs.pop("auth_info", await self.login())
        headers = {"Authorization": f"Bearer {auth_info['access_token']}"}
        async with self.mass.http_session.delete(
            url, headers=headers, params=kwargs, json=data, ssl=False
        ) as response:
            # handle spotify rate limiter
            if response.status == 429:
                backoff_time = int(response.headers["Retry-After"])
                raise ResourceTemporarilyUnavailable(
                    "Spotify Rate Limiter", backoff_time=backoff_time
                )
            # handle token expired, raise ResourceTemporarilyUnavailable
            # so it will be retried (and the token refreshed)
            if response.status == 401:
                self._auth_info = None
                raise ResourceTemporarilyUnavailable("Token expired", backoff_time=0.05)
            # handle temporary server error
            if response.status in (502, 503):
                raise ResourceTemporarilyUnavailable(backoff_time=30)
            response.raise_for_status()

    @throttle_with_retries
    async def _put_data(self, endpoint, data=None, **kwargs) -> None:
        """Put data on api."""
        url = f"https://api.spotify.com/v1/{endpoint}"
        auth_info = kwargs.pop("auth_info", await self.login())
        headers = {"Authorization": f"Bearer {auth_info['access_token']}"}
        async with self.mass.http_session.put(
            url, headers=headers, params=kwargs, json=data, ssl=False
        ) as response:
            # handle spotify rate limiter
            if response.status == 429:
                backoff_time = int(response.headers["Retry-After"])
                raise ResourceTemporarilyUnavailable(
                    "Spotify Rate Limiter", backoff_time=backoff_time
                )
            # handle token expired, raise ResourceTemporarilyUnavailable
            # so it will be retried (and the token refreshed)
            if response.status == 401:
                self._auth_info = None
                raise ResourceTemporarilyUnavailable("Token expired", backoff_time=0.05)

            # handle temporary server error
            if response.status in (502, 503):
                raise ResourceTemporarilyUnavailable(backoff_time=30)
            response.raise_for_status()

    @throttle_with_retries
    async def _post_data(self, endpoint, data=None, **kwargs) -> dict[str, Any]:
        """Post data on api."""
        url = f"https://api.spotify.com/v1/{endpoint}"
        auth_info = kwargs.pop("auth_info", await self.login())
        headers = {"Authorization": f"Bearer {auth_info['access_token']}"}
        async with self.mass.http_session.post(
            url, headers=headers, params=kwargs, json=data, ssl=False
        ) as response:
            # handle spotify rate limiter
            if response.status == 429:
                backoff_time = int(response.headers["Retry-After"])
                raise ResourceTemporarilyUnavailable(
                    "Spotify Rate Limiter", backoff_time=backoff_time
                )
            # handle token expired, raise ResourceTemporarilyUnavailable
            # so it will be retried (and the token refreshed)
            if response.status == 401:
                self._auth_info = None
                raise ResourceTemporarilyUnavailable("Token expired", backoff_time=0.05)
            # handle temporary server error
            if response.status in (502, 503):
                raise ResourceTemporarilyUnavailable(backoff_time=30)
            response.raise_for_status()
            return await response.json(loads=json_loads)

    def _fix_create_playlist_api_bug(self, playlist_obj: dict[str, Any]) -> None:
        """Fix spotify API bug where incorrect owner id is returned from Create Playlist."""
        if playlist_obj["owner"]["id"] != self._sp_user["id"]:
            playlist_obj["owner"]["id"] = self._sp_user["id"]
            playlist_obj["owner"]["display_name"] = self._sp_user["display_name"]
        else:
            self.logger.warning(
                "FIXME: Spotify have fixed their Create Playlist API, this fix can be removed."
            )
