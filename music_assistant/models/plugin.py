"""Model/base for a Plugin Provider implementation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import ProviderFeature

from .provider import Provider

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Sequence

    from music_assistant_models.enums import SourceControl
    from music_assistant_models.media_items import (
        AudioSource,
        BrowseFolder,
        ItemMapping,
        MediaItemType,
        Playlist,
        RecommendationFolder,
        Track,
    )
    from music_assistant_models.streamdetails import StreamDetails


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

    async def get_stream_details(self, source_id: str, queue_id: str) -> StreamDetails:
        """
        Return StreamDetails for streaming the given AudioSource.

        Will only be called if ProviderFeature.AUDIO_SOURCE is declared.

        The returned StreamDetails uses the standard fields:
        ``stream_type`` selects between a custom async generator and a path
        (e.g. NAMED_PIPE); ``audio_format`` describes the PCM format the source
        emits; ``stream_metadata`` carries the initial live metadata (and can
        be updated at runtime via ``mass.streams.update_stream_metadata(queue_id, ...)``,
        the same channel ICY radio metadata uses).

        Must raise ResourceBusyError if the AudioSource has ``exclusive=True``
        and is already streaming to a different consumer.

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

        :param source_id: The AudioSource.item_id requested for playback.
        :param queue_id: The queue that owns this playback session. For groups this is
            the group's queue_id; the streams controller fans the stream out to members.
        """
        raise NotImplementedError

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes, None]:
        """
        Return the (custom) audio stream for an AudioSource.

        Will only be called when the StreamDetails returned by get_stream_details
        has ``stream_type=StreamType.CUSTOM``. The yielded bytes must be in
        the PCM format declared by ``streamdetails.audio_format``.

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

    async def on_source_control(
        self,
        source_id: str,
        action: SourceControl,
        value: int | None = None,
    ) -> None:
        """
        Handle a playback control command for an active AudioSource.

        Called by the player controller when the user (or an automation) issues
        a control command and the active queue item is an AudioSource whose
        capability flag for the action is True (e.g. ``can_next_previous`` for
        NEXT/PREVIOUS).

        :param source_id: The AudioSource.item_id the command applies to.
        :param action: The control action to perform.
        :param value: Optional numeric value: seek position in seconds for SEEK,
            volume level 0-100 for VOLUME, ignored for other actions.
        """
        raise NotImplementedError

    async def on_source_selected(self, source_id: str, player_id: str, queue_id: str) -> None:
        """
        React to an AudioSource being selected for playback.

        Optional hook. Fired by the streams controller **before**
        ``get_stream_details`` so the plugin can stop the previous player and
        release any prior exclusive claim (e.g. an existing ``_in_use_by_queue``
        held by an earlier queue) before the new queue's ``get_stream_details``
        runs. Without this ordering the exclusivity check inside
        ``get_stream_details`` would raise ``ResourceBusyError`` for any
        cross-queue handoff and the takeover path would be unreachable.

        :param source_id: The AudioSource.item_id that was selected.
        :param player_id: The player that will receive the stream.
        :param queue_id: The queue that owns this playback session.
        """

    async def on_source_unselected(self, source_id: str, queue_id: str) -> None:
        """
        React to MA tearing down an AudioSource stream from this queue.

        Fired in the ``finally`` block of the queue-item streaming handler — so
        it runs whether the stream ended normally, the player disconnected, the
        queue moved on, or an exception interrupted streaming. Override to
        release any per-queue state set in ``get_stream_details`` (notably the
        exclusive lock used to reject cross-queue claims) so the source becomes
        available to other queues without depending on an external session
        event.

        Implementations should guard with ``if self._in_use_by_queue ==
        queue_id`` (or equivalent) so a stale callback after a handoff doesn't
        clear state that already belongs to a new queue.

        :param source_id: The AudioSource.item_id whose stream ended.
        :param queue_id: The queue whose stream is being torn down.
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

    async def get_tts_message(self, message: str, language: str | None = None) -> StreamDetails:
        """
        Convert text to speech audio.

        Will only be called if ProviderFeature.TTS is declared.

        :param message: The text to convert to speech.
        :param language: Optional language code.
        :return: StreamDetails for the generated audio.
        """
        raise NotImplementedError

    async def ai_query(self, query: str) -> str:
        """
        Handle an AI query.

        Will only be called if ProviderFeature.AI_QUERY is declared.

        :param query: The query/prompt to send.
        :return: The AI response as a string.
        """
        raise NotImplementedError

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

    async def recommendations(self) -> list[RecommendationFolder]:
        """
        Retrieve a list of recommendation folders from this plugin.

        Will only be called if ProviderFeature.RECOMMENDATIONS is declared.
        """
        if ProviderFeature.RECOMMENDATIONS in self.supported_features:
            raise NotImplementedError
        return []

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

    async def resolve_image(self, path: str) -> str | bytes:
        """
        Resolve an image from an image path.

        This either returns (a generator to get) raw bytes of the image or
        a string with an http(s) URL or local path that is accessible from the server.
        """
        return path
