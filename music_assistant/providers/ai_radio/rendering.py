"""Just-in-time clip rendering for AI Radio."""
# mypy: disable-error-code="attr-defined"

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import tempfile
import time
import wave
from contextlib import aclosing
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from music_assistant_models.enums import ContentType, StreamType, VolumeNormalizationMode
from music_assistant_models.errors import (
    AudioError,
    InvalidDataError,
    MediaNotFoundError,
    MusicAssistantError,
)
from music_assistant_models.media_items import AudioFormat, Track
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
from music_assistant.models.plugin import VoiceOver

from .constants import (
    ATTR_ALLOW_POST,
    ATTR_HOST_ID,
    ATTR_MAX_CHARS,
    ATTR_PROMPT,
    ATTR_RENDERED_TEXT,
    ATTR_SESSION_ID,
    ATTR_WEATHER_REQUIRED,
    ATTR_WEB_SEARCH_MODE,
    CLIP_STREAMDETAILS_EXPIRATION,
    CONF_TTS_LOUDNESS_BOOST,
    DEFAULT_TTS_LOUDNESS_BOOST,
    DEFERRED_PLACEHOLDERS,
    LOUDNESS_MEASURE_TIMEOUT,
    MIN_CLIP_MEDIA_LIFETIME,
    MIN_LOUDNESS_REFERENCE_SECONDS,
    NO_WEATHER_DATA_INSTRUCTION,
    POST_CLIP_MAX_AGE,
    POST_CLIP_PREFIX,
    POST_LYRICS_TIMEOUT,
    POST_MIN_HEAD_SECONDS,
    POST_MIN_SECONDS,
    POST_STAGE_TIMEOUT,
    POST_STAGED_FORMAT,
    POST_TAIL_GAP,
    TTS_CLIP_PCM_FORMAT,
    TTS_PEAK_CEILING_DB,
    TTS_SERVER_ERROR_MARKERS,
    TTS_SPEECHNORM_FILTER,
    WEATHER_PLACEHOLDER_TOKENS,
)
from .helpers import coerce_int, format_ai_radio_timestamp, soft_limit_text
from .post_window import lyric_onset

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
class _PostPlan:
    """How a break is split between its own queue item and the record after it."""

    head: float  # seconds the break airs alone
    overlap: float  # seconds of it carried over the record's intro
    staged: str  # local levelled copy of the break, which both parts are read from
    queue_id: str
    clip_item_id: str  # the break
    track_item_id: str  # the record its tail airs over
    track_name: str
    # whether the tail is still due to air over the record; settled when the break's audio
    # is produced, and cleared once the streams side is done with it
    armed: bool = True


@dataclass(slots=True)
class _ClipAudio:
    """What get_audio_stream needs to serve a levelled clip, carried on StreamDetails.data."""

    path: str
    input_format: AudioFormat
    gain_db: float | None  # None when the clip airs as rendered
    # the planned split, if any; whether it still holds is settled when the audio is produced
    post: _PostPlan | None = None


