"""Snapcast Player."""

import asyncio
import random
import time
import urllib.parse
from contextlib import suppress
from typing import TYPE_CHECKING, cast

from music_assistant_models.config_entries import ConfigEntry, ConfigValueType
from music_assistant_models.enums import PlaybackState, PlayerFeature
from music_assistant_models.player import DeviceInfo, PlayerMedia
from snapcast.control.client import Snapclient
from snapcast.control.group import Snapgroup
from snapcast.control.stream import Snapstream

from music_assistant.constants import (
    ATTR_ANNOUNCEMENT_IN_PROGRESS,
    CONF_ENTRY_FLOW_MODE_ENFORCED,
    CONF_ENTRY_OUTPUT_CODEC_HIDDEN,
)
from music_assistant.helpers.audio import get_player_filter_params
from music_assistant.helpers.compare import create_safe_string
from music_assistant.helpers.ffmpeg import FFMpeg
from music_assistant.models.player import Player
from music_assistant.providers.snapcast.constants import (
    CONF_ENTRY_SAMPLE_RATES_SNAPCAST,
    DEFAULT_SNAPCAST_FORMAT,
    MASS_ANNOUNCEMENT_POSTFIX,
    MASS_STREAM_PREFIX,
    SnapCastStreamType,
)

if TYPE_CHECKING:
    from music_assistant.providers.snapcast.provider import SnapCastProvider


