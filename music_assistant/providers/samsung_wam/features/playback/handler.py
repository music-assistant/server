"""Playback handler."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from music_assistant_models.errors import PlayerCommandFailed
from pywam.lib.exceptions import PywamError
from pywam.lib.url import UrlMediaItem

from music_assistant.providers.samsung_wam.features.base import (
    WamPlayerFeatureBase,
    handle_pywam_errors,
    retry_command,
)

from .consts import SOURCE_CHANGE_TIMEOUT
from .models import WamSource

if TYPE_CHECKING:
    from music_assistant_models.player import PlayerMedia


class PlaybackHandler(WamPlayerFeatureBase):
    """Encapsulates playback and media-related commands."""

    @retry_command()
    @handle_pywam_errors
    async def stop(self) -> None:
        """Stop playback on the speaker."""
        # WAM does not have a discrete stop, so we treat pause as stop
        self.player.stream_active = False
        await self.speaker.cmd_pause()

        # Clear elapsed time tracking
        self.player._attr_elapsed_time = None
        self.player._attr_elapsed_time_last_updated = None

        # Refresh state; the speaker will report either paused or idle after a pause command
        self.player.state_sync.refresh_state(notify_provider=True)

    @retry_command()
    @handle_pywam_errors
    async def play(self) -> None:
        """Resume playback on the speaker."""
        if self.player.active_source == self.player.player_id:
            await self.mass.player_queues.resume(self.player.player_id)
            return

        await self.speaker.cmd_play()

        # Resuming from a pause — reset the elapsed time base so it doesn't include paused time
        if self.player.elapsed_time is not None:
            self.player._attr_elapsed_time = self.player.corrected_elapsed_time
            self.player._attr_elapsed_time_last_updated = time.time()
            self.player.update_state()

    @retry_command()
    @handle_pywam_errors
    async def pause(self) -> None:
        """Pause playback on the speaker."""
        await self.speaker.cmd_pause()

        # Fetch the exact play position to keep elapsed time accurate while paused
        await self.player.state_sync.update_play_time()
        self.player.update_state()

    @retry_command()
    @handle_pywam_errors
    async def play_media(self, media: PlayerMedia) -> None:
        """
        Stream a media URI to the speaker.

        :param media: The details of the media stream to play.
        """
        if self.player.synced_to:
            raise PlayerCommandFailed(
                f"Cannot play media: {self.player.log_name} is a group member"
            )

        if self.player.active_source != WamSource.WIFI:
            await self.select_source(WamSource.WIFI)

        stream_url = await self.mass.streams.resolve_stream_url(self.player.player_id, media)

        duration = media.stream_duration or media.duration
        item = UrlMediaItem(
            url=stream_url,
            title=media.title,
            description=media.artist,
            duration=str(int(duration)) if duration else "0",
            thumbnail=media.image_url,
        )

        try:
            self.player.stream_active = True

            self.player._attr_elapsed_time = 0.0
            self.player._attr_elapsed_time_last_updated = time.time()
            self.player.update_state()

            await self.speaker.play_url(item)

        except (PywamError, PlayerCommandFailed) as err:
            self.player.stream_active = False
            raise PlayerCommandFailed(f"Failed to play media on WAM speaker: {err}") from err

    @retry_command()
    @handle_pywam_errors
    async def select_source(self, source: WamSource | str) -> None:
        """
        Change the input source on the speaker.

        :param source: The target source identifier.
        """
        try:
            target_source = WamSource.WIFI if source == self.player.player_id else WamSource(source)
        except ValueError as err:
            raise PlayerCommandFailed(
                f"'{source}' is not a valid source for {self.player.log_name}"
            ) from err

        if target_source != WamSource.WIFI:
            self.player.stream_active = False
            # The player will briefly appear idle during the source switch; setting the active
            # source here prevents the UI from momentarily reverting to the queue name
            self.player.set_active_mass_source(str(target_source))

        if self.player.active_source == str(target_source):
            # Already on the target source; refresh to push current state through
            self.player.state_sync.refresh_state(notify_provider=True)
            return

        await self.speaker.select_source(str(target_source))

        def check_source() -> bool:
            return self.player.active_source == str(target_source)

        await self.player.await_state_change(check_source, SOURCE_CHANGE_TIMEOUT)

        # Push final state regardless of whether the source change was confirmed
        self.player.state_sync.refresh_state(notify_provider=True)
