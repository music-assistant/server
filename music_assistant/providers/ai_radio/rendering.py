"""Just-in-time clip rendering for AI Radio."""
# mypy: disable-error-code="attr-defined"

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import socket
import xml.etree.ElementTree as ET
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urljoin, urlsplit

import aiohttp
import defusedxml.ElementTree as DefusedET
from defusedxml.common import DefusedXmlException
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
    ATTR_RSS_FEEDS,
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
    NO_RSS_DATA_INSTRUCTION,
    NO_WEATHER_DATA_INSTRUCTION,
    RSS_CACHE_CATEGORY,
    RSS_CACHE_TTL,
    RSS_DEFAULT_MAX_ARTICLES,
    RSS_MAX_ARTICLE_CHARS,
    RSS_MAX_CONCURRENT_FETCHES,
    RSS_MAX_FEED_BYTES,
    RSS_MAX_FEEDS_PER_SECTION,
    RSS_MAX_MAX_ARTICLES,
    RSS_MAX_REDIRECTS,
    RSS_MAX_TOTAL_CHARS,
    RSS_MIN_MAX_ARTICLES,
    RSS_REQUEST_TIMEOUT,
    TTS_CLIP_PCM_FORMAT,
    TTS_PEAK_CEILING_DB,
    TTS_SERVER_ERROR_MARKERS,
    TTS_SPEECHNORM_FILTER,
    WEATHER_PLACEHOLDER_TOKENS,
)
from .helpers import coerce_int, format_ai_radio_timestamp, soft_limit_text

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.enums import MediaType
    from music_assistant_models.queue_item import QueueItem

    from music_assistant.mass import MusicAssistant

    from .models import SessionState


