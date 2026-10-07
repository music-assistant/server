"""Timeline building and broadcasting for Plex remote control."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from aiohttp import ClientTimeout
from music_assistant_models.enums import PlaybackState, RepeatMode

from .parsing import plex_key_for_item

if TYPE_CHECKING:
    from music_assistant.providers.plex import PlexProvider

LOGGER = logging.getLogger(__name__)


class TimelineMixin:
    """Mixin providing timeline building and broadcasting."""

    if TYPE_CHECKING:
        provider: PlexProvider
        client_id: str
        subscriptions: dict[str, dict[str, object]]
        play_queue_id: str | None
        play_queue_version: int
        play_queue_item_ids: dict[int, int]
        _ma_player_id: str | None
        headers: dict[str, str]
        plex_server: Any

    def _resolve_plex_state(self, player: Any, queue: Any) -> str:
        """
        Resolve the Plex playback state string from MA player/queue state.

        The queue is the source of truth for playback: for synced or grouped
        players (and players using a protocol output) the child player's own
        ``playback_state`` stays idle while the active queue (on the sync
        leader / group / protocol parent) is actually playing. Relying on
        ``player.playback_state`` therefore reports "paused" for those players
        even though MA is playing.

        :param player: The MA player (may be a sync child / group member).
        :param queue: The active MA queue for the player (if any).
        :return: One of "playing", "paused" or "stopped".
        """
        if queue is not None:
            state = queue.state
        elif player is not None:
            # player.state.playback_state is MA's resolved/final state, which already
            # follows the active output protocol player and sync leader.
            state = player.state.playback_state
        else:
            return "stopped"

        if state == PlaybackState.PLAYING:
            return "playing"
        if state == PlaybackState.PAUSED:
            return "paused"
        if state == PlaybackState.IDLE:
            has_track = bool(queue and queue.current_item and queue.current_item.media_item)
            return "paused" if has_track else "stopped"
        return "stopped"

    def _build_timeline_attributes(
        self,
        track: Any,
        state: str,
        duration: int,
        time_ms: int,
        volume: int,
        shuffle: int,
        repeat: int,
        controllable: str,
        queue: Any | None,
    ) -> list[str]:
        """
        Build timeline attributes for a playing track.

        :param track: The current track media item.
        :param state: Playback state (playing, paused, etc.).
        :param duration: Track duration in milliseconds.
        :param time_ms: Current playback time in milliseconds.
        :param volume: Volume level (0-100).
        :param shuffle: Shuffle state (0 or 1).
        :param repeat: Repeat mode (0=off, 1=one, 2=all).
        :param controllable: Controllable features string.
        :param queue: The MA queue object.
        :return: List of timeline attribute strings.
        """
        key = plex_key_for_item(track, self.provider.instance_id)
        if not key:
            return []
        rating_key = key.split("/")[-1]

        plex_url = urlparse(self.provider._baseurl)
        machine_identifier = self.provider._plex_server.machineIdentifier
        address = plex_url.hostname
        port = plex_url.port or (443 if plex_url.scheme == "https" else 32400)
        protocol = plex_url.scheme

        attrs = [
            f'state="{state}"',
            f'duration="{duration}"',
            f'time="{time_ms}"',
            f'ratingKey="{rating_key}"',
            f'key="{key}"',
        ]

        if self.play_queue_id and queue:
            if queue.current_index is not None:
                play_queue_item_id = self.play_queue_item_ids.get(
                    queue.current_index, queue.current_index + 1
                )
                attrs.append(f'playQueueItemID="{play_queue_item_id}"')
            attrs.append(f'playQueueID="{self.play_queue_id}"')
            attrs.append(f'playQueueVersion="{self.play_queue_version}"')
            attrs.append(f'containerKey="/playQueues/{self.play_queue_id}"')

        attrs.extend(
            [
                'type="music"',
                f'volume="{volume}"',
                f'shuffle="{shuffle}"',
                f'repeat="{repeat}"',
                f'controllable="{controllable}"',
                f'machineIdentifier="{machine_identifier}"',
                f'address="{address}"',
                f'port="{port}"',
                f'protocol="{protocol}"',
            ]
        )

        return attrs

    async def _build_timeline_xml(
        self, include_metadata: bool = False, command_id: str = "0"
    ) -> str:
        """
        Build timeline XML from current Music Assistant player state.

        :param include_metadata: Whether to include metadata in the timeline.
        :param command_id: The command ID for the timeline response.
        """
        player_id = self._ma_player_id

        player = self.provider.mass.players.get_player(player_id) if player_id else None
        queue = self.provider.mass.players.get_active_queue(player) if player else None

        controllable = (
            "volume,repeat,skipPrevious,seekTo,stepBack,stepForward,stop,playPause,shuffle,skipNext"
        )

        state = self._resolve_plex_state(player, queue)

        # group_volume is MA's effective, group-aware volume: for groups/syncgroups it
        # averages the (powered) members, otherwise it returns the player's own resolved
        # volume (following any attached volume control / protocol player).
        volume = int(player.group_volume or 0) if player else 0

        shuffle = 0
        repeat = 0
        if queue:
            shuffle = 1 if queue.shuffle_enabled else 0
            repeat = {RepeatMode.ONE: 1, RepeatMode.ALL: 2}.get(queue.repeat_mode, 0)

        if (
            state in ["playing", "paused"]
            and queue
            and queue.current_item
            and queue.current_item.media_item
        ):
            track = queue.current_item.media_item
            duration = round(track.duration * 1000) if track.duration else 0
            time_ms = round(queue.corrected_elapsed_time * 1000)

            attrs = self._build_timeline_attributes(
                track, state, duration, time_ms, volume, shuffle, repeat, controllable, queue
            )

            if attrs:
                music_timeline = f"<Timeline {' '.join(attrs)}/>"
            else:
                music_timeline = (
                    f'<Timeline state="{state}" time="{time_ms}" type="music" volume="{volume}" '
                    f'shuffle="{shuffle}" repeat="{repeat}" controllable="{controllable}"/>'
                )
        else:
            time_ms = 0
            music_timeline = (
                f'<Timeline state="{state}" time="{time_ms}" type="music" volume="{volume}" '
                f'shuffle="{shuffle}" repeat="{repeat}" controllable="{controllable}"/>'
            )

        video_timeline = '<Timeline type="video" state="stopped"/>'
        photo_timeline = '<Timeline type="photo" state="stopped"/>'

        return (
            f'<MediaContainer commandID="{command_id}">'
            f"{music_timeline}{video_timeline}{photo_timeline}"
            f"</MediaContainer>"
        )

    async def _send_timeline(self, client_id: str) -> None:
        """
        Send timeline update to a specific subscribed controller.

        :param client_id: The client ID to send the timeline to.
        """
        subscription = self.subscriptions.get(client_id)
        if not subscription:
            return

        timeline_xml = await self._build_timeline_xml()

        try:
            async with self.provider.mass.http_session.post(
                f"{subscription['url']}/:/timeline",
                data=timeline_xml,
                headers={
                    "X-Plex-Client-Identifier": self.client_id,
                    "Content-Type": "text/xml",
                },
                timeout=ClientTimeout(total=5),
            ) as resp:
                if resp.status < 400:
                    subscription["last_update"] = time.time()
        except Exception as e:
            LOGGER.debug(f"Failed to send timeline to {client_id}: {e}")

    async def _send_timeline_to_server(self) -> None:
        """Send timeline update to Plex server for activity tracking."""
        if not self._ma_player_id:
            return

        try:
            player = self.provider.mass.players.get_player(self._ma_player_id)
            queue = self.provider.mass.players.get_active_queue(player) if player else None

            if (
                not player
                or not queue
                or not queue.current_item
                or not queue.current_item.media_item
            ):
                return

            track = queue.current_item.media_item

            plex_key = plex_key_for_item(track, self.provider.instance_id)
            if not plex_key:
                return

            rating_key = plex_key.split("/")[-1]

            plex_state = self._resolve_plex_state(player, queue)

            position_ms = round(queue.corrected_elapsed_time * 1000)
            duration_ms = round(track.duration * 1000) if track.duration else 0

            container_key = ""
            play_queue_item_id = ""
            if self.play_queue_id:
                container_key = f"/playQueues/{self.play_queue_id}"
                if queue.current_index is not None:
                    play_queue_item_id = str(
                        self.play_queue_item_ids.get(queue.current_index, queue.current_index + 1)
                    )

            params: dict[str, str] = {
                "ratingKey": rating_key,
                "key": plex_key,
                "state": plex_state,
                "time": str(position_ms),
                "duration": str(duration_ms),
            }

            if container_key:
                params["containerKey"] = container_key
            if play_queue_item_id:
                params["playQueueItemID"] = play_queue_item_id

            plex_server = self.plex_server
            headers = self.headers

            def send_timeline() -> None:
                plex_server.query("/:/timeline", params=params, headers=headers)

            await asyncio.to_thread(send_timeline)

        except Exception as e:
            LOGGER.debug(f"Failed to send timeline to Plex server: {e}")

    async def _broadcast_timeline(self) -> None:
        """Send timeline to all subscribed controllers."""
        current_time = time.time()
        stale_clients = []
        for client_id, sub in self.subscriptions.items():
            try:
                last_update = float(sub["last_update"])  # type: ignore[arg-type]
                if current_time - last_update > 90:
                    stale_clients.append(client_id)
            except ValueError, TypeError:
                LOGGER.debug(f"Invalid last_update for client {client_id}, treating as stale")
                stale_clients.append(client_id)

        for client_id in stale_clients:
            del self.subscriptions[client_id]

        await asyncio.gather(
            *(self._send_timeline(client_id) for client_id in list(self.subscriptions.keys())),
            return_exceptions=True,
        )
