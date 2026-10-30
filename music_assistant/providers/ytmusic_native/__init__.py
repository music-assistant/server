"""
Native YouTube Music provider for Music Assistant (experimental).

Talks to YouTube Music's own youtubei/v1 API directly, the way the
music.youtube.com web client does: no ytmusicapi, no yt-dlp, no deno and no
PO token server. Audio comes from the authenticated web player; its stream URLs
are solved by running the player's own JavaScript in an embedded V8 (see
cipher.py), which also unlocks the 256k Premium formats.
"""

from __future__ import annotations

import asyncio
import random
import re
import string
import time
from collections.abc import AsyncGenerator, Callable
from contextlib import suppress
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, unquote, urlencode, urlsplit, urlunsplit

from aiohttp import ClientError, DummyCookieJar
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import (
    AlbumType,
    ConfigEntryType,
    ContentType,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import (
    InvalidDataError,
    LoginFailed,
    MediaNotFoundError,
    SetupFailedError,
    UnplayableMediaError,
)
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    BrowseFolder,
    ItemMapping,
    MediaItemImage,
    MediaItemType,
    Playlist,
    Podcast,
    PodcastEpisode,
    ProviderMapping,
    RecommendationFolder,
    SearchResults,
    Track,
    UniqueList,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.constants import CONF_ENTRY_UNOFFICIAL_PROVIDER
from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.aiohttp_client import create_clientsession
from music_assistant.helpers.datetime import utc
from music_assistant.helpers.util import infer_album_type, parse_title_and_version
from music_assistant.models.music_provider import MusicProvider
from music_assistant.models.recommendation_payload import RecommendationPayloadMixin

from . import parsers
from .cipher import CipherError, PlayerCipher, mini_racer_available
from .constants import (
    BROWSE_HOME,
    BROWSE_LIBRARY_ALBUMS,
    BROWSE_LIBRARY_PLAYLISTS,
    BROWSE_LIBRARY_PODCASTS,
    BROWSE_LIBRARY_SUBSCRIPTIONS,
    BROWSE_LIBRARY_TRACK_ARTISTS,
    BROWSE_LIBRARY_TRACKS,
    CONF_COOKIE,
    CONF_HIGH_QUALITY,
    CONF_INCLUDE_VIDEOS,
    CONF_KEEP_SESSION_ALIVE,
    CONF_REPORT_PLAYS,
    DEFAULT_STREAM_URL_EXPIRATION,
    DOMAIN,
    DYNAMIC_PLAYLIST_TRACK_LIMIT,
    MAX_CONTINUATIONS,
    PERSONAL_PLAYLIST_IDS,
    PERSONAL_PLAYLIST_PREFIXES,
    PLAYLIST_ID_DELIMITER,
    PODCAST_EPISODE_SPLITTER,
    PREMIUM_CHECK_VIDEO_ID,
    PREMIUM_ITAGS,
    PREMIUM_RECHECK_INTERVAL,
    REPORT_PLAY_AFTER,
    ROTATE_COOKIES_INTERVAL,
    SEARCH_PARAMS,
    VARIOUS_ARTISTS_YTM_ID,
)
from .innertube import InnerTube

if TYPE_CHECKING:
    from aiohttp import ClientSession
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES = {
    ProviderFeature.SEARCH,
    ProviderFeature.BROWSE,
    ProviderFeature.RECOMMENDATIONS,
    ProviderFeature.LYRICS,
    ProviderFeature.LIBRARY_ARTISTS,
    ProviderFeature.LIBRARY_ALBUMS,
    ProviderFeature.LIBRARY_TRACKS,
    ProviderFeature.LIBRARY_PLAYLISTS,
    ProviderFeature.LIBRARY_PODCASTS,
    ProviderFeature.LIBRARY_ARTISTS_EDIT,
    ProviderFeature.LIBRARY_ALBUMS_EDIT,
    ProviderFeature.LIBRARY_PLAYLISTS_EDIT,
    ProviderFeature.LIBRARY_PODCASTS_EDIT,
    ProviderFeature.FAVORITE_TRACKS_EDIT,
    ProviderFeature.ARTIST_ALBUMS,
    ProviderFeature.ARTIST_TOPTRACKS,
    ProviderFeature.SIMILAR_ARTISTS,
    ProviderFeature.SIMILAR_TRACKS,
    ProviderFeature.PLAYLIST_TRACKS_EDIT,
    ProviderFeature.PLAYLIST_CREATE,
}
# features only newer Music Assistant versions know
SUPPORTED_FEATURES.update(
    feature
    for name in ("ALBUM_VERSIONS", "PLAYLIST_TRACKS_MOVE")
    if (feature := getattr(ProviderFeature, name, None)) is not None
)

ALBUM_TYPES = {"album": AlbumType.ALBUM, "single": AlbumType.SINGLE, "ep": AlbumType.EP}
# shelves of an artist page that list the artist's own releases
DISCOGRAPHY_SHELVES = ("albums", "singles", "eps", "singles & eps")
RECOMMENDATION_ICONS = (
    ("listen again", "mdi-book-refresh-outline"),
    ("mix", "mdi-music-circle-outline"),
    ("new", "mdi-new-box"),
    ("release", "mdi-new-box"),
    ("discover", "mdi-magnify"),
    ("recommended", "mdi-star-circle-outline"),
    ("for you", "mdi-star-circle-outline"),
)
CPN_ALPHABET = string.ascii_letters + string.digits + "-_"


def _premium_served(player: dict[str, Any]) -> bool | None:
    """
    Return whether a player response carries the Premium-only formats.

    None when it says nothing about that: the video is not playable or has no formats.

    :param player: A web player response.
    """
    if (player.get("playabilityStatus") or {}).get("status") != "OK":
        return None
    formats = (player.get("streamingData") or {}).get("adaptiveFormats") or []
    if not formats:
        return None
    return any(fmt.get("itag") in PREMIUM_ITAGS for fmt in formats)


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return YoutubeMusicNativeProvider(mass, manifest, config, SUPPORTED_FEATURES)


class YoutubeMusicNativeProvider(RecommendationPayloadMixin, MusicProvider):
    """YouTube Music, driven through its own web API."""

    _session: ClientSession | None = None
    _innertube: InnerTube
    _cipher: PlayerCipher | None = None
    _keep_alive_task: asyncio.Task[None] | None = None
    _account_name: str | None = None
    _signature_timestamp: int = 0
    _player_lock: asyncio.Lock
    _premium_verified_at: float = 0.0

    @property
    def max_concurrent_streams(self) -> int:
        """Allow a few parallel fetches and leave YouTube to enforce its account allowance."""
        return 3

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to configure this provider."""
        return (
            CONF_ENTRY_UNOFFICIAL_PROVIDER,
            ConfigEntry(key=CONF_HIGH_QUALITY, type=ConfigEntryType.BOOLEAN, default_value=True),
            ConfigEntry(key=CONF_INCLUDE_VIDEOS, type=ConfigEntryType.BOOLEAN, default_value=False),
            ConfigEntry(key=CONF_REPORT_PLAYS, type=ConfigEntryType.BOOLEAN, default_value=True),
        )

    async def handle_async_init(self) -> None:
        """Set up the provider: check the cookie, the client config and Premium."""
        if not mini_racer_available():
            raise SetupFailedError(
                "The embedded JavaScript engine (mini-racer) is not available on this system.",
                translation_key="no_javascript_engine",
                translation_owner=self.translation_owner,
            )
        self._session = create_clientsession(self.mass, cookie_jar=DummyCookieJar())
        self._cipher = PlayerCipher(self.logger)
        self._player_lock = asyncio.Lock()
        try:
            self._innertube = InnerTube(
                self._session,
                str(self.get_setup_value(CONF_COOKIE) or ""),
                self.logger,
                self.translation_owner,
            )
            if self._keep_session_alive_enabled:
                # a cookie that sat unused (e.g. while MA was offline) needs fresh timestamps
                with suppress(ClientError, TimeoutError):
                    await self._rotate_cookies()
            await self._innertube.bootstrap(force=True)
            self._account_name = await self._fetch_account_name()
            await self._require_premium()
        except BaseException:
            # the provider is only registered once init succeeds, so unload won't run
            self._cipher.close()
            await self._session.close()
            raise
        self.logger.info("Logged in to YouTube Music as %s", self._account_name or "unknown user")
        if self._keep_session_alive_enabled:
            self._keep_alive_task = self.mass.create_task(self._keep_session_alive())

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        if self._keep_alive_task:
            self._keep_alive_task.cancel()
        if self._cipher:
            self._cipher.close()
        if self._session and not self._session.closed:
            await self._session.close()
        await super().unload(is_removed)

    async def sync_library(self, media_type: MediaType) -> None:
        """Run library sync for this provider."""
        try:
            await super().sync_library(media_type)
        except LoginFailed as err:
            # every following call fails the same way until the cookie is replaced,
            # so hand the provider back to the user for re-authentication
            if self.available:
                self.unload_with_error(err)
            raise

    # ------------------------------------------------------------------ search

    @use_cache(3600 * 24)
    async def search(
        self, search_query: str, media_types: list[MediaType], limit: int = 5
    ) -> SearchResults:
        """
        Search YouTube Music, one filtered search per requested media type.

        :param search_query: Search query.
        :param media_types: A list of media_types to include.
        :param limit: Number of items to return in the search (per type).
        """
        filters: list[str] = []
        if MediaType.TRACK in media_types:
            filters.append("songs")
            if self.config.get_value(CONF_INCLUDE_VIDEOS):
                filters.append("videos")
        if MediaType.ALBUM in media_types:
            filters.append("albums")
        if MediaType.ARTIST in media_types:
            filters.append("artists")
        if MediaType.PLAYLIST in media_types:
            filters += ["featured_playlists", "community_playlists"]
        if MediaType.PODCAST in media_types:
            filters.append("podcasts")
        responses = await asyncio.gather(
            *(
                self._innertube.call("search", {"query": search_query, "params": SEARCH_PARAMS[f]})
                for f in filters
            )
        )
        found: dict[str, list[dict[str, Any]]] = {}
        for search_filter, response in zip(filters, responses, strict=True):
            found[search_filter] = parsers.parse_items(response)

        def of_kind(kind: str, *keys: str) -> list[dict[str, Any]]:
            lists = [[item for item in found.get(key, []) if item["kind"] == kind] for key in keys]
            # interleave, so a second source (videos, community playlists) survives the cut
            mixed = [item for group in zip(*lists, strict=False) for item in group]
            longest = max(lists, key=len, default=[])
            mixed += longest[len(mixed) // max(len(lists), 1) :]
            return mixed

        results = SearchResults()
        results.tracks = self._valid(self._parse_track, of_kind("track", "songs", "videos"))[:limit]
        results.albums = [self._parse_album(i) for i in of_kind("album", "albums")][:limit]
        results.artists = [self._parse_artist(i) for i in of_kind("artist", "artists")][:limit]
        results.playlists = [
            self._parse_playlist(i)
            for i in of_kind("playlist", "featured_playlists", "community_playlists")
        ][:limit]
        results.podcasts = [self._parse_podcast(i) for i in of_kind("podcast", "podcasts")][:limit]
        return results

    # ----------------------------------------------------------------- library

    async def get_library_artists(self) -> AsyncGenerator[Artist]:
        """Retrieve the subscribed artists plus the artists of the library songs."""
        seen: set[str] = set()
        for browse_id in (BROWSE_LIBRARY_SUBSCRIPTIONS, BROWSE_LIBRARY_TRACK_ARTISTS):
            for item in await self._browse_all(browse_id):
                if item["kind"] == "artist" and item["channel_id"] not in seen:
                    seen.add(item["channel_id"])
                    yield self._parse_artist(item)

    async def get_library_albums(self) -> AsyncGenerator[Album]:
        """Retrieve the saved albums."""
        for item in await self._browse_all(BROWSE_LIBRARY_ALBUMS):
            if item["kind"] == "album":
                yield self._parse_album(item)

    async def get_library_tracks(self) -> AsyncGenerator[Track]:
        """Retrieve the library songs."""
        for item in await self._browse_all(BROWSE_LIBRARY_TRACKS):
            if item["kind"] == "track" and (track := await self._track_or_lookup(item)):
                yield track

    async def get_library_playlists(self) -> AsyncGenerator[Playlist]:
        """Retrieve the saved and owned playlists."""
        for item in await self._browse_all(BROWSE_LIBRARY_PLAYLISTS):
            # the episode lists show up here too; they belong to the podcasts
            if item["kind"] == "playlist" and item["playlist_id"] not in ("SE", "RDPN"):
                yield self._parse_playlist(item)

    async def get_library_podcasts(self) -> AsyncGenerator[Podcast]:
        """Retrieve the saved podcasts."""
        for item in await self._browse_all(BROWSE_LIBRARY_PODCASTS):
            if item["kind"] == "podcast":
                yield self._parse_podcast(item)

    async def library_add(self, item: MediaItemType) -> bool:
        """Save an item to the YouTube Music library."""
        return await self._library_edit(item.item_id, item.media_type, add=True)

    async def library_remove(self, prov_item_id: str, media_type: MediaType) -> bool:
        """Remove an item from the YouTube Music library."""
        return await self._library_edit(prov_item_id, media_type, add=False)

    async def set_favorite(
        self, prov_item_id: str, media_type: MediaType, favorite: bool | None
    ) -> None:
        """
        Like, dislike or clear the rating of a track.

        :param prov_item_id: The video id of the track.
        :param media_type: Media type of the item, only tracks can be rated.
        :param favorite: True to like, False to dislike, None to clear the rating.
        """
        if media_type != MediaType.TRACK:
            return
        endpoint = {True: "like/like", False: "like/dislike", None: "like/removelike"}[favorite]
        await self._innertube.call(endpoint, {"target": {"videoId": prov_item_id}})

    # ------------------------------------------------------------------ albums

    @use_cache(3600 * 24 * 30)
    async def get_album(self, prov_album_id: str) -> Album:
        """Get full album details by id."""
        page = await self._album_page(prov_album_id)
        return self._parse_album(page["album"])

    @use_cache(3600 * 24 * 30, allow_expired_cache=True)
    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get album tracks for given album id."""
        page = await self._album_page(prov_album_id)
        tracks = []
        for number, item in enumerate(page["tracks"], 1):
            if track := self._valid_one(self._parse_track, item):
                track.track_number = item["track_number"] or number
                tracks.append(track)
        return tracks

    @use_cache(3600 * 24 * 7)
    async def get_album_versions(self, prov_album_id: str) -> list[Album]:
        """Get the other versions YouTube Music lists for an album (deluxe, remaster, ...)."""
        page = await self._album_page(prov_album_id)
        return [self._parse_album(item) for item in page["versions"]]

    # ----------------------------------------------------------------- artists

    @use_cache(3600 * 24 * 30)
    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get full artist details by id."""
        page = await self._artist_page(prov_artist_id)
        return self._parse_artist(page["artist"])

    @use_cache(3600 * 24 * 7, allow_expired_cache=True)
    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Get the albums, singles and EPs of an artist."""
        page = await self._artist_page(prov_artist_id)
        credit = {"id": prov_artist_id, "name": page["artist"]["name"]}
        albums: list[Album] = []
        seen: set[str] = set()
        for shelf in page["shelves"]:
            more = shelf["more_browse_id"] or ""
            if not more.startswith("MPAD") and shelf["title"].lower() not in DISCOGRAPHY_SHELVES:
                continue
            items = shelf["items"]
            if more.startswith("MPAD"):
                # the page only previews a shelf; its "more" grid has the full list
                with suppress(MediaNotFoundError):
                    items = await self._browse_all(more, params=unquote(shelf["more_params"] or ""))
            default_type = "album" if shelf["title"].lower() == "albums" else None
            for item in items:
                if item["kind"] != "album" or item["browse_id"] in seen:
                    continue
                seen.add(item["browse_id"])
                item["artists"] = item["artists"] or [credit]
                item["album_type"] = item["album_type"] or default_type
                albums.append(self._parse_album(item))
        return albums

    @use_cache(3600 * 24 * 7, allow_expired_cache=True)
    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """Get the most popular tracks of an artist."""
        page = await self._artist_page(prov_artist_id)
        items = page["top_tracks"]
        if songs := page["songs_playlist_id"]:
            with suppress(MediaNotFoundError):
                items = await self._playlist_items(songs, limit=25)
        return [t for item in items[:25] if (t := await self._track_or_lookup(item))]

    @use_cache(3600 * 24 * 7, allow_expired_cache=True)
    async def get_similar_artists(self, prov_artist_id: str, limit: int = 25) -> list[Artist]:
        """Return the artists the artist page lists as related ("Fans might also like")."""
        page = await self._artist_page(prov_artist_id)
        for shelf in page["shelves"]:
            artists = [item for item in shelf["items"] if item["kind"] == "artist"]
            if artists and len(artists) == len(shelf["items"]):
                return [self._parse_artist(item) for item in artists[:limit]]
        return []

    # ------------------------------------------------------------------ tracks

    @use_cache(3600 * 24 * 30)
    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id, including its lyrics when YouTube has them."""
        response = await self._innertube.call("next", {"videoId": prov_track_id})
        item = parsers.parse_watch_track(response, prov_track_id)
        if item is None:
            raise MediaNotFoundError(f"Track {prov_track_id} not found")
        if not item["artists"]:
            item["artists"] = await self._uploader(prov_track_id)
        track = self._parse_track(item)
        if item.get("like_status") in ("LIKE", "DISLIKE"):
            track.favorite = item["like_status"] == "LIKE"
        if lyrics_id := item.get("lyrics_browse_id"):
            with suppress(MediaNotFoundError):
                lyrics_page = await self._innertube.call("browse", {"browseId": lyrics_id})
                track.metadata.lyrics, _source = parsers.parse_lyrics(lyrics_page)
        return track

    @use_cache(3600 * 24, allow_expired_cache=True)
    async def get_similar_tracks(self, prov_track_id: str, limit: int = 25) -> list[Track]:
        """Return the radio YouTube Music starts from this track."""
        response = await self._innertube.call(
            "next", {"videoId": prov_track_id, "playlistId": f"RDAMVM{prov_track_id}"}
        )
        items = parsers.parse_watch_queue(response)
        pages = 0
        while len(items) <= limit and (token := parsers.radio_continuation(response)) and pages < 5:
            response = await self._innertube.call("next", {"continuation": token})
            items += parsers.parse_watch_queue(response)
            pages += 1
        tracks: list[Track] = []
        seen = {prov_track_id}
        for item in items:
            if item["video_id"] in seen:
                continue
            seen.add(item["video_id"])
            if track := await self._track_or_lookup(item):
                tracks.append(track)
            if len(tracks) >= limit:
                break
        return tracks

    # --------------------------------------------------------------- playlists

    @use_cache(3600 * 24 * 7)
    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get full playlist details by id."""
        raw_id = self._raw_playlist_id(prov_playlist_id)
        response = await self._innertube.call("browse", {"browseId": f"VL{raw_id}"})
        page = parsers.parse_playlist_page(response, raw_id)
        if not page["playlist"]["title"]:
            raise MediaNotFoundError(f"Playlist {prov_playlist_id} not found")
        return self._parse_playlist(page["playlist"])

    @use_cache(3600 * 3, allow_expired_cache=True)
    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """Return all tracks of a playlist (on page 0; YouTube pages by token, not offset)."""
        if page > 0:
            return []
        items = await self._playlist_items(self._raw_playlist_id(prov_playlist_id))
        tracks: list[Track] = []
        for position, item in enumerate(items, 1):
            if item["kind"] == "track" and (track := await self._track_or_lookup(item)):
                track.position = position
                tracks.append(track)
        return tracks

    async def create_playlist(
        self, name: str, media_types: set[MediaType] | None = None
    ) -> Playlist:
        """Create a new (private) playlist."""
        response = await self._innertube.call(
            "playlist/create", {"title": name, "description": "", "privacyStatus": "PRIVATE"}
        )
        playlist_id = response.get("playlistId")
        if not playlist_id:
            raise InvalidDataError("YouTube Music did not return the new playlist")
        return self._parse_playlist(
            {
                "playlist_id": playlist_id,
                "title": name,
                "author": self._account_name,
                "editable": True,
            }
        )

    async def add_playlist_tracks(self, prov_playlist_id: str, prov_track_ids: list[str]) -> None:
        """Add track(s) to a playlist."""
        await self._edit_playlist(
            prov_playlist_id,
            [
                {"action": "ACTION_ADD_VIDEO", "addedVideoId": video_id}
                for video_id in prov_track_ids
            ],
        )

    async def remove_playlist_tracks(
        self, prov_playlist_id: str, positions_to_remove: tuple[int, ...]
    ) -> None:
        """Remove the tracks at the given (1-based) positions from a playlist."""
        items = await self._playlist_items(self._raw_playlist_id(prov_playlist_id))
        actions = [
            {
                "action": "ACTION_REMOVE_VIDEO",
                "removedVideoId": item["video_id"],
                # a playlist can hold a video twice; setVideoId names the entry
                "setVideoId": item["set_video_id"],
            }
            for position, item in enumerate(items, 1)
            if position in positions_to_remove and item.get("set_video_id")
        ]
        if actions:
            await self._edit_playlist(prov_playlist_id, actions)

    async def move_playlist_track(
        self, prov_playlist_id: str, from_position: int, to_position: int
    ) -> None:
        """
        Move one track of a playlist to another position.

        :param prov_playlist_id: The playlist id on the provider.
        :param from_position: The current position of the track, 1-based.
        :param to_position: The position the track ends up at after the move, 1-based.
        """
        items = await self._playlist_items(self._raw_playlist_id(prov_playlist_id))
        if not (1 <= from_position <= len(items) and 1 <= to_position <= len(items)):
            raise InvalidDataError("Playlist position out of range")
        moved = items.pop(from_position - 1)
        # YouTube moves an entry in front of another one (or to the end without one)
        action: dict[str, Any] = {
            "action": "ACTION_MOVE_VIDEO_BEFORE",
            "setVideoId": moved["set_video_id"],
        }
        if to_position - 1 < len(items):
            action["movedSetVideoIdSuccessor"] = items[to_position - 1]["set_video_id"]
        await self._edit_playlist(prov_playlist_id, [action])

    # ---------------------------------------------------------------- podcasts

    @use_cache(3600 * 24 * 14)
    async def get_podcast(self, prov_podcast_id: str) -> Podcast:
        """Get the details of a podcast."""
        page = await self._podcast_page(prov_podcast_id)
        return self._parse_podcast(page["podcast"])

    async def get_podcast_episodes(self, prov_podcast_id: str) -> AsyncGenerator[PodcastEpisode]:
        """Get all episodes of a podcast, newest first."""
        page = await self._podcast_page(prov_podcast_id)
        podcast = self._parse_podcast(page["podcast"])
        episodes = await self._continued(
            page["episodes"],
            page["continuation"],
            lambda response: parsers.parse_episodes(response, page["podcast"]),
            parsers.continuation_token,
        )
        for index, item in enumerate(episodes):
            # bigger position = newer
            yield self._parse_episode(item, podcast, position=len(episodes) - index)

    @use_cache(3600 * 3)
    async def get_podcast_episode(self, prov_episode_id: str) -> PodcastEpisode:
        """Get a single podcast episode."""
        podcast_id, video_id = prov_episode_id.split(PODCAST_EPISODE_SPLITTER, 1)
        podcast = await self.get_podcast(podcast_id)
        response = await self._innertube.call("browse", {"browseId": f"MPED{video_id}"})
        if not (item := parsers.parse_episode_page(response, video_id)):
            raise MediaNotFoundError(f"Episode {prov_episode_id} not found")
        return self._parse_episode(item, podcast, position=0)

    # ---------------------------------------------------------- recommendations

    async def get_recommendations(self) -> list[RecommendationFolder]:
        """Get the rows of the home feed, without items."""
        return await self._recommendation_rows_from_payload()

    async def get_recommendation_items(
        self, item_id: str
    ) -> UniqueList[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Get the items of one home feed row.

        :param item_id: The item_id of the row, as returned by get_recommendations.
        """
        return await self._recommendation_items_from_payload(item_id)

    # ------------------------------------------------------------------- audio

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Resolve a ready-to-stream URL from the authenticated web player."""
        video_id = item_id.rsplit(PODCAST_EPISODE_SPLITTER, 1)[-1]
        player = await self._player(video_id)
        status = player.get("playabilityStatus") or {}
        if status.get("status") != "OK":
            reason = status.get("reason") or status.get("status") or "unknown reason"
            raise UnplayableMediaError(f"{video_id} is not playable: {reason}")
        if _premium_served(player):
            self._premium_verified_at = time.monotonic()
        elif time.monotonic() - self._premium_verified_at > PREMIUM_RECHECK_INTERVAL:
            # not every video has Premium formats, so ask the reference video
            await self._recheck_premium()
        fmt = self._pick_audio_format(player)
        if fmt is None:
            raise UnplayableMediaError(f"{video_id} has no audio formats")
        assert self._cipher is not None
        try:
            url = await self._cipher.resolve(fmt, self._innertube.stream_ok)
        except CipherError as err:
            raise UnplayableMediaError(
                f"Could not solve the stream URL of {video_id}: {err}"
            ) from err
        self.logger.debug(
            "Streaming %s as itag %s (%s)", video_id, fmt.get("itag"), fmt.get("mimeType")
        )

        expiration = DEFAULT_STREAM_URL_EXPIRATION
        if (expire := parse_qs(urlsplit(url).query).get("expire", [""])[0]).isdigit():
            expiration = int(expire) - int(time.time())
        mime = str(fmt.get("mimeType", ""))
        audio_format = AudioFormat(
            content_type=ContentType.M4A if "mp4" in mime else ContentType.UNKNOWN,
            codec_type=ContentType.AAC if "mp4a" in mime else ContentType.OPUS,
            bit_rate=int(fmt.get("bitrate", 0)) // 1000 or None,
        )
        if str(fmt.get("audioSampleRate", "")).isdigit():
            audio_format.sample_rate = int(fmt["audioSampleRate"])
        if str(fmt.get("audioChannels", "")).isdigit():
            audio_format.channels = int(fmt["audioChannels"])
        tracking = (player.get("playbackTracking") or {}).get("videostatsPlaybackUrl") or {}
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=audio_format,
            media_type=media_type,
            stream_type=StreamType.HTTP,
            path=url,
            can_seek=True,
            allow_seek=True,
            expiration=expiration,
            duration=int(fmt["approxDurationMs"]) // 1000
            if str(fmt.get("approxDurationMs", "")).isdigit()
            else None,
            data={"tracking_url": tracking.get("baseUrl")},
        )

    async def on_streamed(self, streamdetails: StreamDetails) -> None:
        """Report a listened track to the YouTube Music history, as the web player does."""
        if not self.config.get_value(CONF_REPORT_PLAYS):
            return
        data = streamdetails.data if isinstance(streamdetails.data, dict) else {}
        if not (tracking_url := data.get("tracking_url")):
            return
        if (streamdetails.seconds_streamed or 0) < REPORT_PLAY_AFTER:
            return
        parts = urlsplit(tracking_url)
        query = {k: v[-1] for k, v in parse_qs(parts.query, keep_blank_values=True).items()}
        # the client playback nonce identifies one playback session
        cpn = "".join(random.choices(CPN_ALPHABET, k=16))  # noqa: S311 - a nonce, not a secret
        query |= {"ver": "2", "c": "WEB_REMIX", "cpn": cpn}
        url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))
        try:
            await self._innertube.ping(url)
        except (ClientError, TimeoutError) as err:
            self.logger.debug("Could not report the play of %s: %s", streamdetails.item_id, err)

    # --------------------------------------------------------- private helpers

    async def _fetch_recommendation_payload(self) -> list[RecommendationFolder]:
        """Read the home feed (following its continuations) into recommendation folders."""
        response = await self._innertube.call("browse", {"browseId": BROWSE_HOME})
        shelves = parsers.parse_shelves(response)
        pages = 0
        while (token := parsers.continuation_token(response)) and pages < 5:
            response = await self._innertube.call("browse", {"continuation": token})
            shelves += parsers.parse_shelves(response)
            pages += 1
        folders: list[RecommendationFolder] = []
        seen: set[str] = set()
        for shelf in shelves:
            title = shelf["title"]
            key = re.sub(r"\W+", "_", title.lower()).strip("_")
            if not title or key in seen:
                continue
            seen.add(key)
            folder = RecommendationFolder(
                name=title,
                item_id=f"{self.instance_id}_{key}",
                provider=self.instance_id,
                icon=next(
                    (icon for word, icon in RECOMMENDATION_ICONS if word in title.lower()),
                    "mdi-music-note-outline",
                ),
            )
            for item in shelf["items"]:
                if media_item := self._valid_one(self._parse_item, item):
                    folder.items.append(media_item)
            if folder.items:
                folders.append(folder)
        return folders

    @property
    def _keep_session_alive_enabled(self) -> bool:
        return bool(self.get_setup_value(CONF_KEEP_SESSION_ALIVE, True))

    async def _player(self, video_id: str) -> dict[str, Any]:
        """Call the web player, loading the current player build into the solver first."""
        assert self._cipher is not None
        await self._innertube.bootstrap()
        async with self._player_lock:
            player_id = self._innertube.player_id
            if player_id and player_id != self._cipher.player_id:
                try:
                    self._cipher.check_player(player_id)
                    base_js, signature_timestamp = await self._innertube.base_js()
                    await self._cipher.load(player_id, base_js)
                except CipherError as err:
                    raise UnplayableMediaError(str(err)) from err
                self._signature_timestamp = signature_timestamp
        return await self._innertube.player(video_id, self._signature_timestamp)

    async def _require_premium(self) -> None:
        """
        Refuse an account without YouTube Music Premium.

        A free account hears ads in YouTube Music, and this provider plays none, so it
        requires Premium like the existing YouTube Music provider does.
        """
        premium = await self._check_premium()
        if premium is None:
            raise SetupFailedError(
                "Could not check whether the account has YouTube Music Premium.",
                translation_key="premium_check_failed",
                translation_owner=self.translation_owner,
            )
        if not premium:
            raise self._premium_required()
        self._premium_verified_at = time.monotonic()

    async def _recheck_premium(self) -> None:
        """Check Premium again while playing, and unload the provider once it is gone."""
        premium = await self._check_premium()
        if premium is None:
            # inconclusive (e.g. a network hiccup); the next track tries again
            return
        if not premium:
            err = self._premium_required()
            self.unload_with_error(err)
            raise err
        self._premium_verified_at = time.monotonic()

    async def _check_premium(self) -> bool | None:
        """Return whether the account is served Premium formats, None when unknown."""
        return _premium_served(await self._player(PREMIUM_CHECK_VIDEO_ID))

    def _premium_required(self) -> LoginFailed:
        return LoginFailed(
            "This provider needs a YouTube Music Premium subscription.",
            translation_key="premium_required",
            translation_owner=self.translation_owner,
        )

    def _pick_audio_format(self, player: dict[str, Any]) -> dict[str, Any] | None:
        """Pick the 256k Premium format when allowed and served, else the best other one."""
        formats: list[dict[str, Any]] = [
            fmt
            for fmt in (player.get("streamingData") or {}).get("adaptiveFormats", [])
            if str(fmt.get("mimeType", "")).startswith("audio/")
        ]
        if self.config.get_value(CONF_HIGH_QUALITY):
            by_itag: dict[Any, dict[str, Any]] = {fmt.get("itag"): fmt for fmt in formats}
            for itag in PREMIUM_ITAGS:
                if itag in by_itag:
                    return by_itag[itag]
        else:
            formats = [fmt for fmt in formats if fmt.get("itag") not in PREMIUM_ITAGS] or formats
        return max(formats, key=lambda fmt: int(fmt.get("bitrate", 0)), default=None)

    async def _browse_all(
        self, browse_id: str, params: str | None = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """Browse a page and follow its continuations, returning every parsed item."""
        body: dict[str, Any] = {"browseId": browse_id}
        if params:
            body["params"] = params
        response = await self._innertube.call("browse", body)
        return await self._continued(
            parsers.parse_items(response),
            parsers.continuation_token(response),
            parsers.parse_items,
            parsers.continuation_token,
            limit,
        )

    async def _playlist_items(self, raw_id: str, limit: int | None = None) -> list[dict[str, Any]]:
        """Read the (uncached) entries of a playlist, with their setVideoIds."""
        if limit is None and raw_id.startswith("RD"):
            # mixes are generated on the fly and can be endless
            limit = DYNAMIC_PLAYLIST_TRACK_LIMIT
        response = await self._innertube.call("browse", {"browseId": f"VL{raw_id}"})
        return await self._continued(
            parsers.parse_playlist_tracks(response),
            parsers.playlist_continuation(response),
            parsers.parse_playlist_tracks,
            parsers.playlist_continuation,
            limit,
        )

    async def _continued(
        self,
        items: list[dict[str, Any]],
        token: str | None,
        parse: Callable[[Any], list[dict[str, Any]]],
        next_token: Callable[[Any], str | None],
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """
        Follow a list's continuation pages until YouTube hands out no further page.

        A library sync treats what is not returned as removed, so this reads to the end.
        It only stops early on a token it already followed or on MAX_CONTINUATIONS pages,
        which real libraries never reach, and logs a warning then.

        :param items: The items of the first page.
        :param token: The continuation token of the first page.
        :param parse: Parses the items out of a continuation response.
        :param next_token: Reads the next token out of a continuation response.
        :param limit: Stop once this many items were read.
        """
        followed: set[str] = set()
        while token and (limit is None or len(items) < limit):
            if token in followed or len(followed) >= MAX_CONTINUATIONS:
                self.logger.warning(
                    "Stopped paging after %s pages, the list may be incomplete", len(followed)
                )
                break
            followed.add(token)
            response = await self._innertube.call("browse", {"continuation": token})
            if not (more := parse(response)):
                break
            items = items + more
            token = next_token(response)
        return items[:limit] if limit else items

    async def _album_page(self, album_id: str) -> dict[str, Any]:
        response = await self._innertube.call("browse", {"browseId": album_id})
        page = parsers.parse_album_page(response, album_id)
        if not page["album"]["title"]:
            raise MediaNotFoundError(f"Album {album_id} not found")
        return page

    async def _artist_page(self, artist_id: str) -> dict[str, Any]:
        response = await self._innertube.call("browse", {"browseId": artist_id})
        page = parsers.parse_artist_page(response, artist_id)
        if not page["artist"]["name"]:
            raise MediaNotFoundError(f"Artist {artist_id} not found")
        return page

    async def _podcast_page(self, podcast_id: str) -> dict[str, Any]:
        response = await self._innertube.call("browse", {"browseId": f"MPSP{podcast_id}"})
        page = parsers.parse_podcast_page(response, podcast_id)
        if not page["podcast"]["title"]:
            raise MediaNotFoundError(f"Podcast {podcast_id} not found")
        return page

    async def _library_edit(self, item_id: str, media_type: MediaType, add: bool) -> bool:
        """Save/unsave an item: subscribe to an artist, like a collection's playlist."""
        if media_type == MediaType.ARTIST:
            page = await self._artist_page(item_id)
            channel_id = page["subscribe_channel_id"] or item_id
            endpoint = "subscription/subscribe" if add else "subscription/unsubscribe"
            await self._innertube.call(endpoint, {"channelIds": [channel_id]})
            return True
        if media_type == MediaType.ALBUM:
            # an album is saved through its audio playlist (OLAK5uy_...)
            playlist_id = (await self._album_page(item_id))["album"]["playlist_id"]
        elif media_type in (MediaType.PLAYLIST, MediaType.PODCAST):
            playlist_id = self._raw_playlist_id(item_id)
        else:
            return False
        if not playlist_id:
            return False
        try:
            await self._innertube.call(
                "like/like" if add else "like/removelike", {"target": {"playlistId": playlist_id}}
            )
        except MediaNotFoundError:
            # an own playlist can't be liked; it is in the library anyway
            return False
        return True

    async def _edit_playlist(self, prov_playlist_id: str, actions: list[dict[str, Any]]) -> None:
        response = await self._innertube.call(
            "browse/edit_playlist",
            {"playlistId": self._raw_playlist_id(prov_playlist_id), "actions": actions},
        )
        if (status := response.get("status")) and status != "STATUS_SUCCEEDED":
            raise InvalidDataError(f"YouTube Music refused the playlist edit: {status}")

    async def _track_or_lookup(self, item: dict[str, Any]) -> Track | None:
        """Parse a track item, looking the track up when the item carries no artist id."""
        if item["artists"]:
            return self._valid_one(self._parse_track, item)
        try:
            return await self.get_track(item["video_id"])
        except (MediaNotFoundError, InvalidDataError) as err:
            self.logger.debug("Skipping track %s: %s", item["video_id"], err)
            return None

    async def _uploader(self, video_id: str) -> list[dict[str, str]]:
        """Credit a video to its uploader channel (for videos YouTube lists without artist)."""
        response = await self._innertube.call("player", {"videoId": video_id})
        details = response.get("videoDetails") or {}
        if details.get("channelId"):
            name = str(details.get("author", "")).removesuffix(" - Topic")
            return [{"id": details["channelId"], "name": name}]
        return []

    async def _fetch_account_name(self) -> str | None:
        """Read the signed-in account's name (owned playlists carry it as owner)."""
        try:
            text = await self._innertube.get_text(
                "https://www.youtube.com/getAccountSwitcherEndpoint", with_cookie=True
            )
        except Exception as err:
            self.logger.debug("Could not read the account name: %s", err)
            return None
        match = re.search(r'"accountName":\{"simpleText":"([^"]+)"\}[^{}]*?"isSelected":true', text)
        match = match or re.search(r'"accountName":\{"simpleText":"([^"]+)"', text)
        return match.group(1) if match else None

    async def _keep_session_alive(self) -> None:
        """Keep the YouTube session from expiring while the provider is loaded."""
        while True:
            await asyncio.sleep(ROTATE_COOKIES_INTERVAL)
            try:
                await self._rotate_cookies()
            except LoginFailed as err:
                self.unload_with_error(err)
                return
            except (ClientError, TimeoutError) as err:
                self.logger.debug("Could not refresh the YouTube session: %s", err)

    async def _rotate_cookies(self) -> None:
        """Refresh the session cookies and store them, so a restart keeps the fresh ones."""
        stored = self.get_setup_value(CONF_COOKIE)
        if not await self._innertube.rotate_cookies() or self.unloading:
            return
        if stored != self.get_setup_value(CONF_COOKIE):
            # the user saved a new cookie meanwhile; this (old) instance is on its way out
            return
        self._update_setup_data(CONF_COOKIE, self._innertube.cookie)

    def _raw_playlist_id(self, prov_playlist_id: str) -> str:
        return prov_playlist_id.split(PLAYLIST_ID_DELIMITER, 1)[0].removeprefix("VL")

    def _valid[T](
        self, parse: Callable[[dict[str, Any]], T], items: list[dict[str, Any]]
    ) -> list[T]:
        return [result for item in items if (result := self._valid_one(parse, item)) is not None]

    def _valid_one[T](self, parse: Callable[[dict[str, Any]], T], item: dict[str, Any]) -> T | None:
        try:
            return parse(item)
        except InvalidDataError as err:
            self.logger.debug("Skipping %s: %s", item.get("title") or item.get("name"), err)
            return None

    # ------------------------------------------------------------ item builders

    def _parse_item(self, item: dict[str, Any]) -> MediaItemType | None:
        match item["kind"]:
            case "track":
                return self._parse_track(item)
            case "album":
                return self._parse_album(item)
            case "artist":
                return self._parse_artist(item)
            case "playlist":
                return self._parse_playlist(item)
            case "podcast":
                return self._parse_podcast(item)
        return None

    def _mapping(self, item_id: str, url: str, **kwargs: Any) -> set[ProviderMapping]:
        return {
            ProviderMapping(
                item_id=item_id,
                provider_domain=self.domain,
                provider_instance=self.instance_id,
                url=url,
                **kwargs,
            )
        }

    def _images(self, thumbs: list[dict[str, Any]] | None) -> UniqueList[MediaItemImage] | None:
        if not (url := parsers.image_url(thumbs or [])):
            return None
        return UniqueList(
            [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=url,
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
            ]
        )

    def _artist_mappings(self, artists: list[dict[str, str]]) -> list[ItemMapping]:
        mappings = []
        for artist in artists:
            artist_id = artist.get("id") or (
                VARIOUS_ARTISTS_YTM_ID if artist.get("name") == "Various Artists" else None
            )
            if artist_id:
                mappings.append(
                    ItemMapping(
                        media_type=MediaType.ARTIST,
                        item_id=artist_id,
                        provider=self.instance_id,
                        name=artist.get("name", ""),
                    )
                )
        return mappings

    def _parse_track(self, item: dict[str, Any]) -> Track:
        artists = self._artist_mappings(item["artists"])
        if not artists:
            raise InvalidDataError("Track has no artist")
        video_id = item["video_id"]
        name, version = parse_title_and_version(item["title"])
        track = Track(
            item_id=video_id,
            provider=self.instance_id,
            name=name,
            version=version,
            provider_mappings=self._mapping(
                video_id,
                f"{DOMAIN}/watch?v={video_id}",
                available=True,
                audio_format=AudioFormat(content_type=ContentType.M4A, codec_type=ContentType.AAC),
            ),
            disc_number=1,
            track_number=item.get("track_number") or 0,
        )
        track.artists = UniqueList(artists)
        if (album := item.get("album")) and album.get("id") and album.get("name"):
            track.album = ItemMapping(
                media_type=MediaType.ALBUM,
                item_id=album["id"],
                provider=self.instance_id,
                name=album["name"],
            )
        if item.get("duration"):
            track.duration = int(item["duration"])
        if item.get("explicit"):
            track.metadata.explicit = True
        if images := self._images(item.get("thumbnails")):
            track.metadata.images = images
        return track

    def _parse_album(self, item: dict[str, Any]) -> Album:
        album_id = item["browse_id"]
        name, version = parse_title_and_version(item["title"])
        album = Album(
            item_id=album_id,
            provider=self.instance_id,
            name=name,
            version=version,
            provider_mappings=self._mapping(album_id, f"{DOMAIN}/browse/{album_id}"),
        )
        album.artists = UniqueList(self._artist_mappings(item.get("artists") or []))
        if item.get("year"):
            album.year = int(item["year"])
        album.album_type = ALBUM_TYPES.get(item.get("album_type") or "", AlbumType.UNKNOWN)
        inferred = infer_album_type(name, version)
        if inferred in (AlbumType.SOUNDTRACK, AlbumType.LIVE):
            album.album_type = inferred
        if item.get("explicit"):
            album.metadata.explicit = True
        if item.get("description"):
            album.metadata.description = item["description"]
        if images := self._images(item.get("thumbnails")):
            album.metadata.images = images
        return album

    def _parse_artist(self, item: dict[str, Any]) -> Artist:
        artist_id = item["channel_id"]
        artist = Artist(
            item_id=artist_id,
            provider=self.instance_id,
            name=item["name"],
            provider_mappings=self._mapping(artist_id, f"{DOMAIN}/channel/{artist_id}"),
        )
        if item.get("description"):
            artist.metadata.description = item["description"]
        if images := self._images(item.get("thumbnails")):
            artist.metadata.images = images
        return artist

    def _parse_playlist(self, item: dict[str, Any]) -> Playlist:
        raw_id = item["playlist_id"]
        item_id, name = raw_id, item["title"]
        if raw_id in PERSONAL_PLAYLIST_IDS or raw_id.startswith(PERSONAL_PLAYLIST_PREFIXES):
            # these ids are the same for every account; make them unique per instance
            item_id = f"{raw_id}{PLAYLIST_ID_DELIMITER}{self.instance_id}"
            name = f"{name} ({self.name})"
        owner = item.get("author")
        editable = bool(item.get("editable")) or bool(
            owner and owner == self._account_name and not raw_id.startswith("RD")
        )
        playlist = Playlist(
            item_id=item_id,
            provider=self.instance_id,
            name=name,
            provider_mappings=self._mapping(
                item_id, f"{DOMAIN}/playlist?list={raw_id}", is_unique=editable
            ),
            is_editable=editable,
            owner=owner or self.name,
        )
        if item.get("description"):
            playlist.metadata.description = item["description"]
        if images := self._images(item.get("thumbnails")):
            playlist.metadata.images = images
        return playlist

    def _parse_podcast(self, item: dict[str, Any]) -> Podcast:
        podcast_id = item["podcast_id"]
        podcast = Podcast(
            item_id=podcast_id,
            provider=self.instance_id,
            name=item["title"],
            provider_mappings=self._mapping(podcast_id, f"{DOMAIN}/playlist?list={podcast_id}"),
        )
        if item.get("author"):
            podcast.publisher = item["author"]
        if item.get("description"):
            podcast.metadata.description = item["description"]
        if images := self._images(item.get("thumbnails")):
            podcast.metadata.images = images
        return podcast

    def _parse_episode(
        self, item: dict[str, Any], podcast: Podcast, position: int
    ) -> PodcastEpisode:
        item_id = f"{podcast.item_id}{PODCAST_EPISODE_SPLITTER}{item['video_id']}"
        episode = PodcastEpisode(
            item_id=item_id,
            provider=self.instance_id,
            name=item["title"],
            position=position,
            podcast=podcast,
            provider_mappings=self._mapping(
                item_id,
                f"{DOMAIN}/watch?v={item['video_id']}",
                audio_format=AudioFormat(content_type=ContentType.M4A, codec_type=ContentType.AAC),
            ),
        )
        if item.get("duration"):
            episode.duration = int(item["duration"])
        if item.get("description"):
            episode.metadata.description = item["description"]
        if release_date := parsers.parse_release_date(item.get("date"), utc()):
            episode.metadata.release_date = release_date
        if images := self._images(item.get("thumbnails")):
            episode.metadata.images = images
        return episode
