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
from music_assistant_models.errors import AudioError
from music_assistant_models.media_items import SearchResults, UniqueList

from .provider import Provider

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Sequence

    from music_assistant_models.enums import MediaType, RepeatMode, SourceControl
    from music_assistant_models.media_items import (
        Album,
        Artist,
        AudioSource,
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


# payload accepted by ``on_source_control``: seek position (seconds) or volume level
# for SEEK/VOLUME, the enabled state for SHUFFLE, the RepeatMode for REPEAT,
# None for plain transport actions
type SourceControlValue = int | bool | RepeatMode | None


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

    async def get_radio_tracks(self, prov_radio_id: str, page: int = 0) -> list[Track]:
        """
        Return one page of a tracklisted radio station's finite tracklist.

        Only called for a Radio with ``is_endless`` and ``is_dynamic`` both unset. Return an
        empty page to signal the end of the listing.

        :param prov_radio_id: Provider-scoped radio id.
        :param page: Zero-based page index for paginated results.
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

    async def get_artist_toptrack_titles(self, artist: Artist, limit: int = 25) -> list[str]:
        """
        Retrieve the titles of an artist's top tracks, without resolving them to playable items.

        Optional, only asked of providers that declare ProviderFeature.ARTIST_TOPTRACKS. Returns
        no titles by default.

        :param artist: The reference artist.
        :param limit: Maximum number of titles to return.
        """
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
        it should stay free of side effects: a provider claims an exclusive AudioSource in
        ``on_source_selected``, a music provider reports playback in ``on_stream_started``
        and ``on_streamed``.

        :param item_id: The provider-scoped id of the item requested for playback, for a
            provider of AudioSources also an ``AudioSource.item_id``.
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

    async def open_local_file(self, path: str) -> int | None:
        """
        Open a local file of this provider's StreamDetails, right before Music Assistant reads it.

        Music Assistant reads the file through the returned descriptor and then closes it, which
        lets a provider check the very file that is read. It is called on every start, seek or
        retry of a stream and for each part of a multi-part stream. Return None (the default)
        to have the path opened as is.

        :param path: The path of a StreamDetails with stream type LOCAL_FILE, or of one of
            its parts.
        :raises MediaNotFoundError: If the file can not or may not be read.
        """
        return None


class AudioSourceRefusedError(AudioError):
    """Raised when the plugin of a live source refuses a consumer; the refusal is logged already."""


class AudioSourceMixin(AudioStreamMixin):
    """Methods of a provider that delivers live AudioSources."""

    async def get_audio_sources(self) -> list[AudioSource]:
        """
        Return all AudioSources this provider currently exposes.

        Will only be called if ProviderFeature.AUDIO_SOURCE is declared.

        May change over time (e.g. when a paired hardware device adds/removes
        favorites). Each AudioSource is a regular MediaItem, playable via the standard
        play_media flow.

        A source carries its own name, without the name of a player. One that belongs to
        a player names it in ``owner_player_id``, and the server adds the name of that
        player where the source is shown elsewhere.

        Streaming contract for AudioSources: ``get_stream_details`` must stay side-effect-free
        (MA also calls it from queue preload), so an exclusive source is claimed in
        ``on_source_selected``, paired with ``on_source_unselected``. In the returned
        StreamDetails ``audio_format`` describes the source, ``decoded_audio_format`` the PCM
        actually delivered (set it when the provider decodes the source itself) and
        ``stream_metadata`` the initial live metadata, updated at runtime through
        ``mass.players.update_source_metadata``. The consuming player needs a continuous byte
        flow: for ``StreamType.CUSTOM`` the server wraps ``get_audio_stream`` with a silence
        keepalive, so the provider can simply stop yielding while the upstream device is paused;
        for ``StreamType.NAMED_PIPE`` the producing process itself must keep writing silence
        during pause (shairport-sync and librespot do by default) or the player disconnects.

        :return: A list of AudioSource items. Return an empty list if the provider
            currently has no sources to expose (e.g. hardware is offline).
        """
        if ProviderFeature.AUDIO_SOURCE in self.supported_features:
            raise NotImplementedError
        return []

    def get_player_audio_sources(self, player_id: str) -> list[AudioSource] | None:
        """
        Return the AudioSources this provider has bound to the given player.

        Providers that expose one source per (connected) player override this so
        consumers can scope source listings to a single player: return the
        player's own sources, or an empty list when the player has none on this
        provider. The default of None means the provider's sources are not
        player-bound and apply to every player.

        Sync on purpose: called from the player's (sync) state calculation.

        :param player_id: The player to return the bound AudioSources for.
        """
        return None

    def get_player_input_sources(self, player_id: str) -> list[AudioSource]:
        """
        Return the AudioSources that are inputs of the given player's own device, like a line-in.

        They are listed in this player's source list and stay available to every
        player: unlike get_player_audio_sources, this does not bind the provider to players.

        Sync on purpose: called from the player's (sync) state calculation.

        :param player_id: The id of the player or of one of its protocols: the player asks
            with each, so a device known by the id of its protocol is found as well.
        """
        return []

    def delivers_normalized_audio(self, streamdetails: StreamDetails) -> bool | None:
        """
        Return whether this provider normalizes the live audio it delivers, if known.

        :param streamdetails: Stream details of the active AudioSource.
        """
        return None

    def delivers_crossfaded_audio(self, streamdetails: StreamDetails) -> bool | None:
        """
        Return whether this provider crossfades the live audio it delivers, if known.

        :param streamdetails: Stream details of the active AudioSource.
        """
        return None

    async def on_source_control(
        self,
        source_id: str,
        action: SourceControl,
        value: SourceControlValue = None,
    ) -> None:
        """
        Handle a playback control command for an active AudioSource.

        Called when the user (or an automation) issues a control command while
        this AudioSource is the live source on a player. The player controller
        gates the transport actions on the flag the source declares for each:
        ``can_play_pause`` for PLAY/PAUSE, ``can_seek`` for SEEK and
        ``can_next_previous`` for NEXT/PREVIOUS. SHUFFLE/REPEAT are forwarded
        whatever ``can_shuffle`` / ``can_repeat`` say, because only the session
        knows whether its current content can be reordered — those flags tell
        clients what to offer, and a source declaring them is expected to report
        the resulting state back via ``mass.players.update_source_options``.

        :param source_id: The AudioSource.item_id the command applies to.
        :param action: The control action to perform.
        :param value: Optional payload for the action: seek position in seconds
            for SEEK, volume level 0-100 for VOLUME, the enabled state (bool)
            for SHUFFLE, the RepeatMode for REPEAT; None for other actions.
        """
        raise NotImplementedError

    async def on_source_selected(
        self,
        source_id: str,
        player_id: str,
        owner_player_id: str,
        stream_session_id: str,
    ) -> None:
        """
        React to an AudioSource being selected for playback.

        Providers exposing an exclusive AudioSource MUST claim ownership in this
        hook (rather than in ``get_stream_details``). This hook fires only on
        the actual stream request — not on queue preload — so claiming here
        keeps the preload path side-effect-free and lets cross-queue handoffs
        succeed (the streams controller fires this **before**
        ``get_stream_details`` so the provider can stop the previous player and
        replace its claim before the upcoming stream-details fetch).

        ``stream_session_id`` is a fresh per-request token paired with the
        matching ``on_source_unselected`` call. Providers should store it (and
        replace any previously stored value) so the unselect callback can be
        rejected as stale when a same-queue reconnect interleaves with the
        prior request's teardown — see ``on_source_unselected`` for details.

        :param source_id: The AudioSource.item_id that was selected.
        :param player_id: The id the stream is fetched under: that of the output protocol
            of the player that renders the audio (only a live source fetched on its own
            route passes the owner player). It is no player or queue id: do not use it as
            one; ``mass.players.get_player_by_protocol`` returns its player.
        :param owner_player_id: The player that owns this playback session. Prefer this
            for anything you store: it is the user-facing player, the id that player and
            queue commands such as play_media and cmd_stop take.
        :param stream_session_id: Opaque controller-generated token identifying
            this specific stream request. The matching ``on_source_unselected``
            receives the same value.
        """

    async def on_source_unselected(
        self,
        source_id: str,
        owner_player_id: str,
        stream_session_id: str,
    ) -> None:
        """
        React to MA tearing down an AudioSource stream from this queue.

        Fired in the ``finally`` block of the queue-item streaming handler — so
        it runs whether the stream ended normally, the player disconnected, the
        queue moved on, or an exception interrupted streaming. Override to
        release any per-queue state set in ``get_stream_details`` (notably the
        exclusive lock used to reject cross-queue claims) so the source becomes
        available to other queues without depending on an external session
        event.

        Implementations MUST guard on ``stream_session_id`` matching the value
        last set in ``on_source_selected``. A owner_player_id-only check is not
        sufficient: same-queue reconnects (player drops + reopens the same
        stream URL before the original request's finally fires) would
        otherwise let the old request's late callback clear the live claim of
        the new stream, silently dropping metadata and volume sync.

        :param source_id: The AudioSource.item_id whose stream ended.
        :param owner_player_id: The player that owns the stream being torn down.
        :param stream_session_id: The token paired with ``on_source_selected``
            for this specific stream request. Ignore the callback if it does
            not match the currently stored active session id.
        """

    async def on_source_released(self, source_id: str, player_id: str) -> None:
        """
        React to a player letting go of this AudioSource.

        Fired when the player stops playing the source for good: another source was
        selected on it, it was deselected, or the player went away. Not fired when a
        stream merely ends — a paused source keeps the player, and its stream is torn
        down without the player being done with it. Override to release state that
        must not outlive the player's use of the source, such as an upstream session
        still pointing at Music Assistant.

        Guard on the player still being the one you hold: a source moving to another
        player claims the new one before releasing the old, so this can arrive after
        the source is already playing elsewhere.

        :param source_id: The AudioSource.item_id that was released.
        :param player_id: The player that let it go.
        """

    async def on_volume_change(self, source_id: str, volume: int) -> None:
        """
        React to a volume change on the player streaming this AudioSource.

        Optional hook. Override when the provider wants to sync the upstream
        device's volume slider with MA (e.g. Spotify Connect updating the
        Spotify app's volume display, Yandex Ynison forwarding the new
        level back to the Yandex device). Fired only on the direct queue
        owner — group volume changes fire once at the group level, not
        per child.

        :param source_id: The AudioSource.item_id currently streaming.
        :param volume: The new volume level (0-100).
        """
