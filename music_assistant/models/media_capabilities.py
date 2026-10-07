"""
Capability mixins shared by the provider base classes.

Each mixin holds the methods of one capability that more than one provider type can offer, so
a controller that works by capability (it checks the provider's features) can type the provider
by the mixin instead of by a union of provider classes. A mixin only declares the contract: a
provider implements the methods for the features it declares. A feature-gated default returns
an empty result until the feature is declared; a lookup of a single item raises
NotImplementedError. The mixins derive from Provider, so a provider typed by a capability still
carries its identity.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import ProviderFeature
from music_assistant_models.media_items import SearchResults, UniqueList

from .provider import Provider

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Sequence

    from music_assistant_models.enums import MediaType
    from music_assistant_models.media_items import (
        Album,
        Artist,
        BrowseFolder,
        ItemMapping,
        MediaItemType,
        Playlist,
        Radio,
        RecommendationFolder,
        Track,
    )
    from music_assistant_models.streamdetails import StreamDetails

    from music_assistant.constants import PlaylistPlayableItem


class MediaCatalogMixin(Provider):
    """Methods of a provider that serves browsable and playable items of its own."""

    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 5,
    ) -> SearchResults:
        """
        Perform a search on this provider.

        Will only be called if ProviderFeature.SEARCH is declared.

        :param search_query: Search query.
        :param media_types: A list of media_types to include.
        :param limit: Number of items to return in the search (per type).
        """
        if ProviderFeature.SEARCH in self.supported_features:
            raise NotImplementedError
        return SearchResults()

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Browse this provider's items.

        Will only be called if ProviderFeature.BROWSE is declared.

        :param path: The path to browse, in the form ``<instance_id>://<sub_path>``.
        """
        if ProviderFeature.BROWSE in self.supported_features:
            raise NotImplementedError
        return []

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """
        Return full details of a single playlist owned by this provider.

        :param prov_playlist_id: Provider-scoped playlist id.
        """
        raise NotImplementedError

    async def get_playlist_tracks(
        self,
        prov_playlist_id: str,
        page: int = 0,
    ) -> Sequence[PlaylistPlayableItem]:
        """
        Return a page of items for a playlist owned by this provider.

        :param prov_playlist_id: Provider-scoped playlist id.
        :param page: Zero-based page index for paginated results.
        """
        raise NotImplementedError

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """
        Return full details of a single radio station owned by this provider.

        :param prov_radio_id: Provider-scoped radio id.
        """
        raise NotImplementedError

    async def get_dynamic_radio_tracks(self, prov_radio_id: str) -> list[Track]:
        """
        Return a fresh batch of tracks for a dynamic radio station owned by this provider.

        Only called for a Radio with ``is_dynamic`` set. Every call returns a new batch; there
        is no stable listing and no pagination. Return an empty batch to signal the station's
        feed is exhausted; the queue then plays out its remaining items and ends.

        :param prov_radio_id: Provider-scoped radio id.
        """
        raise NotImplementedError


class RecommendationsMixin(Provider):
    """Methods of a provider that contributes recommendation rows."""

    async def get_recommendations(self) -> list[RecommendationFolder]:
        """
        Get this provider's available recommendation rows, without items.

        Must be fast: return static or cached row descriptors only, without
        live backend calls. The items for a row are fetched separately
        through get_recommendation_items.

        Will only be called if ProviderFeature.RECOMMENDATIONS is declared.
        """
        if ProviderFeature.RECOMMENDATIONS in self.supported_features:
            raise NotImplementedError
        return []

    async def get_recommendation_items(
        self, item_id: str
    ) -> UniqueList[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Get the items for a single recommendation row.

        Live backend fetches belong here. Will only be called if
        ProviderFeature.RECOMMENDATIONS is declared.

        :param item_id: The item_id of the row, as returned by get_recommendations.
        """
        if ProviderFeature.RECOMMENDATIONS in self.supported_features:
            raise NotImplementedError
        return UniqueList()


class MusicDiscoveryMixin(Provider):
    """
    Methods of a provider that finds items related to a given (library) item.

    The reference item is handed over in full, so the provider can match it by its
    external ids or names. A music provider resolving its own item ids has its own
    variants of these methods instead.
    """

    async def get_similar_tracks(self, track: Track, limit: int = 25) -> list[Track]:
        """
        Retrieve a list of similar tracks for the given track.

        Will only be called if ProviderFeature.SIMILAR_TRACKS is declared.

        :param track: The reference track.
        :param limit: Maximum number of similar tracks to return.
        """
        if ProviderFeature.SIMILAR_TRACKS in self.supported_features:
            raise NotImplementedError
        return []

    async def get_similar_artists(self, artist: Artist, limit: int = 25) -> list[Artist]:
        """
        Retrieve a list of similar artists for the given artist.

        Will only be called if ProviderFeature.SIMILAR_ARTISTS is declared.

        :param artist: The reference artist.
        :param limit: Maximum number of similar artists to return.
        """
        if ProviderFeature.SIMILAR_ARTISTS in self.supported_features:
            raise NotImplementedError
        return []

    async def get_artist_toptracks(self, artist: Artist, limit: int = 25) -> list[Track]:
        """
        Retrieve a list of top tracks for the given artist.

        Will only be called if ProviderFeature.ARTIST_TOPTRACKS is declared.

        :param artist: The reference artist.
        :param limit: Maximum number of top tracks to return.
        """
        if ProviderFeature.ARTIST_TOPTRACKS in self.supported_features:
            raise NotImplementedError
        return []

    async def get_artist_topalbums(self, artist: Artist, limit: int = 25) -> list[Album]:
        """
        Retrieve a list of top albums for the given artist.

        Will only be called if ProviderFeature.ARTIST_TOPALBUMS is declared.

        :param artist: The reference artist.
        :param limit: Maximum number of top albums to return.
        """
        if ProviderFeature.ARTIST_TOPALBUMS in self.supported_features:
            raise NotImplementedError
        return []


class AudioStreamMixin(Provider):
    """Methods of a provider that streams the audio of items it owns."""

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """
        Return StreamDetails for a playable item owned by this provider.

        Music Assistant calls this from the streaming path as well as from queue preload, so
        it should stay free of side effects: a plugin claims an exclusive AudioSource in
        ``on_source_selected``, a music provider reports playback in ``on_streamed``.

        :param item_id: The provider-scoped id of the item requested for playback, for a
            plugin also an ``AudioSource.item_id``.
        :param media_type: The media type of the requested item.
        """
        raise NotImplementedError

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes]:
        """
        Return the (custom) audio stream for an item owned by this provider.

        Will only be called when the StreamDetails returned by get_stream_details has
        ``stream_type=StreamType.CUSTOM``. The yielded bytes arrive in the format
        ``streamdetails.decoded_audio_format`` declares when the provider decodes the source
        itself, otherwise in ``streamdetails.audio_format``, which may be an encoded format
        that Music Assistant decodes. Release any per-session state in a ``try/finally``: the
        consumer closes the generator when playback ends or another queue takes over.

        :param streamdetails: The StreamDetails previously returned by get_stream_details.
        :param seek_position: Position in seconds to start from; ignored for live sources.
        """
        raise NotImplementedError
        # unreachable, but the yield keeps this method an async generator
        # so an unimplemented provider fails deterministically without emitting
        # a stray empty chunk to the downstream consumer first.
        yield b""  # type: ignore[unreachable]
