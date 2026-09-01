"""Model/base for a Plugin Provider implementation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import ProviderFeature
from music_assistant_models.media_items import SearchResults, UniqueList

from .provider import Provider

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Sequence

    from music_assistant_models.enums import MediaType, RepeatMode, SourceControl
    from music_assistant_models.media_items import (
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


# separator between the owning provider's instance_id and the provider-scoped engine id;
# occurs in neither MA instance_ids nor Home Assistant entity_ids
ENGINE_UID_SEPARATOR = "/"

# payload accepted by ``on_source_control``: seek position (seconds) or volume level
# for SEEK/VOLUME, the enabled state for SHUFFLE, the RepeatMode for REPEAT,
# None for plain transport actions
type SourceControlValue = int | bool | RepeatMode | None


@dataclass(kw_only=True)
class PluginEngine:
    """
    A single selectable backend exposed by a plugin provider.

    One plugin can expose several engines (for example one per Home Assistant entity),
    so consumers offer them as options in a config picker rather than treating the
    plugin itself as the unit of choice. The chosen engine is stored in config by its
    ``uid`` and handed back to the owning provider as the provider-scoped ``id``.

    Server-side only: never serialized to clients.
    """

    id: str
    name: str
    provider: PluginProvider

    @property
    def uid(self) -> str:
        """Return the globally unique id for this engine, as stored in config."""
        return f"{self.provider.instance_id}{ENGINE_UID_SEPARATOR}{self.id}"


@dataclass(kw_only=True)
class AIEngine(PluginEngine):
    """An engine that answers AI queries, invoked through ``PluginProvider.ai_query``."""


@dataclass(kw_only=True)
class TTSEngine(PluginEngine):
    """An engine that renders speech, invoked through ``PluginProvider.get_tts_message``."""


class PluginProvider(Provider):
    """
    Base representation of a Plugin for Music Assistant.

    Plugin Provider implementations should inherit from this base model.
    """

    async def get_audio_sources(self) -> list[AudioSource]:
        """
        Return all AudioSources this plugin currently exposes.

        Will only be called if ProviderFeature.AUDIO_SOURCE is declared.

        May change over time (e.g. when a paired hardware device adds/removes
        favorites). Each AudioSource is a regular MediaItem and will be browsable
        under the global "Live Inputs" node and playable via the standard play_media flow.

        :return: A list of AudioSource items. Return an empty list if the plugin
            currently has no sources to expose (e.g. hardware is offline).
        """
        if ProviderFeature.AUDIO_SOURCE in self.supported_features:
            raise NotImplementedError
        return []

    def get_player_audio_sources(self, player_id: str) -> list[AudioSource] | None:
        """
        Return the AudioSources this plugin has bound to the given player.

        Plugins that expose one source per (connected) player override this so
        consumers can scope source listings to a single player: return the
        player's own sources, or an empty list when the player has none on this
        plugin. The default of None means the plugin's sources are not
        player-bound and apply to every player.

        Sync on purpose: called from the player's (sync) state calculation.

        :param player_id: The player to return the bound AudioSources for.
        """
        return None

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """
        Return StreamDetails for a streamable item owned by this plugin.

        Called for a playable item this plugin exposes; ``media_type`` says which kind.
        AudioSource items require ProviderFeature.AUDIO_SOURCE to be declared.

        MUST be side-effect-free. MA calls this from both the streaming path
        and from queue preload (``_load_item``); claiming ownership here would
        let a preload accidentally reserve an exclusive source and block a
        subsequent cross-queue handoff at the actual stream request. Ownership
        is claimed in ``on_source_selected`` (which fires only on the real
        stream request, paired with ``on_source_unselected`` in the finally).

        The returned StreamDetails uses the standard fields:
        ``stream_type`` selects between a custom async generator and a path
        (e.g. NAMED_PIPE); ``audio_format`` describes the source for display and
        ``decoded_audio_format`` the PCM actually delivered, which a plugin that
        decoded the source itself has to set; ``stream_metadata`` carries the initial
        live metadata (and can be updated at runtime via
        ``mass.players.update_source_metadata(player_id, ...)``).

        Silence-during-pause contract:
        the player consuming the stream needs a continuous byte flow or it will
        disconnect after a few seconds. The server keeps the connection alive
        differently depending on ``stream_type``:

        - ``StreamType.CUSTOM`` — the server wraps ``get_audio_stream`` with a
          silence-keepalive so a paused upstream device (no bytes yielded) does
          NOT cause the player to drop out. The plugin can just stop yielding
          while paused; the wrapper inserts silence frames at the declared PCM
          format.
        - ``StreamType.NAMED_PIPE`` — the underlying process MUST keep writing
          silence to the pipe during pause states (shairport-sync and librespot
          in pipe/passthrough mode both do this by default). If the producer
          binary actually stops writing, the consuming ffmpeg will block and
          the player will eventually disconnect.

        :param item_id: The provider-scoped id of the item requested for playback:
            an ``AudioSource.item_id`` or the id of another item this plugin owns.
        :param media_type: The media type of the requested item.
        """
        raise NotImplementedError

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes]:
        """
        Return the (custom) audio stream for an AudioSource.

        Will only be called when the StreamDetails returned by get_stream_details
        has ``stream_type=StreamType.CUSTOM``. The yielded bytes must be in the PCM
        format declared by ``streamdetails.decoded_audio_format``, falling back to
        ``audio_format`` when the plugin delivers its source untouched.

        Pausing is fine: when the upstream device is paused the plugin can stop
        yielding bytes. The server wraps this generator with a silence-keepalive
        that keeps the player connected by inserting silence at the declared PCM
        format during quiet periods. The plugin should release any per-session
        state in a ``try/finally`` — the consumer closes the generator when
        playback ends or another queue takes over.

        :param streamdetails: The StreamDetails previously returned by get_stream_details.
        :param seek_position: Ignored for live AudioSources (no seek through the bytestream).
        """
        raise NotImplementedError
        # unreachable, but the yield keeps this method an async generator
        # so an unimplemented provider fails deterministically without emitting
        # a stray empty chunk to the downstream consumer first.
        yield b""  # type: ignore[unreachable]

    def delivers_normalized_audio(self, streamdetails: StreamDetails) -> bool | None:
        """
        Return whether this plugin normalizes the live audio it delivers, if known.

        :param streamdetails: Stream details of the active AudioSource.
        """
        return None

    def delivers_crossfaded_audio(self, streamdetails: StreamDetails) -> bool | None:
        """
        Return whether this plugin crossfades the live audio it delivers, if known.

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

        Plugins exposing an exclusive AudioSource MUST claim ownership in this
        hook (rather than in ``get_stream_details``). This hook fires only on
        the actual stream request — not on queue preload — so claiming here
        keeps the preload path side-effect-free and lets cross-queue handoffs
        succeed (the streams controller fires this **before**
        ``get_stream_details`` so the plugin can stop the previous player and
        replace its claim before the upcoming stream-details fetch).

        ``stream_session_id`` is a fresh per-request token paired with the
        matching ``on_source_unselected`` call. Plugins should store it (and
        replace any previously stored value) so the unselect callback can be
        rejected as stale when a same-queue reconnect interleaves with the
        prior request's teardown — see ``on_source_unselected`` for details.

        :param source_id: The AudioSource.item_id that was selected.
        :param player_id: The player the audio is served to. For a source playing on
            a player this is the owner itself; only direct-PCM consumers and the
            legacy queue-item path pass a different (protocol or group member) player.
        :param owner_player_id: The player that owns this playback session. Prefer this
            for anything you store: it is the user-facing player and stays valid for
            play_media and cmd_stop, where ``player_id`` can be an ephemeral protocol
            bridge whose id is gone by the time you use it.
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

        Optional hook. Override when the plugin wants to sync the upstream
        device's volume slider with MA (e.g. Spotify Connect updating the
        Spotify app's volume display, Yandex Ynison forwarding the new
        level back to the Yandex device). Fired only on the direct queue
        owner — group volume changes fire once at the group level, not
        per child.

        :param source_id: The AudioSource.item_id currently streaming.
        :param volume: The new volume level (0-100).
        """

    async def get_tts_engines(self) -> list[TTSEngine]:
        """
        Return the TTS engines this plugin exposes.

        Will only be called if ProviderFeature.TTS is declared.

        May change over time (e.g. when the backend adds or removes voices/entities).
        The user picks one of these in the config of a consuming provider.

        :return: A list of TTSEngine items. Return an empty list if the plugin
            currently has no engines to expose (e.g. the backend is offline).
        """
        if ProviderFeature.TTS in self.supported_features:
            raise NotImplementedError
        return []

    async def get_tts_message(
        self,
        message: str,
        language: str | None = None,
        engine_id: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> StreamDetails:
        """
        Convert text to speech audio.

        Will only be called if ProviderFeature.TTS is declared.

        :param message: The text to convert to speech.
        :param language: Optional language code.
        :param engine_id: The provider-scoped id of the engine to use (``TTSEngine.id``,
            not its ``uid``). Omit or pass None to use the plugin's own default engine.
        :param options: Optional integration-specific options (for example a voice
            tuning parameter), passed through to the engine as-is. Ignored by plugins
            that have none.
        :return: StreamDetails for the generated audio. ``path`` must be either a
            fetchable http(s)/rtsp/rtmp URL or the absolute path of an existing local
            file, and must stay resolvable for as long as consumers may play the clip.
        """
        raise NotImplementedError

    async def get_ai_engines(self) -> list[AIEngine]:
        """
        Return the AI engines this plugin exposes.

        Will only be called if ProviderFeature.AI_QUERY is declared.

        May change over time (e.g. when the backend adds or removes entities).
        The user picks one of these in the config of a consuming provider.

        :return: A list of AIEngine items. Return an empty list if the plugin
            currently has no engines to expose (e.g. the backend is offline).
        """
        if ProviderFeature.AI_QUERY in self.supported_features:
            raise NotImplementedError
        return []

    async def ai_query(self, query: str, engine_id: str | None = None) -> str:
        """
        Handle an AI query.

        Will only be called if ProviderFeature.AI_QUERY is declared.

        :param query: The query/prompt to send.
        :param engine_id: The provider-scoped id of the engine to use (``AIEngine.id``,
            not its ``uid``). Omit or pass None to use the plugin's own default engine.
        :return: The AI response as a string.
        """
        raise NotImplementedError

    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 5,
    ) -> SearchResults:
        """
        Perform a search on this plugin.

        Will only be called if ProviderFeature.SEARCH is declared.

        :param search_query: Search query.
        :param media_types: A list of media_types to include.
        :param limit: Number of items to return in the search (per type).
        """
        if ProviderFeature.SEARCH in self.supported_features:
            raise NotImplementedError
        return SearchResults()

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

    async def get_recommendations(self) -> list[RecommendationFolder]:
        """
        Get this plugin's available recommendation rows, without items.

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

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Browse this plugin's contents.

        Will only be called if ProviderFeature.BROWSE is declared.

        :param path: The path to browse, in the form ``<instance_id>://<sub_path>``.
        """
        if ProviderFeature.BROWSE in self.supported_features:
            raise NotImplementedError
        return []

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """
        Return details of a single playlist owned by this plugin.

        :param prov_playlist_id: Provider-scoped playlist id.
        """
        raise NotImplementedError

    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """
        Return a page of tracks for a playlist owned by this plugin.

        :param prov_playlist_id: Provider-scoped playlist id.
        :param page: Zero-based page index for paginated results.
        """
        raise NotImplementedError

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """
        Return details of a single radio station owned by this plugin.

        :param prov_radio_id: Provider-scoped radio id.
        """
        raise NotImplementedError

    async def get_dynamic_radio_tracks(self, prov_radio_id: str) -> list[Track]:
        """
        Return a fresh batch of tracks for a dynamic radio station owned by this plugin.

        Return an empty batch to signal the station's feed is exhausted; the queue then
        plays out its remaining items and ends.

        :param prov_radio_id: Provider-scoped radio id.
        """
        raise NotImplementedError

    async def resolve_image(self, path: str) -> str | bytes:
        """
        Resolve an image from an image path.

        This either returns (a generator to get) raw bytes of the image or
        a string with an http(s) URL or local path that is accessible from the server.
        """
        return path
