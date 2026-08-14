"""Just-in-time clip rendering for AI Radio."""
# mypy: disable-error-code="attr-defined"

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, cast

from music_assistant_models.enums import ContentType, StreamType, VolumeNormalizationMode
from music_assistant_models.errors import (
    InvalidDataError,
    MediaNotFoundError,
    MusicAssistantError,
)
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.constants import (
    CONF_VALUE_DISABLED,
    CONF_VALUE_ENABLED,
    CONF_VOLUME_NORMALIZATION,
    CONF_VOLUME_NORMALIZATION_TARGET,
    CONF_VOLUME_NORMALIZATION_TRACKS,
)
from music_assistant.helpers.audio import parse_loudnorm
from music_assistant.helpers.ffmpeg import get_ffmpeg_stream
from music_assistant.helpers.process import check_output
from music_assistant.helpers.tags import async_parse_tags
from music_assistant.helpers.tts import (
    query_tts_engine_with_language_fallback,
    resolve_tts_language,
    resolve_tts_stream_path,
)

from .constants import (
    ATTR_HOST_ID,
    ATTR_MAX_CHARS,
    ATTR_PROMPT,
    ATTR_RENDERED_TEXT,
    ATTR_SESSION_ID,
    ATTR_WEB_SEARCH_MODE,
    CLIP_STREAMDETAILS_EXPIRATION,
    CONF_TTS_LOUDNESS_BOOST,
    DEFAULT_TTS_LOUDNESS_BOOST,
    DEFERRED_PLACEHOLDERS,
    LOUDNESS_MEASURE_TIMEOUT,
    MIN_CLIP_MEDIA_LIFETIME,
    MIN_LOUDNESS_REFERENCE_SECONDS,
    TTS_CLIP_PCM_FORMAT,
    TTS_PEAK_CEILING_DB,
    TTS_SERVER_ERROR_MARKERS,
    TTS_SPEECHNORM_FILTER,
)
from .helpers import coerce_int, soft_limit_text

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.enums import MediaType
    from music_assistant_models.queue_item import QueueItem

    from music_assistant.mass import MusicAssistant

    from .models import SessionState


@dataclass(slots=True)
class _CachedClipMedia:
    """Media previously minted for a clip, kept until it expires."""

    path: str
    stream_type: StreamType
    audio_format: AudioFormat
    duration: int | None
    minted_at: float
    loudness: float | None


@dataclass(slots=True)
class _ClipAudio:
    """What get_audio_stream needs to serve a levelled clip, carried on StreamDetails.data."""

    path: str
    input_format: AudioFormat
    gain_db: float


