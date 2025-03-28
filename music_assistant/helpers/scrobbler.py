"""Helper class to aid scrobblers."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from music_assistant_models.event import MassEvent
    from music_assistant_models.playback_progress_report import MediaItemPlaybackProgressReport


class ScrobblerHelper:
    """Base class to aid scrobbling tracks."""

    logger: logging.Logger
    currently_playing: str | None = None
    last_scrobbled: str | None = None

    def __init__(self, logger: logging.Logger) -> None:
        """Initialize."""
        self.logger = logger

    def _is_configured(self) -> bool:
        """Override if subclass needs specific configuration."""
        return True

    async def _update_now_playing(self, report: MediaItemPlaybackProgressReport) -> None:
        """Send a Now Playing update to the scrobbling service."""

    async def _scrobble(self, report: MediaItemPlaybackProgressReport) -> None:
        """Scrobble."""

    async def _on_mass_media_item_played(self, event: MassEvent) -> None:
        """Media item has finished playing, we'll scrobble the track."""
        if not self._is_configured():
            return

        report: MediaItemPlaybackProgressReport = event.data

        # poor mans attempt to detect a song on loop
        if not report.fully_played and report.uri == self.last_scrobbled:
            self.logger.debug(
                "reset _last_scrobbled and _currently_playing because the song was restarted"
            )
            self.last_scrobbled = None
            # reset currently playing to avoid it expiring when looping single songs
            self.currently_playing = None

        async def update_now_playing() -> None:
            try:
                await self._update_now_playing(report)
                self.logger.debug(f"track {report.uri} marked as 'now playing'")
                self.currently_playing = report.uri
            except Exception as err:
                # TODO: try to make this a more specific exception instead of a generic one
                self.logger.exception(err)

        async def scrobble() -> None:
            try:
                await self._scrobble(report)
                self.last_scrobbled = report.uri
            except Exception as err:
                # TODO: try to make this a more specific exception instead of a generic one
                self.logger.exception(err)

        # update now playing if needed
        if report.is_playing and (
            self.currently_playing is None or self.currently_playing != report.uri
        ):
            await update_now_playing()

        if self.should_scrobble(report):
            await scrobble()

    def should_scrobble(self, report: MediaItemPlaybackProgressReport) -> bool:
        """Determine if a track should be scrobbled, to be extended later."""
        if self.last_scrobbled == report.uri:
            self.logger.debug("skipped scrobbling due to duplicate event")
            return False

        # ideally we want more precise control
        # but because the event is triggered every 30s
        # and we don't have full queue details to determine
        # the exact context in which the event was fired
        # we can only rely on fully_played for now
        return bool(report.fully_played)
