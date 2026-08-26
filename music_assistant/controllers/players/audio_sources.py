"""
Audio Source Mixin for the Player Controller.

Holds the live external AudioSource playing on a player, independently of that
player's queue. A queue is Music Assistant's or it is not a queue: while an
external source plays, the player's queue keeps its own items and goes inactive,
exactly as it does for a line-in or TV input.

This module provides the AudioSourceMixin class which is inherited by
PlayerController to add per-player audio source sessions.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import uuid4

from music_assistant_models.enums import ProviderFeature, RepeatMode

from music_assistant.models.plugin import PluginProvider

if TYPE_CHECKING:
    from music_assistant_models.audio_processing import ActiveSourceAudioDetails
    from music_assistant_models.media_items import AudioSource
    from music_assistant_models.streamdetails import StreamDetails, StreamMetadata

    from music_assistant.mass import MusicAssistant


@dataclass
class AudioSourceSession:
    """
    A live external AudioSource playing on a player.

    Carries what the source is, who owns it, and what it reports about itself,
    so none of it has to be read out of a queue item.

    ``streamdetails`` and ``stream_session_id`` are independent of the session's
    own existence: a paused external source keeps the player while its stream is
    torn down, so both fall back to None without the session ending. The
    ``playback_session_id`` identifies the current selection through pauses and
    stream reconnects, and is refreshed when the source is explicitly reselected.
    """

    player_id: str
    source: AudioSource
    provider_instance_id: str
    # identifies the current selection in its stream URLs
    playback_session_id: str = field(default_factory=lambda: uuid4().hex)
    started_at: float = field(default_factory=time.time)
    streamdetails: StreamDetails | None = None
    active_source_audio: ActiveSourceAudioDetails | None = None
    stream_metadata: StreamMetadata | None = None
    stream_metadata_last_updated: float | None = None
    # an adopted placeholder stays replaceable by a later one, a report does not
    stream_metadata_reported: bool = False
    # the ordering the source reports for its own session; None = it has not said
    shuffle_enabled: bool | None = None
    repeat_mode: RepeatMode | None = None
    # token of the stream request currently holding the source's claim
    stream_session_id: str | None = None

    @property
    def source_id(self) -> str:
        """
        Return the AudioSource.item_id this session plays.

        Provider-scoped rather than unique: plugins reuse ids like "main" or a
        player id across instances. Use ``source_uri`` wherever the identifier
        has to be unique server-wide, such as a player's active source.
        """
        return self.source.item_id

    @property
    def source_uri(self) -> str | None:
        """Return the server-wide unique uri of the AudioSource this session plays."""
        return self.source.uri

    def attach_streamdetails(self, streamdetails: StreamDetails) -> None:
        """
        Record the stream details resolved for this session's source.

        Adopts the metadata they carry unless the source has reported something
        itself: while a source is still selected on a player, what it reported is
        what the session reports, however long ago it said it. Until it says
        anything, the placeholder every plugin sets in ``get_stream_details``
        stands in — for vban_receiver and sendspin_source that is the only
        metadata there is, and a later placeholder replaces an earlier one so
        those two still follow a reconnect that changed what they describe.

        :param streamdetails: The stream details resolved for this source.
        """
        self.streamdetails = streamdetails
        if streamdetails.stream_metadata is not None and not self.stream_metadata_reported:
            self.stream_metadata = streamdetails.stream_metadata
            self.stream_metadata_last_updated = time.time()


class AudioSourceMixin:
    """
    Mixin class providing live audio source sessions for PlayerController.

    Handles:
    - Tracking which external AudioSource is playing on which player
    - Committing a source's move between players once its stream is claimed
    - Resolving that source (and its owning plugin) for command proxying
    - Receiving the live metadata the owning plugin pushes about the source

    This mixin expects to be mixed with a class that provides:
    - mass: MusicAssistant instance
    - logger: logging.Logger instance
    - _source_sessions: dict of live sessions, keyed on player_id
    - trigger_player_update(): method to signal a player state change
    """

    # Type hints for attributes provided by the class this mixin is used with
    if TYPE_CHECKING:
        mass: MusicAssistant
        logger: logging.Logger
        _source_sessions: dict[str, AudioSourceSession]

        def trigger_player_update(self, player_id: str) -> None: ...  # noqa: D102

    def get_audio_source_session(self, player_id: str) -> AudioSourceSession | None:
        """
        Return the live AudioSource session on the given player, if any.

        :param player_id: The player to inspect.
        """
        return self._source_sessions.get(player_id)

    def get_player_audio_source(self, player_id: str) -> tuple[AudioSource, PluginProvider] | None:
        """
        Return the AudioSource playing on the given player and its owning PluginProvider.

        Resolves the given player alone, so a group member playing its group's
        source has to be asked for by the group's id.

        Returns None when no source is playing on the player, or when the owning
        plugin provider is no longer available.

        :param player_id: The player whose source to resolve.
        """
        if (session := self._source_sessions.get(player_id)) is None:
            return None
        provider = self.mass.get_provider(session.provider_instance_id)
        if not isinstance(provider, PluginProvider):
            return None
        # a provider can drop the feature at runtime (reload, config change), which
        # would leave the control hooks raising NotImplementedError
        if ProviderFeature.AUDIO_SOURCE not in provider.supported_features:
            return None
        return session.source, provider

    def claim_audio_source_session(
        self,
        session: AudioSourceSession,
        playback_session_id: str,
        stream_session_id: str,
    ) -> bool:
        """
        Register a stream request as the one serving a live source session.

        The claim is the point of no return for a source moving between players:
        whichever other player still held the source is evicted here, so a
        takeover that never produces a stream request leaves it untouched on the
        player that has it. Returns False when the session is no longer the live
        one on its player, in which case the caller must not serve it.

        :param session: The session the stream request resolved.
        :param playback_session_id: The playback session the request set out to serve.
        :param stream_session_id: Token identifying this stream request.
        """
        if (
            self._source_sessions.get(session.player_id) is not session
            or session.playback_session_id != playback_session_id
        ):
            return False
        # a source plays on one player at a time, so it leaves whichever other player
        # was holding it: two players both reporting it would let a command on the one
        # that lost it drive the one that has it
        for other_id, other in list(self._source_sessions.items()):
            if (
                other_id != session.player_id
                and other.source_id == session.source_id
                and other.provider_instance_id == session.provider_instance_id
            ):
                self.mass.streams.audio_processing.clear_source(other_id, other.playback_session_id)
                del self._source_sessions[other_id]
                self.trigger_player_update(other_id)
        session.stream_session_id = stream_session_id
        return True

    def update_source_metadata(
        self,
        player_id: str,
        source_id: str,
        provider_instance_id: str,
        stream_metadata: StreamMetadata,
    ) -> None:
        """
        Push a live metadata update for the AudioSource playing on a player.

        Used by plugin providers exposing an AudioSource (e.g. AirPlay receiver,
        Spotify Connect) to surface live track-change info without restarting the
        stream. Accepted from the moment the source is selected, so a provider can
        report what it already knows before any stream exists.

        The update is rejected silently unless the source playing on the player is
        owned by ``provider_instance_id`` with ``item_id == source_id``.

        :param player_id: The player whose session should receive the update.
        :param source_id: The AudioSource.item_id emitting this metadata.
        :param provider_instance_id: The provider instance id emitting this metadata.
        :param stream_metadata: The new stream metadata to attach.
        """
        session = self._source_sessions.get(player_id)
        if (
            session is None
            or session.source_id != source_id
            or session.provider_instance_id != provider_instance_id
        ):
            self.logger.debug(
                "Rejected source update for player %s from provider %s source %s "
                "(playing: provider %s source %s)",
                player_id,
                provider_instance_id,
                source_id,
                session.provider_instance_id if session else None,
                session.source_id if session else None,
            )
            return
        session.stream_metadata = stream_metadata
        session.stream_metadata_last_updated = time.time()
        session.stream_metadata_reported = True
        self.trigger_player_update(player_id)

    def refresh_source(self, player_id: str, source: AudioSource) -> None:
        """
        Replace the AudioSource a session is publishing with a rebuilt one.

        A plugin rebuilds its source whenever its capability flags change, and the
        controls the player publishes come from the object the session holds, so it
        has to be handed the new one for the change to reach a client. Rejected
        silently unless it is the same source, from the same provider, as the one
        playing.

        :param player_id: The player whose session should publish the new object.
        :param source: The rebuilt AudioSource.
        """
        session = self._source_sessions.get(player_id)
        if (
            session is None
            or session.source_id != source.item_id
            or session.provider_instance_id != source.provider
        ):
            return
        session.source = source
        self.trigger_player_update(player_id)

    def update_source_options(
        self,
        player_id: str,
        source_id: str,
        provider_instance_id: str,
        *,
        shuffle_enabled: bool | None,
        repeat_mode: RepeatMode | None,
    ) -> None:
        """
        Record the ordering a live source reports for its own session.

        A None value leaves that option as it was, as does ``RepeatMode.UNKNOWN``:
        neither is the source saying anything. Rejected silently unless the source
        playing on the player is owned by ``provider_instance_id`` with
        ``item_id == source_id``.

        :param player_id: The player whose session should receive the update.
        :param source_id: The AudioSource.item_id emitting this update.
        :param provider_instance_id: The provider instance id emitting this update.
        :param shuffle_enabled: The session's shuffle state, or None to leave it.
        :param repeat_mode: The session's repeat mode, or None to leave it.
        """
        session = self._source_sessions.get(player_id)
        if (
            session is None
            or session.source_id != source_id
            or session.provider_instance_id != provider_instance_id
        ):
            self.logger.debug(
                "Rejected source options for player %s from provider %s source %s",
                player_id,
                provider_instance_id,
                source_id,
            )
            return
        changed = False
        if shuffle_enabled is not None and session.shuffle_enabled != shuffle_enabled:
            session.shuffle_enabled = shuffle_enabled
            changed = True
        if repeat_mode not in (None, RepeatMode.UNKNOWN) and session.repeat_mode != repeat_mode:
            session.repeat_mode = repeat_mode
            changed = True
        if changed:
            self.trigger_player_update(player_id)

    def _start_audio_source_session(
        self,
        player_id: str,
        source: AudioSource,
        provider_instance_id: str,
    ) -> AudioSourceSession:
        """
        Record that an AudioSource is now playing on the given player.

        Re-selecting the source already playing keeps its session and re-stamps
        the stream token, so a player that drops and reconnects keeps the metadata
        and stream details it had. The source object itself is always taken from
        this call: a plugin rebuilds it whenever its capability flags change, and
        the session has to report the current ones. Selecting a different source
        replaces the session: a player outputs one source at a time. A source
        playing on another player stays there for now: that player is only
        evicted when a stream request claims the new session, so a start that
        never gets that far leaves the source where it was.

        :param player_id: The player the source plays on.
        :param source: The AudioSource that was selected.
        :param provider_instance_id: Instance id of the plugin exposing it.
        """
        session = self._source_sessions.get(player_id)
        if (
            session is not None
            and session.source_id == source.item_id
            and session.provider_instance_id == provider_instance_id
        ):
            self.mass.streams.audio_processing.clear_source(
                player_id,
                session.playback_session_id,
                preserve_details=True,
            )
            session.source = source
            session.playback_session_id = uuid4().hex
            session.stream_session_id = None
            return session
        if session is not None:
            self.mass.streams.audio_processing.clear_source(player_id, session.playback_session_id)
        session = AudioSourceSession(
            player_id=player_id,
            source=source,
            provider_instance_id=provider_instance_id,
        )
        self._source_sessions[player_id] = session
        return session

    def _end_audio_source_session(self, player_id: str) -> AudioSourceSession | None:
        """
        Drop the AudioSource session on the given player and return it.

        Not tied to a stream: a paused source keeps the player while its stream is
        torn down, so this is only for the player being done with the source.

        :param player_id: The player whose session ended.
        :return: The session that was ended, or None if there was none.
        """
        session = self._source_sessions.get(player_id)
        if session is not None:
            self.mass.streams.audio_processing.clear_source(player_id, session.playback_session_id)
        return self._source_sessions.pop(player_id, None)
