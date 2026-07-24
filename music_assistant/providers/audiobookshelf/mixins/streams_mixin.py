"""StreamsMixin for Audiobookshelf."""

from __future__ import annotations

import time
from contextlib import suppress
from datetime import datetime

from aioaudiobookshelf.exceptions import SessionNotFoundError as AbsSessionNotFoundError
from aioaudiobookshelf.exceptions import (
    SessionSyncError as AbsSessionSyncError,
)
from aioaudiobookshelf.schema.calls_items import (
    PlaybackSessionParameters as AbsPlaybackSessionParameters,
)
from aioaudiobookshelf.schema.calls_session import SyncOpenSessionParameters
from aioaudiobookshelf.schema.session import DeviceInfo as AbsDeviceInfo
from aioaudiobookshelf.schema.session import PlaybackSessionExpanded as AbsPlaybackSessionExpanded
from aiohttp import web
from music_assistant_models.enums import (
    ContentType,
    MediaType,
    StreamType,
)
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import (
    Audiobook,
    AudioFormat,
    MediaItemType,
    PodcastEpisode,
)
from music_assistant_models.streamdetails import MultiPartPath, StreamDetails

from music_assistant.constants import PLAYBACK_REPORT_INTERVAL_SECONDS
from music_assistant.helpers.datetime import from_utc_timestamp
from music_assistant.providers.audiobookshelf.constants import CONF_URL
from music_assistant.providers.audiobookshelf.helpers import SessionHelper, handle_refresh_token
from music_assistant.providers.audiobookshelf.mixins.mixin_base import AbsMixinBase


