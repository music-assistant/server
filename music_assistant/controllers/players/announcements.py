"""
Announcements Mixin for the Player Controller.

Handles playback of announcements (such as TTS messages) on a player: preparing the
player, handing it the announcement, waiting for it to finish and restoring whatever
the player was doing before.

This module provides the AnnouncementsMixin class which is inherited by
PlayerController to add announcement capabilities. The audio itself is rendered and
served by the streams controller (see controllers/streams/announcements.py).
"""

from __future__ import annotations

import asyncio
import logging
import time
from math import ceil
from typing import TYPE_CHECKING, cast

from music_assistant_models.auth import Scope
from music_assistant_models.constants import PLAYER_CONTROL_NONE
from music_assistant_models.enums import (
    MediaType,
    PlaybackState,
    PlayerFeature,
    PlayerType,
)
from music_assistant_models.errors import PlayerCommandFailed
from music_assistant_models.player import PlayerMedia

from music_assistant.constants import (
    ANNOUNCE_ALERT_FILE,
    ATTR_ANNOUNCEMENT_IN_PROGRESS,
    CONF_ENTRY_ANNOUNCE_VOLUME,
    CONF_ENTRY_ANNOUNCE_VOLUME_MAX,
    CONF_ENTRY_ANNOUNCE_VOLUME_MIN,
    CONF_ENTRY_ANNOUNCE_VOLUME_STRATEGY,
    CONF_ENTRY_TTS_PRE_ANNOUNCE,
    CONF_PRE_ANNOUNCE_CHIME_URL,
)
from music_assistant.controllers.streams.announcements import MAX_CLIP_SECONDS
from music_assistant.helpers.api import api_command
from music_assistant.helpers.util import TaskManager, validate_announcement_chime_url
from music_assistant.models.player import Player

from .constants import PlayerLockPurpose
from .helpers import AnnounceData, handle_player_command

if TYPE_CHECKING:
    from collections.abc import Iterator

    from music_assistant import MusicAssistant


