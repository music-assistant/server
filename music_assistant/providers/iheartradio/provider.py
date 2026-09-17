"""iHeartRadio music provider for Music Assistant."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import MediaType, ProviderFeature
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import SearchResults

from music_assistant.constants import CONF_ENTRY_UNOFFICIAL_PROVIDER, CONF_USERNAME
from music_assistant.helpers.podcast_parsers import rank_episodes_by_date
from music_assistant.models.music_provider import MusicProvider

from .api import IHeartRadioApiClient, json_items
from .auth import IHeartRadioAuthManager
from .browse import IHeartRadioBrowseManager
from .constants import (
    CONF_COUNTRY,
    DEFAULT_COUNTRY,
    REPORT_STATUS_DONE,
    REPORT_STATUS_SKIP,
)
from .library import IHeartRadioLibraryManager
from .parsers import (
    parse_album,
    parse_artist,
    parse_artist_radio,
    parse_live_station,
    parse_podcast,
    parse_podcast_episode,
    parse_track,
    split_artist_radio_item_id,
    split_catalog_track_item_id,
    split_episode_item_id,
)
from .stations import IHeartRadioStationManager
from .streaming import IHeartRadioStreamingManager

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Sequence

    from music_assistant_models.config_entries import ConfigEntry
    from music_assistant_models.media_items import (
        Album,
        Artist,
        BrowseFolder,
        ItemMapping,
        MediaItemType,
        Podcast,
        PodcastEpisode,
        Radio,
        Track,
    )
    from music_assistant_models.streamdetails import StreamDetails

SUPPORTED_FEATURES = {
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
}
# Library features, available with a signed-in account only.
LIBRARY_FEATURES = {
    ProviderFeature.LIBRARY_RADIOS,
    ProviderFeature.LIBRARY_RADIOS_EDIT,
    ProviderFeature.LIBRARY_PODCASTS,
    ProviderFeature.LIBRARY_PODCASTS_EDIT,
}


class IHeartRadioProvider(MusicProvider):
    """iHeartRadio music provider."""

    api: IHeartRadioApiClient
    auth: IHeartRadioAuthManager | None = None
    browse_manager: IHeartRadioBrowseManager
    library_manager: IHeartRadioLibraryManager
    stations: IHeartRadioStationManager
    streaming_manager: IHeartRadioStreamingManager

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return the supported features, library sync only with a configured account."""
        # Read from setup rather than the session: MA resolves a provider's config entries
        # (and so its features) before handle_async_init has signed in.
        if str(self.get_setup_value(CONF_USERNAME) or "").strip():
            return SUPPORTED_FEATURES | LIBRARY_FEATURES
        return set(SUPPORTED_FEATURES)

    @property
    def supported_media_types(self) -> set[MediaType]:
        """Return the media types this provider can serve."""
        # Without library support the base implementation would report none, which would
        # keep the provider out of search-based lookups.
        return {MediaType.RADIO, MediaType.PODCAST, MediaType.PODCAST_EPISODE}

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return the (options) config entries for this provider instance."""
        return (CONF_ENTRY_UNOFFICIAL_PROVIDER,)

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        country = str(self.get_setup_value(CONF_COUNTRY) or DEFAULT_COUNTRY)
        self.api = IHeartRadioApiClient(self, country)
        self.browse_manager = IHeartRadioBrowseManager(self)
        self.library_manager = IHeartRadioLibraryManager(self)
        self.stations = IHeartRadioStationManager(self)
        self.streaming_manager = IHeartRadioStreamingManager(self)
        self.auth = IHeartRadioAuthManager(self)
        await self.auth.login()

    async def get_library_radios(self) -> AsyncGenerator[Radio]:
        """Retrieve the followed live stations and artist radios."""
        async for radio in self.library_manager.get_library_radios():
            yield radio

    async def get_library_podcasts(self) -> AsyncGenerator[Podcast]:
        """Retrieve the followed podcasts."""
        async for podcast in self.library_manager.get_library_podcasts():
            yield podcast

    async def library_add(self, item: MediaItemType) -> bool:
        """
        Follow a station, artist radio or podcast on iHeartRadio.

        :param item: The item to follow.
        """
        return await self.library_manager.library_add(item)

    async def library_remove(self, prov_item_id: str, media_type: MediaType) -> bool:
        """
        Unfollow a station, artist radio or podcast on iHeartRadio.

        :param prov_item_id: The provider item id.
        :param media_type: The media type of the item.
        """
        return await self.library_manager.library_remove(prov_item_id, media_type)

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """
        Get full radio details by id.

        :param prov_radio_id: The iHeartRadio live station id, or an artist radio id.
        """
        if artist_id := split_artist_radio_item_id(prov_radio_id):
            profile = await self.api.get_artist_profile(artist_id)
            radio = parse_artist_radio(profile.get("artist") or {}, self.instance_id, self.domain)
        else:
            station = await self.api.get_station(prov_radio_id)
            radio = (
                parse_live_station(station, self.instance_id, self.domain)
                if station is not None
                else None
            )
        if radio is None:
            raise MediaNotFoundError(f"Station {prov_radio_id} not found")
        return radio

    async def get_dynamic_radio_tracks(self, prov_radio_id: str) -> list[Track]:
        """
        Return a fresh batch of tracks for an artist radio.

        :param prov_radio_id: The artist radio id.
        """
        return await self.stations.get_dynamic_radio_tracks(prov_radio_id)

    async def get_track(self, prov_track_id: str) -> Track:
        """
        Get full track details by id.

        :param prov_track_id: The iHeartRadio track id, or the id of a catalog listing.
        """
        if catalog_track_id := split_catalog_track_item_id(prov_track_id):
            # an album's listing stays unplayable even while an artist radio holds the song
            payload = await self.api.get_catalog_track(catalog_track_id) or {}
            track = parse_track(payload, self.instance_id, self.domain, catalog=True)
        elif found := self.stations.find(prov_track_id):
            # a track served by an artist radio is still retained
            track = parse_track(found[1].get("content") or {}, self.instance_id, self.domain)
        else:
            # the catalog answers for the rest, but those cannot be played
            payload = await self.api.get_catalog_track(prov_track_id) or {}
            track = parse_track(payload, self.instance_id, self.domain, available=False)
        if track is None:
            raise MediaNotFoundError(f"Track {prov_track_id} not found")
        return track

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """
        Get full artist details by id.

        :param prov_artist_id: The iHeartRadio artist id.
        """
        profile = await self.api.get_artist_profile(prov_artist_id)
        if (
            artist := parse_artist(profile.get("artist") or {}, self.instance_id, self.domain)
        ) is None:
            raise MediaNotFoundError(f"Artist {prov_artist_id} not found")
        return artist

    async def get_album(self, prov_album_id: str) -> Album:
        """
        Get full album details by id.

        :param prov_album_id: The iHeartRadio album id.
        """
        payload = await self.api.get_catalog_album(prov_album_id) or {}
        if (album := parse_album(payload, self.instance_id, self.domain)) is None:
            raise MediaNotFoundError(f"Album {prov_album_id} not found")
        return album

    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """
        Get the tracks of an album, listed but not playable.

        :param prov_album_id: The iHeartRadio album id.
        """
        if (album := await self.api.get_catalog_album(prov_album_id)) is None:
            raise MediaNotFoundError(f"Album {prov_album_id} not found")
        # the album's own tracks carry no artist, artwork or album reference of their own
        shared = {
            "albumId": album.get("albumId"),
            "albumName": album.get("title"),
            "artistId": album.get("artistId"),
            "artistName": album.get("artistName"),
            "imageUrl": album.get("image"),
        }
        # on-demand playback of an album needs iHeartRadio's All Access tier, which this
        # provider does not stream, so the tracks are shown but marked unavailable
        return [
            track
            for item in json_items(album.get("tracks"))
            if (
                track := parse_track(
                    {**shared, **item}, self.instance_id, self.domain, catalog=True
                )
            )
        ]

    async def get_podcast(self, prov_podcast_id: str) -> Podcast:
        """
        Get full podcast details by id.

        :param prov_podcast_id: The iHeartRadio podcast id.
        """
        data = await self.api.get_podcast_data(prov_podcast_id)
        podcast = parse_podcast(data, self.instance_id, self.domain) if data is not None else None
        if podcast is None:
            raise MediaNotFoundError(f"Podcast {prov_podcast_id} not found")
        return podcast

    async def get_podcast_episodes(self, prov_podcast_id: str) -> AsyncGenerator[PodcastEpisode]:
        """
        Get all episodes of a podcast, newest episode holding the highest position.

        :param prov_podcast_id: The iHeartRadio podcast id.
        """
        podcast = await self.api.get_podcast_data(prov_podcast_id) or {}
        for position, episode in await self._positioned_episodes(prov_podcast_id):
            if mass_episode := parse_podcast_episode(
                episode, prov_podcast_id, position, self.instance_id, self.domain, podcast
            ):
                yield mass_episode

    async def get_podcast_episode(self, prov_episode_id: str) -> PodcastEpisode:
        """
        Get full podcast episode details by id.

        :param prov_episode_id: The MA episode id, holding the podcast and episode id.
        """
        if (parsed := split_episode_item_id(prov_episode_id)) is None:
            raise MediaNotFoundError(f"Not an iHeartRadio episode: {prov_episode_id}")
        podcast_id, episode_id = parsed
        episode = await self.api.get_episode(episode_id)
        if episode is None:
            raise MediaNotFoundError(f"Episode {episode_id} not found")
        podcast = await self.api.get_podcast_data(podcast_id) or {}
        mass_episode = parse_podcast_episode(
            episode,
            podcast_id,
            await self._episode_position(podcast_id, episode_id),
            self.instance_id,
            self.domain,
            podcast,
        )
        if mass_episode is None:
            raise MediaNotFoundError(f"Episode {episode_id} not found")
        return mass_episode

    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 5,
    ) -> SearchResults:
        """
        Search the iHeartRadio catalogue for stations, artist radios and podcasts.

        :param search_query: The query to search for.
        :param media_types: The media types to include in the results.
        :param limit: The maximum number of results per kind; live stations and artist
            radios are each capped at it.
        """
        want_radio = MediaType.RADIO in media_types
        want_podcasts = MediaType.PODCAST in media_types
        if not (want_radio or want_podcasts) or not (keywords := search_query.strip()):
            return SearchResults()
        results = await self.api.search(keywords, want_radio, want_podcasts, limit)
        radio: list[Radio] = []
        podcasts: list[Podcast] = []
        if want_radio:
            # the API pads the stations with unrelated ones when nothing matches, so a
            # station is only kept when it names every word of the query
            words = re.findall(r"\w[\w.]*", keywords.casefold())
            # a search hit carries no streams; they are resolved when playback starts
            radio = [
                station
                for hit in json_items(results.get("stations"))
                if _station_matches(hit, words)
                and (station := parse_live_station(hit, self.instance_id, self.domain))
            ][:limit]
            radio += [
                artist_radio
                for hit in json_items(results.get("artists"))[:limit]
                if (artist_radio := parse_artist_radio(hit, self.instance_id, self.domain))
            ]
        if want_podcasts:
            podcasts = [
                podcast
                for hit in json_items(results.get("podcasts"))[:limit]
                if (podcast := parse_podcast(hit, self.instance_id, self.domain))
            ]
        return SearchResults(radio=radio, podcasts=podcasts)

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Browse this provider's items.

        :param path: The path to browse, e.g. ``iheartradio--xx://live/genres``.
        """
        return await self.browse_manager.browse(path)

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """
        Get the stream details for a live station, a podcast episode or a radio track.

        :param item_id: The live station id, the MA episode id or the track id.
        :param media_type: The media type to stream.
        """
        return await self.streaming_manager.get_stream_details(item_id, media_type)

    async def on_played(
        self,
        media_type: MediaType,
        prov_item_id: str,
        fully_played: bool,
        position: int,
        media_item: MediaItemType,
        is_playing: bool = False,
    ) -> None:
        """
        Report a started, finished or skipped artist radio track to iHeartRadio.

        :param media_type: The media type of the played item.
        :param prov_item_id: The provider item id.
        :param fully_played: Whether the item was played to the end.
        :param position: The last known position in seconds.
        :param media_item: The played item.
        :param is_playing: Whether the item is still playing.
        """
        if media_type != MediaType.TRACK:
            return
        if is_playing:
            # the queue resolves stream details ahead of playback to preload the next
            # track, so the start is only reported once the track is actually heard
            await self.stations.report_start(prov_item_id)
            return
        if not fully_played and position == 0:
            # the user marked the item as unplayed; nothing was heard
            return
        status = REPORT_STATUS_DONE if fully_played else REPORT_STATUS_SKIP
        await self.stations.report_play(prov_item_id, status, position)

    async def _episode_position(self, podcast_id: str, episode_id: str) -> int:
        """
        Return the listing position of one episode, or 0 when it cannot be placed.

        :param podcast_id: The iHeartRadio podcast id.
        :param episode_id: The iHeartRadio episode id.
        """
        # the episode endpoint reports no episode number, so the position comes from the
        # (cached) listing
        for position, episode in await self._positioned_episodes(podcast_id):
            if str(episode.get("id")) == episode_id:
                return position
        return 0

    async def _positioned_episodes(self, podcast_id: str) -> list[tuple[int, dict[str, Any]]]:
        """
        Return a podcast's episodes with their listing position, oldest to newest.

        :param podcast_id: The iHeartRadio podcast id.
        """
        episodes = await self.api.get_episodes(podcast_id)
        positions = rank_episodes_by_date(
            [episode.get("startDate") or None for episode in episodes]
        )
        return list(zip(positions, episodes, strict=True))


def _station_matches(station: dict[str, Any], words: list[str]) -> bool:
    """Return whether a station search hit names every one of the query words."""
    text = " ".join(
        str(station.get(key) or "")
        for key in ("name", "callLetters", "description", "frequency", "genre")
    ).casefold()
    return all(word in text for word in words)
