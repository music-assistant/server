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

from music_assistant_models.enums import ProviderFeature

from music_assistant.models.plugin import PluginProvider

if TYPE_CHECKING:
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
    torn down, so both fall back to None without the session ending.
    """

    player_id: str
    source: AudioSource
    # instance id of the PluginProvider exposing this source
    provider_instance_id: str
    started_at: float = field(default_factory=time.time)
    # set once a stream is requested; None during the selection window and again
    # after a paused source's stream is torn down
    streamdetails: StreamDetails | None = None
    # what the source reports it is playing
    stream_metadata: StreamMetadata | None = None
    stream_metadata_last_updated: float | None = None
    # token of the stream request currently holding the source's claim
    stream_session_id: str | None = None

    @property
    def source_id(self) -> str:
        """
        Return the AudioSource.item_id this session plays.

        Provider-scoped rather than unique: every shipped plugin names its only
        source "main". Use ``source_uri`` wherever the identifier has to be
        unique server-wide, such as a player's active source.
        """
        return self.source.item_id

    @property
    def source_uri(self) -> str | None:
        """Return the server-wide unique uri of the AudioSource this session plays."""
        return self.source.uri

    def attach_streamdetails(self, streamdetails: StreamDetails) -> None:
        """
        Record the stream details resolved for this session's source.

        Adopts the metadata they carry unless the source has already reported
        something itself, so the placeholder every plugin sets in
        ``get_stream_details`` is what the session reports until then — for
        vban_receiver and sendspin_source it is the only metadata there is.

        :param streamdetails: The stream details resolved for this source.
        """
        self.streamdetails = streamdetails
        if streamdetails.stream_metadata is not None and self.stream_metadata is None:
            self.stream_metadata = streamdetails.stream_metadata
            self.stream_metadata_last_updated = time.time()


class AudioSourceMixin:
    """
    Mixin class providing live audio source sessions for PlayerController.

    Handles:
    - Tracking which external AudioSource is playing on which player
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
        # A session can only have been started by a provider that declared the
        # feature, but a flag flipped off at runtime (provider reload, config
        # change) would leave on_source_control / on_volume_change raising
        # NotImplementedError. Skip cleanly.
        if ProviderFeature.AUDIO_SOURCE not in provider.supported_features:
            return None
        return session.source, provider

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
            # Debug level so a misbehaving provider firing constantly stays
            # diagnosable (the count alone is the signal) without spamming higher
            # log levels for the legitimate transition cases.
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
        self.trigger_player_update(player_id)

    def _start_audio_source_session(
        self,
        player_id: str,
        source: AudioSource,
        provider_instance_id: str,
        stream_session_id: str | None = None,
    ) -> AudioSourceSession:
        """
        Record that an AudioSource is now playing on the given player.

        Re-selecting the source already playing keeps its session and re-stamps
        the stream token, so a player that drops and reconnects keeps the metadata
        and stream details it had. Selecting a different source replaces the
        session: a player outputs one source at a time.

        :param player_id: The player the source plays on.
        :param source: The AudioSource that was selected.
        :param provider_instance_id: Instance id of the plugin exposing it.
        :param stream_session_id: Token of the stream claiming the source, when one
            has been requested; pass it so the matching release is recognised.
        """
        session = self._source_sessions.get(player_id)
        if (
            session is not None
            and session.source_id == source.item_id
            and session.provider_instance_id == provider_instance_id
        ):
            session.stream_session_id = stream_session_id
            return session
        session = AudioSourceSession(
            player_id=player_id,
            source=source,
            provider_instance_id=provider_instance_id,
            stream_session_id=stream_session_id,
        )
        self._source_sessions[player_id] = session
        return session

    def _end_audio_source_session(
        self, player_id: str, stream_session_id: str | None = None
    ) -> AudioSourceSession | None:
        """
        Drop the AudioSource session on the given player and return it.

        Pass the ``stream_session_id`` of the stream being torn down to end only
        the session that stream owns. A reconnect (the player drops and reopens
        the stream before the first request's teardown runs) leaves the previous
        request finishing *after* its replacement has started, and an unguarded
        end would let that late teardown drop the live session — the same hazard
        ``PluginProvider.on_source_unselected`` requires plugins to guard against.
        Omit it to end whatever is playing, for a teardown that is not scoped to
        one stream (an explicit deselect, or the player going away).

        :param player_id: The player whose session ended.
        :param stream_session_id: Only end the session holding this stream token.
        :return: The session that was ended, or None if none matched.
        """
        if stream_session_id is not None:
            session = self._source_sessions.get(player_id)
            if session is None or session.stream_session_id != stream_session_id:
                return None
        return self._source_sessions.pop(player_id, None)
