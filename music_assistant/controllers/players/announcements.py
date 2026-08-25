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
from music_assistant_models.constants import PLAYER_CONTROL_NATIVE, PLAYER_CONTROL_NONE
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
    CONF_ANNOUNCE_TTS_ENGINE,
    CONF_ENTRY_ANNOUNCE_VOLUME,
    CONF_ENTRY_ANNOUNCE_VOLUME_MAX,
    CONF_ENTRY_ANNOUNCE_VOLUME_MIN,
    CONF_ENTRY_ANNOUNCE_VOLUME_STRATEGY,
    CONF_ENTRY_TTS_PRE_ANNOUNCE,
    CONF_PRE_ANNOUNCE_CHIME_URL,
)
from music_assistant.controllers.streams.announcements import MAX_CLIP_SECONDS
from music_assistant.helpers.api import api_command
from music_assistant.helpers.plugin_engines import (
    engine_display_name,
    get_tts_engines,
    resolve_tts_engine,
    select_core_tts_engine,
)
from music_assistant.helpers.tts import (
    query_tts_engine_with_language_fallback,
    resolve_tts_stream_path,
)
from music_assistant.helpers.util import TaskManager, validate_announcement_chime_url
from music_assistant.models.player import Player

from .constants import PlayerLockPurpose
from .helpers import AnnounceData, handle_player_command

if TYPE_CHECKING:
    from collections.abc import Iterator

    from music_assistant import MusicAssistant