def _levelling_filters(gain_db: float | None) -> list[str]:
    """
    Return the filter chain that levels a spoken clip, empty when it airs as rendered.

    :param gain_db: The trim that brings the clip to the wanted level, or None for none.
    """
    if gain_db is None:
        return []
    # speechnorm evens the clip out, the trim places it, the limiter backstops the peaks
    return [
        TTS_SPEECHNORM_FILTER,
        f"volume={round(gain_db, 2)}dB",
        f"alimiter=limit={TTS_PEAK_CEILING_DB}dB:level=false:latency=true",
    ]


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
    _post_plans: dict[str, _PostPlan | None]
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
            gain_db = self._loudness_gain(queue_item.queue_id, media.loudness)
            post = await self._plan_post(queue_item, media, item_id, gain_db)

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
        if post is not None:
            # served through get_audio_stream, which is where the break gets cut short - or
            # not, if the queue changed since the split was planned
            streamdetails.duration = max(1, math.ceil(post.head))
            streamdetails.stream_type = StreamType.CUSTOM
            streamdetails.decoded_audio_format = replace(TTS_CLIP_PCM_FORMAT)
            streamdetails.data = _ClipAudio(media.path, media.audio_format, gain_db, post)
        elif gain_db is not None:
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
        path, input_format = clip.path, clip.input_format
        filters = _levelling_filters(clip.gain_db)
        if clip.post is not None:
            # the split was planned up to a song ahead, so the break is only cut short if its
            # tail can still air. The staged copy is levelled already and is what the tail
            # is read from, so the head plays from it too for as long as it exists
            tail_airs = await self._recheck_post(clip.post)
            if tail_airs or await asyncio.to_thread(Path(clip.post.staged).is_file):
                path, input_format = clip.post.staged, POST_STAGED_FORMAT
                filters = [f"atrim=end={clip.post.head:.3f}"] if tail_airs else []
        async for chunk in get_ffmpeg_stream(
            audio_input=path,
            input_format=input_format,
            output_format=TTS_CLIP_PCM_FORMAT,
            filter_params=filters,
        ):
            yield chunk

    async def get_voice_over(
        self, streamdetails: StreamDetails, next_item: QueueItem
    ) -> VoiceOver | None:
        """
        Return the tail of a break to mix over the record after it, if the break has one.

        :param streamdetails: Stream details of the break that played right before.
        :param next_item: The record about to stream.
        """
        plan = getattr(self, "_post_plans", {}).get(streamdetails.item_id)
        if (
            plan is None
            or not plan.armed
            or plan.queue_id != next_item.queue_id
            or plan.track_item_id != next_item.queue_item_id
        ):
            return None
        # start 0: the voice is already talking when the record comes in
        return VoiceOver(path=plan.staged, start=0.0, end=plan.overlap, offset=plan.head)

    async def on_voice_over_ended(self, streamdetails: StreamDetails, aired: bool) -> None:
        """
        Take a break's tail off its record once the streams side is done with it.

        The plan and its staged copy are kept: a break that airs again re-arms its tail when
        its audio is produced, and both go when the break's media expires.

        :param streamdetails: Stream details of the break the tail belongs to.
        :param aired: Whether the tail was mixed in.
        """
        if (plan := getattr(self, "_post_plans", {}).get(streamdetails.item_id)) is not None:
            plan.armed = False

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
        expired_staged: list[str] = []
        for expired_id in [
            key
            for key, entry in self._media_cache.items()
            if now - entry.minted_at >= CLIP_STREAMDETAILS_EXPIRATION
        ]:
            del self._media_cache[expired_id]
            if plan := getattr(self, "_post_plans", {}).pop(expired_id, None):
                expired_staged.append(plan.staged)
        self._media_cache[clip_id] = media
        await self._delete_staged_clips(expired_staged)
        return media

    def _post_skipped(self, item_name: str, reason: str) -> None:
        """
        Log why an opted-in break did not post.

        :param item_name: The track the post would have been attached to.
        :param reason: Why it was not.
        """
        # INFO: only sections that opted in reach here, and a post that quietly does not
        # happen looks the same as one that was never enabled
        self.logger.info("AI Radio post skipped on %s: %s", item_name, reason)

    async def _plan_post(
        self,
        queue_item: QueueItem,
        media: _CachedClipMedia,
        clip_id: str,
        gain_db: float | None,
    ) -> _PostPlan | None:
        """
        Decide whether a break carries over the next record.

        Returns how the break is split, or None when it airs whole.

        :param queue_item: The clip about to air.
        :param media: The clip as minted.
        :param clip_id: The clip's id, so a repeat request reuses the same plan.
        :param gain_db: The trim the clip airs with, or None when it airs as rendered.
        """
        if not queue_item.extra_attributes.get(ATTR_ALLOW_POST):
            return None
        if not hasattr(self, "_post_plans"):
            self._post_plans = {}
        if clip_id in self._post_plans:
            # a repeat request for the same clip must get the same split
            plan = self._post_plans[clip_id]
            if plan is None or await self._recheck_post(plan):
                return plan
            # planned afresh below, so the copy the old split was read from is done with
            await self._delete_staged_clips([plan.staged])
        self._post_plans[clip_id] = None

        next_item = self.mass.player_queues.get_next_item(
            queue_item.queue_id, queue_item.queue_item_id
        )
        if next_item is None or next_item.media_item is None:
            self._post_skipped(queue_item.name, "no next track in the queue")
            return None
        onset, reason = await self._resolve_vocal_onset(next_item)
        if onset is None:
            self._post_skipped(next_item.name, reason)
            return None
        window = onset - POST_TAIL_GAP
        if window < POST_MIN_SECONDS:
            self._post_skipped(
                next_item.name, f"vocal enters at {onset:.1f}s, too little instrumental intro"
            )
            return None

        staged = await self._stage_post_clip(media.path, media.audio_format, gain_db)
        if staged is None:
            self._post_skipped(next_item.name, "rendered audio could not be staged")
            return None
        staged_path, total = staged

        overlap = min(window, total - POST_MIN_HEAD_SECONDS)
        if overlap < POST_MIN_SECONDS:
            self._post_skipped(
                next_item.name, f"break is only {total:.1f}s, too short to carry over"
            )
            await self._delete_staged_clips([staged_path])
            return None
        head = total - overlap

        plan = _PostPlan(
            head=head,
            overlap=overlap,
            staged=staged_path,
            queue_id=queue_item.queue_id,
            clip_item_id=queue_item.queue_item_id,
            track_item_id=next_item.queue_item_id,
            track_name=next_item.name,
        )
        self.logger.info(
            "AI Radio post armed on %s: break %.1fs airs alone for %.1fs, last %.1fs "
            "over the intro, vocal at %.1fs",
            next_item.name,
            total,
            head,
            overlap,
            onset,
        )
        self._post_plans[clip_id] = plan
        return plan

    async def _recheck_post(self, plan: _PostPlan) -> bool:
        """
        Return whether a planned post can still air, and arm or disarm it to match.

        :param plan: How the break was split.
        """
        next_item = self.mass.player_queues.get_next_item(plan.queue_id, plan.clip_item_id)
        if next_item is None or next_item.queue_item_id != plan.track_item_id:
            reason = "it no longer follows the break"
        elif not await asyncio.to_thread(Path(plan.staged).is_file):
            reason = "the staged audio is gone"
        else:
            # re-armed because the streams side disarms a post once it is done with it
            plan.armed = True
            return True
        self._post_skipped(plan.track_name, reason)
        plan.armed = False
        return False

    async def _stage_post_clip(
        self, path: str, input_format: AudioFormat, gain_db: float | None
    ) -> tuple[str, float] | None:
        """
        Render a clip into a local, levelled copy and return its path and duration in seconds.

        Returns None when the render failed, in which case the break airs whole.

        :param path: Path or URL the TTS engine returned.
        :param input_format: The audio format of that clip.
        :param gain_db: The trim the clip airs with, or None when it airs as rendered.
        """
        # Levelled here in one pass over the whole break, so the head and the carried-over
        # tail share a level with no seam between them. Fetched now because an HA tts_proxy
        # url dies about 60 s after its last use, and the tail airs a minute or more from now.
        chunks: list[bytes] = []
        try:
            async with (
                asyncio.timeout(POST_STAGE_TIMEOUT),
                aclosing(
                    get_ffmpeg_stream(
                        audio_input=path,
                        input_format=input_format,
                        output_format=TTS_CLIP_PCM_FORMAT,
                        filter_params=_levelling_filters(gain_db),
                    )
                ) as pcm_stream,
            ):
                async for chunk in pcm_stream:
                    chunks.append(chunk)
        except (TimeoutError, AudioError) as err:
            self.logger.warning(
                "AI Radio post clip could not be staged: %s", str(err) or type(err).__name__
            )
            return None
        pcm = b"".join(chunks)
        if not pcm:
            return None
        staged = await asyncio.to_thread(self._write_staged_clip, pcm)
        return staged, len(pcm) / TTS_CLIP_PCM_FORMAT.pcm_sample_size

    def _write_staged_clip(self, pcm: bytes) -> str:
        """
        Write levelled clip audio to a uniquely named WAV file and return its path.

        :param pcm: The audio, as raw PCM in ``TTS_CLIP_PCM_FORMAT``.
        """
        self._prune_post_clips()
        handle, staged = tempfile.mkstemp(prefix=POST_CLIP_PREFIX, suffix=".wav")
        with os.fdopen(handle, "wb") as staged_file, wave.open(staged_file, "wb") as wav:
            wav.setnchannels(TTS_CLIP_PCM_FORMAT.channels)
            wav.setsampwidth(TTS_CLIP_PCM_FORMAT.bit_depth // 8)
            wav.setframerate(TTS_CLIP_PCM_FORMAT.sample_rate)
            wav.writeframes(pcm)
        return staged

    async def _discard_post_plans(self) -> None:
        """Forget every planned post and delete the staged copies they were read from."""
        plans = getattr(self, "_post_plans", {})
        staged = [plan.staged for plan in plans.values() if plan is not None]
        plans.clear()
        await self._delete_staged_clips(staged)

    async def _delete_staged_clips(self, paths: list[str]) -> None:
        """
        Delete staged post clips, off the event loop.

        :param paths: The staged copies to delete; missing ones are skipped.
        """
        for path in paths:
            await asyncio.to_thread(Path(path).unlink, missing_ok=True)

    def _prune_post_clips(self) -> None:
        """Delete staged clips left behind by posts that never aired."""
        cutoff = time.time() - POST_CLIP_MAX_AGE
        try:
            staged_clips = list(Path(tempfile.gettempdir()).glob(f"{POST_CLIP_PREFIX}*"))
        except OSError as err:
            self.logger.debug("Staged post clips could not be listed for pruning: %s", err)
            return
        for stale in staged_clips:
            try:
                if stale.stat().st_mtime < cutoff:
                    stale.unlink()
            except OSError:
                # another pass removed it, or it is not ours to remove
                continue

    async def _resolve_vocal_onset(self, queue_item: QueueItem) -> tuple[float | None, str]:
        """
        Return the second the next track's vocal enters, and the reason when there is none.

        :param queue_item: The upcoming track.
        """
        media_item = queue_item.media_item
        if not isinstance(media_item, Track):
            return None, "no track details"
        if onset := lyric_onset(media_item.metadata.lrc_lyrics):
            return onset, ""
        try:
            # the lookup walks every metadata provider, longer than a clip about to air can wait
            async with asyncio.timeout(POST_LYRICS_TIMEOUT):
                plain, lrc_lyrics = await self.mass.metadata.get_track_lyrics(media_item)
        except TimeoutError:
            return None, f"lyrics lookup took longer than {POST_LYRICS_TIMEOUT:.0f}s"
        except MusicAssistantError as err:
            return None, f"lyrics lookup failed ({err})"
        if onset := lyric_onset(lrc_lyrics):
            return onset, ""
        if lrc_lyrics:
            return None, "synced lyrics have no sung line"
        if plain:
            return None, "only unsynced lyrics available, so no vocal timing"
        return None, "no lyrics found"

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
        empty_weather_tokens = [
            token
            for token in WEATHER_PLACEHOLDER_TOKENS
            if token in prompt and not deferred.get(token)
        ]
        if empty_weather_tokens:
            if attributes.get(ATTR_WEATHER_REQUIRED):
                error = "weather data unavailable for a weather-required clip"
                self.logger.warning(
                    "AI Radio clip %s (%s) skipped: %s", clip_id, queue_item.name, error
                )
                self._record_skip(queue_item, error)
                raise MediaNotFoundError(f"AI Radio clip {clip_id} has no weather data")
            # weather is optional in this clip, so the LLM must skip it rather than invent it
            for token in empty_weather_tokens:
                deferred[token] = NO_WEATHER_DATA_INSTRUCTION
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
        values["<timestamp>"] = format_ai_radio_timestamp(self._configured_now())
        # weather is the only deferred placeholder that costs a network round-trip, so it is
        # only fetched when the prompt actually references it
        if any(token in prompt for token in WEATHER_PLACEHOLDER_TOKENS):
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
                # the engine reports no reason of its own (Home Assistant answers a failed
                # render with an empty 500), so the probe's message is the only clue there is
                raise MusicAssistantError(
                    f"{err}. The TTS engine failed to generate the audio it handed out. "
                    "Check the logs of the TTS engine for the reason (for a Home Assistant "
                    "engine that is the Home Assistant core log). A cloud engine may be "
                    "out of credit or having an outage."
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
