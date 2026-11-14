"""
Audio Source Mixin for the Player Controller.

Holds the live external AudioSource playing on a player, independently of that
player's queue. A queue is Music Assistant's or it is not a queue: while an
external source plays, the player's queue keeps its own items and goes inactive,
exactly as it does for a line-in or TV input.

This module provides the AudioSourceMixin class which is inherited by
PlayerController to add per-player audio source sessions and the source commands.
"""

from __future__ import annotations

import time
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import uuid4

from music_assistant_models.auth import Scope
from music_assistant_models.constants import PLAYER_CONTROL_NONE
from music_assistant_models.enums import (
    MediaType,
    PlaybackState,
    PlayerFeature,
    ProviderFeature,
    RepeatMode,
    SourceControl,
)
from music_assistant_models.errors import (
    MusicAssistantError,
    PlayerCommandFailed,
    PlayerUnavailableError,
    UnsupportedFeaturedException,
)
from music_assistant_models.media_items import AudioSource

from music_assistant.helpers.api import api_command
from music_assistant.models.player import Player, PlayerMedia
from music_assistant.models.plugin import PluginProvider, SourceControlValue

from .base import _PlayerControllerBase
from .constants import PlayerLockPurpose, SwitchedOffAction
from .helpers import handle_player_command

if TYPE_CHECKING:
    from music_assistant_models.audio_processing import ActiveSourceAudioDetails
    from music_assistant_models.streamdetails import StreamDetails, StreamMetadata


# How long a freshly started source session may wait for its first stream request
# before it is considered never started and released.
AUDIO_SOURCE_CLAIM_TIMEOUT = 30


