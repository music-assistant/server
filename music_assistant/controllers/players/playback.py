"""
Playback Mixin for the Player Controller.

The playback commands of a player: play, pause, stop, seek, next and previous, play
media and enqueue the next item, and the handlers that carry them out on its protocols.

This module provides the PlaybackMixin class which is inherited by
PlayerController to add the playback commands.
"""

from __future__ import annotations

import contextlib
from contextlib import suppress
from typing import TYPE_CHECKING

from music_assistant_models.auth import Scope, UserRole
from music_assistant_models.constants import PLAYER_CONTROL_NONE
from music_assistant_models.enums import (
    MediaType,
    PlaybackState,
    PlayerFeature,
    PlayerType,
    RepeatMode,
    SourceControl,
)
from music_assistant_models.errors import (
    InvalidCommand,
    MusicAssistantError,
    PlayerCommandFailed,
    UnsupportedFeaturedException,
)
from music_assistant_models.player import PlayerOptionValueType  # noqa: TC002

from music_assistant.constants import CONF_PLAY_MEDIA_OVERRIDES_GROUP
from music_assistant.controllers.webserver.helpers.auth_middleware import (
    get_current_user,
    set_current_user,
)
from music_assistant.helpers.api import api_command
from music_assistant.helpers.provider_access import resolve_playback_user
from music_assistant.models.player import Player, PlayerMedia

from .base import _PlayerControllerBase
from .constants import PlayerLockPurpose, SwitchedOffAction
from .grouping import apply_group_change
from .grouping_plan import ChangeKind, GroupChange
from .helpers import handle_player_command

if TYPE_CHECKING:
    from music_assistant_models.player import OutputProtocol

    from music_assistant.models.player_protocol import PlayerProtocol


# How long a release for an explicit play_media waits for the player to confirm it left its group.
# A Sonos speaker not reached by the direct refresh reports it ~7 s later; others can be late too.
RELEASE_FOR_PLAY_MEDIA_TIMEOUT = 10