class AnnouncementsMixin:
    """
    Mixin class providing announcement playback for PlayerController.

    Handles:
    - Resolving the pre-announce chime and announcement volume from configuration
    - Forwarding a group announcement to its individual members
    - Native announcement support (on the player itself or a linked protocol)
    - The fallback implementation for players without native support

    This mixin expects to be mixed with a class that provides:
    - mass: MusicAssistant instance
    - logger: logging.Logger instance
    - get_player(): method to get a player by ID
    - iter_group_members(): method to iterate the members of a group player
    - _get_control_target(): method to resolve the player to send a command to
    - the _handle_cmd_* / cmd_* playback and grouping commands used below
    """

    # Type hints for attributes provided by the class this mixin is used with
    if TYPE_CHECKING:
        mass: MusicAssistant
        logger: logging.Logger
        _players: dict[str, Player]

        def get_player(  # noqa: D102
            self, player_id: str, raise_unavailable: bool = False
        ) -> Player | None: ...

        def iter_group_members(  # noqa: D102
            self,
            group_player: Player,
            only_powered: bool = False,
            only_playing: bool = False,
            active_only: bool = False,
            exclude_self: bool = True,
        ) -> Iterator[Player]: ...

        def _get_control_target(
            self,
            player: Player,
            required_feature: PlayerFeature,
            require_active: bool = False,
        ) -> Player | None: ...

        async def _wait_for_playback_state(
            self,
            player: Player,
            wanted_state: PlaybackState,
            timeout: float,
            minimal_time: float = 0,
        ) -> None: ...

        async def _handle_play_media(self, player_id: str, media: PlayerMedia) -> None: ...

        async def _handle_cmd_stop(self, player_id: str) -> None: ...

        async def _handle_cmd_volume_set(self, player_id: str, volume_level: int) -> None: ...

        async def _handle_cmd_power(
            self, player_id: str, powered: bool, skip_auto_play: bool = False
        ) -> None: ...

        async def _handle_cmd_resume(
            self, player_id: str, source: str | None = None, media: PlayerMedia | None = None
        ) -> None: ...

        async def cmd_play(self, player_id: str) -> None: ...  # noqa: D102

        async def cmd_ungroup(self, player_id: str) -> None: ...  # noqa: D102

        async def cmd_set_members(  # noqa: D102
            self,
            target_player: str,
            player_ids_to_add: list[str] | None = None,
            player_ids_to_remove: list[str] | None = None,
        ) -> None: ...

    # handle_player_command is typed against PlayerController, which this mixin only
    # becomes once mixed in; the attributes it needs are declared in the block above.
    # mypy reports that on the outermost decorator, hence the ignore below.
    @api_command("players/cmd/play_announcement", required_scope=Scope.PLAYERS_CONTROL)  # type: ignore[type-var]
    @handle_player_command(lock=PlayerLockPurpose.PLAYBACK)
    async def play_announcement(
        self,
        player_id: str,
        url: str,
        pre_announce: bool | None = None,
        volume_level: int | None = None,
        pre_announce_url: str | None = None,
    ) -> None:
        """
        Handle playback of an announcement (url) on given player.

        :param player_id: Player ID of the player to handle the command.
        :param url: URL of the announcement to play.
        :param pre_announce: Optional bool if pre-announce should be used.
        :param volume_level: Optional volume level to set for the announcement.
        :param pre_announce_url: Optional custom URL to use for the pre-announce chime.
        """
        player = self.get_player(player_id, True)
        assert player is not None  # for type checking
        if not url.startswith("http"):
            raise PlayerCommandFailed("Only URLs are supported for announcements")
        if (
            pre_announce
            and pre_announce_url
            and not validate_announcement_chime_url(pre_announce_url)
        ):
            raise PlayerCommandFailed("Invalid pre-announce chime URL specified.")
        # determine pre-announce from (group)player config
        if pre_announce is None and "tts" in url:
            conf_pre_announce = self.mass.config.get_raw_player_config_value(
                player_id,
                CONF_ENTRY_TTS_PRE_ANNOUNCE.key,
                CONF_ENTRY_TTS_PRE_ANNOUNCE.default_value,
            )
            pre_announce = cast("bool", conf_pre_announce)
        if pre_announce_url is None:
            if conf_pre_announce_url := self.mass.config.get_raw_player_config_value(
                player_id,
                CONF_PRE_ANNOUNCE_CHIME_URL,
            ):
                # player default custom chime url
                pre_announce_url = cast("str", conf_pre_announce_url)
            else:
                # use global default chime url
                pre_announce_url = ANNOUNCE_ALERT_FILE
        announce_data = AnnounceData(
            announcement_url=url,
            pre_announce=bool(pre_announce),
            pre_announce_url=pre_announce_url,
            # filled in below, once we know which player fetches the stream
            announce_player_id=None,
        )
        # Register right away, so the audio is (nearly always fully) rendered by the time
        # the player is ready for it. The render is shared by everything that consumes
        # this announcement, including all members of a group.
        render = self.mass.streams.announcement_renderer.register(player_id, announce_data)
        try:
            # mark announcement_in_progress on player
            player.extra_data[ATTR_ANNOUNCEMENT_IN_PROGRESS] = True
            # if player type is group with all members supporting announcements,
            # we forward the request to each individual player
            if player.state.type == PlayerType.GROUP and (
                all(
                    PlayerFeature.PLAY_ANNOUNCEMENT in x.state.supported_features
                    for x in self.iter_group_members(player)
                )
            ):
                # forward the request to each individual player
                async with TaskManager(self.mass) as tg:
                    for group_member in player.state.group_members:
                        tg.create_task(
                            self.play_announcement(
                                group_member,
                                url=url,
                                pre_announce=pre_announce,
                                volume_level=volume_level,
                                pre_announce_url=pre_announce_url,
                            )
                        )
                return
            self.logger.info(
                "Playback announcement to player %s (with pre-announce: %s): %s",
                player.state.name,
                pre_announce,
                url,
            )
            # determine if the player has native announcements support
            # or if any linked protocol has announcement support
            native_announce_support = False
            if announce_player := self._get_control_target(
                player,
                required_feature=PlayerFeature.PLAY_ANNOUNCEMENT,
                require_active=False,
            ):
                native_announce_support = True
            else:
                announce_player = player
            # create a PlayerMedia object for the announcement so
            # we can send a regular play-media call downstream
            announce_data["announce_player_id"] = (
                announce_player.player_id if native_announce_support else None
            )
            announcement = PlayerMedia(
                uri=self.mass.streams.get_announcement_url(player_id),
                media_type=MediaType.ANNOUNCEMENT,
                title="Announcement",
                custom_data=dict(announce_data),
            )
            # handle native announce support (player or linked protocol)
            if native_announce_support:
                # hand the url to the player as soon as there is audio to serve from;
                # its exact length is resolved further downstream, while it plays
                if not await render.wait_ready():
                    self.logger.warning(
                        "Announcement to player %s - no audio available for %s",
                        player.state.name,
                        url,
                    )
                announcement_volume = self.get_announcement_volume(player_id, volume_level)
                await announce_player.play_announcement(announcement, announcement_volume)
                return
            # use fallback/default implementation
            await self._play_announcement(player, announcement, volume_level)
        finally:
            player.extra_data[ATTR_ANNOUNCEMENT_IN_PROGRESS] = False
            await self.mass.streams.announcement_renderer.unregister(player_id, render)

    def get_announcement_volume(self, player_id: str, volume_override: int | None) -> int | None:
        """
        Get the (player specific) volume for a announcement.

        :param player_id: The player the announcement is played on.
        :param volume_override: Volume level that overrides the configured strategy.
        """
        volume_strategy = self.mass.config.get_raw_player_config_value(
            player_id,
            CONF_ENTRY_ANNOUNCE_VOLUME_STRATEGY.key,
            CONF_ENTRY_ANNOUNCE_VOLUME_STRATEGY.default_value,
        )
        volume_strategy_volume = self.mass.config.get_raw_player_config_value(
            player_id,
            CONF_ENTRY_ANNOUNCE_VOLUME.key,
            CONF_ENTRY_ANNOUNCE_VOLUME.default_value,
        )
        if volume_strategy == "none":
            return None
        volume_level = volume_override
        if volume_level is None and volume_strategy == "absolute":
            volume_level = int(cast("float", volume_strategy_volume))
        elif volume_level is None and volume_strategy == "relative":
            if (player := self.get_player(player_id)) and player.state.volume_level is not None:
                volume_level = int(
                    player.state.volume_level + cast("float", volume_strategy_volume)
                )
        elif volume_level is None and volume_strategy == "percentual":
            if (player := self.get_player(player_id)) and player.state.volume_level is not None:
                percentual = (player.state.volume_level / 100) * cast(
                    "float", volume_strategy_volume
                )
                volume_level = int(player.state.volume_level + percentual)
        if volume_level is not None:
            announce_volume_min = cast(
                "float",
                self.mass.config.get_raw_player_config_value(
                    player_id,
                    CONF_ENTRY_ANNOUNCE_VOLUME_MIN.key,
                    CONF_ENTRY_ANNOUNCE_VOLUME_MIN.default_value,
                ),
            )
            volume_level = max(int(announce_volume_min), volume_level)
            announce_volume_max = cast(
                "float",
                self.mass.config.get_raw_player_config_value(
                    player_id,
                    CONF_ENTRY_ANNOUNCE_VOLUME_MAX.key,
                    CONF_ENTRY_ANNOUNCE_VOLUME_MAX.default_value,
                ),
            )
            volume_level = min(int(announce_volume_max), volume_level)
        return None if volume_level is None else int(volume_level)

    async def _play_announcement(
        self,
        player: Player,
        announcement: PlayerMedia,
        volume_level: int | None = None,
    ) -> None:
        """
        Handle (default/fallback) implementation of the play announcement feature.

        This default implementation will;
        - stop playback of the current media (if needed)
        - power on the player (if needed)
        - raise the volume a bit
        - play the announcement (from given url)
        - wait for the player to finish playing
        - restore the previous power and volume
        - restore playback (if needed and if possible)

        This default implementation will only be used if the player
        (provider) has no native support for the PLAY_ANNOUNCEMENT feature.
        """
        prev_state = player.state.playback_state
        # A player without power control has no power state to restore, so it counts as
        # powered here - otherwise the restore below would be skipped altogether for it,
        # leaving the player ungrouped from its (sync)group.
        prev_power = (
            player.state.power_control == PLAYER_CONTROL_NONE
            or bool(player.state.powered)
            or prev_state != PlaybackState.IDLE
        )
        prev_synced_to = player.state.synced_to
        prev_group = (
            self.get_player(player.state.active_group) if player.state.active_group else None
        )
        prev_source = player.state.active_source
        prev_media = player.state.current_media
        prev_media_name = prev_media.title or prev_media.uri if prev_media else None
        # An announcement is transient: a player that is still busy with an earlier
        # announcement holds no user content, so there is nothing to restore for it.
        # The raw media attribute is read here (instead of state.current_media, which
        # reports the active queue item) since it tells what the device is playing.
        restore_playback = prev_state == PlaybackState.PLAYING and not (
            player.current_media is not None
            and player.current_media.media_type == MediaType.ANNOUNCEMENT
        )
        # filled while the temporary announcement volume is applied below
        prev_volumes: dict[str, int] = {}
        # everything from here on alters the player state, so the restore in the finally
        # block must run even when the announcement itself fails halfway through
        try:
            await self._prepare_for_announcement(
                player,
                volume_level=volume_level,
                prev_state=prev_state,
                prev_synced_to=prev_synced_to,
                prev_group=prev_group,
                prev_media_name=prev_media_name,
                prev_volumes=prev_volumes,
            )
            # play the announcement
            self.logger.debug(
                "Announcement to player %s - playing the announcement on the player...",
                player.state.name,
            )
            render = (
                self.mass.streams.announcement_renderer.get(
                    cast("AnnounceData", announcement.custom_data)
                )
                if announcement.custom_data
                else None
            )
            if render is not None and not await render.wait_ready():
                # the render has been filling while the player was prepared above; play on
                # regardless when it came up empty, so the restore still runs
                self.logger.warning(
                    "Announcement to player %s - no audio available for %s",
                    player.state.name,
                    announcement.uri,
                )
            await self._handle_play_media(player.player_id, announcement)
            # wait for the player(s) to play
            await self._wait_for_playback_state(player, PlaybackState.PLAYING, 10, minimal_time=0.1)
            playback_started = time.time()
            # wait for the player to stop playing
            duration = float(announcement.duration) if announcement.duration else None
            if duration is None and render is not None:
                # the render knows the exact length of the audio it produced
                duration = await render.wait_finished()
                if duration:
                    announcement.duration = ceil(duration)
            if duration is None:
                # length unknown (e.g. the source stalled): wait for the player to report it
                # finished, bounded by the longest clip an announcement can produce
                await self._wait_for_playback_state(
                    player, PlaybackState.IDLE, timeout=MAX_CLIP_SECONDS + 10
                )
            else:
                # waiting for the length above already consumed part of the announcement
                elapsed = time.time() - playback_started
                await self._wait_for_playback_state(
                    player,
                    PlaybackState.IDLE,
                    timeout=max(duration + 10 - elapsed, 1),
                    minimal_time=max(duration + 2 - elapsed, 0),
                )
        finally:
            await self._restore_after_announcement(
                player,
                prev_power=prev_power,
                prev_volumes=prev_volumes,
                prev_synced_to=prev_synced_to,
                prev_group=prev_group,
                prev_source=prev_source,
                prev_media=prev_media,
                restore_playback=restore_playback,
            )

    async def _prepare_for_announcement(
        self,
        player: Player,
        *,
        volume_level: int | None,
        prev_state: PlaybackState,
        prev_synced_to: str | None,
        prev_group: Player | None,
        prev_media_name: str | None,
        prev_volumes: dict[str, int],
    ) -> None:
        """
        Free up the player for an announcement and apply the temporary announcement volume.

        :param player: The player the announcement will be played on.
        :param volume_level: Optional volume level override for the announcement.
        :param prev_state: The playback state the player had before the announcement.
        :param prev_synced_to: Player ID of the sync leader the player is synced to (if any).
        :param prev_group: The group player the player is a member of (if any).
        :param prev_media_name: Name of the media the player was playing (for logging).
        :param prev_volumes: Mapping that is filled in-place with the previous volume level
            per player id, so the caller can restore the volumes even if this call fails.
        """
        if prev_synced_to:
            # ungroup player if its currently synced
            self.logger.debug(
                "Announcement to player %s - ungrouping player from %s...",
                player.state.name,
                prev_synced_to,
            )
            await self.cmd_ungroup(player.player_id)
        elif prev_group:
            # if the player is part of a group player, we need to ungroup it
            if PlayerFeature.SET_MEMBERS in prev_group.supported_features:
                self.logger.debug(
                    "Announcement to player %s - ungrouping from group player %s...",
                    player.state.name,
                    prev_group.display_name,
                )
                await prev_group.set_members(player_ids_to_remove=[player.player_id])
            else:
                # if the player is part of a group player that does not support ungrouping,
                # we need to power off the groupplayer instead
                self.logger.debug(
                    "Announcement to player %s - turning off group player %s...",
                    player.state.name,
                    prev_group.display_name,
                )
                await self._handle_cmd_power(prev_group.player_id, False)
        elif prev_state in (PlaybackState.PLAYING, PlaybackState.PAUSED):
            # normal/standalone player: stop player if its currently playing
            self.logger.debug(
                "Announcement to player %s - stop existing content (%s)...",
                player.state.name,
                prev_media_name,
            )
            await self._handle_cmd_stop(player.player_id)
            # wait for the player to stop
            await self._wait_for_playback_state(player, PlaybackState.IDLE, 10, 0.4)
        # adjust volume if needed
        # in case of a (sync) group, we need to do this for all child players
        async with TaskManager(self.mass) as tg:
            for volume_player_id in player.state.group_members or (player.player_id,):
                if not (volume_player := self.get_player(volume_player_id)):
                    continue
                # catch any players that have a different source active
                if (
                    volume_player.state.active_source
                    not in (
                        player.state.active_source,
                        volume_player.player_id,
                        None,
                    )
                    and volume_player.state.playback_state == PlaybackState.PLAYING
                ):
                    self.logger.warning(
                        "Detected announcement to playergroup %s while group member %s is playing "
                        "other content, this may lead to unexpected behavior.",
                        player.state.name,
                        volume_player.state.name,
                    )
                    tg.create_task(self._handle_cmd_stop(volume_player.player_id))
                if volume_player.state.volume_control == PLAYER_CONTROL_NONE:
                    continue
                if (prev_volume := volume_player.state.volume_level) is None:
                    continue
                announcement_volume = self.get_announcement_volume(volume_player_id, volume_level)
                # get_announcement_volume already returns None when the volume must be left
                # alone, so any number it does return is the volume to announce at - including
                # 0, which must not be mistaken for 'no volume configured'
                if announcement_volume is None:
                    continue
                if announcement_volume != prev_volume:
                    prev_volumes[volume_player_id] = prev_volume
                    self.logger.debug(
                        "Announcement to player %s - setting temporary volume (%s)...",
                        volume_player.state.name,
                        announcement_volume,
                    )
                    tg.create_task(
                        self._handle_cmd_volume_set(volume_player.player_id, announcement_volume)
                    )

    async def _restore_after_announcement(
        self,
        player: Player,
        *,
        prev_power: bool,
        prev_volumes: dict[str, int],
        prev_synced_to: str | None,
        prev_group: Player | None,
        prev_source: str | None,
        prev_media: PlayerMedia | None,
        restore_playback: bool,
    ) -> None:
        """
        Restore the player state that was captured before an announcement was played.

        This also runs when the announcement failed halfway through, so a failing restore
        step is logged instead of raised: it may never mask the error that caused it.

        :param player: The player the announcement was played on.
        :param prev_power: Whether the player was powered before the announcement.
        :param prev_volumes: The previous volume level per player id.
        :param prev_synced_to: Player ID of the sync leader the player was synced to (if any).
        :param prev_group: The group player the player was a member of (if any).
        :param prev_source: The source that was active before the announcement.
        :param prev_media: The media that was loaded before the announcement.
        :param restore_playback: Whether playback needs to be resumed.
        """
        self.logger.debug(
            "Announcement to player %s - restore previous state...", player.state.name
        )
        # restore volume
        async with TaskManager(self.mass) as tg:
            for volume_player_id, prev_volume in prev_volumes.items():
                tg.create_task(self._handle_cmd_volume_set(volume_player_id, prev_volume))
        await asyncio.sleep(0.2)
        try:
            # either power off the player or resume playing
            if not prev_power:
                # prev_power is always True for a player without power control,
                # so there is an actual power control to switch off here
                self.logger.debug(
                    "Announcement to player %s - turning player off again...", player.state.name
                )
                await self._handle_cmd_power(player.player_id, False)
                return
            if prev_synced_to:
                self.logger.debug(
                    "Announcement to player %s - syncing back to %s...",
                    player.state.name,
                    prev_synced_to,
                )
                await self.cmd_set_members(prev_synced_to, player_ids_to_add=[player.player_id])
            elif prev_group:
                if PlayerFeature.SET_MEMBERS in prev_group.supported_features:
                    self.logger.debug(
                        "Announcement to player %s - grouping back to group player %s...",
                        player.state.name,
                        prev_group.display_name,
                    )
                    await prev_group.set_members(player_ids_to_add=[player.player_id])
                elif restore_playback:
                    # if the player is part of a group player that does not support set_members,
                    # we need to restart the groupplayer
                    self.logger.debug(
                        "Announcement to player %s - restarting playback on group player %s...",
                        player.state.name,
                        prev_group.display_name,
                    )
                    await self.cmd_play(prev_group.player_id)
            elif restore_playback:
                # player was playing something before the announcement - try to resume that here
                await self._handle_cmd_resume(player.player_id, prev_source, prev_media)
        except Exception as err:
            # deliberately broad: set_members is a raw provider call that is not wrapped
            # into a MusicAssistantError, so it can surface anything its client library
            # raises. CancelledError is a BaseException and still propagates.
            self.logger.warning(
                "Announcement to player %s - restoring the previous state failed: %s",
                player.state.name,
                err,
            )
