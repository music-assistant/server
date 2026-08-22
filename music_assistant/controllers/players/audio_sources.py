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
    """

    player_id: str
    source: AudioSource
    # instance id of the PluginProvider exposing this source
    provider_instance_id: str
    started_at: float = field(default_factory=time.time)
    # resolved once the stream is requested; absent during the selection window
    streamdetails: StreamDetails | None = None
    # what the source reports it is playing, pushed by the owning plugin
    stream_metadata: StreamMetadata | None = None
    stream_metadata_last_updated: float | None = None
    # token of the stream request currently holding the source's claim
    stream_session_id: str | None = None

    @property
    def source_id(self) -> str:
        """Return the AudioSource.item_id this session plays."""
        return self.source.item_id


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
    - trigger_player_update(): method to signal a player state change
    """

    # Type hints for attributes provided by the class this mixin is used with
    if TYPE_CHECKING:
        mass: MusicAssistant
        logger: logging.Logger

        def trigger_player_update(self, player_id: str) -> None: ...  # noqa: D102

    _source_sessions: dict[str, AudioSourceSession]

    def get_audio_source_session(self, player_id: str) -> AudioSourceSession | None:
        """
        Return the live AudioSource session on the given player, if any.

        :param player_id: The player to inspect.
        """
        return self._source_sessions.get(player_id)

    def get_player_audio_source(self, player_id: str) -> tuple[AudioSource, PluginProvider] | None:
        """
        Return the AudioSource playing on the given player and its owning PluginProvider.

        Named to contrast with ``helpers.player.get_queue_audio_source``, which reads
        the same pair off a queue's current item and is what this replaces.

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
        provider: str,
        stream_metadata: StreamMetadata,
    ) -> None:
        """
        Push a live metadata update for the AudioSource playing on a player.

        Used by plugin providers exposing an AudioSource (e.g. AirPlay receiver,
        Spotify Connect) to surface live track-change info without restarting the
        stream. Accepted before the stream details exist, so a provider can
        replay what it already knows the moment it claims the player.

        The update is rejected silently unless the source playing on the player
        is owned by ``provider`` with ``item_id == source_id``.

        :param player_id: The player whose session should receive the update.
        :param source_id: The AudioSource.item_id emitting this metadata.
        :param provider: The provider instance id emitting this metadata.
        :param stream_metadata: The new stream metadata to attach.
        """
        session = self._source_sessions.get(player_id)
        if (
            session is None
            or session.source_id != source_id
            or session.provider_instance_id != provider
        ):
            self._log_rejected_source_update(player_id, source_id, provider, session)
            return
        session.stream_metadata = stream_metadata
        session.stream_metadata_last_updated = time.time()
        self.trigger_player_update(player_id)

    def _start_audio_source_session(
        self,
        player_id: str,
        source: AudioSource,
        provider_instance_id: str,
    ) -> AudioSourceSession:
        """
        Record that an AudioSource is now playing on the given player.

        Replaces any session already on the player: a player outputs one source
        at a time, and the previous one is released by its own stream teardown.

        :param player_id: The player the source plays on.
        :param source: The AudioSource that was selected.
        :param provider_instance_id: Instance id of the plugin exposing it.
        """
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

        :param player_id: The player whose session ended.
        """
        return self._source_sessions.pop(player_id, None)

    def _log_rejected_source_update(
        self,
        player_id: str,
        source_id: str,
        provider: str,
        session: AudioSourceSession | None,
    ) -> None:
        """
        Log an update that did not match the player's session.

        Debug level so a misbehaving provider firing constantly stays diagnosable
        (the count alone is the signal) without spamming higher log levels for
        the legitimate transition cases.
        """
        self.logger.debug(
            "Rejected source update for player %s from provider %s source %s (playing: %s)",
            player_id,
            provider,
            source_id,
            f"{session.provider_instance_id}/{session.source_id}" if session else "nothing",
        )