@dataclass
class AudioSourceSession:
    """
    A live external AudioSource playing on a player.

    Carries what the source is, who owns it, and what it reports about itself,
    so none of it has to be read out of a queue item.

    ``streamdetails`` and ``stream_session_id`` are independent of the session's
    own existence: a paused external source keeps the player while its stream is
    torn down, so neither is cleared by a stream ending. Both are None until the
    first stream request, which is what makes ``stream_session_id`` the record of
    whether this selection was ever streamed. The ``playback_session_id``
    identifies the current selection through pauses and stream reconnects, and is
    refreshed when the source is explicitly reselected.
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

    @property
    def active_source(self) -> str:
        """Return what the player publishes as its active source while this session runs."""
        return self.source_uri or self.player_id

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


class AudioSourceMixin(_PlayerControllerBase):
    """
    Mixin class providing live audio source sessions for PlayerController.

    Handles:
    - Tracking which external AudioSource is playing on which player
    - Committing a source's move between players once its stream is claimed
    - Resolving that source (and its owning plugin) for command proxying
    - Receiving the live metadata the owning plugin pushes about the source
    - Selecting a source on a player, and handing a command on to the source playing on it
    """

    def get_audio_source_session(self, player_id: str) -> AudioSourceSession | None:
        """
        Return the live AudioSource session on the given player, if any.

        :param player_id: The player to inspect.
        """
        return self._source_sessions.get(player_id)

    def is_live_audio_source(self, source: str) -> bool:
        """
        Return whether the given source string names a live AudioSource session.

        MA pulls a live source's audio from its plugin and streams it out itself, so
        such a source is MA-managed exactly like a queue is: it can be distributed to
        a group, and a player playing one has not been taken over by anything.

        Answered across every player, because a group member publishes the source
        of the group it plays with rather than one of its own.

        :param source: The source string to check.
        :return: True if a live session publishes this source, False otherwise.
        """
        return any(session.active_source == source for session in self._source_sessions.values())

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

        Called once the owning plugin has accepted the stream request, which is
        where a source moving between players commits: whichever other player
        still held it is evicted on the selection's first stream request, so a
        takeover that never gets one leaves it untouched on the player that has
        it. Returns False when the session is no longer the live one on its
        player, in which case the caller must not serve it.

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
        # that lost it drive the one that has it. Only the first request for this
        # selection evicts: a reconnect on the player already streaming the source
        # would otherwise take away a player it is being handed to, before that one
        # has had its chance to start.
        if session.stream_session_id is None:
            for other_id, other in list(self._source_sessions.items()):
                if (
                    other_id != session.player_id
                    and other.source_id == session.source_id
                    and other.provider_instance_id == session.provider_instance_id
                ):
                    self.mass.streams.audio_processing.clear_source(
                        other_id, other.playback_session_id
                    )
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

    @api_command("players/cmd/select_source", required_scope=Scope.PLAYERS_CONTROL)
    @handle_player_command(switched_off=SwitchedOffAction.POWER_ON)
    async def select_source(self, player_id: str, source: str | None) -> None:
        """
        Handle SELECT SOURCE command on given player.

        - player_id: player_id of the player to handle the command.
        - source: The ID of the source that needs to be activated/selected.
        """
        if source is None:
            source = player_id  # default to MA queue source
        player = self.get_player(player_id, True)
        assert player is not None  # for type checking
        # taking the player out of its group acts on the group, so the group's lock is
        # taken before the player's own and both are held until the source is selected
        async with self.get_group_and_player_lock(player_id):
            # If player is currently grouped, handle it so the source switch can proceed.
            # This allows external sources (e.g. Spotify Connect, AirPlay) to take over a
            # grouped player.
            if player.active_group and (group_player := self.get_player(player.active_group)):
                if player_id in group_player.static_group_members:
                    # player is a static member of a permanent group - stop the group
                    # and power it off if supported, rather than removing the member
                    await self._handle_cmd_stop(group_player.player_id)
                    if group_player.power_control != PLAYER_CONTROL_NONE:
                        await self._handle_cmd_power(group_player.player_id, False)
                else:
                    await self.cmd_ungroup(player_id)
            elif player.synced_to:
                await self.cmd_ungroup(player_id)
            # Delegate to internal handler for actual implementation
            await self._handle_select_source(player_id, source)

    async def deselect_source(
        self,
        player_id: str,
        stop_playback: bool = True,
        provider_instance_id: str | None = None,
        source_id: str | None = None,
        playback_session_id: str | None = None,
    ) -> None:
        """
        Give up the source a player was playing, and stop it.

        Call this from a plugin when its session ends — the player has nothing to play
        any more, so it goes back to reporting its own queue rather than a source that
        has gone. Pausing is not this: a paused source keeps the player, so that its
        session survives being resumed.

        :param player_id: player_id of the player to give the source up on.
        :param stop_playback: Whether to stop the player as well. Pass False when the
            caller has already stopped it, or is about to.
        :param provider_instance_id: Optional provider instance that owns the source session.
        :param source_id: Optional provider-scoped source id that owns the source session.
        :param playback_session_id: Optional playback session expected to own the player.
        """
        async with self.get_player_lock(player_id, PlayerLockPurpose.PLAYBACK):
            player = self.get_player(player_id, raise_unavailable=False)
            if not player:
                return
            session = self._source_sessions.get(player_id)
            active_provider_instance_id = session.provider_instance_id if session else None
            active_source_id = session.source_id if session else None
            active_playback_session_id = session.playback_session_id if session else None
            if provider_instance_id is not None and (
                active_provider_instance_id != provider_instance_id
                or (source_id is not None and active_source_id != source_id)
                or playback_session_id is None
                or active_playback_session_id != playback_session_id
            ):
                self.logger.debug(
                    "Ignoring source release for provider %s source %s session %s on player %s: "
                    "active source is provider %s source %s session %s",
                    provider_instance_id,
                    source_id,
                    playback_session_id,
                    player_id,
                    active_provider_instance_id,
                    active_source_id,
                    active_playback_session_id,
                )
                return
            try:
                if stop_playback:
                    with suppress(PlayerCommandFailed, PlayerUnavailableError, RuntimeError):
                        await self._handle_cmd_stop(player_id)
            finally:
                if session is not None:
                    current_session = self._source_sessions.get(player_id)
                    if (
                        current_session is session
                        and current_session.playback_session_id == active_playback_session_id
                    ):
                        await self._release_audio_source(player_id)
                    else:
                        self.logger.debug(
                            "Not releasing provider %s source %s session %s on player %s: "
                            "the source changed while playback was stopping",
                            provider_instance_id,
                            source_id,
                            playback_session_id,
                            player_id,
                        )

    async def release_provider_sources(self, provider_instance_id: str) -> None:
        """
        Give up the sources a plugin owns on every player playing one.

        Call this when the plugin goes away: a session outliving its provider leaves
        the player naming a source that can no longer be streamed nor handed back,
        with its own queue held inactive behind it.

        :param provider_instance_id: Instance id of the plugin that is going away.
        """
        sessions = [
            (player_id, session.source_id, session.playback_session_id)
            for player_id, session in self._source_sessions.items()
            if session.provider_instance_id == provider_instance_id
        ]
        for player_id, source_id, playback_session_id in sessions:
            self.logger.debug(
                "Provider %s is unloading, releasing its source on player %s",
                provider_instance_id,
                player_id,
            )
            await self.deselect_source(
                player_id,
                provider_instance_id=provider_instance_id,
                source_id=source_id,
                playback_session_id=playback_session_id,
            )

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

    def _get_active_audio_source(self, player: Player) -> tuple[AudioSource, PluginProvider] | None:
        """
        Return the live AudioSource a player is playing, and its owning PluginProvider.

        A player hearing its group's or sync leader's audio is playing that player's
        source, so the owner is resolved the same way its active queue is. Returns
        None when no source is playing on it, or when the owning plugin is gone.

        :param player: The player whose source to resolve.
        """
        return self.get_player_audio_source(self._audio_source_owner(player).player_id)

    def _audio_source_owner(self, player: Player) -> Player:
        """
        Return the player whose source the given player is hearing.

        Mirrors ``get_active_queue``: a sync child hears its leader and a group member
        hears its group.

        :param player: The player to resolve the owner for.
        """
        if player.synced_to and player.synced_to != player.player_id:
            if sync_leader := self.get_player(player.synced_to):
                return self._audio_source_owner(sync_leader)
        if player.active_group and player.active_group != player.player_id:
            if group_player := self.get_player(player.active_group):
                return self._audio_source_owner(group_player)
        return player

    def _is_ma_managed_source(self, player: Player, source: str | None) -> bool:
        """
        Check if a source is managed by Music Assistant.

        MA-managed sources include:
        - None (=autodetect, no source explicitly set by player)
        - The player's own ID (MA queue)
        - Any active queue ID
        - Any live AudioSource session

        :param player: The player to check.
        :param source: The source ID to check.
        :return: True if the source is MA-managed, False if external.
        """
        if source is None:
            return True

        # Player's own ID means MA queue is active
        if source == player.player_id:
            return True

        # A live AudioSource (e.g. Spotify Connect) is streamed by MA itself, so it is
        # MA that put it on the player rather than something taking the player over
        if self.is_live_audio_source(source):
            return True

        # Check if it's a known queue ID
        return self.mass.player_queues.get(source) is not None

    async def _notify_source_volume_change(self, player: Player, volume_level: int) -> None:
        """
        Tell the source playing on a player that its volume changed.

        Only the player the source is actually playing on notifies, never one that
        merely hears it as a group member — otherwise a group volume change would
        fire the callback once per child, each with a different value.

        :param player: The player whose volume changed.
        :param volume_level: The new volume, 0-100.
        """
        if (session := self.get_audio_source_session(player.player_id)) is None:
            return
        provider = self.mass.get_provider(session.provider_instance_id)
        if not isinstance(provider, PluginProvider):
            return
        await provider.on_volume_change(session.source_id, volume_level)

    def _resolve_command_target(self, player: Player, source_id: str | None) -> str:
        """
        Return the source (id) a command issued to a player applies to.

        :param player: The player the command was issued to.
        :param source_id: The source the caller aimed the command at, if it named one.
        :return: The id of the player's active source, which is the id of Music
            Assistant's own queue when nothing else is playing on it.
        :raises PlayerCommandFailed: When the caller named a source that is no longer
            the one playing.
        """
        active_source_id = player.active_source or player.player_id
        # a queue can be named by a legacy id of its player
        if source_id is not None and self.resolve_player_id(source_id) != active_source_id:
            msg = f"The source this was meant for is no longer playing on {player.name}."
            raise PlayerCommandFailed(msg)
        return active_source_id

    async def _forward_to_external_source(
        self,
        player: Player,
        action: SourceControl,
        value: SourceControlValue = None,
    ) -> bool:
        """
        Hand a control action to the external source playing on a player.

        Covers the external sources Music Assistant provides itself, which own a
        session it can talk to. A source belonging to the player (its line-in, TV
        input, or its own Spotify Connect) has no such session, so this reports that
        it did not take the action and the caller goes on to the player itself.

        The per-action transport flags gate what the source advertises it can do, so
        a client is refused rather than left waiting. Ordering is not gated here: the
        session decides what reordering means for its own content. Most sources do not
        implement it at all, though, so a client should ask the source whether it can
        before offering the control - handing it one that quietly does nothing is
        worse than not offering it.

        :param player: The player the action was issued to.
        :param action: The control action to hand over.
        :param value: The action's argument, where it takes one.
        :return: True when an external source took the action.
        """
        if (active := self._get_active_audio_source(player)) is None:
            return False
        audio_source, provider = active
        supported = {
            SourceControl.PLAY: audio_source.can_play_pause,
            SourceControl.PAUSE: audio_source.can_play_pause,
            SourceControl.SEEK: audio_source.can_seek,
            SourceControl.NEXT: audio_source.can_next_previous,
            SourceControl.PREVIOUS: audio_source.can_next_previous,
        }.get(action, True)
        if not supported:
            msg = (
                f"The active source ({audio_source.name}) on player "
                f"{player.display_name} does not support this action"
            )
            raise PlayerCommandFailed(msg)
        try:
            await provider.on_source_control(audio_source.item_id, action, value)
        except NotImplementedError as err:
            # a source with no control surface at all (vban_receiver) reaches the base
            # implementation; a caller deserves a refusal rather than a server error
            msg = (
                f"The active source ({audio_source.name}) on player "
                f"{player.display_name} can not be controlled"
            )
            raise PlayerCommandFailed(msg) from err
        return True

    async def _release_audio_source(self, player_id: str) -> None:
        """
        Let go of the live source a player was playing, if it had one.

        Tells the owning plugin so an upstream session still pointing at Music
        Assistant is released. A plugin that raises must not stop the player from
        moving on, so failures are logged rather than propagated.

        :param player_id: The player that is done with its source.
        """
        if (session := self._end_audio_source_session(player_id)) is None:
            return
        self.trigger_player_update(player_id)
        provider = self.mass.get_provider(session.provider_instance_id)
        if not isinstance(provider, PluginProvider):
            return
        try:
            await provider.on_source_released(session.source_id, player_id)
        except Exception:
            self.logger.warning(
                "on_source_released raised for provider %s source %s player %s",
                provider.instance_id,
                session.source_id,
                player_id,
                exc_info=True,
            )

    async def _release_unclaimed_audio_source(
        self, player_id: str, session: AudioSourceSession, playback_session_id: str
    ) -> None:
        """
        Release a source whose renderer never requested the stream.

        The play command returned without error, so the late-start release in the
        streams controller never fires: no stream request means no failed stream
        request either. Without this the player would keep publishing a source
        that never started, with its own queue held inactive behind it.

        :param player_id: The player the source was started on.
        :param session: The session that was started for it.
        :param playback_session_id: Playback session active when it was started.
        """
        current = self.get_audio_source_session(player_id)
        if (
            current is not session
            or current.playback_session_id != playback_session_id
            or current.stream_session_id is not None
        ):
            return
        self.logger.info(
            "AudioSource %s was never streamed by player %s, releasing it",
            session.source_id,
            player_id,
        )
        await self.deselect_source(
            player_id,
            provider_instance_id=session.provider_instance_id,
            source_id=session.source_id,
            playback_session_id=playback_session_id,
        )

    async def _resolve_audio_source_uri(
        self, source: str
    ) -> tuple[AudioSource, PluginProvider] | None:
        """
        Resolve a source string to a live AudioSource, if that is what it names.

        :param source: The source string a select names.
        :return: The source and its owning plugin, or None when the string names
            something else (a queue, a player-native source).
        """
        if "://" not in source:
            return None
        try:
            item = await self.mass.music.get_item_by_uri(source)
        except MusicAssistantError as err:
            # not resolvable as media, so it is something else (a queue id, a
            # player-native source) — logged because a provider being unavailable
            # or unauthenticated also lands here
            self.logger.debug("Could not resolve %s as an audio source: %s", source, err)
            return None
        if not isinstance(item, AudioSource):
            return None
        provider = self.mass.get_provider(item.provider)
        if not isinstance(provider, PluginProvider):
            return None
        if ProviderFeature.AUDIO_SOURCE not in provider.supported_features:
            return None
        return item, provider

    async def _start_audio_source(
        self, player: Player, audio_source: AudioSource, provider: PluginProvider
    ) -> None:
        """
        Start a live external source on a player.

        The player's queue is left exactly as it is: it simply stops being the
        active source, so it is still there to resume when the source ends.

        :param player: The player to play the source on.
        :param audio_source: The source that was selected.
        :param provider: The plugin exposing that source.
        """
        # a player outputs one source at a time, so another one already on it has to be
        # handed back first: replacing the session silently would leave its plugin
        # holding an upstream session that still points at this player
        if (current := self.get_audio_source_session(player.player_id)) is not None and (
            current.source_id != audio_source.item_id
            or current.provider_instance_id != provider.instance_id
        ):
            await self._release_audio_source(player.player_id)
        session = self._start_audio_source_session(
            player.player_id, audio_source, provider.instance_id
        )
        try:
            await self._handle_play_media(
                player.player_id,
                PlayerMedia(
                    uri=audio_source.uri or audio_source.item_id,
                    media_type=MediaType.AUDIO_SOURCE,
                    title=audio_source.name,
                    # the session's owner, which its stream url is keyed on
                    source_id=player.player_id,
                    queue_session_id=session.playback_session_id,
                ),
            )
        except Exception:
            # the source never started, so the player must not go on publishing it:
            # a session left behind holds the queue inactive with nothing playing it
            if self.get_audio_source_session(player.player_id) is session:
                await self._release_audio_source(player.player_id)
            raise
        # the play command returning does not mean the renderer ever fetched the
        # stream url: until a stream request claims the session nothing will evict
        # the player the source may be moving from, and nothing else would ever
        # clear a session that is never streamed
        self.mass.call_later(
            AUDIO_SOURCE_CLAIM_TIMEOUT,
            self._release_unclaimed_audio_source,
            player.player_id,
            session,
            session.playback_session_id,
            task_id=f"release_unclaimed_audio_source_{player.player_id}",
        )

    async def _handle_select_source(self, player_id: str, source: str | None) -> None:
        """
        Handle select source command without group redirect.

        Skips permission checks, locking, and all redirect logic (internal use only).

        :param player_id: player_id of the player to handle the command.
        :param source: The ID of the source that needs to be activated/selected.
        """
        # default to MA queue source, which can also be named by a legacy id of its player
        source = player_id if source is None else self.resolve_player_id(source)
        player = self._get_reachable_player(player_id)
        # check if player is already playing and source is different
        # in that case we need to stop the player first
        prev_source = player.active_source
        if prev_source and source != prev_source:
            with suppress(PlayerCommandFailed, RuntimeError):
                # just try to stop (regardless of state) and let it settle, so the
                # new source does not race the teardown. A player that already
                # reports idle has nothing to tear down and does not wait at all.
                async with self.wait_for_player_update(
                    player_id,
                    attribute_name="playback_state",
                    attribute_value=PlaybackState.IDLE,
                    timeout=5,
                ):
                    await self._handle_cmd_stop(player_id)
        # an audio source uri selects the live source itself, which plays on the
        # player while its queue keeps its own items and goes inactive
        if (resolved := await self._resolve_audio_source_uri(source)) is not None:
            await self._start_audio_source(player, *resolved)
            return
        # anything else takes the player away from a live source it was playing
        await self._release_audio_source(player_id)
        # check if source is a mass queue
        # this can be used to restore the queue after a source switch
        if self.mass.player_queues.get(source):
            player.set_active_mass_source(source)
            return
        # Legacy compatibility: the old plugin-source API used the
        # plugin's instance_id directly as the source string. The refactor
        # moved plugin sources to first-class AudioSource MediaItems played
        # via player_queues.play_media. Translate a legacy plugin-instance-id
        # source into the new flow so old frontends, third-party scripts,
        # and HA automations keep working — but only when the provider
        # exposes EXACTLY ONE AudioSource (it was always a 1:1 mapping under
        # the old API; multi-source providers have to use the explicit URI).
        if (legacy_prov := self.mass.get_provider(source)) and isinstance(
            legacy_prov, PluginProvider
        ):
            if ProviderFeature.AUDIO_SOURCE not in legacy_prov.supported_features:
                raise PlayerCommandFailed(f"Provider {source} does not expose AudioSources")
            sources = await legacy_prov.get_audio_sources()
            if len(sources) == 1:
                self.logger.debug(
                    "Translating legacy select_source(%s) to play_media(%s)",
                    source,
                    sources[0].uri,
                )
                await self.mass.player_queues.play_media(player_id, str(sources[0].uri))
                return
            raise UnsupportedFeaturedException(
                f"Provider {source} exposes {len(sources)} AudioSources; the legacy "
                "select_source(plugin_instance_id) API only supported 1:1 mappings. "
                "Use player_queues.play_media with an explicit AudioSource URI."
            )
        # basic check if player supports source selection
        if PlayerFeature.SELECT_SOURCE not in player.supported_features:
            raise UnsupportedFeaturedException(
                f"Player {player.name} does not support source selection"
            )
        # basic check if source is valid for player
        if not any(x for x in player.source_list if x.id == source):
            raise PlayerCommandFailed(f"{source} is an invalid source for player {player.name}")
        # the selected source plays, not the queue that auto play would resume
        await self._power_on_for_playback(player, skip_auto_play=True)
        # forward to player
        if (own := player.own_protocol) is None:
            msg = f"Player {player.name} has no protocol that can select a source"
            raise PlayerUnavailableError(msg)
        await own.select_source(source)