# Matches the generic RSS placeholder (``<rss_feed>``) as well as the per-section
# variants (``<rss_feed_0>``, ``<rss_feed_1>``, ...) used when several sections are
# merged into a single prompt.
_RSS_TOKEN_RE = re.compile(r"<rss_feed(?:_\d+)?>")


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
        deferred = await self._resolve_deferred_placeholders(prompt, queue_item=queue_item)
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

    async def _resolve_deferred_placeholders(
        self, prompt: str, queue_item: QueueItem | None = None
    ) -> dict[str, str]:
        """Return freshly resolved values for the placeholders deferred until airtime."""
        values = dict.fromkeys(DEFERRED_PLACEHOLDERS, "")
        values["<timestamp>"] = format_ai_radio_timestamp(self._configured_now())
        # weather is the only deferred placeholder that costs a network round-trip, so it is
        # only fetched when the prompt actually references it
        if any(token in prompt for token in WEATHER_PLACEHOLDER_TOKENS):
            values.update(await self._prepare_weather_tokens())
        # RSS is likewise fetched only when referenced; each token (bare "<rss_feed>" for a
        # standalone section, or "<rss_feed_N>" for a merged one) resolves against its own feeds so
        # articles stay attached to the section that requested them
        rss_tokens = _rss_tokens_in(prompt)
        if rss_tokens:
            feeds_by_token = (
                self._decode_rss_feed_map(queue_item.extra_attributes.get(ATTR_RSS_FEEDS))
                if queue_item is not None
                else {}
            )
            values.update(await self._resolve_rss_tokens(rss_tokens, feeds_by_token))
        return values

    async def _resolve_rss_tokens(
        self, rss_tokens: list[str], feeds_by_token: dict[str, list[dict[str, Any]]]
    ) -> dict[str, str]:
        """
        Resolve every RSS token in a clip concurrently under one shared budget.

        A merged clip can reference several sections (``<rss_feed_0>``, ``<rss_feed_1>``, ...), each
        with its own feeds. Resolving them sequentially meant one slow section's feed timeout stacked
        onto the next, so every token is now fetched concurrently. All feeds across all tokens share
        one concurrency limit, so a clip still cannot open more than RSS_MAX_CONCURRENT_FETCHES
        connections no matter how many sections it merges. The combined article text is capped at
        RSS_MAX_TOTAL_CHARS so a station wiring up many feeds cannot balloon the prompt (the per-feed
        and per-article caps only bound one feed at a time).
        """
        # one semaphore shared across the whole clip, so N merged sections cannot each open their
        # own RSS_MAX_CONCURRENT_FETCHES connections
        semaphore = asyncio.Semaphore(RSS_MAX_CONCURRENT_FETCHES)
        tokens = [(token, feeds_by_token.get(token) or []) for token in rss_tokens]

        async def resolve_one(feeds: list[dict[str, Any]]) -> str:
            return await self._fetch_rss_content(feeds, semaphore) if feeds else ""

        texts = await asyncio.gather(*(resolve_one(feeds) for _token, feeds in tokens))
        resolved: dict[str, str] = {}
        remaining = RSS_MAX_TOTAL_CHARS
        for (token, _feeds), text in zip(tokens, texts, strict=True):
            clipped = _clip_to_budget(text, remaining) if text else ""
            remaining -= len(clipped)
            resolved[token] = clipped or NO_RSS_DATA_INSTRUCTION
        return resolved

    def _decode_rss_feed_map(self, raw: Any) -> dict[str, list[dict[str, Any]]]:
        """
        Decode the per-token feed map stored on a queue item, tolerating malformed data.

        Only JSON decoding and shape errors are swallowed here; anything else should surface as a
        real defect rather than being silently treated as "no feeds".
        """
        if not raw:
            return {}
        try:
            decoded = json.loads(cast("str", raw))
        except (TypeError, ValueError) as err:  # ValueError also covers json.JSONDecodeError
            self.logger.warning("Could not decode RSS feed map: %s", err)
            return {}
        if not isinstance(decoded, dict):
            self.logger.warning(
                "RSS feed map has unexpected shape %r; ignoring", type(decoded).__name__
            )
            return {}
        result: dict[str, list[dict[str, Any]]] = {}
        for token, feeds in decoded.items():
            if isinstance(feeds, list):
                result[str(token)] = [feed for feed in feeds if isinstance(feed, dict)]
        return result

    async def _fetch_rss_content(
        self, feeds: list[dict[str, Any]], semaphore: asyncio.Semaphore | None = None
    ) -> str:
        """Fetch RSS/Atom feeds and return formatted article text for LLM context."""
        # never fan out beyond the section cap, even if a stale or hand-edited config slips through
        feeds = feeds[:RSS_MAX_FEEDS_PER_SECTION]
        # bound concurrency so a single clip cannot drain the shared HTTP pool; when several sections
        # are resolved together they pass a shared semaphore so the limit spans the whole clip
        if semaphore is None:
            semaphore = asyncio.Semaphore(RSS_MAX_CONCURRENT_FETCHES)

        async def fetch_one(feed: dict[str, Any]) -> str:
            url = str(feed.get("url", "")).strip()
            if not url:
                return ""
            # re-clamp at render time too: storage normalizes on write, but a legacy or hand-edited
            # config can still carry an out-of-range value, and a negative count would slice from
            # the tail and silently include the wrong articles
            max_articles = max(
                RSS_MIN_MAX_ARTICLES,
                min(
                    RSS_MAX_MAX_ARTICLES,
                    coerce_int(feed.get("max_articles"), RSS_DEFAULT_MAX_ARTICLES),
                ),
            )
            async with semaphore:
                xml_text = await self._fetch_feed_document(url)
            if not xml_text:
                return ""
            # _parse_rss_articles swallows malformed-XML errors itself; anything else is a genuine
            # defect and is left to surface rather than being masked as "no articles"
            return _parse_rss_articles(xml_text, max_articles)

        results = await asyncio.gather(*(fetch_one(feed) for feed in feeds))
        parts = [result.strip() for result in results if isinstance(result, str) and result.strip()]
        return "\n\n".join(parts)

    async def _fetch_feed_document(self, url: str) -> str:
        """
        Return the raw feed document for a URL, served from a short-lived cache when possible.

        Back-to-back clips that reference the same feed reuse one download instead of repeatedly
        hitting the feed server, and the response is size-capped so a runaway feed cannot exhaust
        memory or bloat the prompt.
        """
        cached = await self.mass.cache.get(
            url, provider=self.instance_id, category=RSS_CACHE_CATEGORY, default=None
        )
        if isinstance(cached, str):
            return cached
        xml_text = await self._download_feed(url)
        if not xml_text:
            return ""
        await self.mass.cache.set(
            url,
            xml_text,
            expiration=RSS_CACHE_TTL,
            provider=self.instance_id,
            category=RSS_CACHE_CATEGORY,
        )
        return xml_text

    async def _download_feed(self, url: str) -> str:
        """
        Download a feed document, following redirects manually so every hop is SSRF-validated.

        Feed URLs are operator-supplied, but a malicious or compromised feed server can still answer
        with a redirect pointing at a private, loopback or link-local address (cloud metadata at
        169.254.169.254, internal admin panels, ...). aiohttp would follow those blindly, so redirect
        handling is disabled and done by hand: the target host of every hop is resolved and checked
        against the public-address allow-list before it is fetched.

        Known limitation: this closes the redirect-based SSRF vector, but a TOCTOU / DNS-rebinding
        gap remains -- aiohttp re-resolves the host when it opens the socket, so a name that resolves
        to a public address during validation could resolve to a private one an instant later.
        Fully closing that needs a connector pinned to the validated IP, which is out of scope for
        this change and inconsistent with how the rest of the codebase fetches user-supplied URLs.
        """
        current = url
        for _hop in range(RSS_MAX_REDIRECTS + 1):
            if not await self._is_public_http_url(current):
                self.logger.warning(
                    "Refusing to fetch RSS feed at non-public or unsupported address: %s", current
                )
                return ""
            try:
                async with self.mass.http_session.get(
                    current,
                    timeout=aiohttp.ClientTimeout(total=RSS_REQUEST_TIMEOUT),
                    headers={"User-Agent": "MusicAssistant/AIRadio RSS Reader"},
                    allow_redirects=False,
                ) as resp:
                    if resp.status in (301, 302, 303, 307, 308):
                        location = resp.headers.get("Location")
                        if not location:
                            self.logger.warning(
                                "RSS feed %s returned a redirect without a Location header", current
                            )
                            return ""
                        # resolve relative redirects against the current URL and re-validate the hop
                        current = urljoin(current, location)
                        continue
                    if resp.status != 200:
                        self.logger.warning("RSS feed %s returned HTTP %s", current, resp.status)
                        return ""
                    # read in chunks and stop once the cap is exceeded, so memory stays bounded even
                    # when the server sends no Content-Length or lies about it
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in resp.content.iter_chunked(65536):
                        chunks.append(chunk)
                        total += len(chunk)
                        if total > RSS_MAX_FEED_BYTES:
                            self.logger.warning(
                                "RSS feed %s exceeded the %d byte limit; truncating",
                                current,
                                RSS_MAX_FEED_BYTES,
                            )
                            break
                    raw = b"".join(chunks)[:RSS_MAX_FEED_BYTES]
                    return _decode_feed_bytes(raw, resp.charset)
            except (aiohttp.ClientError, TimeoutError) as err:
                self.logger.warning("Could not fetch RSS feed %s: %s", current, err)
                return ""
        self.logger.warning("RSS feed %s exceeded the redirect limit", url)
        return ""

    async def _is_public_http_url(self, url: str) -> bool:
        """
        Return True only when url is an http(s) URL whose host resolves to public addresses.

        The scheme is restricted to http/https (so file://, ftp://, gopher://, ... are rejected) and
        the host is resolved up front; if any resolved address is private, loopback, link-local,
        multicast, reserved or unspecified the URL is refused, which blocks SSRF via a hostname that
        maps to an internal address.
        """
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            return False
        host = parts.hostname
        if not host:
            return False
        try:
            port = parts.port or (443 if parts.scheme == "https" else 80)
        except ValueError:
            return False
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(
                host, port, type=socket.SOCK_STREAM
            )
        except socket.gaierror, UnicodeError, OSError:
            return False
        if not infos:
            return False
        # sockaddr[0] is the address; typed as str | int, but always a str for AF_INET/AF_INET6
        return all(_is_public_ip(str(info[4][0])) for info in infos)

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


