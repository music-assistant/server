"""Deezer music provider support for MusicAssistant."""

import hashlib
import uuid
from asyncio import TaskGroup
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from math import ceil
from typing import Any, Literal, cast

import deezer
from aiohttp import ClientSession, ClientTimeout
from Crypto.Cipher import Blowfish
from deezer import exceptions as deezer_exceptions
from music_assistant_models.config_entries import ConfigEntry, ConfigValueType, ProviderConfig
from music_assistant_models.enums import (
    AlbumType,
    ConfigEntryType,
    ContentType,
    ExternalID,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import InvalidDataError, LoginFailed, MediaNotFoundError
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    ItemMapping,
    MediaItemImage,
    MediaItemMetadata,
    MediaItemType,
    Playlist,
    ProviderMapping,
    RecommendationFolder,
    SearchResults,
    Track,
    UniqueList,
)
from music_assistant_models.provider import ProviderManifest
from music_assistant_models.streamdetails import StreamDetails

from music_assistant import MusicAssistant
from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.app_vars import app_var  # type: ignore[attr-defined]
from music_assistant.helpers.auth import AuthenticationHelper
from music_assistant.helpers.datetime import utc_timestamp
from music_assistant.helpers.util import infer_album_type, parse_title_and_version
from music_assistant.models import ProviderInstanceType
from music_assistant.models.music_provider import MusicProvider

from .gw_client import GWClient

SUPPORTED_FEATURES = {
    ProviderFeature.LIBRARY_ARTISTS,
    ProviderFeature.LIBRARY_ALBUMS,
    ProviderFeature.LIBRARY_TRACKS,
    ProviderFeature.LIBRARY_PLAYLISTS,
    ProviderFeature.LIBRARY_ALBUMS_EDIT,
    ProviderFeature.LIBRARY_TRACKS_EDIT,
    ProviderFeature.LIBRARY_ARTISTS_EDIT,
    ProviderFeature.LIBRARY_PLAYLISTS_EDIT,
    ProviderFeature.ALBUM_METADATA,
    ProviderFeature.TRACK_METADATA,
    ProviderFeature.ARTIST_METADATA,
    ProviderFeature.ARTIST_ALBUMS,
    ProviderFeature.ARTIST_TOPTRACKS,
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
    ProviderFeature.PLAYLIST_TRACKS_EDIT,
    ProviderFeature.PLAYLIST_CREATE,
    ProviderFeature.RECOMMENDATIONS,
    ProviderFeature.SIMILAR_TRACKS,
}


@dataclass
class DeezerCredentials:
    """Class for storing credentials."""

    app_id: int
    app_secret: str
    access_token: str


CONF_ACCESS_TOKEN = "access_token"
CONF_ARL_TOKEN = "arl_token"
CONF_ACTION_AUTH = "auth"
DEEZER_AUTH_URL = "https://connect.deezer.com/oauth/auth.php"
RELAY_URL = "https://deezer.oauth.jonathanbangert.com/"
DEEZER_PERMS = "basic_access,email,offline_access,manage_library,\
manage_community,delete_library,listening_history"
DEEZER_APP_ID = app_var(6)
DEEZER_APP_SECRET = app_var(7)

# Virtual playlist IDs for dynamic Deezer content
FLOW_PLAYLIST_ID = "flow"
RECOMMENDED_TRACKS_PLAYLIST_ID = "recommended_tracks"
TOP_CHARTS_PLAYLIST_ID = "top_charts"
RADIO_PLAYLIST_PREFIX = "radio_"
MOOD_FLOW_PREFIX = "mood_flow_"

# Curated Deezer radio station IDs
CURATED_RADIO_IDS = [
    37151,  # Hits
    38305,  # The '80s
    38295,  # The '70s
    31061,  # Pop
    37765,  # Rock classics
    30901,  # Metal
    30991,  # Hip Hop
    30771,  # Indie
    30621,  # Electronic
    31031,  # Jazz
    30661,  # Classical
    36791,  # Latin Music
    38225,  # Focus
    39041,  # Happy Hour
]


async def get_access_token(
    app_id: str, app_secret: str, code: str, http_session: ClientSession
) -> str:
    """Update the access_token."""
    response = await http_session.post(
        "https://connect.deezer.com/oauth/access_token.php",
        params={"code": code, "app_id": app_id, "secret": app_secret},
        ssl=False,
    )
    if response.status != 200:
        msg = f"HTTP Error {response.status}: {response.reason}"
        raise ConnectionError(msg)
    response_text = await response.text()
    try:
        return response_text.split("=")[1].split("&")[0]
    except Exception as error:
        msg = "Invalid auth code"
        raise LoginFailed(msg) from error


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return DeezerProvider(mass, manifest, config, SUPPORTED_FEATURES)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    # Action is to launch oauth flow
    if action == CONF_ACTION_AUTH:
        # Use the AuthenticationHelper to authenticate
        if not values or "session_id" not in values:
            raise InvalidDataError("session_id not found in values")
        async with AuthenticationHelper(mass, cast("str", values["session_id"])) as auth_helper:
            url = f"{DEEZER_AUTH_URL}?app_id={DEEZER_APP_ID}&redirect_uri={RELAY_URL}\
&perms={DEEZER_PERMS}&state={auth_helper.callback_url}"
            code = (await auth_helper.authenticate(url))["code"]
            values[CONF_ACCESS_TOKEN] = await get_access_token(
                DEEZER_APP_ID, DEEZER_APP_SECRET, code, mass.http_session
            )

    return (
        ConfigEntry(
            key=CONF_ACCESS_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            label="Access token",
            required=True,
            action=CONF_ACTION_AUTH,
            description="You need to authenticate on Deezer.",
            action_label="Authenticate with Deezer",
            value=values.get(CONF_ACCESS_TOKEN) if values else None,
        ),
        ConfigEntry(
            key=CONF_ARL_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            label="Arl token",
            required=True,
            description="See https://www.dumpmedia.com/deezplus/deezer-arl.html",
            value=values.get(CONF_ARL_TOKEN) if values else None,
        ),
    )


