"""YT Music support for MusicAssistant"""
import json
from requests.structures import CaseInsensitiveDict
from typing import Dict, List, Optional

import ytmusicapi

from music_assistant.models.enums import ProviderType
from music_assistant.models.errors import LoginFailed, MediaNotFoundError
from music_assistant.models.media_items import (
    Album,
    AlbumType,
    Artist,
    ContentType,
    ImageType,
    MediaItemImage,
    MediaItemProviderId,
    MediaItemType,
    MediaQuality,
    MediaType,
    Playlist,
    StreamDetails,
    Track,
)
from music_assistant.models.music_provider import MusicProvider

YTM_DOMAIN = "https://music.youtube.com"
YTM_BASE_URL = f"{YTM_DOMAIN}/youtubei/v1/"


class YTMusic(MusicProvider):
    """Provider for Youtube Music"""

    _attr_type = ProviderType.YTMUSIC
    _attr_name = "YTMusic"
    _attr_supported_mediatypes = [
        MediaType.ARTIST,
        MediaType.ALBUM,
        MediaType.TRACK,
        MediaType.PLAYLIST,
    ]
    _headers = None

    async def setup(self) -> bool:
        """Sets up the YTMusic provider"""
        self._headers = await self._initialize_headers()
        self._context = await self._initialize_context()
        return True

    async def get_album(self, prov_album_id) -> Album:
        """Get full album details by id."""
        data = {"browseId": prov_album_id}
        album_obj = await self._post_data(endpoint="browse", data=data)
        return (
            await self._parse_album(album_obj=album_obj)
            if album_obj
            else None
        )

    async def get_artist(self, prov_artist_id) -> Artist:
        """Get full artist details by id"""

    async def get_track(self, prov_track_id) -> Track:
        """Get full track details by id."""
        signatureTimestamp = ytmusicapi.mixins._utils.get_datestamp() - 1
        data = {
            "playbackContext": {
                "contentPlaybackContext": {
                    "signatureTimestamp": signatureTimestamp
                }
            },
            "video_id": prov_track_id
        }
        track_obj = await self._post_data(f"player", data=data)
        return await self._parse_track(track_obj) if track_obj else None

    async def _post_data(self, endpoint: str, data: Dict[str, str], **kwargs):
        url = f"{YTM_BASE_URL}{endpoint}"
        data.update(self._context)

        async with self.mass.http_session.post(
            url, headers=self._headers, json=data, verify_ssl=False
        ) as response:
            return await response.json()

    async def _initialize_headers(self) -> Dict[str, str]:
        """Returns headers to include in the requests"""
        # TODO: Replace with Cookie string from Config
        path = "../headers_auth.json"
        headers = None
        with open(path) as json_file:
            headers = CaseInsensitiveDict(json.load(json_file))
        cookie = headers.get("cookie")
        sapisid = ytmusicapi.helpers.sapisid_from_cookie(cookie)
        origin = headers.get('origin', headers.get('x-origin'))
        headers["Authorization"] = ytmusicapi.helpers.get_authorization(sapisid + ' ' + origin)

        return headers


    async def _initialize_context(self) -> Dict[str, str]:
        """Returns a dict to use as a context in requests"""
        return {
            "context": {
                "client": {
                    "clientName": "WEB_REMIX",
                    "clientVersion": "0.1"
                },
                "user": {}
            }
        }

    async def _parse_album(self, album_obj: dict) -> Album:
        """Parses a YT Album response to an Album model object"""
        parsed_album = ytmusicapi.parsers.albums.parse_album_header(album_obj)

        album = Album(
            item_id = parsed_album["audioPlaylistId"],
            name = parsed_album["title"],
            album_type = AlbumType.ALBUM,
            provider = self.type
        )
        #TODO Add metadata
        return album

    async def _parse_artist(self, artist_obj: dict) -> Artist:
        """Parse a YT Artist response to Artist model object"""
        print(json.dumps(artist_obj))

    async def _parse_track(self, track_obj: dict) -> Track:
        """Parses a YT Track response to a Track model object"""
        keys = ['videoDetails', 'playabilityStatus', 'streamingData', 'microformat']
        for k in list(track_obj.keys()):
            if k not in keys:
                del track_obj[k]
        print(json.dumps(track_obj))
        track = Track(
            item_id=track_obj["videoDetails"]["videoId"],
            provider=self.type,
            name=track_obj["videoDetails"]["title"]
        )
        return track