# matches the encoding attribute in an XML prolog, e.g. <?xml version="1.0" encoding="ISO-8859-1"?>
_XML_ENCODING_RE = re.compile(rb"""<\?xml[^>]*?encoding=["']([\w.\-]+)["']""", re.IGNORECASE)


def _decode_feed_bytes(raw: bytes, http_charset: str | None) -> str:
    # Ignore codespell warnings for common non-English characters in feeds
    # codespell:ignore caf
    """
    Decode raw feed bytes to text, honoring the feed's declared encoding.

    Forcing utf-8 corrupts feeds that legitimately declare another charset (a feed that is ISO-8859-1
    would render ``café`` as ``caf<0xef>``), so the encoding is resolved in priority order: a
    byte-order mark first, then the HTTP ``Content-Type`` charset, then the ``encoding=...`` attribute
    in the XML prolog, and finally utf-8. ``errors="replace"`` guarantees a string is always returned.
    """
    # a BOM is authoritative and wins over any declared charset
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig", errors="replace")
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    candidates: list[str] = []
    if http_charset:
        candidates.append(http_charset)
    if (match := _XML_ENCODING_RE.search(raw[:1024])) is not None:
        candidates.append(match.group(1).decode("ascii", errors="replace"))
    candidates.append("utf-8")
    for candidate in candidates:
        try:
            # errors="replace" never raises, so the first codec that actually exists is used
            return raw.decode(candidate, errors="replace")
        except LookupError:
            continue
    return raw.decode("utf-8", errors="replace")