# the caller waits for this command and it holds the player's playback lock while it runs,
# so a wedged engine must give up well before the generic (background) engine timeout
ANNOUNCEMENT_TTS_TIMEOUT = 30


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
        domain: str
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

        async def _handle_cmd_volume_set(
            self, player_id: str, volume_level: int, *, record_target: bool = True
        ) -> None: ...

        async def _handle_cmd_volume_mute(
            self, player: Player, mute_control: str, muted: bool
        ) -> None: ...

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
        url: str | None = None,
        pre_announce: bool | None = None,
        volume_level: int | None = None,
        pre_announce_url: str | None = None,
        message: str | None = None,
        tts_engine: str | None = None,
        language: str | None = None,
    ) -> None:
        """
        Handle playback of an announcement on given player.

        Provide either a url to play or a message to speak, not both.

        :param player_id: Player ID of the player to handle the command.
        :param url: URL of the announcement to play.
        :param pre_announce: Optional bool if pre-announce should be used.
        :param volume_level: Optional volume level to set for the announcement.
        :param pre_announce_url: Optional custom URL to use for the pre-announce chime.
        :param message: Text to speak as the announcement, rendered by a TTS engine.
        :param tts_engine: Optional uid of the TTS engine to speak the message,
            defaults to the engine configured on the player controller.
        :param language: Optional language code to speak the message in (e.g. 'nl-NL'),
            omit to let the engine speak in the language it is configured for.
        """
        player = self.get_player(player_id, True)
        assert player is not None  # for type checking
        if not url and not message:
            raise PlayerCommandFailed("Either a url or a message is required.")
        if url and message:
            raise PlayerCommandFailed("Provide either a url or a message, not both.")
        if tts_engine and not message:
            raise PlayerCommandFailed("A tts_engine can only be used to speak a message.")
        if language and not message:
            raise PlayerCommandFailed("A language can only be used to speak a message.")
        if url and not url.startswith("http"):
            raise PlayerCommandFailed("Only URLs are supported for announcements")
        if (
            pre_announce
            and pre_announce_url
            and not validate_announcement_chime_url(pre_announce_url)
        ):
            raise PlayerCommandFailed("Invalid pre-announce chime URL specified.")
        # a spoken message is rendered up front so everything below - including each member of
        # a group - plays the resulting audio instead of speaking the text again
        is_speech = bool(message)
        if message:
            url = await self._render_announcement_message(message, tts_engine, language)
        assert url is not None  # for type checking
        # determine pre-announce from (group)player config
        if pre_announce is None and (is_speech or "tts" in url):
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
            announce_player = self._resolve_announce_player(player)
            native_announce_support = announce_player is not None
            if announce_player is None:
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
                await self._play_native_announcement(
                    player, announce_player, announcement, volume_level
                )
                return
            # use fallback/default implementation
            await self._play_announcement(player, announcement, volume_level)
        finally:
            player.extra_data[ATTR_ANNOUNCEMENT_IN_PROGRESS] = False
            await self.mass.streams.announcement_renderer.unregister(player_id, render)

    @api_command("players/tts_engines", required_scope=Scope.PLAYERS_CONTROL)
    async def get_announcement_tts_engines(self) -> list[dict[str, str]]:
        """Return the TTS engines that can speak an announcement."""
        return [
            {"uid": engine.uid, "name": engine_display_name(engine)}
            for engine in await get_tts_engines(self.mass)
        ]

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

    def _resolve_announce_player(self, player: Player) -> Player | None:
        """
        Return the player (or linked protocol) that plays an announcement natively.

        Returns None when nothing in the chain announces natively, so the caller has to
        fall back to the default implementation.

        :param player: The player the announcement is played on.
        """
        if PlayerFeature.PLAY_ANNOUNCEMENT in player.supported_features:
            # The device's own announcement handler is built for exactly this and
            # overlays the clip on whatever the speaker is playing - including the
            # stream a linked protocol renders into it (e.g. Sonos audioClip while
            # the Sonos plays through its AirPlay child), so it always wins.
            return player
        if announce_player := self._get_control_target(
            player,
            required_feature=PlayerFeature.PLAY_ANNOUNCEMENT,
            require_active=True,
        ):
            # No native handler, so the output that is ACTIVELY rendering announces:
            # the announcement rides the same audio path as the music (mixed into
            # that live stream, in sync with the rest of the group) instead of a
            # second mechanism firing beside the playback.
            return announce_player
        if player.state.playback_state != PlaybackState.PLAYING:
            # An idle player may announce through any linked protocol. A
            # PLAYING player deliberately gets no such fallback: routing to
            # an idle linked protocol (e.g. the AirPlay child of a WiiM
            # playing natively) would seize the device from the active
            # output, with nothing restoring that playback afterwards.
            # The pick is deliberately not published as the active output
            # protocol: the player would then mirror that protocol's playback
            # state, report PLAYING for the length of the clip and so fail
            # the check above on the next announcement.
            return self._get_control_target(
                player,
                required_feature=PlayerFeature.PLAY_ANNOUNCEMENT,
                require_active=False,
            )
        return None

    async def _render_announcement_message(
        self, message: str, tts_engine: str | None, language: str | None
    ) -> str:
        """
        Speak a message through a TTS engine and return the url of the resulting audio.

        :param message: The text to speak.
        :param tts_engine: Optional uid of the engine to use, defaults to the configured one.
        :param language: Optional language to speak the message in, omit to let the
            engine speak in the language it is configured for.
        """
        if tts_engine:
            engine = await resolve_tts_engine(self.mass, tts_engine)
            if engine is None:
                raise PlayerCommandFailed(f"TTS engine '{tts_engine}' is not available.")
        else:
            engine = await select_core_tts_engine(self.mass, self.domain, CONF_ANNOUNCE_TTS_ENGINE)
            if engine is None:
                raise PlayerCommandFailed("No text-to-speech engine is available.")
        stream_details = await query_tts_engine_with_language_fallback(
            engine,
            message,
            language,
            timeout=ANNOUNCEMENT_TTS_TIMEOUT,
            logger=self.logger,
        )
        path, _ = await resolve_tts_stream_path(engine, stream_details)
        if not path.startswith("http"):
            # a group announcement is forwarded to each member through this same command,
            # whose url guard only accepts http - so a clip rendered to disk never gets past it
            raise PlayerCommandFailed(
                f"TTS engine '{engine.uid}' rendered the message to a local file. "
                "Announcements need an engine that serves its audio over http."
            )
        return path

    async def _play_native_announcement(
        self,
        player: Player,
        announce_player: Player,
        announcement: PlayerMedia,
        volume_level: int | None,
    ) -> None:
        """
        Hand an announcement to a player that plays it natively.

        :param player: The player the announcement is played on.
        :param announce_player: The player (or linked protocol) that plays the announcement.
        :param announcement: The announcement to play.
        :param volume_level: Optional volume level override for the announcement.
        """
        # an announcement is always meant to be heard, so a deliberate mute is lifted for
        # its duration. this happens before the announcement volume is resolved below,
        # since a fake mute control parks the player at volume 0 to mute it.
        # in case of a (sync) group, this covers all child players.
        muted_players = [
            muted_player
            for member_id in player.state.group_members or (player.player_id,)
            if (muted_player := self.get_player(member_id)) and muted_player.state.volume_muted
        ]
        # filled while the announcement volume is applied below
        prev_volumes: dict[str, int] = {}
        try:
            async with TaskManager(self.mass) as tg:
                for muted_player in muted_players:
                    tg.create_task(self._set_announcement_mute(muted_player, False))
            announcement_volume = self.get_announcement_volume(player.player_id, volume_level)
            if (
                announcement_volume is not None
                and not announce_player.applies_announcement_volume
                and not self._output_owns_volume(player, announce_player)
            ):
                # The level is resolved on the scale of the control that owns the player's
                # volume, so an output that does not own it cannot apply it: whatever that
                # output sets is either discarded or stacks on top of the control that is
                # already attenuating on the device. Apply it through the control instead
                # and let the provider announce at the level the device now plays at.
                await self._set_announcement_volume(player, announcement_volume, prev_volumes)
                announcement_volume = None
            await announce_player.play_announcement(announcement, announcement_volume)
        finally:
            # the provider only returns once the announcement finished playing
            async with TaskManager(self.mass) as tg:
                for volume_player_id, prev_volume in prev_volumes.items():
                    tg.create_task(self._handle_cmd_volume_set(volume_player_id, prev_volume))
            # restore mute after the volume: a fake mute is simulated with the volume itself,
            # so it only sticks once the level it hides behind is back in place
            async with TaskManager(self.mass) as tg:
                for muted_player in muted_players:
                    tg.create_task(self._set_announcement_mute(muted_player, True))

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
        # filled while the players are unmuted and the temporary volume is applied below
        prev_volumes: dict[str, int] = {}
        prev_muted: set[str] = set()
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
                prev_muted=prev_muted,
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
                prev_muted=prev_muted,
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
        prev_muted: set[str],
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
        :param prev_muted: Set that is filled in-place with the ids of the players that were
            muted, so the caller can restore the mute state even if this call fails.
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
        # unmute and adjust volume if needed
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
                tg.create_task(
                    self._unmute_and_set_announcement_volume(
                        volume_player, volume_level, prev_volumes, prev_muted
                    )
                )

    async def _restore_after_announcement(
        self,
        player: Player,
        *,
        prev_power: bool,
        prev_volumes: dict[str, int],
        prev_muted: set[str],
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
        :param prev_muted: The ids of the players that were muted before the announcement.
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
        # restore mute after the volume: a fake mute is simulated with the volume itself,
        # so it only sticks once the level it hides behind is back in place
        async with TaskManager(self.mass) as tg:
            for muted_player_id in prev_muted:
                if not (muted_player := self.get_player(muted_player_id)):
                    continue
                tg.create_task(self._set_announcement_mute(muted_player, True))
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

    async def _unmute_and_set_announcement_volume(
        self,
        volume_player: Player,
        volume_level: int | None,
        prev_volumes: dict[str, int],
        prev_muted: set[str],
    ) -> None:
        """
        Make a single player ready to be heard: unmute it and set the announcement volume.

        :param volume_player: The player to prepare.
        :param volume_level: Optional volume level override for the announcement.
        :param prev_volumes: Mapping that is filled in-place with the previous volume level
            per player id.
        :param prev_muted: Set that is filled in-place with the ids of the players that
            were muted.
        """
        if volume_player.state.volume_muted:
            # an announcement is always meant to be heard, so a deliberate mute is lifted
            # for its duration. this happens before the volume is read below, since a
            # fake mute control parks the player at volume 0 to mute it.
            prev_muted.add(volume_player.player_id)
            await self._set_announcement_mute(volume_player, False)
        if volume_player.state.volume_control == PLAYER_CONTROL_NONE:
            return
        if (prev_volume := volume_player.state.volume_level) is None:
            return
        announcement_volume = self.get_announcement_volume(volume_player.player_id, volume_level)
        # get_announcement_volume already returns None when the volume must be left
        # alone, so any number it does return is the volume to announce at - including
        # 0, which must not be mistaken for 'no volume configured'
        if announcement_volume is None or announcement_volume == prev_volume:
            return
        prev_volumes[volume_player.player_id] = prev_volume
        self.logger.debug(
            "Announcement to player %s - setting temporary volume (%s)...",
            volume_player.state.name,
            announcement_volume,
        )
        await self._handle_cmd_volume_set(volume_player.player_id, announcement_volume)

    def _output_owns_volume(self, player: Player, announce_player: Player) -> bool:
        """
        Return True if the announcing output can apply the announcement volume itself.

        :param player: The player the announcement is played on.
        :param announce_player: The player (or linked protocol) that plays the announcement.
        """
        volume_control = player.volume_control_for_output(announce_player.player_id)
        if volume_control == PLAYER_CONTROL_NATIVE:
            # A native volume lives on the player itself, so only its own output can
            # apply it: a linked protocol rendering the audio has no way to reach it.
            return announce_player.player_id == player.player_id
        if volume_control == announce_player.player_id:
            # the volume lives on the device the announcing output talks to
            return True
        # a bridge player riding on the announcing output forwards its volume to it
        if control_player := self.get_player(volume_control):
            return control_player.underlying_player_id == announce_player.player_id
        return False

    async def _set_announcement_volume(
        self, player: Player, announcement_volume: int, prev_volumes: dict[str, int]
    ) -> None:
        """
        Apply the announcement volume through the player's own volume control.

        :param player: The player to set the announcement volume on.
        :param announcement_volume: The resolved announcement volume level.
        :param prev_volumes: Mapping that is filled in-place with the previous volume level
            per player id, so the caller can restore the volume even if this call fails.
        """
        if player.state.volume_control == PLAYER_CONTROL_NONE:
            # nothing in the signal path can set a volume at all
            return
        if (prev_volume := player.state.volume_level) is None or prev_volume == announcement_volume:
            return
        prev_volumes[player.player_id] = prev_volume
        self.logger.debug(
            "Announcement to player %s - setting temporary volume (%s)...",
            player.state.name,
            announcement_volume,
        )
        await self._handle_cmd_volume_set(player.player_id, announcement_volume)

    async def _set_announcement_mute(self, player: Player, muted: bool) -> None:
        """
        Mute or unmute a player for the duration of an announcement.

        :param player: The player to mute or unmute.
        :param muted: bool if the player should be muted.
        """
        # the internal handler is used instead of cmd_volume_mute so the player keeps the
        # mute lock it earned as a group member: the public command clears that lock on
        # unmute and the player may well be ungrouped by the time it is muted back.
        mute_control = player.mute_control
        if mute_control == PLAYER_CONTROL_NONE:
            return
        await self._handle_cmd_volume_mute(player, mute_control, muted)