class SnapCastPlayer(Player):
    """SnapCastPlayer."""

    def __init__(
        self,
        provider: "SnapCastProvider",
        player_id: str,
        snap_client: Snapclient,
        snap_client_id: str,
    ) -> None:
        """Init."""
        self.provider: SnapCastProvider  # type: ignore[misc]
        self.snap_client = snap_client
        self.snap_client_id = snap_client_id
        super().__init__(provider, player_id)
        self._stream_task: asyncio.Task[None] | None = None

    @property
    def synced_to(self) -> str | None:
        """
        Return the id of the player this player is synced to (sync leader).

        If this player is not synced to another player (or is the sync leader itself),
        this should return None.
        If it is part of a (permanent) group, this should also return None.
        """
        snap_group = self._get_snapgroup()
        assert snap_group is not None  # for type checking
        master_id: str = self.provider._get_ma_id(snap_group.clients[0])
        if len(snap_group.clients) < 2 or self.player_id == master_id:
            return None
        return master_id

    def setup(self) -> None:
        """Set up player."""
        self._attr_name = self.snap_client.friendly_name
        self._attr_available = self.snap_client.connected
        self._attr_device_info = DeviceInfo(
            model=self.snap_client._client.get("host").get("os"),
            ip_address=self.snap_client._client.get("host").get("ip"),
            manufacturer=self.snap_client._client.get("host").get("arch"),
        )
        self._attr_supported_features = {
            PlayerFeature.SET_MEMBERS,
            PlayerFeature.VOLUME_SET,
            PlayerFeature.VOLUME_MUTE,
            PlayerFeature.PLAY_ANNOUNCEMENT,
        }
        self._attr_can_group_with = {self.provider.instance_id}

    async def volume_set(self, volume_level: int) -> None:
        """Send VOLUME_SET command to given player."""
        await self.snap_client.set_volume(volume_level)

    async def stop(self) -> None:
        """Send STOP command to given player."""
        # update the state first to avoid race conditions, if an active play_announcement
        # finishes the player.state should be IDLE.
        self._attr_playback_state = PlaybackState.IDLE
        self._attr_current_media = None
        self._set_childs_state()

        self.update_state()

        # we change the active stream only if music was playing
        if not self.extra_data.get(ATTR_ANNOUNCEMENT_IN_PROGRESS):
            snapgroup = self._get_snapgroup()
            assert snapgroup is not None  # for type checking
            await snapgroup.set_stream("default")

        # but we always delete the music stream (whether it was active or not)
        await self._delete_stream(self._get_stream_name(SnapCastStreamType.MUSIC))

        if self._stream_task is not None:
            if not self._stream_task.done():
                self._stream_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._stream_task
            self._stream_task = None

    async def volume_mute(self, muted: bool) -> None:
        """Send MUTE command to given player."""
        # Using optimistic value because the library does not return the response from the api
        await self.snap_client.set_muted(muted)
        self._attr_volume_muted = muted
        self.update_state()

    async def set_members(
        self,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        """Handle SET_MEMBERS command on the player."""
        group = self._get_snapgroup()
        assert group is not None  # for type checking
        # handle client additions
        for player_id in player_ids_to_add or []:
            snapcast_id = self.provider._get_snapclient_id(player_id)
            if snapcast_id not in group.clients:
                await group.add_client(snapcast_id)
                if player_id not in self._attr_group_members:
                    self._attr_group_members.append(player_id)
        # handle client removals
        for player_id in player_ids_to_remove or []:
            snapcast_id = self.provider._get_snapclient_id(player_id)
            if snapcast_id in group.clients:
                await group.remove_client(snapcast_id)
                if player_id in self._attr_group_members:
                    self._attr_group_members.remove(player_id)
                # Set default stream and stop ungrouped players
                removed_snapclient = self.provider._snapserver.client(snapcast_id)
                await removed_snapclient.group.set_stream("default")
                if removed_player := self.mass.players.get(player_id):
                    await removed_player.stop()
        self.update_state()

    async def play_media(self, media: PlayerMedia) -> None:
        """Handle PLAY MEDIA on given player."""
        if self.synced_to:
            msg = "A synced player cannot receive play commands directly"
            raise RuntimeError(msg)

        # stop any existing streamtasks first
        if self._stream_task is not None:
            if not self._stream_task.done():
                self._stream_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._stream_task
            self._stream_task = None

        # get stream or create new one
        stream_name = self._get_stream_name(SnapCastStreamType.MUSIC)
        stream = await self._get_or_create_stream(stream_name, media.source_id or self.player_id)

        # if no announcement is playing we activate the stream now, otherwise it
        # will be activated by play_announcement when the announcement is over.
        if not self.extra_data.get(ATTR_ANNOUNCEMENT_IN_PROGRESS):
            snap_group = self._get_snapgroup()
            assert snap_group is not None  # for type checking
            await snap_group.set_stream(stream.identifier)

        self._attr_current_media = media

        # select audio source
        audio_source = self.mass.streams.get_stream(media, DEFAULT_SNAPCAST_FORMAT)

        async def _streamer() -> None:
            stream_path = self._get_stream_path(stream)
            self.logger.debug("Start streaming to %s", stream_path)
            async with FFMpeg(
                audio_input=audio_source,
                input_format=DEFAULT_SNAPCAST_FORMAT,
                output_format=DEFAULT_SNAPCAST_FORMAT,
                filter_params=get_player_filter_params(
                    self.mass, self.player_id, DEFAULT_SNAPCAST_FORMAT, DEFAULT_SNAPCAST_FORMAT
                ),
                audio_output=stream_path,
                extra_input_args=["-y", "-re"],
            ) as ffmpeg_proc:
                self._attr_playback_state = PlaybackState.PLAYING
                self._attr_current_media = media
                self._attr_elapsed_time = 0
                self._attr_elapsed_time_last_updated = time.time()
                self.update_state()

                self._set_childs_state()
                await ffmpeg_proc.wait()

            self.logger.debug("Finished streaming to %s", stream_path)
            # we need to wait a bit for the stream status to become idle
            # to ensure that all snapclients have consumed the audio
            while stream.status != "idle":
                await asyncio.sleep(0.25)
            self._attr_playback_state = PlaybackState.IDLE
            self._attr_elapsed_time = time.time() - self._attr_elapsed_time_last_updated
            self.update_state()
            self._set_childs_state()

        # start streaming the queue (pcm) audio in a background task
        self._stream_task = self.mass.create_task(_streamer())

    async def play_announcement(
        self, announcement: PlayerMedia, volume_level: int | None = None
    ) -> None:
        """Handle (provider native) playback of an announcement on given player."""
        # get stream or create new one
        stream_name = self._get_stream_name(SnapCastStreamType.ANNOUNCEMENT)
        stream = await self._get_or_create_stream(stream_name, None)

        # always activate the stream (announcements have priority over music)
        snap_group = self._get_snapgroup()
        assert snap_group is not None  # for type checking
        await snap_group.set_stream(stream.identifier)

        # Unfortunately snapcast sets a volume per client (not per stream), so we need a way to
        # set the announcement volume without affecting the music volume.
        # We go for the simplest solution: save the previous volume, change it, restore later
        # (with the downside that the change will be visible in the UI)
        orig_volume_level = self.volume_level  # Note: might be None

        if volume_level is not None:
            await self.volume_set(volume_level)

        input_format = DEFAULT_SNAPCAST_FORMAT
        assert announcement.custom_data is not None  # for type checking
        audio_source = self.mass.streams.get_announcement_stream(
            announcement.custom_data["announcement_url"],
            output_format=DEFAULT_SNAPCAST_FORMAT,
            pre_announce=announcement.custom_data["pre_announce"],
            pre_announce_url=announcement.custom_data["pre_announce_url"],
        )

        # stream the audio, wait for it to finish (play_announcement should return after the
        # announcement is over to avoid simultaneous announcements).
        stream_path = self._get_stream_path(stream)
        self.logger.debug("Start announcement streaming to %s", stream_path)
        async with FFMpeg(
            audio_input=audio_source,
            input_format=input_format,
            output_format=DEFAULT_SNAPCAST_FORMAT,
            filter_params=get_player_filter_params(
                self.mass, self.player_id, input_format, DEFAULT_SNAPCAST_FORMAT
            ),
            audio_output=stream_path,
            extra_input_args=["-y", "-re"],
        ) as ffmpeg_proc:
            await ffmpeg_proc.wait()

        self.logger.debug("Finished announcement streaming to %s", stream_path)
        # we need to wait a bit for the stream status to become idle
        # to ensure that all snapclients have consumed the audio
        while stream.status != "idle":
            await asyncio.sleep(0.25)

        # delete the announcement stream
        await self._delete_stream(stream_name)

        # restore volume, if we changed it above and it's still the same we set
        # (the user did not change it himself while the announcement was playing)
        if self.volume_level == volume_level and orig_volume_level is not None:
            await self.volume_set(orig_volume_level)

        # and restore the group to either the default or the music stream
        if self.playback_state == PlaybackState.IDLE:
            new_stream_name = "default"
        else:
            new_stream_name = self._get_stream_name(SnapCastStreamType.MUSIC)
        group = self._get_snapgroup()
        assert group is not None  # for type checking
        await group.set_stream(new_stream_name)

    async def get_config_entries(
        self,
        action: str | None = None,
        values: dict[str, ConfigValueType] | None = None,
    ) -> list[ConfigEntry]:
        """Player config."""
        base_entries = await super().get_config_entries(action=action, values=values)
        return [
            *base_entries,
            CONF_ENTRY_FLOW_MODE_ENFORCED,
            CONF_ENTRY_SAMPLE_RATES_SNAPCAST,
            CONF_ENTRY_OUTPUT_CODEC_HIDDEN,
        ]

    def _handle_player_update(self, snap_client: Snapclient) -> None:
        """Process Snapcast update to Player controller.

        This is a callback function
        """
        self._attr_name = self.snap_client.friendly_name
        self._attr_volume_level = self.snap_client.volume
        self._attr_volume_muted = self.snap_client.muted
        self._attr_available = self.snap_client.connected

        # Note: when the active stream is a MASS stream the active_source is __not__ updated at all.
        # So it doesn't matter whether a MASS stream is for music or announcements.
        if stream := self._get_active_snapstream():
            if stream.identifier == "default":
                self._attr_active_source = None
            elif not stream.identifier.startswith(MASS_STREAM_PREFIX):
                # unknown source
                self._attr_active_source = stream.identifier
        else:
            self._attr_active_source = None

        self._group_childs()

        self.update_state()

    def _get_stream_name(self, stream_type: SnapCastStreamType) -> str:
        """Return the name of the stream for the given player.

        Each player can have up to two concurrent streams, for music and announcements.

        The stream name depends only on player_id (not queue_id) for two reasones:
        1. Avoid issues when the same queue_id is simultaneously used by two players
           (eg in universal groups).
        2. Easily identify which stream belongs to which player, for instance to be able to
           delete a music stream even when it is not active due to an announcement.
        """
        safe_name = create_safe_string(self.player_id, replace_space=True)
        stream_name = f"{MASS_STREAM_PREFIX}{safe_name}"
        if stream_type == SnapCastStreamType.ANNOUNCEMENT:
            stream_name += MASS_ANNOUNCEMENT_POSTFIX
        return stream_name

    async def _get_or_create_stream(self, stream_name: str, queue_id: str | None) -> Snapstream:
        """Create new stream on snapcast server (or return existing one)."""
        # prefer to reuse existing stream if possible
        if stream := self._get_snapstream(stream_name):
            return stream
        # The control script is used only for music streams in the builtin server
        # (queue_id is None only for announcement streams).
        extra_args = ""
        if (
            self.provider._use_builtin_server
            and queue_id
            and self.provider._controlscript_available
        ):
            # Create socket server for control script communication
            socket_path = await self.provider.get_or_create_socket_server(queue_id)
            extra_args = (
                f"&controlscript={urllib.parse.quote_plus('control.py')}"
                f"&controlscriptparams=--queueid={urllib.parse.quote_plus(queue_id)}%20"
                f"--socket={urllib.parse.quote_plus(socket_path)}%20"
                f"--streamserver-ip={self.mass.streams.publish_ip}%20"
                f"--streamserver-port={self.mass.streams.publish_port}"
            )

        attempts = 50
        while attempts:
            attempts -= 1
            # pick a random port
            port = random.randint(4953, 4953 + 200)
            result = await self.provider._snapserver.stream_add_stream(
                # NOTE: setting the sampleformat to something else
                # (like 24 bits bit depth) does not seem to work at all!
                f"tcp://0.0.0.0:{port}?sampleformat=48000:16:2"
                f"&idle_threshold={self.provider._snapcast_stream_idle_threshold}"
                f"{extra_args}&name={stream_name}"
            )
            if "id" not in result:
                # if the port is already taken, the result will be an error
                self.logger.warning(result)
                continue
            return self.provider._snapserver.stream(result["id"])
        msg = "Unable to create stream - No free port found?"
        raise RuntimeError(msg)

    def _get_snapstream(self, stream_name: str) -> Snapstream | None:
        """Get a stream by name."""
        with suppress(KeyError):
            return self.provider._snapserver.stream(stream_name)
        return None

    def _get_stream_path(self, stream: Snapstream) -> str:
        stream_path = stream.path or f"tcp://{stream._stream['uri']['host']}"
        return stream_path.replace("0.0.0.0", self.provider._snapcast_server_host)

    async def _delete_stream(self, stream_name: str) -> None:
        if stream := self._get_snapstream(stream_name):
            with suppress(TypeError, KeyError, AttributeError):
                await self.provider._snapserver.stream_remove_stream(stream.identifier)

    def _get_snapgroup(self) -> Snapgroup | None:
        """Get snapcast group for given player_id."""
        return cast("Snapgroup | None", self.snap_client.group)

    def _set_childs_state(self) -> None:
        """Set the state of the child`s of the player."""
        for child_player_id in self.group_members:
            if child_player_id == self.player_id:
                continue
            if mass_child_player := self.mass.players.get(child_player_id):
                mass_child_player._attr_playback_state = self.playback_state
                mass_child_player.update_state()

    def _get_active_snapstream(self) -> Snapstream | None:
        """Get active stream for given player_id."""
        if group := self._get_snapgroup():
            return self._get_snapstream(group.stream)
        return None

    def _group_childs(self) -> None:
        """Return player_ids of the players synced to this player."""
        snap_group = self._get_snapgroup()
        assert snap_group is not None  # for type checking
        self._attr_group_members.clear()
        if self.synced_to is not None:
            return
        self._attr_group_members.append(self.player_id)
        for snap_client_id in snap_group.clients:
            if (
                self.provider._get_ma_id(snap_client_id) != self.player_id
                and self.provider._snapserver.client(snap_client_id).connected
            ):
                self._attr_group_members.append(self.provider._get_ma_id(snap_client_id))
        self.update_state()