def _is_public_ip(ip_str: str) -> bool:
    """Return True only when ip_str is a routable, public IP address."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    # an IPv4 address mapped into IPv6 (::ffff:127.0.0.1) must be judged by its embedded IPv4 form,
    # otherwise a loopback/private address tunnelled through IPv6 would look public
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local  # also covers the 169.254.169.254 cloud metadata endpoint
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _clip_to_budget(text: str, budget: int) -> str:
    """
    Trim text to at most budget characters, cutting only on an article (newline) boundary.

    Articles are newline-separated, so the text is cut back to the last whole article that fits. When
    not even one article fits (no newline within the budget) an empty string is returned rather than
    a mid-article fragment, so a nearly-exhausted budget injects nothing instead of a meaningless
    snippet. In practice a single article is far smaller than the total budget, so this only bites
    once a clip's earlier sections have already consumed almost all of it.
    """
    if budget <= 0:
        return ""
    if len(text) <= budget:
        return text
    truncated = text[:budget]
    newline = truncated.rfind("\n")
    if newline > 0:
        return truncated[:newline].rstrip()
    # no whole article fits in the remaining budget -> add nothing rather than a partial line
    return ""


def _rss_tokens_in(prompt: str) -> list[str]:
    """
    Return the RSS placeholder tokens present in a prompt, de-duplicated in first-seen order.

    Matches the bare ``<rss_feed>`` used by standalone sections as well as the ``<rss_feed_N>``
    variants a merged plan assigns per section.
    """
    seen: dict[str, None] = {}
    for match in _RSS_TOKEN_RE.findall(prompt):
        seen.setdefault(match, None)
    return list(seen)


def _parse_rss_articles(xml_text: str, max_articles: int) -> str:
    """Parse RSS 2.0 or Atom 1.0 and return a formatted article list."""
    atom_ns = "http://www.w3.org/2005/Atom"

    def _element_text(element: ET.Element | None) -> str:
        # itertext() walks nested nodes, so Atom text constructs that wrap their body in XHTML
        # child elements (e.g. <div><p>...</p></div>) still yield the full title/article text
        if element is None:
            return ""
        return "".join(element.itertext())

    def _clean(text: str | None) -> str:
        if not text:
            return ""
        # strip any HTML markup a feed may embed in titles or summaries
        cleaned = re.sub(r"<[^>]+>", "", text).strip()
        # cap per-article text so a verbose feed cannot blow up the prompt size
        if len(cleaned) > RSS_MAX_ARTICLE_CHARS:
            cleaned = cleaned[:RSS_MAX_ARTICLE_CHARS].rstrip() + "…"
        return cleaned

    def _format(title: str, body: str) -> str | None:
        title = title.strip()
        body = body.strip()
        if not title and not body:
            return None
        if not title:
            return f"- {body}"
        return f"- {title}: {body}" if body else f"- {title}"

    try:
        root = DefusedET.fromstring(xml_text)
    except ET.ParseError, DefusedXmlException:
        # ET.ParseError covers malformed XML; DefusedXmlException covers a document defused for an
        # XML attack (entity expansion, external entities, ...). Both mean "unusable feed", not a bug
        return ""

    articles: list[str] = []
    tag = root.tag.lower()

    if "rss" in tag or root.find("channel") is not None:
        # RSS 2.0
        channel = root.find("channel") if "rss" in tag else root
        items = channel.findall("item") if channel is not None else []
        for item in items[:max_articles]:
            title = _clean(_element_text(item.find("title")))
            desc = _clean(_element_text(item.find("description")))
            if (formatted := _format(title, desc)) is not None:
                articles.append(formatted)
    elif root.tag == f"{{{atom_ns}}}feed":
        # Atom 1.0
        for entry in root.findall(f"{{{atom_ns}}}entry")[:max_articles]:
            title = _clean(_element_text(entry.find(f"{{{atom_ns}}}title")))
            summary_el = entry.find(f"{{{atom_ns}}}summary")
            if summary_el is None:
                summary_el = entry.find(f"{{{atom_ns}}}content")
            summary = _clean(_element_text(summary_el))
            if (formatted := _format(title, summary)) is not None:
                articles.append(formatted)

    return "\n".join(articles)