class DeezerProvider(MusicProvider):
    """Deezer provider support."""

    client: deezer.Client
    gw_client: GWClient
    credentials: DeezerCredentials
    user: deezer.User

    async def handle_async_init(self) -> None:
        """Handle async init of the Deezer provider."""
        self.credentials = DeezerCredentials(
            app_id=DEEZER_APP_ID,
            app_secret=DEEZER_APP_SECRET,
            access_token=cast("str", self.config.get_value(CONF_ACCESS_TOKEN)),
        )

        self.client = deezer.Client(
            app_id=self.credentials.app_id,
            app_secret=self.credentials.app_secret,
            access_token=self.credentials.access_token,
        )

        self.user = await self.client.get_user()

        self.gw_client = GWClient(
            self.mass.http_session,
            str(self.config.get_value(CONF_ACCESS_TOKEN)),
            str(self.config.get_value(CONF_ARL_TOKEN)),
        )
        await self.gw_client.setup()

    # Cached wrappers for dynamic Deezer content (ensures consistent data across calls)
    @use_cache(3600)  # Cache for 1 hour
    async def _get_flow_tracks(self) -> list[deezer.Track]:
        """Get cached Flow tracks."""
        return list(await self.client.get_user_flow())

    @use_cache(3600)  # Cache for 1 hour
    async def _get_recommended_tracks(self) -> list[deezer.Track]:
        """Get cached recommended tracks."""
        return list(await self.client.get_user_recommended_tracks())

    @use_cache(3600)  # Cache for 1 hour
    async def _get_chart_tracks(self) -> list[deezer.Track]:
        """Get cached chart tracks."""
        chart = await self.client.get_chart()
        return list(chart.tracks[:100]) if chart.tracks else []

    @use_cache(3600)  # Cache for 1 hour
    async def _get_mood_flow_tracks(self, config_id: str) -> list[dict[str, Any]]:
        """Get cached mood/genre Flow tracks from the GW API.

        :param config_id: The Flow config identifier (e.g. "happy", "chill", "genre-rock").
        """
        return await self.gw_client.get_user_radio(config_id)

    @use_cache(3600 * 24)  # Cache for 24 hours
    async def _get_available_flows(self) -> list[tuple[str, str, str | None]]:
        """Discover available mood/genre Flow variants from the Deezer home page.

        Genre flows have config_ids starting with 'genre-'.
        Returns a list of (config_id, display_name, cover_url) tuples.
        """
        items = await self.gw_client.get_home_flows()
        flows: list[tuple[str, str, str | None]] = []
        for item in items:
            config_id = item["data"]["id"]
            if config_id == "default":
                continue
            title = f"Flow: {item['title']}"
            cover_url = None
            if pictures := item.get("pictures"):
                cover_url = f"https://e-cdns-images.dzcdn.net/images/misc/{pictures[0]['md5']}/264x264-000000-80-0-0.jpg"
            flows.append((config_id, title, cover_url))
        return flows

    @use_cache(3600 * 24 * 7)  # Cache for 7 days
    async def search(
        self, search_query: str, media_types: list[MediaType], limit: int = 5
    ) -> SearchResults:
        """Perform search on music provider.

        :param search_query: Search query.
        :param media_types: A list of media_types to include. All types if None.
        """
        # Create a task for each media_type
        tasks: dict[MediaType, Any] = {}

        async with TaskGroup() as taskgroup:
            for media_type in media_types:
                if media_type == MediaType.TRACK:
                    tasks[MediaType.TRACK] = taskgroup.create_task(
                        self.search_and_parse_tracks(
                            query=search_query,
                            limit=limit,
                            user_country=self.gw_client.user_country,
                        )
                    )
                elif media_type == MediaType.ARTIST:
                    tasks[MediaType.ARTIST] = taskgroup.create_task(
                        self.search_and_parse_artists(query=search_query, limit=limit)
                    )
                elif media_type == MediaType.ALBUM:
                    tasks[MediaType.ALBUM] = taskgroup.create_task(
                        self.search_and_parse_albums(query=search_query, limit=limit)
                    )
                elif media_type == MediaType.PLAYLIST:
                    tasks[MediaType.PLAYLIST] = taskgroup.create_task(
                        self.search_and_parse_playlists(query=search_query, limit=limit)
                    )

        results = SearchResults()

        for media_type, task in tasks.items():
            if media_type == MediaType.ARTIST:
                results.artists = task.result()
            elif media_type == MediaType.ALBUM:
                results.albums = task.result()
            elif media_type == MediaType.TRACK:
                results.tracks = task.result()
            elif media_type == MediaType.PLAYLIST:
                results.playlists = task.result()

        return results

    async def get_library_artists(self) -> AsyncGenerator[Artist, None]:
        """Retrieve all library artists from Deezer."""
        async for artist in await self.client.get_user_artists():
            yield self.parse_artist(artist=artist)

    async def get_library_albums(self) -> AsyncGenerator[Album, None]:
        """Retrieve all library albums from Deezer."""
        async for album in await self.client.get_user_albums():
            yield self.parse_album(album=album)

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Retrieve all library playlists from Deezer."""
        async for playlist in await self.user.get_playlists():
            yield self.parse_playlist(playlist=playlist)

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """Retrieve all library tracks from Deezer."""
        async for track in await self.client.get_user_tracks():
            yield self.parse_track(track=track, user_country=self.gw_client.user_country)

    @use_cache(3600 * 24 * 30)  # Cache for 30 days
    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get full artist details by id."""
        try:
            return self.parse_artist(
                artist=await self.client.get_artist(artist_id=int(prov_artist_id))
            )
        except deezer_exceptions.DeezerErrorResponse as error:
            self.logger.warning("Failed getting artist: %s", error)
            raise MediaNotFoundError(f"Artist {prov_artist_id} not found on Deezer") from error

    @use_cache(3600 * 24 * 30)  # Cache for 30 days
    async def get_album(self, prov_album_id: str) -> Album:
        """Get full album details by id."""
        try:
            return self.parse_album(album=await self.client.get_album(album_id=int(prov_album_id)))
        except deezer_exceptions.DeezerErrorResponse as error:
            self.logger.warning("Failed getting album: %s", error)
            raise MediaNotFoundError(f"Album {prov_album_id} not found on Deezer") from error

    @use_cache(3600 * 24 * 30)  # Cache for 30 days
    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get full playlist details by id."""
        # Handle virtual playlists (Flow, Recommended tracks, Top Charts, Radios)
        if prov_playlist_id == FLOW_PLAYLIST_ID:
            flow_tracks = await self._get_flow_tracks()
            flow_cover = None
            if flow_tracks and hasattr(flow_tracks[0], "album"):
                flow_cover = getattr(flow_tracks[0].album, "cover_medium", None)
            return self._create_virtual_playlist(FLOW_PLAYLIST_ID, "Flow", image_url=flow_cover)
        if prov_playlist_id == RECOMMENDED_TRACKS_PLAYLIST_ID:
            rec_tracks = await self._get_recommended_tracks()
            rec_cover = None
            if rec_tracks and hasattr(rec_tracks[0], "album"):
                rec_cover = getattr(rec_tracks[0].album, "cover_medium", None)
            return self._create_virtual_playlist(
                RECOMMENDED_TRACKS_PLAYLIST_ID, "Recommended tracks", image_url=rec_cover
            )
        if prov_playlist_id == TOP_CHARTS_PLAYLIST_ID:
            chart_tracks = await self._get_chart_tracks()
            chart_cover = None
            if chart_tracks and hasattr(chart_tracks[0], "album"):
                chart_cover = getattr(chart_tracks[0].album, "cover_medium", None)
            return self._create_virtual_playlist(
                TOP_CHARTS_PLAYLIST_ID, "Top Charts", image_url=chart_cover
            )
        if prov_playlist_id.startswith(RADIO_PLAYLIST_PREFIX):
            radio_id = int(prov_playlist_id.replace(RADIO_PLAYLIST_PREFIX, ""))
            try:
                radio = await self.client.get_radio(radio_id)
                return self._create_virtual_playlist(
                    prov_playlist_id,
                    f"Radio: {radio.title}",
                    image_url=getattr(radio, "picture_medium", None),
                )
            except Exception as err:
                self.logger.warning("Failed getting radio %s: %s", radio_id, err)
                raise MediaNotFoundError(f"Radio {prov_playlist_id} not found on Deezer") from err
        if prov_playlist_id.startswith(MOOD_FLOW_PREFIX):
            config_id = prov_playlist_id.removeprefix(MOOD_FLOW_PREFIX)
            all_flows = await self._get_available_flows()
            flow_info = {cid: (name, cover) for cid, name, cover in all_flows}
            name, cover_url = flow_info.get(config_id, (f"Flow: {config_id}", None))
            return self._create_virtual_playlist(prov_playlist_id, name, image_url=cover_url)
        try:
            return self.parse_playlist(
                playlist=await self.client.get_playlist(playlist_id=int(prov_playlist_id)),
            )
        except deezer_exceptions.DeezerErrorResponse as error:
            self.logger.warning("Failed getting playlist: %s", error)
            raise MediaNotFoundError(f"Album {prov_playlist_id} not found on Deezer") from error

    @use_cache(3600 * 24 * 30)  # Cache for 30 days
    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        try:
            return self.parse_track(
                track=await self.client.get_track(track_id=int(prov_track_id)),
                user_country=self.gw_client.user_country,
            )
        except deezer_exceptions.DeezerErrorResponse as error:
            self.logger.warning("Failed getting track: %s", error)
            raise MediaNotFoundError(f"Album {prov_track_id} not found on Deezer") from error

    @use_cache(3600 * 24 * 30)  # Cache for 30 days
    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get all tracks in an album."""
        album = await self.client.get_album(album_id=int(prov_album_id))
        return [
            self.parse_track(
                track=deezer_track,
                user_country=self.gw_client.user_country,
                # TODO: doesn't Deezer have disc and track number in the api ?
                position=0,
            )
            for deezer_track in await album.get_tracks()
        ]

    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """Get playlist tracks."""
        if page > 0:
            # paging not supported, we always return the whole list at once
            return []

        # Virtual playlists use their own cached wrappers (not double-cached)
        if prov_playlist_id == FLOW_PLAYLIST_ID:
            return self._parse_tracks_list(await self._get_flow_tracks())

        if prov_playlist_id == RECOMMENDED_TRACKS_PLAYLIST_ID:
            return self._parse_tracks_list(await self._get_recommended_tracks())

        if prov_playlist_id == TOP_CHARTS_PLAYLIST_ID:
            return self._parse_tracks_list(await self._get_chart_tracks())

        if prov_playlist_id.startswith(RADIO_PLAYLIST_PREFIX):
            radio_id = int(prov_playlist_id.replace(RADIO_PLAYLIST_PREFIX, ""))
            try:
                radio = await self.client.get_radio(radio_id)
                return self._parse_tracks_list(list(await radio.get_tracks()))
            except Exception as err:
                self.logger.debug("Failed to get radio tracks %s: %s", radio_id, err)
                return []

        if prov_playlist_id.startswith(MOOD_FLOW_PREFIX):
            config_id = prov_playlist_id.removeprefix(MOOD_FLOW_PREFIX)
            gw_tracks = await self._get_mood_flow_tracks(config_id)
            return [await self.get_track(track["SNG_ID"]) for track in gw_tracks]

        # Regular Deezer playlists (cached separately)
        return await self._get_regular_playlist_tracks(prov_playlist_id)

    @use_cache(3600 * 3)  # Cache for 3 hours
    async def _get_regular_playlist_tracks(self, prov_playlist_id: str) -> list[Track]:
        """Get tracks for regular Deezer playlists (cached)."""
        playlist = await self.client.get_playlist(int(prov_playlist_id))
        playlist_tracks = await playlist.get_tracks()
        return self._parse_tracks_list(list(playlist_tracks))

    def _parse_tracks_list(self, tracks: list[deezer.Track]) -> list[Track]:
        """Parse a list of Deezer tracks to Music Assistant tracks."""
        return [
            self.parse_track(
                track=track,
                user_country=self.gw_client.user_country,
                position=index,
            )
            for index, track in enumerate(tracks, 1)
        ]

    @use_cache(3600 * 24 * 7)  # Cache for 7 days
    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Get albums by an artist."""
        artist = await self.client.get_artist(artist_id=int(prov_artist_id))
        return [self.parse_album(album=album) async for album in await artist.get_albums()]

    @use_cache(3600 * 24 * 7)  # Cache for 7 days
    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """Get top 50 tracks of an artist."""
        artist = await self.client.get_artist(artist_id=int(prov_artist_id))
        return [
            self.parse_track(track=track, user_country=self.gw_client.user_country)
            async for track in await artist.get_top(limit=50)
        ]

    async def library_add(self, item: MediaItemType) -> bool:
        """Add an item to the provider's library/favorites."""
        result = False
        if item.media_type == MediaType.ARTIST:
            result = bool(
                await self.client.add_user_artist(
                    artist_id=int(item.item_id),
                )
            )
        elif item.media_type == MediaType.ALBUM:
            result = bool(
                await self.client.add_user_album(
                    album_id=int(item.item_id),
                )
            )
        elif item.media_type == MediaType.TRACK:
            result = bool(
                await self.client.add_user_track(
                    track_id=int(item.item_id),
                )
            )
        elif item.media_type == MediaType.PLAYLIST:
            result = bool(
                await self.client.add_user_playlist(
                    playlist_id=int(item.item_id),
                )
            )
        else:
            raise NotImplementedError
        return result

    async def library_remove(self, prov_item_id: str, media_type: MediaType) -> bool:
        """Remove an item from the provider's library/favorites."""
        result = False
        if media_type == MediaType.ARTIST:
            result = bool(
                await self.client.remove_user_artist(
                    artist_id=int(prov_item_id),
                )
            )
        elif media_type == MediaType.ALBUM:
            result = bool(
                await self.client.remove_user_album(
                    album_id=int(prov_item_id),
                )
            )
        elif media_type == MediaType.TRACK:
            result = bool(
                await self.client.remove_user_track(
                    track_id=int(prov_item_id),
                )
            )
        elif media_type == MediaType.PLAYLIST:
            result = bool(
                await self.client.remove_user_playlist(
                    playlist_id=int(prov_item_id),
                )
            )
        else:
            raise NotImplementedError
        return result

    @use_cache(3600)
    async def recommendations(self) -> list[RecommendationFolder]:
        """Get Deezer's recommendations including Flow and personalized content."""
        result: list[RecommendationFolder] = []

        # Made for you - combines Flow, Recommended tracks, and recommended playlists
        # Get covers from first track's album for each virtual playlist
        flow_cover = None
        flow_tracks = await self._get_flow_tracks()
        if flow_tracks and hasattr(flow_tracks[0], "album"):
            flow_cover = getattr(flow_tracks[0].album, "cover_medium", None)

        recommended_cover = None
        recommended_tracks = await self._get_recommended_tracks()
        if recommended_tracks and hasattr(recommended_tracks[0], "album"):
            recommended_cover = getattr(recommended_tracks[0].album, "cover_medium", None)

        chart_tracks = await self._get_chart_tracks()
        chart_cover = None
        if chart_tracks and hasattr(chart_tracks[0], "album"):
            chart_cover = getattr(chart_tracks[0].album, "cover_medium", None)

        made_for_you_items: list[Playlist] = [
            # Flow - personalized endless radio
            self._create_virtual_playlist(FLOW_PLAYLIST_ID, "Flow", image_url=flow_cover),
            # Recommended tracks
            self._create_virtual_playlist(
                RECOMMENDED_TRACKS_PLAYLIST_ID, "Recommended tracks", image_url=recommended_cover
            ),
            # Top Charts - global top tracks
            self._create_virtual_playlist(
                TOP_CHARTS_PLAYLIST_ID, "Top Charts", image_url=chart_cover
            ),
        ]
        # Add recommended playlists from Deezer
        for playlist in await self.client.get_user_recommended_playlists():
            made_for_you_items.append(self.parse_playlist(playlist=playlist))

        result.append(
            RecommendationFolder(
                item_id="made_for_you",
                provider=self.instance_id,
                name="Made for you",
                items=UniqueList(made_for_you_items),
            )
        )

        # Recommended albums
        try:
            recommended_albums = list(await self.client.get_user_recommended_albums())
            if recommended_albums:
                result.append(
                    RecommendationFolder(
                        item_id="recommended_albums",
                        provider=self.instance_id,
                        name="Recommended albums",
                        items=UniqueList(
                            [self.parse_album(album=album) for album in recommended_albums]
                        ),
                    )
                )
        except deezer_exceptions.DeezerErrorResponse as err:
            self.logger.debug("Failed to get recommended albums: %s", err)

        # Recommended artists
        try:
            recommended_artists = list(await self.client.get_user_recommended_artists())
            if recommended_artists:
                result.append(
                    RecommendationFolder(
                        item_id="recommended_artists",
                        provider=self.instance_id,
                        name="Recommended artists",
                        items=UniqueList(
                            [self.parse_artist(artist=artist) for artist in recommended_artists]
                        ),
                    )
                )
        except deezer_exceptions.DeezerErrorResponse as err:
            self.logger.debug("Failed to get recommended artists: %s", err)

        # Deezer Mood and Genre Flows - personalized playlists (dynamically discovered)
        all_flows = await self._get_available_flows()
        mood_flows = [(c, n, img) for c, n, img in all_flows if not c.startswith("genre-")]
        genre_flows = [(c, n, img) for c, n, img in all_flows if c.startswith("genre-")]
        for folder_id, folder_name, flows in [
            ("mood_flows", "Deezer Mood Flows", mood_flows),
            ("genre_flows", "Deezer Genre Flows", genre_flows),
        ]:
            flow_playlists = [
                self._create_virtual_playlist(
                    item_id=f"{MOOD_FLOW_PREFIX}{config_id}",
                    name=display_name,
                    image_url=cover_url,
                )
                for config_id, display_name, cover_url in flows
            ]
            if flow_playlists:
                result.append(
                    RecommendationFolder(
                        item_id=folder_id,
                        provider=self.instance_id,
                        name=folder_name,
                        items=UniqueList(flow_playlists),
                    )
                )

        # Deezer Radios - curated selection (as virtual playlists in one folder)
        radio_playlists: list[Playlist] = []
        for radio_id in CURATED_RADIO_IDS:
            try:
                radio = await self.client.get_radio(radio_id)
                radio_playlists.append(
                    self._create_virtual_playlist(
                        item_id=f"{RADIO_PLAYLIST_PREFIX}{radio_id}",
                        name=f"Radio: {radio.title}",
                        image_url=getattr(radio, "picture_medium", None),
                    )
                )
            except Exception as err:
                self.logger.debug("Failed to load radio %s: %s", radio_id, err)

        if radio_playlists:
            result.append(
                RecommendationFolder(
                    item_id="radios",
                    provider=self.instance_id,
                    name="Deezer Radios",
                    items=UniqueList(radio_playlists),
                )
            )

        return result

    async def add_playlist_tracks(self, prov_playlist_id: str, prov_track_ids: list[str]) -> None:
        """Add track(s) to playlist."""
        playlist = await self.client.get_playlist(int(prov_playlist_id))
        await playlist.add_tracks(tracks=[int(i) for i in prov_track_ids])

    async def remove_playlist_tracks(
        self, prov_playlist_id: str, positions_to_remove: tuple[int, ...]
    ) -> None:
        """Remove track(s) from playlist."""
        playlist_track_ids = []
        for track in await self.get_playlist_tracks(prov_playlist_id, 0):
            if track.position in positions_to_remove:
                playlist_track_ids.append(int(track.item_id))
            if len(playlist_track_ids) == len(positions_to_remove):
                break
        playlist = await self.client.get_playlist(int(prov_playlist_id))
        await playlist.delete_tracks(playlist_track_ids)

    async def create_playlist(self, name: str, media_types: set[MediaType]) -> Playlist:
        """Create a new playlist on provider with given name."""
        playlist_id = await self.client.create_playlist(playlist_name=name)
        playlist = await self.client.get_playlist(playlist_id)
        return self.parse_playlist(playlist=playlist)

    @use_cache(3600 * 24)  # Cache for 24 hours
    async def get_similar_tracks(self, prov_track_id: str, limit: int = 25) -> list[Track]:
        """Retrieve a dynamic list of tracks based on the provided item."""
        endpoint = "song.getSearchTrackMix"
        tracks = (await self.gw_client._gw_api_call(endpoint, args={"SNG_ID": prov_track_id}))[
            "results"
        ]["data"][:limit]
        return [await self.get_track(track["SNG_ID"]) for track in tracks]

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Return the content details for the given track when it will be streamed."""
        url_details, song_data = await self.gw_client.get_deezer_track_urls(item_id)
        url = url_details["sources"][0]["url"]
        return StreamDetails(
            item_id=item_id,
            provider=self.instance_id,
            audio_format=AudioFormat(
                content_type=ContentType.try_parse(url_details["format"].split("_")[0])
            ),
            stream_type=StreamType.CUSTOM,
            duration=int(song_data["DURATION"]),
            # Due to track replacement, the track ID of the stream may be different from the ID
            # that is stored. We need the proper track ID to decrypt the stream, so store it
            # separately so we can use it later on.
            data={"url": url, "format": url_details["format"], "track_id": song_data["SNG_ID"]},
            size=int(song_data[f"FILESIZE_{url_details['format']}"]),
            can_seek=True,
            allow_seek=True,
        )

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes, None]:
        """Return the audio stream for the provider item."""
        blowfish_key = self.get_blowfish_key(streamdetails.data["track_id"])
        chunk_index = 0
        timeout = ClientTimeout(total=None, connect=30, sock_read=600)
        headers: dict[str, str] = {}
        # if seek_position and streamdetails.size:
        #     chunk_count = ceil(streamdetails.size / 2048)
        #     chunk_index = int(chunk_count / streamdetails.duration) * seek_position
        #     skip_bytes = chunk_index * 2048
        #     headers["Range"] = f"bytes={skip_bytes}-"

        # NOTE: Seek with using the Range header is not working properly
        # causing malformed audio so this is a temporary patch
        # by just skipping chunks
        if seek_position and streamdetails.size and streamdetails.duration:
            chunk_count = ceil(streamdetails.size / 2048)
            skip_chunks = int(chunk_count / streamdetails.duration) * seek_position
        else:
            skip_chunks = 0

        buffer = bytearray()
        streamdetails.data["start_ts"] = utc_timestamp()
        streamdetails.data["stream_id"] = uuid.uuid1()
        self.mass.create_task(self.gw_client.log_listen(next_track=streamdetails.item_id))
        async with self.mass.http_session.get(
            streamdetails.data["url"], headers=headers, timeout=timeout
        ) as resp:
            async for chunk in resp.content.iter_chunked(2048):
                buffer += chunk
                if len(buffer) >= 2048:
                    if chunk_index >= skip_chunks or chunk_index == 0:
                        if chunk_index % 3 > 0:
                            yield bytes(buffer[:2048])
                        else:
                            yield self.decrypt_chunk(bytes(buffer[:2048]), blowfish_key)

                    chunk_index += 1
                    del buffer[:2048]
        yield bytes(buffer)

    async def on_streamed(
        self,
        streamdetails: StreamDetails,
    ) -> None:
        """Handle callback when an item completed streaming."""
        await self.gw_client.log_listen(last_track=streamdetails)

    ### PARSING METADATA FUNCTIONS ###

    def parse_metadata_track(self, track: deezer.Track) -> MediaItemMetadata:
        """Parse the track metadata."""
        metadata = MediaItemMetadata()
        if hasattr(track, "preview"):
            metadata.preview = track.preview
        if hasattr(track, "explicit_lyrics"):
            metadata.explicit = track.explicit_lyrics
        if hasattr(track, "rank"):
            metadata.popularity = track.rank
        if hasattr(track, "album") and hasattr(track.album, "cover_big"):
            metadata.add_image(
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=track.album.cover_big,
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
            )
        return metadata

    def parse_metadata_album(self, album: deezer.Album) -> MediaItemMetadata:
        """Parse the album metadata."""
        return MediaItemMetadata(
            explicit=album.explicit_lyrics,
            images=UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=album.cover_big,
                        provider=self.instance_id,
                        remotely_accessible=True,
                    )
                ]
            ),
        )

    def parse_metadata_artist(self, artist: deezer.Artist) -> MediaItemMetadata:
        """Parse the artist metadata."""
        return MediaItemMetadata(
            images=UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=artist.picture_big,
                        provider=self.instance_id,
                        remotely_accessible=True,
                    )
                ]
            ),
        )

    ### PARSING FUNCTIONS ###
    def parse_artist(self, artist: deezer.Artist) -> Artist:
        """Parse the deezer-python artist to a Music Assistant artist."""
        return Artist(
            item_id=str(artist.id),
            provider=self.instance_id,
            name=artist.name,
            media_type=MediaType.ARTIST,
            provider_mappings={
                ProviderMapping(
                    item_id=str(artist.id),
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=getattr(artist, "link", None),  # Sometimes the API doesn't return a link
                )
            },
            metadata=self.parse_metadata_artist(artist=artist),
        )

    def parse_album(self, album: deezer.Album) -> Album:
        """Parse the deezer-python album to a Music Assistant album."""
        name, version = parse_title_and_version(album.title)
        return Album(
            album_type=self.get_album_type(album),
            item_id=str(album.id),
            provider=self.instance_id,
            name=name,
            version=version,
            year=album.release_date.year if getattr(album, "release_date", None) else None,
            artists=UniqueList(
                [
                    ItemMapping(
                        media_type=MediaType.ARTIST,
                        item_id=str(album.artist.id),
                        provider=self.instance_id,
                        name=album.artist.name,
                    )
                ]
            ),
            media_type=MediaType.ALBUM,
            provider_mappings={
                ProviderMapping(
                    item_id=str(album.id),
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=getattr(album, "link", None),
                )
            },
            metadata=self.parse_metadata_album(album=album),
        )

    def parse_playlist(self, playlist: deezer.Playlist) -> Playlist:
        """Parse the deezer-python playlist to a Music Assistant playlist."""
        creator = self.get_playlist_creator(playlist)
        is_editable = creator.id == self.user.id
        return Playlist(
            item_id=str(playlist.id),
            provider=self.instance_id,
            name=playlist.title,
            media_type=MediaType.PLAYLIST,
            provider_mappings={
                ProviderMapping(
                    item_id=str(playlist.id),
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=getattr(playlist, "link", None),
                    is_unique=is_editable,  # user-owned playlists are unique
                )
            },
            metadata=MediaItemMetadata(
                images=UniqueList(
                    [
                        MediaItemImage(
                            type=ImageType.THUMB,
                            path=playlist.picture_big,
                            provider=self.instance_id,
                            remotely_accessible=True,
                        )
                    ]
                ),
            ),
            is_editable=is_editable,
            owner=creator.name,
        )

    def get_playlist_creator(self, playlist: deezer.Playlist) -> deezer.User:
        """On playlists, the creator is called creator, elsewhere it's called user."""
        if hasattr(playlist, "creator"):
            return playlist.creator
        return playlist.user

    def _create_virtual_playlist(
        self,
        item_id: str,
        name: str,
        image_url: str | None = None,
    ) -> Playlist:
        """Create a virtual playlist for Flow, Recommended tracks, or Radios.

        :param item_id: The unique identifier (e.g., "flow", "radio_37151").
        :param name: Display name for the playlist.
        :param image_url: Optional image URL.
        """
        images: UniqueList[MediaItemImage] = UniqueList()
        if image_url:
            images.append(
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=image_url,
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
            )
        return Playlist(
            item_id=item_id,
            provider=self.instance_id,
            name=name,
            media_type=MediaType.PLAYLIST,
            provider_mappings={
                ProviderMapping(
                    item_id=item_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
            metadata=MediaItemMetadata(images=images) if images else MediaItemMetadata(),
            is_editable=False,
            owner="Deezer",
        )

    def parse_track(self, track: deezer.Track, user_country: str, position: int = 0) -> Track:
        """Parse the deezer-python track to a Music Assistant track."""
        if hasattr(track, "artist"):
            artist = ItemMapping(
                media_type=MediaType.ARTIST,
                item_id=str(getattr(track.artist, "id", f"deezer-{track.artist.name}")),
                provider=self.instance_id,
                name=track.artist.name,
            )
        else:
            artist = None
        if hasattr(track, "album"):
            album = ItemMapping(
                media_type=MediaType.ALBUM,
                item_id=str(track.album.id),
                provider=self.instance_id,
                name=track.album.title,
            )
        else:
            album = None

        name, version = parse_title_and_version(track.title)
        item = Track(
            item_id=str(track.id),
            provider=self.instance_id,
            name=name,
            version=version,
            sort_name=self.get_short_title(track),
            duration=track.duration,
            artists=UniqueList([artist]) if artist else UniqueList(),
            album=album,
            provider_mappings={
                ProviderMapping(
                    item_id=str(track.id),
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    available=self.track_available(track=track, user_country=user_country),
                    url=getattr(track, "link", None),
                )
            },
            metadata=self.parse_metadata_track(track=track),
            track_number=getattr(track, "track_position", position),
            position=position,
            disc_number=getattr(track, "disk_number", 0),
        )
        if isrc := getattr(track, "isrc", None):
            item.external_ids.add((ExternalID.ISRC, isrc))
        return item

    def get_short_title(self, track: deezer.Track) -> str:
        """Short names only returned, if available."""
        if hasattr(track, "title_short"):
            return str(track.title_short)
        return str(track.title)

    def get_album_type(self, album: deezer.Album) -> AlbumType:
        """Read and convert the Deezer album type."""
        # Get provider's basic type first
        provider_type = AlbumType.UNKNOWN
        if hasattr(album, "record_type"):
            match album.record_type:
                case "album":
                    provider_type = AlbumType.ALBUM
                case "single":
                    provider_type = AlbumType.SINGLE
                case "ep":
                    provider_type = AlbumType.EP
                case "compile":
                    provider_type = AlbumType.COMPILATION

        # Try inference - override if it finds something more specific
        inferred_type = infer_album_type(album.title, "")
        if inferred_type in (AlbumType.SOUNDTRACK, AlbumType.LIVE):
            return inferred_type

        # Otherwise use provider type
        return provider_type

    ### SEARCH AND PARSE FUNCTIONS ###
    async def search_and_parse_tracks(
        self, query: str, user_country: str, limit: int = 20
    ) -> list[Track]:
        """Search for tracks and parse them."""
        deezer_tracks = await self.client.search(query=query, limit=limit)
        tracks = []
        for index, track in enumerate(deezer_tracks):
            tracks.append(self.parse_track(track, user_country))
            if index == limit:
                return tracks
        return tracks

    async def search_and_parse_artists(self, query: str, limit: int = 20) -> list[Artist]:
        """Search for artists and parse them."""
        deezer_artist = await self.client.search_artists(query=query, limit=limit)
        artists = []
        for index, artist in enumerate(deezer_artist):
            artists.append(self.parse_artist(artist))
            if index == limit:
                return artists
        return artists

    async def search_and_parse_albums(self, query: str, limit: int = 20) -> list[Album]:
        """Search for album and parse them."""
        deezer_albums = await self.client.search_albums(query=query, limit=limit)
        albums = []
        for index, album in enumerate(deezer_albums):
            albums.append(self.parse_album(album))
            if index == limit:
                return albums
        return albums

    async def search_and_parse_playlists(self, query: str, limit: int = 20) -> list[Playlist]:
        """Search for playlists and parse them."""
        deezer_playlists = await self.client.search_playlists(query=query, limit=limit)
        playlists = []
        for index, playlist in enumerate(deezer_playlists):
            playlists.append(self.parse_playlist(playlist))
            if index == limit:
                return playlists
        return playlists

    ### OTHER FUNCTIONS ###

    async def get_track_content_type(
        self, gw_client: GWClient, track_id: str
    ) -> Literal[ContentType.FLAC, ContentType.MP3]:
        """Get a tracks contentType."""
        song_data = await gw_client.get_song_data(track_id)
        if song_data["results"]["FILESIZE_FLAC"]:
            return ContentType.FLAC

        if song_data["results"]["FILESIZE_MP3_320"] or song_data["results"]["FILESIZE_MP3_128"]:
            return ContentType.MP3

        msg = "Unsupported contenttype"
        raise NotImplementedError(msg)

    def track_available(self, track: deezer.Track, user_country: str) -> bool:
        """Check if a given track is available in the users country."""
        if hasattr(track, "available_countries"):
            return user_country in track.available_countries
        return True

    def _md5(self, data: str, data_type: str = "ascii") -> str:
        md5sum = hashlib.md5()
        md5sum.update(data.encode(data_type))
        return md5sum.hexdigest()

    def get_blowfish_key(self, track_id: str) -> str:
        """Get blowfish key to decrypt a chunk of a track."""
        secret = app_var(5)
        id_md5 = self._md5(track_id)
        return "".join(
            chr(ord(id_md5[i]) ^ ord(id_md5[i + 16]) ^ ord(secret[i])) for i in range(16)
        )

    def decrypt_chunk(self, chunk: bytes, blowfish_key: str) -> bytes:
        """Decrypt a given chunk using the blow fish key."""
        cipher = Blowfish.new(
            blowfish_key.encode("ascii"),
            Blowfish.MODE_CBC,
            b"\x00\x01\x02\x03\x04\x05\x06\x07",
        )
        return cipher.decrypt(chunk)  # type: ignore[no-any-return,unused-ignore]
