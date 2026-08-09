"""Just-in-time clip rendering for AI Radio."""
# mypy: disable-error-code="attr-defined"

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from music_assistant_models.enums import ContentType, StreamType
from music_assistant_models.errors import (
    InvalidDataError,
    MediaNotFoundError,
    MusicAssistantError,
)
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.helpers.tags import async_parse_tags

from .constants import (
    ATTR_HOST_ID,
    ATTR_MAX_CHARS,
    ATTR_PROMPT,
    ATTR_RENDERED_TEXT,
    ATTR_SESSION_ID,
    ATTR_WEB_SEARCH_MODE,
    CLIP_STREAMDETAILS_EXPIRATION,
    DEFERRED_PLACEHOLDERS,
    TTS_QUERY_TIMEOUT_SECONDS,
    TTS_SERVER_ERROR_MARKERS,
)
from .helpers import soft_limit_text

if TYPE_CHECKING:
    from music_assistant_models.enums import MediaType
    from music_assistant_models.queue_item import QueueItem

    from music_assistant.mass import MusicAssistant

    from .models import SessionState


class AIRadioRenderMixin:
    """Renders an AI Radio clip at the moment MA needs its audio."""

    if TYPE_CHECKING:
        mass: MusicAssistant
        logger: logging.Logger
        _hosts: dict[str, dict[str, Any]]
        _sessions: dict[str, SessionState]

    _render_locks: dict[str, asyncio.Lock]

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """
        Render the AI Radio clip with the given id and return its StreamDetails.

        :param item_id: The clip id of the queue item MA wants to play.
        :param media_type: The media type of the requested item.
        """
        queue_item = self._find_clip_item(item_id)
        if queue_item is None:
            raise MediaNotFoundError(f"AI Radio clip {item_id} is not in any queue")
        prompt = str(queue_item.extra_attributes.get(ATTR_PROMPT) or "")
        if not prompt:
            self._record_skip(queue_item, "clip has no prompt to render")
            raise MediaNotFoundError(f"AI Radio clip {item_id} has no prompt to render")

        async with self._lock_for(item_id):
            text = str(queue_item.extra_attributes.get(ATTR_RENDERED_TEXT) or "")
            if not text:
                text = await self._generate_script(queue_item, prompt, item_id)
                queue_item.extra_attributes[ATTR_RENDERED_TEXT] = text
                # the signal is what marks the items cache dirty and schedules the persist
                self.mass.player_queues.signal_update(queue_item.queue_id, items_changed=True)
            path, stream_type, audio_format, duration = await self._mint_clip_media(
                queue_item, text, item_id
            )

        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=audio_format,
            media_type=media_type,
            stream_type=stream_type,
            path=path,
            duration=duration,
            # a talk clip has nothing worth seeking to, and a seek is the one path that
            # would re-fetch a possibly-expired HA url mid-playback
            can_seek=False,
            allow_seek=False,
            expiration=CLIP_STREAMDETAILS_EXPIRATION,
        )

    def _lock_for(self, clip_id: str) -> asyncio.Lock:
        """Return the per-clip render lock, creating it on first use."""
        if not hasattr(self, "_render_locks"):
            self._render_locks = {}
        if clip_id not in self._render_locks:
            self._render_locks[clip_id] = asyncio.Lock()
        return self._render_locks[clip_id]

    def _find_clip_item(self, clip_id: str) -> QueueItem | None:
        """Return the queue item holding the given clip, or None when no queue holds it."""
        for queue_id in self._candidate_queue_ids(clip_id):
            if (item := self._find_clip_in_queue(clip_id, queue_id)) is not None:
                return item
        return None

    def _candidate_queue_ids(self, clip_id: str) -> list[str]:
        """
        Return the queue ids to search for a clip, the most likely one first.

        The owning session knows its queue, but the session registry is empty after a
        restart while the clip lives on in the persisted queue, so every queue stays a
        candidate. Clip ids carry a uuid4-based session id, so a hit is unambiguous.
        """
        queue_ids = [queue.queue_id for queue in self.mass.player_queues.all()]
        session = self._sessions.get(clip_id.rpartition("_")[0])
        if session is not None and session.queue_id in queue_ids:
            queue_ids.remove(session.queue_id)
            queue_ids.insert(0, session.queue_id)
        return queue_ids

    def _find_clip_in_queue(self, clip_id: str, queue_id: str) -> QueueItem | None:
        """Return the queue item holding the given clip, paging through the queue."""
        page_size = 500
        offset = 0
        while True:
            page = self.mass.player_queues.items(queue_id, limit=page_size, offset=offset)
            if not page:
                return None
            for item in page:
                if item.media_item is not None and item.media_item.item_id == clip_id:
                    return item
            if len(page) < page_size:
                return None
            offset += page_size

    async def _generate_script(self, queue_item: QueueItem, prompt: str, clip_id: str) -> str:
        """Resolve the deferred placeholders and generate the spoken script."""
        attributes = queue_item.extra_attributes
        deferred = await self._resolve_deferred_placeholders(prompt)
        resolved = prompt
        for key, value in deferred.items():
            resolved = resolved.replace(key, value)
        host = self._hosts.get(str(attributes.get(ATTR_HOST_ID) or "")) or {}
        instructions = str(host.get("instructions") or "")
        max_chars = int(attributes.get(ATTR_MAX_CHARS) or 0)
        web_mode = str(attributes.get(ATTR_WEB_SEARCH_MODE) or "disabled")
        try:
            text = cast(
                "str",
                await self._generate_text(
                    instructions=instructions, prompt=resolved, web_mode=web_mode
                ),
            )
        except Exception as err:
            self.logger.warning(
                "AI Radio clip %s (%s) failed to generate: %s", clip_id, queue_item.name, err
            )
            self._record_skip(queue_item, f"generation failed: {err}")
            raise MediaNotFoundError(f"AI Radio clip {clip_id} failed to generate") from err
        if max_chars > 0:
            text = soft_limit_text(text, max_chars=max_chars)
        self.logger.debug(
            "AI Radio clip %s (%s) rendered: %d chars", clip_id, queue_item.name, len(text)
        )
        return text

    async def _resolve_deferred_placeholders(self, prompt: str) -> dict[str, str]:
        """Return freshly resolved values for the placeholders deferred until airtime."""
        values = dict.fromkeys(DEFERRED_PLACEHOLDERS, "")
        values["<timestamp>"] = self._configured_now().strftime("%Y-%m-%d %H:%M %Z")
        # weather is the only deferred placeholder that costs a network round-trip, so it is
        # only fetched when the prompt actually references it
        weather_tokens = ("<weather_hourly>", "<weather_daily>")
        if any(token in prompt for token in weather_tokens):
            values.update(await self._prepare_weather_tokens())
        return values

    async def _mint_clip_media(
        self, queue_item: QueueItem, text: str, clip_id: str
    ) -> tuple[str, StreamType, AudioFormat, int | None]:
        """Convert the script to playable audio via the configured TTS engine."""
        host = self._hosts.get(str(queue_item.extra_attributes.get(ATTR_HOST_ID) or "")) or {}
        engine_uid = str(host.get("tts_engine") or "") or None
        try:
            path, stream_type, audio_format = await self._render_tts_media(text, engine_uid)
            # the probe is the first fetch, so a failed render surfaces here and not in playback
            duration = await self._probe_duration(path)
            return path, stream_type, audio_format, duration
        except Exception as err:
            self.logger.warning("AI Radio clip %s failed TTS: %s", clip_id, err)
            self._record_skip(queue_item, f"TTS failed: {err}")
            raise MediaNotFoundError(f"AI Radio clip {clip_id} failed TTS") from err

    async def _render_tts_media(
        self, text: str, engine_uid: str | None = None
    ) -> tuple[str, StreamType, AudioFormat]:
        """Ask the TTS engine for audio and return the path, stream type and format to play it."""
        engine = await self._get_tts_engine(engine_uid)
        try:
            async with asyncio.timeout(TTS_QUERY_TIMEOUT_SECONDS) as query_timeout:
                stream_details = await engine.provider.get_tts_message(text, engine_id=engine.id)
        except TimeoutError as err:
            # expired() tells our own cap apart from a timeout raised inside the engine
            if not query_timeout.expired():
                raise
            raise MusicAssistantError(
                f"TTS engine '{engine.uid}' did not respond within {TTS_QUERY_TIMEOUT_SECONDS}s"
            ) from err
        path = str(getattr(stream_details, "path", "") or "").strip()
        if path.startswith(("http://", "https://", "rtsp://", "rtmp://")):
            stream_type = StreamType.HTTP
        elif path and Path(path).is_absolute() and await asyncio.to_thread(Path(path).is_file):
            stream_type = StreamType.LOCAL_FILE
        else:
            raise InvalidDataError(
                f"TTS engine '{engine.uid}' returned an unusable stream path: "
                f"{path or '<empty>'}. StreamDetails.path must be a fetchable "
                "http(s)/rtsp/rtmp URL or the absolute path of an existing local file."
            )
        audio_format = stream_details.audio_format
        if audio_format.content_type == ContentType.UNKNOWN:
            audio_format = AudioFormat(content_type=ContentType.MP3)
        return path, stream_type, audio_format

    async def _probe_duration(self, path: str) -> int | None:
        """Return the clip duration in seconds, or None when it cannot be determined."""
        try:
            tags = await async_parse_tags(path, require_duration=True)
        except (InvalidDataError, OSError) as err:
            if any(marker in str(err) for marker in TTS_SERVER_ERROR_MARKERS):
                raise MusicAssistantError(
                    "Error during TTS generation. Does your TTS provider have enough credit? "
                    "Check the logs of your TTS provider for the reason."
                ) from err
            self.logger.warning("Could not determine AI Radio clip duration: %s", err)
            return None
        return int(tags.duration) if tags.duration else None

    def _record_skip(self, queue_item: QueueItem, error: str) -> None:
        """Record a skipped clip on its owning session."""
        session_id = str(queue_item.extra_attributes.get(ATTR_SESSION_ID) or "")
        if (session := self._sessions.get(session_id)) is None:
            return
        session.skipped_sections += 1
        session.last_render_error = error