class PlaybackMixin(_PlayerControllerBase):
    """Mixin class providing the playback commands for PlayerController."""

    # Player commands

    @api_command("players/cmd/stop", required_scope=Scope.PLAYERS_CONTROL)
    @handle_player_command(switched_off=SwitchedOffAction.CANCEL_POWER_ON, to_playback_owner=True)
    async def cmd_stop(self, player_id: str) -> None:
        """
        Send STOP command to given player.

        - player_id: player_id of the player to handle the command.
        """
        async with self._playback_owner_locks(player_id, hold_player=False):
            player = self._get_player_with_redirect(player_id)
            # the player can have joined a group while waiting for the lock
            self.check_player_access(self._resolve_playback_owner(player).player_id)
            # Redirect to queue controller if it is active (skip if already in queue command context)
            if active_queue := self.get_active_queue(player):
                await self.mass.player_queues.stop(active_queue.queue_id)
                return
            # Delegate to internal handler for actual implementation
            await self._handle_cmd_stop(player.player_id)

    @api_command("players/cmd/play", required_scope=Scope.PLAYERS_CONTROL)
    @handle_player_command(switched_off=SwitchedOffAction.POWER_ON, to_playback_owner=True)
    async def cmd_play(self, player_id: str) -> None:
        """
        Send PLAY (unpause) command to given player.

        - player_id: player_id of the player to handle the command.
        """
        async with self._playback_owner_locks(player_id, hold_player=False):
            player = self._get_player_with_redirect(player_id)
            # the player can have joined a group while waiting for the lock
            self.check_player_access(self._resolve_playback_owner(player).player_id)
            if player.playback_state == PlaybackState.PLAYING:
                self.logger.info(
                    "Ignore PLAY request to player %s: player is already playing",
                    player.name,
                )
                return
            # a player that is not paused resumes its queue, and so does one paused on a
            # stream its queue has since ended; any other paused player is unpaused
            source = player.active_source
            if (active_queue := self.mass.player_queues.get(source or player_id)) and (
                player.playback_state != PlaybackState.PAUSED
                or self.mass.player_queues.has_lost_paused_stream(active_queue.queue_id)
            ):
                await self.mass.player_queues.resume(active_queue.queue_id)
                return
            # Delegate to internal handler for actual implementation
            await self._handle_cmd_play(player.player_id)
            if active_queue:
                # the queue follows its player with a delay: it may not have seen the pause
                # yet, and would then not see its player play again either
                self.mass.player_queues.on_player_unpaused(active_queue.queue_id)

    @api_command("players/cmd/pause", required_scope=Scope.PLAYERS_CONTROL)
    @handle_player_command(switched_off=SwitchedOffAction.CANCEL_POWER_ON, to_playback_owner=True)
    async def cmd_pause(self, player_id: str) -> None:
        """
        Send PAUSE command to given player.

        - player_id: player_id of the player to handle the command.
        """
        player = self._get_player_with_redirect(player_id)
        # Redirect to queue controller if it is active (skip if already in queue command context)
        if active_queue := self.get_active_queue(player):
            await self.mass.player_queues.pause(active_queue.queue_id)
            return
        # Delegate to internal handler for actual implementation
        await self._handle_cmd_pause(player.player_id)

    @api_command("players/cmd/play_pause", required_scope=Scope.PLAYERS_CONTROL)
    @handle_player_command(switched_off=SwitchedOffAction.RUN, to_playback_owner=True)
    async def cmd_play_pause(self, player_id: str) -> None:
        """
        Toggle play/pause on given player.

        - player_id: player_id of the player to handle the command.
        """
        player = self._get_player_with_redirect(player_id)
        if player.playback_state == PlaybackState.PLAYING:
            await self.cmd_pause(player.player_id)
        else:
            await self.cmd_play(player.player_id)

    @api_command("players/cmd/resume", required_scope=Scope.PLAYERS_CONTROL)
    @handle_player_command(switched_off=SwitchedOffAction.POWER_ON, to_playback_owner=True)
    async def cmd_resume(
        self, player_id: str, source: str | None = None, media: PlayerMedia | None = None
    ) -> None:
        """
        Send RESUME command to given player.

        Resume (or restart) playback on the player.

        :param player_id: player_id of the player to handle the command.
        :param source: Optional source to resume.
        :param media: Optional media to resume.
        """
        async with self._playback_owner_locks(player_id, hold_player=False):
            player = self._get_player_with_redirect(player_id)
            # the player can have joined a group while waiting for the lock
            self.check_player_access(self._resolve_playback_owner(player).player_id)
            await self._handle_cmd_resume(player.player_id, source, media)

    @api_command("players/cmd/seek", required_scope=Scope.PLAYERS_CONTROL)
    @handle_player_command(to_playback_owner=True)
    async def cmd_seek(self, player_id: str, position: int) -> None:
        """
        Handle SEEK command for given player.

        - player_id: player_id of the player to handle the command.
        - position: position in seconds to seek to in the current playing item.
        """
        player = self._get_player_with_redirect(player_id)
        if await self._forward_to_external_source(player, SourceControl.SEEK, position):
            return
        # Redirect to queue controller if it is active
        if active_queue := self.get_active_queue(player):
            await self.mass.player_queues.seek(active_queue.queue_id, position)
            return
        # handle command on player/source directly
        active_source = next((x for x in player.source_list if x.id == player.active_source), None)
        if active_source and not active_source.can_seek:
            msg = (
                f"The active source ({active_source.name}) on player "
                f"{player.name} does not support seeking"
            )
            raise PlayerCommandFailed(msg)
        if PlayerFeature.SEEK not in player.supported_features:
            msg = f"Player {player.name} does not support seeking"
            raise UnsupportedFeaturedException(msg)
        # handle command on player directly
        await self._get_transport_protocol(player).seek(position)

    @api_command("players/cmd/shuffle", required_scope=Scope.PLAYERS_CONTROL)
    @handle_player_command(to_playback_owner=True)
    async def cmd_shuffle(
        self, player_id: str, shuffle_enabled: bool, source_id: str | None = None
    ) -> None:
        """
        Handle SHUFFLE command for given player.

        Applies to whatever the player is playing: a live external source orders its
        own session, a source the device runs itself orders its own content, and
        Music Assistant's queue orders its own items.

        :param player_id: player_id of the player to handle the command.
        :param shuffle_enabled: Whether to play the current content shuffled.
        :param source_id: Optional source (id) the command is aimed at, as listed in the
            player's source_list. Given one, the command is refused when that source is
            no longer playing, so it can never land on whatever took the player since.
        """
        player = self._get_player_with_redirect(player_id)
        active_source_id = self._resolve_command_target(player, source_id)
        if await self._forward_to_external_source(player, SourceControl.SHUFFLE, shuffle_enabled):
            return
        if active_queue := self.get_active_queue(player):
            await self.mass.player_queues.set_shuffle(active_queue.queue_id, shuffle_enabled)
            return
        if active_source := next((x for x in player.source_list if x.id == active_source_id), None):
            # the source belongs to the player itself (its own Spotify Connect, a device input)
            if not active_source.can_shuffle:
                msg = "This action is (currently) unavailable for this source."
                raise PlayerCommandFailed(msg)
            await self._get_transport_protocol(player).set_shuffle(shuffle_enabled)
            return
        msg = f"There is nothing playing on {player.name} to shuffle."
        raise PlayerCommandFailed(msg)

    @api_command("players/cmd/repeat", required_scope=Scope.PLAYERS_CONTROL)
    @handle_player_command(to_playback_owner=True)
    async def cmd_repeat(
        self, player_id: str, repeat_mode: RepeatMode, source_id: str | None = None
    ) -> None:
        """
        Handle REPEAT command for given player.

        Applies to whatever the player is playing: a live external source repeats
        within its own session, a source the device runs itself repeats its own
        content, and Music Assistant's queue repeats its own items.

        :param player_id: player_id of the player to handle the command.
        :param repeat_mode: The repeat mode to apply.
        :param source_id: Optional source (id) the command is aimed at, as listed in the
            player's source_list. Given one, the command is refused when that source is
            no longer playing, so it can never land on whatever took the player since.
        """
        if repeat_mode == RepeatMode.UNKNOWN:
            # not a mode to set: it is what a source reports when it cannot say
            raise InvalidCommand("Cannot set an unknown repeat mode")
        player = self._get_player_with_redirect(player_id)
        active_source_id = self._resolve_command_target(player, source_id)
        if await self._forward_to_external_source(player, SourceControl.REPEAT, repeat_mode):
            return
        if active_queue := self.get_active_queue(player):
            await self.mass.player_queues.set_repeat(active_queue.queue_id, repeat_mode)
            return
        if active_source := next((x for x in player.source_list if x.id == active_source_id), None):
            # the source belongs to the player itself (its own Spotify Connect, a device input)
            if not active_source.can_repeat:
                msg = "This action is (currently) unavailable for this source."
                raise PlayerCommandFailed(msg)
            await self._get_transport_protocol(player).set_repeat(repeat_mode)
            return
        msg = f"There is nothing playing on {player.name} to repeat."
        raise PlayerCommandFailed(msg)

    @api_command("players/cmd/next", required_scope=Scope.PLAYERS_CONTROL)
    @handle_player_command(to_playback_owner=True)
    async def cmd_next_track(self, player_id: str) -> None:
        """Handle NEXT TRACK command for given player."""
        player = self._get_player_with_redirect(player_id)
        active_source_id = player.active_source or player.player_id
        if await self._forward_to_external_source(player, SourceControl.NEXT):
            return
        # Redirect to queue controller if it is active
        if active_queue := self.get_active_queue(player):
            await self.mass.player_queues.next(active_queue.queue_id)
            return
        if PlayerFeature.NEXT_PREVIOUS in player.supported_features:
            # player has some other source active and native next/previous support
            active_source = next((x for x in player.source_list if x.id == active_source_id), None)
            if active_source and active_source.can_next_previous:
                await self._get_transport_protocol(player).next_track()
                return
            msg = "This action is (currently) unavailable for this source."
            raise PlayerCommandFailed(msg)
        # Player does not support next/previous feature
        msg = f"Player {player.name} does not support skipping to the next track."
        raise UnsupportedFeaturedException(msg)

    @api_command("players/cmd/previous", required_scope=Scope.PLAYERS_CONTROL)
    @handle_player_command(to_playback_owner=True)
    async def cmd_previous_track(self, player_id: str) -> None:
        """Handle PREVIOUS TRACK command for given player."""
        player = self._get_player_with_redirect(player_id)
        active_source_id = player.active_source or player.player_id
        if await self._forward_to_external_source(player, SourceControl.PREVIOUS):
            return
        # Redirect to queue controller if it is active
        if active_queue := self.get_active_queue(player):
            await self.mass.player_queues.previous(active_queue.queue_id)
            return
        if PlayerFeature.NEXT_PREVIOUS in player.supported_features:
            # player has some other source active and native next/previous support
            active_source = next((x for x in player.source_list if x.id == active_source_id), None)
            if active_source and active_source.can_next_previous:
                await self._get_transport_protocol(player).previous_track()
                return
            msg = "This action is (currently) unavailable for this source."
            raise PlayerCommandFailed(msg)
        # Player does not support next/previous feature
        msg = f"Player {player.name} does not support skipping to the previous track."
        raise UnsupportedFeaturedException(msg)

    @handle_player_command(switched_off=SwitchedOffAction.POWER_ON)
    async def play_media(self, player_id: str, media: PlayerMedia) -> None:
        """
        Handle PLAY MEDIA on given player.

        :param player_id: player_id of the player to handle the command.
        :param media: The Media that needs to be played on the player.
        """
        # An explicit play_media on a captured player honors the player's
        # CONF_PLAY_MEDIA_OVERRIDES_GROUP preference (default: True) — the
        # player is released from its group/sync first, then plays the media
        # standalone. With the preference off, behavior falls back to the
        # legacy "redirect to group leader" path below.
        # With the override the command works on the player: the release acts on the
        # group, so the group's lock is taken before this player's own and both are held
        # across the release and the play (see get_group_and_player_lock). Without it the
        # command is redirected to the owner and holds the owner's lock alone. A queue
        # action calling in holds these locks already.
        override = bool(
            self.mass.config.get_player_value(player_id, CONF_PLAY_MEDIA_OVERRIDES_GROUP, True)
        )
        if override:
            async with self.get_group_and_player_lock(player_id):
                target_player = self.get_player(player_id, True)
                if target_player is not None and (
                    target_player.synced_to or target_player.active_group
                ):
                    await self._release_player_for_play_media(target_player)
                await self._handle_play_media(player_id, media)
            return
        async with self._playback_owner_locks(player_id, hold_player=False):
            player = self._get_player_with_redirect(player_id)
            await self._handle_play_media(player.player_id, media)

    @api_command("players/cmd/select_sound_mode", required_scope=Scope.PLAYERS_CONTROL)
    @handle_player_command
    async def select_sound_mode(self, player_id: str, sound_mode: str) -> None:
        """
        Handle SELECT SOUND MODE command on given player.

        - player_id: player_id of the player to handle the command
        - sound_mode: The ID of the sound mode that needs to be activated/selected.
        """
        player = self.get_player(player_id, True)
        assert player is not None  # for type checking

        protocol = player.own_protocol
        if protocol is None or PlayerFeature.SELECT_SOUND_MODE not in protocol.supported_features:
            raise UnsupportedFeaturedException(
                f"Player {player.name} does not support sound mode selection"
            )

        prev_sound_mode = protocol.active_sound_mode
        if sound_mode == prev_sound_mode:
            return

        # basic check if sound mode is valid for player
        if not any(x for x in protocol.sound_mode_list if x.id == sound_mode):
            raise PlayerCommandFailed(
                f"{sound_mode} is an invalid sound_mode for player {player.name}"
            )

        # forward to player
        await protocol.select_sound_mode(sound_mode)

    @api_command("players/cmd/set_option", required_scope=Scope.PLAYERS_CONTROL)
    @handle_player_command
    async def set_option(
        self, player_id: str, option_key: str, option_value: PlayerOptionValueType
    ) -> None:
        """
        Handle SET_OPTION command on given player.

        - player_id: player_id of the player to handle the command
        - option_key: The key of the player option that needs to be activated/selected.
        - option_value: The new value of the player option.
        """
        player = self.get_player(player_id, True)
        assert player is not None  # for type checking

        protocol = player.own_protocol
        if protocol is None or PlayerFeature.OPTIONS not in protocol.supported_features:
            raise UnsupportedFeaturedException(f"Player {player.name} does not support set_option")

        prev_player_option = next((x for x in protocol.options if x.key == option_key), None)
        if not prev_player_option:
            return
        if prev_player_option.value == option_value:
            return

        if prev_player_option.read_only:
            raise UnsupportedFeaturedException(
                f"Player {player.name} option {option_key} is read-only"
            )

        # forward to player
        await protocol.set_option(option_key=option_key, option_value=option_value)

    @handle_player_command(lock=PlayerLockPurpose.PLAYBACK)
    async def enqueue_next_media(self, player_id: str, media: PlayerMedia) -> None:
        """
        Handle enqueuing of a next media item on the player.

        :param player_id: player_id of the player to handle the command.
        :param media: The Media that needs to be enqueued on the player.
        :raises UnsupportedFeaturedException: if the player does not support enqueueing.
        :raises PlayerUnavailableError: if the player is not available.
        """
        # Note: No group redirect needed here as enqueue doesn't use _get_player_with_redirect
        # Delegate to internal handler for actual implementation
        await self._handle_enqueue_next_media(player_id, media)

    @api_command("players/add_currently_playing_to_favorites", required_scope=Scope.LIBRARY_WRITE)
    async def add_currently_playing_to_favorites(self, player_id: str) -> None:
        """
        Add the currently playing item/track on given player to the favorites.

        This tries to resolve the currently playing media to an actual media item
        and add that to the favorites in the library. Will raise an error if the
        player is not currently playing anything or if the currently playing media
        can not be resolved to a media item.
        """
        player = self._get_player_with_redirect(player_id)
        mass_queue = self.get_active_queue(player)
        # a favorite set from a player belongs to the user the playback is for; a service
        # account (such as the Home Assistant integration) acts for that user as well
        session_user = get_current_user()
        if (
            (session_user is None or session_user.role == UserRole.SERVICE)
            and mass_queue
            and (playback_user := await resolve_playback_user(self.mass, mass_queue.queue_id))
        ):
            set_current_user(playback_user)
        # handle mass player queue active
        if mass_queue:
            if not (current_item := mass_queue.current_item) or not current_item.media_item:
                raise PlayerCommandFailed("No current item to add to favorites")
            # if we're playing a radio station, try to resolve the currently playing track
            if current_item.media_item.media_type == MediaType.RADIO:
                if not (
                    (streamdetails := mass_queue.current_item.streamdetails)
                    and (stream_title := streamdetails.stream_title)
                    and " - " in stream_title
                ):
                    # no stream title available, so we can't resolve the track
                    # this can happen if the radio station does not provide metadata
                    # or there's a commercial break
                    # Possible future improvement could be to actually detect the song with a
                    # shazam-like approach.
                    raise PlayerCommandFailed("No current item to add to favorites")
                # send the streamtitle into a global search query
                search_artist, search_title_title = stream_title.split(" - ", 1)
                # strip off any additional comments in the title (such as from Radio Paradise)
                search_title_title = search_title_title.split(" | ")[0].strip()
                if track := await self.mass.music.get_track_by_name(
                    search_title_title, search_artist
                ):
                    # we found a track, so add it to the favorites
                    await self.mass.music.add_item_to_favorites(track)
                    return
                # we could not resolve the track, so raise an error
                raise PlayerCommandFailed("No current item to add to favorites")

            # else: any other media item, just add it to the favorites directly
            await self.mass.music.add_item_to_favorites(current_item.media_item)
            return

        # guard for player with no active source
        if not player.active_source:
            raise PlayerCommandFailed("Player has no active source")
        # handle other source active using the current_media with uri
        if current_media := player.current_media:
            # prefer the uri of the current media item
            if current_media.uri:
                with suppress(MusicAssistantError):
                    await self.mass.music.add_item_to_favorites(current_media.uri)
                    return
            # fallback to search based on artist and title (and album if available)
            if current_media.artist and current_media.title:
                if track := await self.mass.music.get_track_by_name(
                    current_media.title,
                    current_media.artist,
                    current_media.album,
                ):
                    # we found a track, so add it to the favorites
                    await self.mass.music.add_item_to_favorites(track)
                    return
        # if we reach here, we could not resolve the currently playing item
        raise PlayerCommandFailed("No current item to add to favorites")

    async def _release_player_for_play_media(self, player: Player) -> None:
        """
        Release a captured player so a play_media command can target it directly.

        :param player: The captured player to release.
        """
        # Strategy is picked from how the player is currently captured:
        #   synced_to            → unsync this player (cmd_ungroup)
        #   dynamic group member → remove from group via cmd_set_members
        #   static group member  → dissolve the whole group (power off if it
        #                          has a real power control, otherwise stop)
        # In every branch we wait for the relevant state attribute to actually
        # clear before returning. Providers (Sonos in particular) reject a
        # play_media on a player whose synced_to/active_group is still set
        # locally even though the release command has been acknowledged.
        if player.synced_to:
            self.logger.debug(
                "Unsyncing %s from %s to honor explicit play_media target",
                player.name,
                player.synced_to,
            )
            async with self.wait_for_player_update(
                player.player_id,
                attribute_name="synced_to",
                attribute_value=None,
                timeout=RELEASE_FOR_PLAY_MEDIA_TIMEOUT,
            ):
                await self.cmd_ungroup(player.player_id)
            return
        if not player.active_group:
            return
        group = self.get_player(player.active_group)
        if group is None:
            return
        is_dynamic_member = (
            PlayerFeature.SET_MEMBERS in group.supported_features
            and player.player_id not in group.static_group_members
        )
        if is_dynamic_member:
            self.logger.debug(
                "Removing %s from dynamic group %s to honor explicit play_media target",
                player.name,
                group.name,
            )
            async with self.wait_for_player_update(
                player.player_id,
                attribute_name="active_group",
                attribute_value=None,
                timeout=RELEASE_FOR_PLAY_MEDIA_TIMEOUT,
            ):
                await self.cmd_set_members(group.player_id, player_ids_to_remove=[player.player_id])
            return
        # static member: a single member can't be released, so the whole
        # group must dissolve. Prefer cmd_power when an explicit power
        # control is set so the user-visible state stays consistent.
        async with self.wait_for_player_update(
            player.player_id,
            attribute_name="active_group",
            attribute_value=None,
            timeout=RELEASE_FOR_PLAY_MEDIA_TIMEOUT,
        ):
            if group.power_control != PLAYER_CONTROL_NONE and group.powered:
                self.logger.debug(
                    "Powering off %s to honor explicit play_media target on %s",
                    group.name,
                    player.name,
                )
                await self._handle_cmd_power(group.player_id, False)
            else:
                self.logger.debug(
                    "Stopping %s to honor explicit play_media target on %s",
                    group.name,
                    player.name,
                )
                await self._handle_cmd_stop(group.player_id)

    async def _stop_player_or_its_queue(self, player: Player) -> None:
        """
        Stop the player, ending its queue when it is playing one of its own.

        :param player: The player to stop.
        """
        # End the queue itself, exactly as a stop command does: stopping only the device
        # leaves the queue session open, so its preloading keeps pulling audio and a
        # provider streaming a live session (Spotify) stays tethered for another track or
        # two. Restricted to the player's own queue: get_active_queue resolves a protocol
        # player to its parent, and stopping that parent's queue would come straight back
        # here. The permission-free handler, because each caller either acts on behalf of
        # the server or has already checked that the user can address the player.
        if (
            active_queue := self.get_active_queue(player)
        ) and active_queue.queue_id == player.player_id:
            await self.mass.player_queues._handle_stop(player.player_id)
            return
        await self._handle_cmd_stop(player.player_id)

    # Private command handlers (no permission checks)

    async def _handle_cmd_resume(
        self, player_id: str, source: str | None = None, media: PlayerMedia | None = None
    ) -> None:
        """
        Handle resume playback command.

        Skips permission checks and locking (internal use only).
        """
        player = self._get_player_with_redirect(player_id)
        source = source or player.active_source
        media = media or player.current_media
        await self._power_on_for_playback(player)
        # Redirect to queue controller if it is active
        if active_queue := self.mass.player_queues.get(source or player_id):
            await self.mass.player_queues.resume(active_queue.queue_id)
            return
        # try to handle command on player directly
        # TODO: check if player has an active source with native resume support
        active_source = next((x for x in player.source_list if x.id == source), None)
        if (
            player.playback_state in (PlaybackState.IDLE, PlaybackState.PAUSED)
            and active_source
            and active_source.can_play_pause
            and PlayerFeature.PAUSE in player.supported_features
        ):
            # player has some other source active and native resume support
            await self._get_transport_protocol(player).play()
            return
        if active_source and not active_source.passive:
            await self.select_source(player_id, active_source.id)
            return
        if media:
            # try to re-play the current media item
            await self._handle_play_media(player.player_id, media)
            return
        # fallback: just try to resume queue playback
        await self.mass.player_queues.resume(player.player_id)

    async def _handle_play_media(self, player_id: str, media: PlayerMedia) -> None:
        """
        Handle play media command without group redirect.

        Skips permission checks, locking, and all redirect logic (internal use only).

        :param player_id: player_id of the player to handle the command.
        :param media: The Media that needs to be played on the player.
        """
        player = self._get_reachable_player(player_id)
        # media that is not the live source itself takes the player away from it. An
        # announcement is the exception: it interrupts the player and hands it straight
        # back, so releasing the source would tear down a session that is about to
        # resume — and one that cannot be re-selected once its plugin has let go.
        if media.media_type not in (MediaType.AUDIO_SOURCE, MediaType.ANNOUNCEMENT):
            await self._release_audio_source(player_id)
        # set active source if media has a source_id (e.g. plugin source or mass queue source)
        if media.source_id:
            player.set_active_mass_source(media.source_id)

        # Determine output protocol to use:
        # While a session is active (playing/paused), keep using the already active
        # protocol so mid-session commands stay on the same output.
        # On a fresh start always (re)select: a leftover active protocol from a
        # previous session must not overrule user preference, a grouped protocol
        # or native playback (and it may point at a player that is gone by now).
        # A group left on a protocol that none of its members need anymore is
        # regrouped first, as it would otherwise keep that grouped protocol selected.
        target_player: PlayerProtocol | None = None
        output_protocol: OutputProtocol | None = None
        is_fresh_start = player.playback_state not in (
            PlaybackState.PLAYING,
            PlaybackState.PAUSED,
        )
        if (
            not is_fresh_start
            and player.active_output_protocol
            and player.active_output_protocol != "native"
            and (protocol_player := player.attached_protocols.get(player.active_output_protocol))
        ):
            # Use the already-set protocol directly
            output_protocol = player.get_linked_protocol(player.active_output_protocol)
            if output_protocol is not None:
                target_player = protocol_player
        if target_player is None:
            # nothing to regroup without a grouped audio protocol, which the player alone tells
            if is_fresh_start and self._get_grouped_output_protocol(player) is not None:
                await apply_group_change(
                    self,
                    GroupChange(kind=ChangeKind.REGROUP_FOR_START, target_id=player.player_id),
                    player,
                )
            target_player, output_protocol = self._select_best_output_protocol(player)

        own = player.own_protocol
        if target_player is not own:
            # Playing via an audio protocol - update active output protocol
            # output_protocol is guaranteed to be non-None when the target is not the own protocol
            assert output_protocol is not None
            self.logger.debug(
                "Starting playback on %s via protocol %s (target=%s), group_members=%s",
                player.name,
                output_protocol.name,
                target_player.protocol_id,
                target_player.group_members,
            )
            await self._activate_output_protocol(player, output_protocol.output_protocol_id)
        elif player.type != PlayerType.GROUP:
            # Native playback - group players don't have output protocols of their own
            # (they delegate to a sync leader / member which manages its own protocol)
            self.logger.debug(
                "Starting playback on %s via native, group_members=%s",
                player.name,
                player.group_members,
            )
            await self._activate_output_protocol(player, "native")

        async with own.prepare_play_media() if own else contextlib.nullcontext():
            # skip auto-play since we're about to start playback
            await self._power_on_for_playback(player, skip_auto_play=True)
            await target_player.play_media(media)
            if own is not None and target_player is not own:
                # notify the native protocol that protocol playback started
                assert output_protocol is not None
                await own.on_protocol_playback(output_protocol=output_protocol)

    async def _handle_enqueue_next_media(self, player_id: str, media: PlayerMedia) -> None:
        """
        Handle enqueue next media command without group redirect.

        Skips permission checks, locking, and all redirect logic (internal use only).

        :param player_id: player_id of the player to handle the command.
        :param media: The Media that needs to be enqueued on the player.
        """
        player = self._get_reachable_player(player_id)
        if target_player := self._get_control_target(
            player,
            required_feature=PlayerFeature.ENQUEUE,
            require_active=True,
        ):
            self.logger.debug(
                "Redirecting enqueue command to protocol %s",
                target_player.provider.manifest.name,
            )
            await target_player.enqueue_next_media(media)
            return

        own = player.own_protocol
        if own is None or PlayerFeature.ENQUEUE not in own.supported_features:
            raise UnsupportedFeaturedException(f"Player {player.name} does not support enqueueing")
        await own.enqueue_next_media(media)

    async def _handle_cmd_stop(self, player_id: str) -> None:
        """
        Handle stop command without any redirects.

        Skips permission checks, locking, and all redirect logic (internal use only).

        :param player_id: player_id of the player to handle the command.
        """
        player = self._get_reachable_player(player_id)
        protocol_player = player.foreign_protocol
        if player.active_output_protocol and player.active_output_protocol != "native":
            protocol_player = player.attached_protocols.get(player.active_output_protocol)
        if player.playback_state == PlaybackState.IDLE:
            # The player already reports idle but an output protocol is still marked
            # active: the protocol may never have received a stop at all
            # (e.g. the source stream ended on its own before this stop command
            # arrived). Forward an (idempotent) stop and schedule the protocol clear
            # so no stale session lingers on the device and the next playback
            # (re)selects the output protocol.
            if protocol_player is not None:
                await protocol_player.stop()
                if len(protocol_player.group_members) <= 1:
                    self.schedule_active_output_protocol_clear(player)
            return
        player.mark_stop_called()
        # Delegate to the active audio protocol if one is active
        target_player = protocol_player or player.own_protocol
        if target_player is None:
            return
        if (
            protocol_player is not None
            and PlayerFeature.POWER in protocol_player.supported_features
        ):
            # if the audio protocol supports/requires power,
            # we power it off instead of just stopping (which also stops playback)
            # this is rare as most protocols do not support power control (except for cast)
            await protocol_player.power(False)
            return

        # handle command on the protocol directly
        await target_player.stop()
        # Only clear active protocol if the audio protocol has no remaining group members.
        # If there are still protocol group members, keep the protocol active so that
        # when playback resumes it continues on the same protocol.
        if protocol_player is None or len(protocol_player.group_members) <= 1:
            self.schedule_active_output_protocol_clear(player)

    async def _handle_cmd_play(self, player_id: str) -> None:
        """
        Handle play command without group redirect.

        Skips permission checks, locking, and all redirect logic (internal use only).

        :param player_id: player_id of the player to handle the command.
        """
        player = self._get_reachable_player(player_id)
        if player.playback_state == PlaybackState.PLAYING:
            self.logger.info(
                "Ignore PLAY request to player %s: player is already playing", player.name
            )
            return
        # If an AudioSource is the active queue item, proxy play to the plugin
        if active := self._get_active_audio_source(player):
            audio_source, plugin_prov = active
            if audio_source.can_play_pause:
                await plugin_prov.on_source_control(audio_source.item_id, SourceControl.PLAY)
                return
        # handle unpause (=play if player is paused)
        if player.playback_state == PlaybackState.PAUSED:
            active_source = next(
                (x for x in player.source_list if x.id == player.active_source), None
            )
            # raise if active source does not support play/pause
            if active_source and not active_source.can_play_pause:
                msg = (
                    f"The active source ({active_source.name}) on player "
                    f"{player.name} does not support play/pause"
                )
                raise PlayerCommandFailed(msg)
            # Delegate to the active audio protocol if one is active
            if target_player := player.foreign_protocol or self._get_control_target(
                player, PlayerFeature.PAUSE, require_active=True
            ):
                await target_player.play()
                return
            # No active protocol target: if the protocol rendering the audio supports pause and
            # the active (external) source can be paused, unpause it directly instead of
            # restarting the source.
            output_player = player.resolve_output_protocol()
            if (
                output_player is not None
                and active_source
                and active_source.can_play_pause
                and PlayerFeature.PAUSE in output_player.supported_features
            ):
                await output_player.play()
                return

        # player is not paused: try to resume the player
        # Note: We handle resume inline here without calling _handle_cmd_resume
        active_source = next((x for x in player.source_list if x.id == player.active_source), None)
        media = player.current_media
        await self._power_on_for_playback(player)
        if active_source and not active_source.passive:
            await self._handle_select_source(player_id, active_source.id)
            return
        if media:
            # try to re-play the current media item
            await self._handle_play_media(player_id, media)
            return
        # fallback: just send play command - which will fail if nothing can be played
        await self._get_transport_protocol(player).play()

    async def _handle_cmd_pause(self, player_id: str) -> None:
        """
        Handle pause command without any redirects.

        Skips permission checks, locking, and all redirect logic (internal use only).

        :param player_id: player_id of the player to handle the command.
        """
        player = self._get_reachable_player(player_id)
        if player.playback_state == PlaybackState.IDLE:
            return
        # If an AudioSource is the active queue item, proxy pause to the plugin
        if active := self._get_active_audio_source(player):
            audio_source, plugin_prov = active
            if audio_source.can_play_pause:
                await plugin_prov.on_source_control(audio_source.item_id, SourceControl.PAUSE)
                return
        # handle command on player/source directly
        active_source = next((x for x in player.source_list if x.id == player.active_source), None)
        if active_source and not active_source.can_play_pause:
            # raise if active source does not support play/pause
            msg = (
                f"The active source ({active_source.name}) on player "
                f"{player.name} does not support play/pause"
            )
            raise PlayerCommandFailed(msg)
        # Delegate to the active audio protocol if one is active
        if target_player := player.foreign_protocol or self._get_control_target(
            player, PlayerFeature.PAUSE, require_active=True
        ):
            await target_player.pause()
            return
        # No active protocol target: if the protocol rendering the audio supports pause and the
        # active (external) source can be paused, forward the command to it instead of stopping
        # it (mirrors the external-source handling in cmd_seek/cmd_next_track).
        output_player = player.resolve_output_protocol()
        if (
            output_player is not None
            and active_source
            and active_source.can_play_pause
            and PlayerFeature.PAUSE in output_player.supported_features
        ):
            await output_player.pause()
            return
        # player/protocol does not support pause: fall back to stop, which also ends the
        # player's own queue so it releases its audio and the music source right away
        self.logger.debug(
            "Player/protocol %s does not support pause, using STOP instead",
            player.name,
        )
        await self._stop_player_or_its_queue(player)