class AIRadioRenderMixin:
    """Renders an AI Radio clip at the moment MA needs its audio."""

    if TYPE_CHECKING:
        mass: MusicAssistant
        config: ProviderConfig
        logger: logging.Logger
        _hosts: dict[str, dict[str, Any]]
        _sessions: dict[str, SessionState]

    _render_locks: dict[str, asyncio.Lock]
    _media_cache: dict[str, _CachedClipMedia]
    _engine_loudness: dict[tuple[str, str, str], float]

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
            media = await self._cached_clip_media(queue_item, text, item_id)

        streamdetails = StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=media.audio_format,
            media_type=media_type,
            stream_type=media.stream_type,
            path=media.path,
            duration=media.duration,
            # a talk clip has nothing worth seeking to, and a seek is the one path that
            # would re-fetch a possibly-expired HA url mid-playback
            can_seek=False,
            allow_seek=False,
            # a cache hit serves a url that was minted earlier, so it may only claim the life
            # that url has left or the stream outlives the token behind it
            expiration=self._remaining_media_lifetime(media),
        )
        gain_db = self._loudness_gain(queue_item.queue_id, media.loudness)
        if gain_db is not None:
            # core never normalizes a sound effect, so the clip is levelled here or it
            # airs noticeably quieter than the music around it
            streamdetails.stream_type = StreamType.CUSTOM
            # core mirrors what ffmpeg reports onto this object, so it gets a copy of the
            # constant rather than a handle on the one every clip shares
            streamdetails.decoded_audio_format = replace(TTS_CLIP_PCM_FORMAT)
            streamdetails.data = _ClipAudio(media.path, media.audio_format, gain_db)
        return streamdetails

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes]:
        """
        Return the levelled audio of a spoken clip as PCM.

        :param streamdetails: The StreamDetails previously returned by get_stream_details.
        :param seek_position: Ignored, a spoken clip cannot be seeked.
        """
        clip = cast("_ClipAudio", streamdetails.data)
        async for chunk in get_ffmpeg_stream(
            audio_input=clip.path,
            input_format=clip.input_format,
            output_format=TTS_CLIP_PCM_FORMAT,
            filter_params=[
                TTS_SPEECHNORM_FILTER,
                f"volume={round(clip.gain_db, 2)}dB",
                f"alimiter=limit={TTS_PEAK_CEILING_DB}dB:level=false:latency=true",
            ],
        ):
            yield chunk

    def _lock_for(self, clip_id: str) -> asyncio.Lock:
        """Return the per-clip render lock, creating it on first use."""
        if not hasattr(self, "_render_locks"):
            self._render_locks = {}
        if clip_id not in self._render_locks:
            self._render_locks[clip_id] = asyncio.Lock()
        return self._render_locks[clip_id]

    async def _cached_clip_media(
        self, queue_item: QueueItem, text: str, clip_id: str
    ) -> _CachedClipMedia:
        """Return the clip's minted media, re-minting only once the cache entry has expired."""
        if not hasattr(self, "_media_cache"):
            self._media_cache = {}
        now = asyncio.get_running_loop().time()
        cached = self._media_cache.get(clip_id)
        if cached is not None and self._remaining_media_lifetime(cached) > MIN_CLIP_MEDIA_LIFETIME:
            return cached
        # the caller holds the per-clip render lock, so of the several uncoordinated paths
        # that resolve the same clip only the first one mints; the rest hit the cache above
        path, stream_type, audio_format, duration, loudness = await self._mint_clip_media(
            queue_item, text, clip_id
        )
        media = _CachedClipMedia(path, stream_type, audio_format, duration, now, loudness)
        # clips are minted per queue item, so without pruning the cache grows for as long as
        # the server runs. an entry past its window can never be served again anyway
        for expired_id in [
            key
            for key, entry in self._media_cache.items()
            if now - entry.minted_at >= CLIP_STREAMDETAILS_EXPIRATION
        ]:
            del self._media_cache[expired_id]
        self._media_cache[clip_id] = media
        return media

    def _remaining_media_lifetime(self, media: _CachedClipMedia) -> int:
        """Return the seconds the given minted media is still usable for."""
        elapsed = asyncio.get_running_loop().time() - media.minted_at
        return max(MIN_CLIP_MEDIA_LIFETIME, round(CLIP_STREAMDETAILS_EXPIRATION - elapsed))

    def _wanted_loudness(self, queue_id: str) -> float | None:
        """Return the level in LUFS a clip should air at, or None when it should air as is."""
        normalization = self.mass.config.get_effective_player_queue_config_value(
            queue_id, CONF_VOLUME_NORMALIZATION, CONF_VALUE_ENABLED
        )
        if normalization == CONF_VALUE_DISABLED:
            return None
        # the queue switch only says normalization may run; the tracks around the clip are
        # the ones it has to match, and their own preference can still turn it off
        tracks_mode = self.mass.streams.get_config_value(CONF_VOLUME_NORMALIZATION_TRACKS)
        if tracks_mode == VolumeNormalizationMode.DISABLED.value:
            return None
        target = self.mass.streams.get_config_value(
            CONF_VOLUME_NORMALIZATION_TARGET, return_type=int
        )
        boost = coerce_int(
            self.config.get_value(CONF_TTS_LOUDNESS_BOOST), DEFAULT_TTS_LOUDNESS_BOOST
        )
        return target + boost

    def _loudness_gain(self, queue_id: str, loudness: float | None) -> float | None:
        """Return the dB to lift the clip by, or None when it should air untouched."""
        if loudness is None or (wanted := self._wanted_loudness(queue_id)) is None:
            return None
        # the reference is taken behind speechnorm, which lands close to the target on its
        # own, so this trim is small and runs in either direction
        return wanted - loudness

    def _tts_language(self, host_language: str | None = None) -> str | None:
        """
        Return the host's language, or the server locale, as a hyphenated language code.

        :param host_language: The host's configured language override, if any.
        """
        if override := (host_language or "").strip():
            return override.replace("_", "-")
        return resolve_tts_language(self.mass)

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
        language = str(host.get("language") or "")
        max_chars = int(attributes.get(ATTR_MAX_CHARS) or 0)
        web_mode = str(attributes.get(ATTR_WEB_SEARCH_MODE) or "disabled")
        try:
            text = cast(
                "str",
                await self._generate_text(
                    instructions=instructions,
                    prompt=resolved,
                    web_mode=web_mode,
                    language=language,
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
    ) -> tuple[str, StreamType, AudioFormat, int | None, float | None]:
        """Convert the script to playable audio via the configured TTS engine."""
        host = self._hosts.get(str(queue_item.extra_attributes.get(ATTR_HOST_ID) or "")) or {}
        engine_uid = str(host.get("tts_engine") or "") or None
        language = self._tts_language(str(host.get("language") or ""))
        options = host.get("options") or {}
        try:
            path, stream_type, audio_format = await self._render_tts_media(
                text, engine_uid, language, options
            )
            # the probe is the first fetch, so a failed render surfaces here and not in playback
            duration = await self._probe_duration(path)
        except Exception as err:
            self.logger.warning("AI Radio clip %s failed TTS: %s", clip_id, err)
            self._record_skip(queue_item, f"TTS failed: {err}")
            raise MediaNotFoundError(f"AI Radio clip {clip_id} failed TTS") from err
        # measuring costs a fetch and a decode on the just-in-time render path, so it only
        # runs where the reading has somewhere to go
        loudness = (
            await self._reference_loudness(engine_uid, language, options, path, duration)
            if self._wanted_loudness(queue_item.queue_id) is not None
            else None
        )
        return path, stream_type, audio_format, duration, loudness

    async def _reference_loudness(
        self,
        engine_uid: str | None,
        language: str | None,
        options: dict[str, Any],
        path: str,
        duration: int | None,
    ) -> float | None:
        """Return the loudness in LUFS to level this clip against, or None when unknown."""
        if not hasattr(self, "_engine_loudness"):
            self._engine_loudness = {}
        # engine, language and options together decide which voice speaks, and clips from one
        # voice land within a dB of each other, so measuring one of them is enough
        key = (engine_uid or "", language or "", json.dumps(options, sort_keys=True, default=str))
        if (cached := self._engine_loudness.get(key)) is not None:
            return cached
        if (loudness := await self._measure_loudness(path)) is None:
            return None
        if (duration or 0) >= MIN_LOUDNESS_REFERENCE_SECONDS:
            self._engine_loudness[key] = loudness
        return loudness

    async def _measure_loudness(self, path: str) -> float | None:
        """Return the integrated loudness of the given audio in LUFS, or None when it fails."""
        try:
            returncode, output = await check_output(
                "ffmpeg",
                "-hide_banner",
                "-nostats",
                "-i",
                path,
                # measure behind speechnorm: it is what the gain is applied on top of, and it
                # levels the clip itself, so the reading has to come from its output or the
                # gain corrects for a level that no longer reaches it
                "-af",
                f"{TTS_SPEECHNORM_FILTER},loudnorm=print_format=json",
                "-f",
                "null",
                "-",
                timeout=LOUDNESS_MEASURE_TIMEOUT,
            )
        except (OSError, TimeoutError) as err:
            self.logger.debug("Could not measure AI Radio clip loudness: %s", err)
            return None
        if returncode != 0:
            self.logger.debug("Could not measure AI Radio clip loudness: ffmpeg failed")
            return None
        return parse_loudnorm(output)

    async def _render_tts_media(
        self,
        text: str,
        engine_uid: str | None = None,
        language: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> tuple[str, StreamType, AudioFormat]:
        """Ask the TTS engine for audio and return the path, stream type and format to play it."""
        engine = await self._get_tts_engine(engine_uid)
        stream_details = await query_tts_engine_with_language_fallback(
            engine, text, language, logger=self.logger, options=options
        )
        path, stream_type = await resolve_tts_stream_path(engine, stream_details)
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
                # the engine reports no reason (Home Assistant answers a failed render with an
                # empty 500), so carry what the probe saw or the hint is all the user gets
                raise MusicAssistantError(
                    f"Error during TTS generation: {err}. Does your TTS provider have enough "
                    "credit? Check the logs of your TTS provider for the reason."
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
