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
        """Send STOP command to player."""
        # WAM does not have a discrete stop, so we treat pause as stop.
        self.player.stream_active = False
        await self.speaker.cmd_pause()

        # Reset tracking properties entirely
        self.player._attr_elapsed_time = None
        self.player._attr_elapsed_time_last_updated = None

        # Force a refresh. This will likely map to PAUSED or IDLE depending on speaker response.
        self.player.state_sync.refresh_state(notify_provider=True)

    @retry_command()
    @handle_pywam_errors
    async def play(self) -> None:
        """Resume playback on the speaker."""
        if self.player.active_source == self.player.player_id:
            await self.mass.player_queues.resume(self.player.player_id)
            return

        await self.speaker.cmd_play()

        # We are resuming from a pause on a non-queue source. Refresh the last_updated
        # timestamp so corrected_elapsed_time doesn't count the pause time.
        if self.player.elapsed_time is not None:
            self.player._attr_elapsed_time = self.player.corrected_elapsed_time
            self.player._attr_elapsed_time_last_updated = time.time()
            self.player.update_state()

    @retry_command()
    @handle_pywam_errors
    async def pause(self) -> None:
        """Pause playback on the speaker."""
        await self.speaker.cmd_pause()

        # Request exact play time to ensure an accurate pause marker
        await self.player.state_sync.update_play_time()
        self.player.update_state()

    @retry_command()
    @handle_pywam_errors
    async def play_media(self, media: PlayerMedia) -> None:
        """Stream a media URI to the speaker.

        :param media: The details of the media stream to play.
        """
        if getattr(self.player, "synced_to_internal", None):
            raise PlayerCommandFailed(f"Player {self.player.log_name} is a group child.")

        if self.player.active_source != WamSource.WIFI:
            await self.select_source(WamSource.WIFI)

        stream_url = await self.mass.streams.resolve_stream_url(self.player.player_id, media)

        item = UrlMediaItem(
            url=stream_url,
            title=media.title,
            description=media.artist,
            duration=str(int(media.duration)) if media.duration else "0",
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
        """Change the input source on the speaker.

        :param source: The target source identifier.
        """
        try:
            target_source = WamSource.WIFI if source == self.player.player_id else WamSource(source)
        except ValueError as err:
            raise PlayerCommandFailed(
                f"'{source}' is not a valid source for: {self.player.log_name}"
            ) from err

        if self.player.active_source == str(target_source):
            return

        if target_source != WamSource.WIFI:
            self.player.stream_active = False

        await self.speaker.select_source(str(target_source))

        def check_source() -> bool:
            return self.player.active_source == str(target_source)

        await self.player.await_state_change(check_source, SOURCE_CHANGE_TIMEOUT)
