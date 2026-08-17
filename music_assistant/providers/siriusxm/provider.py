"""SiriusXM music provider for Music Assistant."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from aiosxm import AuthenticationError, NotEntitledError, SxmClient, SxmError
from aiosxm.proxy import make_routes
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType, MediaType, ProviderFeature
from music_assistant_models.errors import (
    LoginFailed,
    MediaNotFoundError,
    UnsupportedFeaturedException,
)
from music_assistant_models.media_items import (
    Artist,
    ProviderMapping,
    SearchResults,
)
from music_assistant_models.media_items import Track as MassTrack

from music_assistant.constants import CONF_ENTRY_UNOFFICIAL_PROVIDER
from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.webserver import Webserver
from music_assistant.models.music_provider import MusicProvider

from .browse import SiriusXMBrowseManager
from .constants import (
    BITRATE_256,
    BITRATES,
    CACHE_TTL_TRACKS,
    CONF_BITRATE,
    CONF_SXM_PASSWORD,
    CONF_SXM_USERNAME,
    PROXY_BIND_IP,
    QUEUE_BATCH_SIZE,
    QUEUE_LOW_WATER_MARK,
    TRACK_QUEUE_TYPES,
)
from .parsers import (
    parse_radio,
    parse_station,
    parse_track,
    parse_xtra_playlist,
    queue_item_id,
    split_queue_item_id,
    split_track_item_id,
    track_item_id,
)
from .streaming import SiriusXMStreamingManager

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Sequence

    from aiosxm import ArtistStation, Channel, SxmStream
    from aiosxm import SearchResults as SxmSearchResults
    from aiosxm import Track as SxmTrack
    from music_assistant_models.media_items import (
        BrowseFolder,
        ItemMapping,
        MediaItemType,
        Playlist,
        Radio,
    )
    from music_assistant_models.streamdetails import StreamDetails

SUPPORTED_FEATURES = {
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
    ProviderFeature.LIBRARY_RADIOS,
    ProviderFeature.LIBRARY_RADIOS_EDIT,
    ProviderFeature.LIBRARY_PLAYLISTS,
    ProviderFeature.LIBRARY_PLAYLISTS_EDIT,
}


class SiriusXMProvider(MusicProvider):
    """SiriusXM music provider."""

    client: SxmClient
    browse_manager: SiriusXMBrowseManager
    streaming_manager: SiriusXMStreamingManager
    channels_by_id: dict[str, Channel]
    # Resolved queue tracks by MA item id, with the time each was cached. A
    # SiriusXM track is only addressable through its parent's tune, so the url
    # has to be kept from when the batch was built; its CDN signature lasts an
    # hour, after which the entry is useless and gets dropped.
    _tracks: dict[str, tuple[float, SxmTrack]]
    # Tuned streams held per station, so the queue cursor advances between
    # refills instead of restarting at the same three tracks. One entry per
    # station played, and dropping one would rewind that listener's queue, so
    # these are kept for the life of the provider.
    _streams: dict[str, SxmStream]
    # Tracks fetched but not yet handed to MA, per station.
    _upcoming: dict[str, list[SxmTrack]]

    _server: Webserver
    _proxy_base_url: str

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return the (options) config entries for this provider instance."""
        return (
            CONF_ENTRY_UNOFFICIAL_PROVIDER,
            ConfigEntry(
                key=CONF_BITRATE,
                type=ConfigEntryType.STRING,
                default_value=BITRATE_256,
                options=[ConfigValueOption(bitrate) for bitrate in BITRATES],
                required=False,
            ),
        )

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        username = self.get_setup_value(CONF_SXM_USERNAME)
        password = self.get_setup_value(CONF_SXM_PASSWORD)
        assert isinstance(username, str)  # for type checker
        assert isinstance(password, str)  # for type checker

        self.channels_by_id = {}
        self._tracks = {}
        self._streams = {}
        self._upcoming = {}
        self.client = SxmClient(username, password, session=self.mass.http_session)

        try:
            await self.client.connect()
        except AuthenticationError as err:
            raise LoginFailed(f"Could not login to SiriusXM: {err}") from err

        if not await self.client.is_entitled():
            raise LoginFailed("This SiriusXM account has no active streaming subscription")

        self.browse_manager = SiriusXMBrowseManager(self)
        self.streaming_manager = SiriusXMStreamingManager(self)

        # SiriusXM requires an Authorization header on every playlist, key and
        # segment request, which ffmpeg won't send. Mount aiosxm's playback
        # routes on a loopback server and point the player at that instead.
        self._server = Webserver(self.logger)
        await self._server.setup(
            bind_ip=PROXY_BIND_IP,
            bind_port=0,
            static_routes=make_routes(self.client, stream_only=True),
        )
        self._proxy_base_url = f"http://{PROXY_BIND_IP}:{self._server.port}"
        self.logger.debug("SiriusXM stream proxy listening on %s", self._proxy_base_url)

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        await self._server.close()
        await self.client.disconnect()
        await super().unload(is_removed)

    @property
    def is_streaming_provider(self) -> bool:
        """Return True if the provider is a streaming provider."""
        return True

    @property
    def proxy_base_url(self) -> str:
        """Return the base URL of the local stream proxy."""
        return self._proxy_base_url

    @property
    def preferred_bitrate(self) -> str:
        """Return the configured stream bitrate."""
        bitrate = self.get_config_value(CONF_BITRATE)
        return bitrate if isinstance(bitrate, str) and bitrate in BITRATES else BITRATE_256

    async def get_channels(self) -> list[Channel]:
        """Return the full channel catalog, keeping the id lookup in step."""
        channels = await self.client.get_channels()
        self.channels_by_id = {channel.id: channel for channel in channels}
        return channels

    async def get_channel(self, channel_id: str) -> Channel:
        """
        Return a single channel by id.

        :param channel_id: The SiriusXM channel id.
        """
        if channel_id not in self.channels_by_id:
            await self.get_channels()
        if channel := self.channels_by_id.get(channel_id):
            return channel
        raise MediaNotFoundError(f"Channel {channel_id} not found")

    async def get_genres(self) -> dict[str, int]:
        """Return the available genres and how many channels each holds."""
        return await self.client.get_genres()

    async def get_library_channels(self) -> list[Channel]:
        """Return the channels saved to the SiriusXM account."""
        channels = await self.client.get_library_channels()
        for channel in channels:
            self.channels_by_id.setdefault(channel.id, channel)
        return channels

    async def get_library_radios(self) -> AsyncGenerator[Radio]:
        """Retrieve the live channels saved to the SiriusXM account."""
        # Only linear channels are radio; artist stations and Xtra channels are
        # runs of discrete tracks and surface as dynamic playlists instead.
        for channel in await self.get_library_channels():
            if channel.type in TRACK_QUEUE_TYPES:
                continue
            yield parse_radio(channel, self.instance_id, self.domain)

    async def get_library_playlists(self) -> AsyncGenerator[Playlist]:
        """Retrieve the artist stations and Xtra channels saved to the account."""
        for channel in await self.get_library_channels():
            if channel.type not in TRACK_QUEUE_TYPES:
                continue
            yield parse_xtra_playlist(channel, self.instance_id, self.domain)
        for station in await self.client.get_library_artist_stations():
            yield parse_station(station, self.instance_id, self.domain)

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """
        Get full playlist details by id.

        :param prov_playlist_id: A composite station or Xtra channel id.
        """
        parsed = split_queue_item_id(prov_playlist_id)
        if parsed is None:
            raise MediaNotFoundError(f"Not a SiriusXM station: {prov_playlist_id}")
        entity_type, entity_id = parsed
        if entity_type == "artist-station":
            station = await self.client.get_artist_station(entity_id)
            if station is None:
                raise MediaNotFoundError(f"Artist station {entity_id} not found")
            return parse_station(station, self.instance_id, self.domain)
        channel = await self.get_channel(entity_id)
        return parse_xtra_playlist(channel, self.instance_id, self.domain)

    @use_cache(CACHE_TTL_TRACKS)
    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[MassTrack]:
        """
        Get the upcoming tracks for a station.

        :param prov_playlist_id: A composite station or Xtra channel id.
        :param page: Pagination; only page 0 yields tracks. A dynamic playlist
            returns one batch and is asked again when the queue runs low.
        """
        # SiriusXM hands back three tracks at a time behind a cursor it keeps
        # server-side, and asking for more moves that cursor for good, so tracks
        # are buffered here. The result is cached because MA bypasses the cache
        # only when it is really filling a queue: browsing a station then shows
        # the tracks that will play rather than quietly using them up.
        if page > 0:
            return []
        parsed = split_queue_item_id(prov_playlist_id)
        if parsed is None:
            return []
        entity_type, entity_id = parsed

        upcoming = self._upcoming.setdefault(prov_playlist_id, [])
        if len(upcoming) < QUEUE_LOW_WATER_MARK:
            upcoming.extend(await self._pull_tracks(prov_playlist_id, entity_type, entity_id))
        if not upcoming:
            return []

        # Hand out the head of the buffer without removing it. The same batch is
        # returned until one of its tracks is actually streamed, so the listing
        # a listener sees is the listing that plays; _drop_played then advances
        # past what has been heard.
        return [
            parse_track(track, entity_type, entity_id, self.instance_id, self.domain)
            for track in upcoming[:QUEUE_BATCH_SIZE]
        ]

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """
        Get full radio details by id.

        :param prov_radio_id: The SiriusXM channel id.
        """
        channel = await self.get_channel(prov_radio_id)
        return parse_radio(channel, self.instance_id, self.domain)

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """
        Get artist details by id.

        :param prov_artist_id: The artist name, which doubles as its id.
        """
        # SiriusXM has no artist entity: a queued track only carries a name.
        # MA resolves the ItemMapping on every track lookup, so return the name
        # as a minimal artist rather than failing playback.
        return Artist(
            provider=self.instance_id,
            item_id=prov_artist_id,
            name=prov_artist_id,
            provider_mappings={
                ProviderMapping(
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    item_id=prov_artist_id,
                )
            },
        )

    async def get_track(self, prov_track_id: str) -> MassTrack:
        """
        Get full track details by id.

        :param prov_track_id: A composite ``entity_type|entity_id|track_id`` id.
        """
        parsed = split_track_item_id(prov_track_id)
        if parsed is None:
            raise MediaNotFoundError(f"Not a SiriusXM track: {prov_track_id}")
        entity_type, entity_id, _ = parsed
        track = await self.get_queue_track(prov_track_id)
        if track is None:
            raise MediaNotFoundError(f"Track {prov_track_id} is no longer available")
        return parse_track(track, entity_type, entity_id, self.instance_id, self.domain)

    async def get_queue_track(self, item_id: str) -> SxmTrack | None:
        """
        Return a queued track by its MA item id.

        Returns None if the track is no longer available.

        :param item_id: A composite ``entity_type|entity_id|track_id`` id.
        """
        if cached := self._tracks.get(item_id):
            return cached[1]
        parsed = split_track_item_id(item_id)
        if parsed is None:
            return None
        entity_type, entity_id, _ = parsed
        # Not cached: a restart or an expired batch loses the url, so re-tune
        # the parent station and look again.
        try:
            stream = await self.client.get_stream(entity_type, entity_id)
        except SxmError as err:
            self.logger.debug("Could not re-resolve track %s: %s", item_id, err)
            return None
        self._remember_tracks(entity_type, entity_id, stream.tracks)
        if cached := self._tracks.get(item_id):
            return cached[1]
        return None

    async def library_add(self, item: MediaItemType) -> bool:
        """
        Save a channel or station to the SiriusXM account library.

        :param item: The radio channel, artist station or Xtra channel to save.
        """
        if item.media_type == MediaType.RADIO:
            channel = await self.get_channel(item.item_id)
            await self.client.add_to_library(channel.type, channel.id)
            return True
        if item.media_type == MediaType.PLAYLIST:
            if (parsed := split_queue_item_id(item.item_id)) is None:
                raise MediaNotFoundError(f"Not a SiriusXM station: {item.item_id}")
            await self.client.add_to_library(*parsed)
            return True
        raise UnsupportedFeaturedException(
            f"Unsupported media type for library_add: {item.media_type}"
        )

    async def library_remove(self, prov_item_id: str, media_type: MediaType) -> bool:
        """
        Remove a channel or station from the SiriusXM account library.

        :param prov_item_id: A channel id, or a composite station id.
        :param media_type: The media type being removed.
        """
        if media_type == MediaType.RADIO:
            channel = await self.get_channel(prov_item_id)
            await self.client.remove_from_library(channel.type, channel.id)
            return True
        if media_type == MediaType.PLAYLIST:
            if (parsed := split_queue_item_id(prov_item_id)) is None:
                raise MediaNotFoundError(f"Not a SiriusXM station: {prov_item_id}")
            await self.client.remove_from_library(*parsed)
            return True
        raise UnsupportedFeaturedException(
            f"Unsupported media type for library_remove: {media_type}"
        )

    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 5,
    ) -> SearchResults:
        """
        Search the SiriusXM catalog.

        Live channels come back as radio; Xtra channels and artist stations as
        dynamic playlists.

        :param search_query: The query to search for.
        :param media_types: The media types to include in the results.
        :param limit: The maximum number of results per media type.
        """
        want_radio = MediaType.RADIO in media_types
        want_playlists = MediaType.PLAYLIST in media_types
        if not (want_radio or want_playlists) or not search_query.strip():
            return SearchResults()

        results = await self.client.search_all(search_query)
        radios: list[Radio] = []
        xtra: list[Playlist] = []
        for channel in results.channels:
            self.channels_by_id.setdefault(channel.id, channel)
            if channel.type in TRACK_QUEUE_TYPES:
                if want_playlists:
                    xtra.append(parse_xtra_playlist(channel, self.instance_id, self.domain))
            elif want_radio:
                radios.append(parse_radio(channel, self.instance_id, self.domain))

        playlists: list[Playlist] = []
        if want_playlists:
            # Artist stations lead: a query naming an artist is nearly always
            # after that artist's station, and there are far more Xtra channels,
            # which would otherwise push it past the limit.
            playlists = [
                parse_station(station, self.instance_id, self.domain)
                for station in await self._search_stations(results, limit)
            ]
            playlists.extend(xtra)

        return SearchResults(radio=radios[:limit], playlists=playlists[:limit])

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Browse this provider's items.

        :param path: The path to browse, (e.g. provider_id://genres).
        """
        return await self.browse_manager.browse(path)

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """
        Get the stream details for a channel.

        :param item_id: The SiriusXM channel id.
        :param media_type: The media type to stream.
        """
        try:
            return await self.streaming_manager.get_stream_details(item_id, media_type)
        except NotEntitledError as err:
            raise MediaNotFoundError(f"Channel {item_id} is not available: {err}") from err

    def drop_played(self, item_id: str) -> None:
        """
        Advance a station's buffer past a track that is now playing.

        :param item_id: A composite track id that is being streamed.
        """
        parsed = split_track_item_id(item_id)
        if parsed is None:
            return
        entity_type, entity_id, track_id = parsed
        playlist_id = queue_item_id(entity_type, entity_id)
        upcoming = self._upcoming.get(playlist_id)
        if not upcoming:
            return
        for index, track in enumerate(upcoming):
            if track.id == track_id:
                del upcoming[: index + 1]
                return

    async def _search_stations(self, results: SxmSearchResults, limit: int) -> list[ArtistStation]:
        """
        Find the artist stations a query implies.

        :param results: The search response to draw stations from.
        :param limit: How many results the caller will keep.
        """
        stations: list[ArtistStation] = []
        seen: set[str] = set()

        def keep(found: list[ArtistStation]) -> None:
            for station in found:
                if station.id not in seen:
                    seen.add(station.id)
                    stations.append(station)

        keep(results.artist_stations)
        if len(stations) >= limit:
            return stations

        # A station only matches on its own name, so a themed query like
        # "holiday" finds none directly. The artists the query matched each
        # have one, so resolve those until the limit is filled — a request
        # apiece, hence one at a time rather than all at once.
        async def collect(query: str) -> None:
            try:
                keep(await self.client.search_artist_stations(query))
            except SxmError as err:
                self.logger.debug("Station search failed for %r: %s", query, err)

        for name in (t.title for t in results.talent if t.title):
            await collect(name)
            if len(stations) >= limit:
                break
        return stations

    async def _pull_tracks(
        self, playlist_id: str, entity_type: str, entity_id: str
    ) -> list[SxmTrack]:
        """
        Fetch the next tracks from SiriusXM and remember how to stream them.

        :param playlist_id: The MA playlist id the tracks belong to.
        :param entity_type: The SiriusXM entity type to tune.
        :param entity_id: The SiriusXM entity id to tune.
        """
        try:
            stream = self._streams.get(playlist_id)
            if stream is None:
                stream = await self.client.get_stream(entity_type, entity_id)
                self._streams[playlist_id] = stream
                tracks = stream.tracks
            else:
                tracks = await stream.next_tracks()
                if not tracks:
                    # the queue stopped issuing a cursor; start it over
                    stream = await self.client.get_stream(entity_type, entity_id)
                    self._streams[playlist_id] = stream
                    tracks = stream.tracks
        except SxmError as err:
            self.logger.debug("Could not fetch tracks for %s: %s", playlist_id, err)
            return []
        playable = [track for track in tracks if track.url]
        self._remember_tracks(entity_type, entity_id, playable)
        return playable

    def _remember_tracks(self, entity_type: str, entity_id: str, tracks: list[SxmTrack]) -> None:
        """
        Hold on to a batch of tracks so they can be streamed by id.

        :param entity_type: The SiriusXM entity type of the parent station.
        :param entity_id: The SiriusXM entity id of the parent station.
        :param tracks: The tracks to remember.
        """
        # Nothing else empties this, so drop what has outlived its signed url
        # while writing rather than growing for as long as the server runs.
        now = time.time()
        expired = [
            key
            for key, (cached_at, _) in self._tracks.items()
            if now - cached_at > CACHE_TTL_TRACKS
        ]
        for key in expired:
            del self._tracks[key]
        for track in tracks:
            self._tracks[track_item_id(entity_type, entity_id, track.id)] = (now, track)