class AbsStreamsMixin(AbsMixinBase):
    """StreamsMixin for Audiobookshelf."""

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get stream of item."""
        # We always create a playback session. The default is direct playback.
        # In that case, session.tracks holds the exact same as the audiobook/ podcast.track,
        # so we only use the session to update our progress.
        if media_type in (MediaType.PODCAST_EPISODE, MediaType.AUDIOBOOK):
            session = await self._get_playback_session(mass_item_id=item_id)
            return await self._get_stream_details_session(
                session, session_helper=self.sessions[item_id], media_type=media_type
            )
        raise MediaNotFoundError("Stream unknown")

    @handle_refresh_token
    async def get_resume_position(
        self, item_id: str, media_type: MediaType
    ) -> tuple[bool, int, datetime | None]:
        """Return finished:bool, position_ms: int."""
        # this method is called _before_ get_stream_details, so the playback session
        # is created here.
        session = await self._get_playback_session(mass_item_id=item_id)

        item_ids = item_id.split(" ")
        abs_item_id = item_ids[0]
        episode_id = item_ids[1] if len(item_ids) == 2 else None
        progress = await self._client.get_my_media_progress(
            item_id=abs_item_id, episode_id=episode_id
        )
        # only the progress object has a timestamp of the progress (not the session)
        # last_update is in ms epoch
        # If there is an open session, that session might have the old progress time,
        # whereas the explicit progress call above gives the most recent time.
        timestamp = from_utc_timestamp(progress.last_update / 1000) if progress else None
        current_time = (
            progress.current_time
            if progress is not None and progress.current_time is not None
            else session.current_time
        )
        finished = current_time > session.duration - PLAYBACK_REPORT_INTERVAL_SECONDS
        self.logger.debug("Resume position %s: obtained.", current_time)
        return (
            finished,
            int(current_time * 1000),
            timestamp,
        )

    @handle_refresh_token
    async def on_played(
        self,
        media_type: MediaType,
        prov_item_id: str,
        fully_played: bool,
        position: int,
        media_item: MediaItemType,
        is_playing: bool = False,
    ) -> None:
        """
        Update progress in Audiobookshelf.

        In our case media_type may have 3 values:
            - PODCAST
            - PODCAST_EPISODE
            - AUDIOBOOK
        We ignore PODCAST (function is called on adding a podcast with position=None)

        """

        async def _update_by_session(session_helper: SessionHelper, duration: int) -> bool:
            now = time.time()
            time_listened = now - session_helper.last_sync_time
            if time_listened > PLAYBACK_REPORT_INTERVAL_SECONDS * 2 + 10:
                # See player_queues controller, we get an update every 30s, and immediately on pause
                # or play.
                # We reset after two missed updates, as this indicates a trigger after a longer
                # absence and should not count into abs' statistics
                self.logger.debug("Resetting time_listened due to longer absence.")
                time_listened = 0.0
            try:
                await self._client.sync_open_session(
                    session_id=session_helper.abs_session_id,
                    parameters=SyncOpenSessionParameters(
                        current_time=position,
                        time_listened=time_listened,
                        duration=duration,
                    ),
                )
                session_helper.last_sync_time = now
                self.logger.debug("Synced playback session, position %s s.", position)
                return True
            except AbsSessionSyncError:
                self.logger.debug(
                    "Was unable to sync session. Falling back to non-session approach."
                )
            return False

        if media_type == MediaType.PODCAST_EPISODE:
            abs_podcast_id, abs_episode_id = prov_item_id.split(" ")

            # guard, see progress guard class docstrings for explanation
            if not self.progress_guard.guard_ok_mass(
                item_id=abs_podcast_id, episode_id=abs_episode_id
            ):
                return
            self.progress_guard.add_progress(item_id=abs_podcast_id, episode_id=abs_episode_id)

            if media_item is None or not isinstance(media_item, PodcastEpisode):
                return

            if fully_played and position < media_item.duration - PLAYBACK_REPORT_INTERVAL_SECONDS:
                # faulty position update
                # occurs sometimes, if a player disconnects unexpectedly, or reports
                # a false position - seen this for MC players, but not for sendspin
                return

            if position == 0 and not fully_played:
                # marked unplayed
                mp = await self._client.get_my_media_progress(
                    item_id=abs_podcast_id, episode_id=abs_episode_id
                )
                if mp is not None:
                    await self._client.remove_my_media_progress(media_progress_id=mp.id_)
                    self.logger.debug(f"Removed media progress of {media_type.value}.")
                    return

            duration = media_item.duration
            updated = False
            if session_helper := self.sessions.get(prov_item_id):
                updated = await _update_by_session(session_helper=session_helper, duration=duration)
            if not updated:
                self.logger.debug(
                    f"Updating media progress of {media_type.value}, title {media_item.name}."
                )
                await self._client.update_my_media_progress(
                    item_id=abs_podcast_id,
                    episode_id=abs_episode_id,
                    duration_seconds=duration,
                    progress_seconds=position,
                    is_finished=fully_played,
                )

        if media_type == MediaType.AUDIOBOOK:
            # guard, see progress guard class docstrings for explanation
            if not self.progress_guard.guard_ok_mass(item_id=prov_item_id):
                return
            self.progress_guard.add_progress(item_id=prov_item_id)

            if media_item is None or not isinstance(media_item, Audiobook):
                return

            if fully_played and position < media_item.duration - PLAYBACK_REPORT_INTERVAL_SECONDS:
                # faulty position update, see above
                return

            if position == 0 and not fully_played:
                # marked unplayed
                mp = await self._client.get_my_media_progress(item_id=prov_item_id)
                if mp is not None:
                    await self._client.remove_my_media_progress(media_progress_id=mp.id_)
                    self.logger.debug(f"Removed media progress of {media_type.value}.")
                return

            duration = media_item.duration
            updated = False
            if session_helper := self.sessions.get(prov_item_id):
                updated = await _update_by_session(session_helper=session_helper, duration=duration)
            if not updated:
                self.logger.debug(f"Updating {media_type.value} named {media_item.name} progress")
                await self._client.update_my_media_progress(
                    item_id=prov_item_id,
                    duration_seconds=duration,
                    progress_seconds=position,
                    is_finished=fully_played,
                )

    async def _get_stream_details_session(
        self,
        abs_session: AbsPlaybackSessionExpanded,
        session_helper: SessionHelper,
        media_type: MediaType,
    ) -> StreamDetails:
        """
        Streamdetails audiobook.

        We always use a custom stream type, also for single file, such
        that we can handle an ffmpeg error and refresh our tokens.
        """
        abs_base_url = str(self.config.get_value(CONF_URL))
        tracks = abs_session.audio_tracks

        if len(tracks) == 0:
            raise MediaNotFoundError("Session has no tracks.")

        content_type = ContentType.UNKNOWN
        if abs_session.audio_tracks[0].metadata is not None:
            content_type = ContentType.try_parse(abs_session.audio_tracks[0].metadata.ext)

        file_parts: list[MultiPartPath] = []
        if self.is_token_user:
            self.logger.debug("Token User - Streams are direct.")
        for idx, track in enumerate(tracks):
            if self.is_token_user:
                # an api key is long-lived
                stream_url = f"{abs_base_url}{track.content_url}?token={self._client.token}"
            else:
                # to ensure token is always valid, we create a dynamic url
                # this ensures that we always get a fresh token on each part
                # without having to deal with a custom stream etc.
                # we also use this for a single track, otherwise we can't seek
                stream_url = (
                    f"{self.mass.streams.base_url}/{self.instance_id}_part_stream?"
                    f"session_id={abs_session.id_}&part_id={idx}"
                )
            file_parts.append(MultiPartPath(path=stream_url, duration=track.duration))

        return StreamDetails(
            provider=self.instance_id,
            item_id=abs_session.id_,
            audio_format=AudioFormat(content_type=content_type),
            media_type=media_type,
            stream_type=StreamType.HTTP,
            duration=int(abs_session.duration),
            path=file_parts[0].path if len(file_parts) == 1 else file_parts,
            can_seek=True,
            allow_seek=True,
        )

    async def _get_playback_session(self, mass_item_id: str) -> AbsPlaybackSessionExpanded:
        """Either creates or returns an open abs session."""
        async with self.create_session_lock:
            # check for an available open session
            if session_helper := self.sessions.get(mass_item_id):
                # reset here, as this is our "time listened".
                session_helper.last_sync_time = time.time()
                with suppress(AbsSessionNotFoundError):
                    return await self._client.get_open_session(
                        session_id=session_helper.abs_session_id
                    )

            item_ids = mass_item_id.split(" ")
            abs_item_id = item_ids[0]
            episode_id = item_ids[1] if len(item_ids) == 2 else None

            client_name = f"Music Assistant {self.instance_id}"
            device_info = AbsDeviceInfo(
                device_id=self.instance_id,
                client_name=client_name,
                client_version=self.mass.version,
                manufacturer="",
                model=self.mass.server_id,
            )

            session = await self._client.get_playback_session(
                # Direct play gives us the individual files. Transcode give an HLS session.
                # Sessions without HLS proved to be stable. See:
                # https://github.com/music-assistant/support/issues/4754
                # https://github.com/music-assistant/support/issues/4586
                session_parameters=AbsPlaybackSessionParameters(
                    device_info=device_info,
                    force_direct_play=True,
                    force_transcode=False,
                    # mimetypes are only checked for abs' internal "should transcode
                    # see https://github.com/advplyr/audiobookshelf/blob/master/server/managers/PlaybackSessionManager.js
                    supported_mime_types=[],
                    media_player=client_name,
                ),
                item_id=abs_item_id,
                episode_id=episode_id,
            )

            self.sessions[mass_item_id] = SessionHelper(
                abs_session_id=session.id_,
                last_sync_time=time.time(),
            )
            return session

    @handle_refresh_token
    async def _handle_session_part_request(self, request: web.Request) -> web.Response:
        """
        Handle dynamic audiobook part stream request.

        We redirect to the actual stream url with token.
        This is done because the token might expire, so we need to
        generate a fresh url on each part.
        """
        if not (session_id := request.query.get("session_id")):
            return web.Response(status=400, text="Missing session_id")
        if not (part_id := request.query.get("part_id")):
            return web.Response(status=400, text="Missing part_id")
        self.logger.debug(
            "Handling session part request for session %s and part %s", session_id, part_id
        )
        try:
            abs_session = await self._client.get_open_session(session_id=session_id)
        except AbsSessionNotFoundError as err:
            raise web.HTTPNotFound from err
        try:
            part_track = abs_session.audio_tracks[int(part_id)]
        except IndexError:
            return web.Response(status=404, text="Part not found")

        base_url = str(self.config.get_value(CONF_URL))
        stream_url = f"{base_url}{part_track.content_url}?token={self._client.token}"
        # redirect to the actual stream url
        raise web.HTTPFound(location=stream_url)
