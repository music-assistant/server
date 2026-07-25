"""
TeddyCloud MusicProvider implementation.

Enumerates TeddyCloud content via the HTTP API (``/api/getTagIndex``) and exposes each
Tonie as an **Audiobook** with chapters. Chapter marks come from the tag's
``trackSeconds`` (real Ogg-page boundaries in the TAF); names from ``tonieInfo.tracks``
where available.

Total duration: TeddyCloud's API doesn't report it, so we derive it accurately from the
TAF header. The header (first 4096 bytes of the raw TAF) carries ``num_bytes`` (total
Opus byte length) and ``track_page_nums`` (each chapter's 4096-byte page offset). The
last chapter's page offset ÷ its start time gives the file's own byte-rate, so
``total ≈ num_bytes * last_chapter_time / (last_page * 4096)`` — accurate to well under a
second, using only a 4 KB read per tonie (cached).

Audio streams straight from TeddyCloud as clean OGG/Opus (``skip_header=true`` makes the
server strip the 4096-byte TAF header), so Music Assistant/ffmpeg can seek.
"""

from __future__ import annotations

import time
from contextlib import suppress
from typing import TYPE_CHECKING, Any

import aiohttp
from music_assistant_models.enums import (
    ContentType,
    ImageType,
    MediaType,
    StreamType,
)
from music_assistant_models.errors import LoginFailed, MediaNotFoundError
from music_assistant_models.media_items import (
    Audiobook,
    AudioFormat,
    MediaItemChapter,
    MediaItemImage,
    ProviderMapping,
    UniqueList,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.helpers.datetime import from_utc_timestamp
from music_assistant.models.music_provider import MusicProvider

from .constants import (
    CACHE_CATEGORY_DURATION,
    CONF_URL,
    CONTENT_DOWNLOAD_PATH,
    DEFAULT_ARTIST,
    DURATION_CACHE_TTL,
    TAF_HEADER_SIZE,
    TAG_CACHE_TTL,
    TAG_INDEX_PATH,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    """Read a protobuf base-128 varint; return (value, new_pos)."""
    shift = 0
    result = 0
    while pos < len(buf):
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            break
        shift += 7
    return result, pos


def _parse_taf_header(header: bytes) -> tuple[int | None, int | None]:
    """
    Parse a TAF protobuf header, returning (num_bytes, last_track_page).

    Layout: 4 big-endian bytes = protobuf length, then the TonieboxAudioFileHeader.
    We only need field 2 (num_bytes, varint) and field 4 (track_page_nums, packed
    varints) — the Opus byte length and each chapter's Ogg-page index.
    """
    if len(header) < 8:
        return None, None
    proto_len = int.from_bytes(header[0:4], "big")
    buf = header[4 : 4 + proto_len]
    pos = 0
    num_bytes: int | None = None
    last_page: int | None = None
    while pos < len(buf):
        tag, pos = _read_varint(buf, pos)
        field_no, wire_type = tag >> 3, tag & 0x07
        if wire_type == 0:  # varint
            value, pos = _read_varint(buf, pos)
            if field_no == 2:
                num_bytes = value
        elif wire_type == 2:  # length-delimited
            length, pos = _read_varint(buf, pos)
            chunk = buf[pos : pos + length]
            pos += length
            if field_no == 4 and chunk:  # packed uint32 page numbers
                cpos = 0
                while cpos < len(chunk):
                    page, cpos = _read_varint(chunk, cpos)
                    last_page = page
        elif wire_type == 1:  # 64-bit
            pos += 8
        elif wire_type == 5:  # 32-bit
            pos += 4
        else:
            break
    return num_bytes, last_page


class TeddyCloudProvider(MusicProvider):
    """Music provider that exposes TeddyCloud (Toniebox) content as audiobooks."""

    async def handle_async_init(self) -> None:
        """Read config, prepare the HTTP client and verify connectivity."""
        base_url = str(self.get_setup_value(CONF_URL)).rstrip("/")
        # accept a bare host/IP and default to http:// (TeddyCloud is usually plain HTTP on
        # the local network); an explicit http(s):// scheme is always respected.
        if not base_url.startswith(("http://", "https://")):
            base_url = f"http://{base_url}"
        self.base_url = base_url
        # small in-memory cache of the parsed tag index (uid -> raw tag dict)
        self._tags: dict[str, dict[str, Any]] = {}
        self._tags_fetched_at: float = 0.0

        # verify we can reach the server and that the tag index is well-formed
        try:
            await self._fetch_tags(force=True)
        except (MediaNotFoundError, aiohttp.ClientError) as err:
            raise LoginFailed(
                f"Cannot reach TeddyCloud at {self.base_url}: {err}",
                translation_key="login_failed",
                translation_owner=self.translation_owner,
                translation_args=[self.base_url],
            ) from err

    @property
    def is_streaming_provider(self) -> bool:
        """Return False: TeddyCloud is a self-hosted library, catalog equals contents."""
        return False

    async def get_library_audiobooks(self) -> AsyncGenerator[Audiobook]:
        """Yield every Tonie as an Audiobook."""
        for tag in (await self._fetch_tags()).values():
            total = await self._total_seconds(tag)
            yield self._parse_audiobook(tag, total)

    async def get_audiobook(self, prov_audiobook_id: str) -> Audiobook:
        """Get full audiobook details by id."""
        tag = await self._get_tag(prov_audiobook_id)
        total = await self._total_seconds(tag)
        return self._parse_audiobook(tag, total)

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Report how to stream a Tonie: seekable OGG/Opus over HTTP."""
        tag = await self._get_tag(item_id)
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(content_type=ContentType.OGG),
            media_type=MediaType.AUDIOBOOK,
            stream_type=StreamType.HTTP,
            path=self._stream_url(tag),
            allow_seek=True,
            can_seek=True,
        )

    async def resolve_image(self, path: str) -> str | bytes:
        """Resolve a (teddycloud-hosted) image path to an absolute URL."""
        return self._abs_url(path)

    # ------------------------------------------------------------------ #
    # HTTP helpers
    # ------------------------------------------------------------------ #

    @property
    def _session(self) -> aiohttp.ClientSession:
        """Return the shared aiohttp session."""
        return self.mass.http_session

    def _abs_url(self, path_or_url: str) -> str:
        """Turn a possibly-relative TeddyCloud path into an absolute URL."""
        if path_or_url.startswith(("http://", "https://")):
            return path_or_url
        if not path_or_url.startswith("/"):
            path_or_url = "/" + path_or_url
        return f"{self.base_url}{path_or_url}"

    def _content_url(self, tag: dict[str, Any]) -> str:
        """
        Absolute URL of the tag's RAW content (the full TAF, header intact).

        We deliberately strip ``skip_header`` here so this yields the raw TAF (needed to
        read the protobuf header for the duration). ``_stream_url`` re-adds it for playback.
        """
        audio_url = str(tag.get("audioUrl") or "")
        if not audio_url:
            source = str(tag.get("source") or "")
            if not source:
                raise MediaNotFoundError(f"Tonie {tag.get('uid')} has no audio source")
            if not source.startswith("/"):
                source = "/" + source
            audio_url = f"{CONTENT_DOWNLOAD_PATH}{source}"
        audio_url = audio_url.replace("skip_header=true", "")
        audio_url = audio_url.replace("?&", "?").replace("&&", "&").rstrip("?&")
        return self._abs_url(audio_url)

    async def _fetch_tags(self, *, force: bool = False) -> dict[str, dict[str, Any]]:
        """Fetch and cache the parsed tag index keyed by uid."""
        now = time.monotonic()
        if not force and self._tags and (now - self._tags_fetched_at) < TAG_CACHE_TTL:
            return self._tags

        url = f"{self.base_url}{TAG_INDEX_PATH}"
        async with self._session.get(url) as resp:
            resp.raise_for_status()
            payload = await resp.json(content_type=None)

        # getTagIndex always returns an object with a "tags" array.
        raw_tags = payload.get("tags") if isinstance(payload, dict) else None
        if not isinstance(raw_tags, list):
            raise MediaNotFoundError("Unexpected response from TeddyCloud getTagIndex")

        tags: dict[str, dict[str, Any]] = {}
        for tag in raw_tags:
            if not isinstance(tag, dict):
                continue
            uid = tag.get("uid") or tag.get("ruid")
            if not uid or not self._is_playable(tag):
                continue
            # normalise: downstream code (e.g. _parse_audiobook) reads tag["uid"] directly,
            # so make sure it is always present as a string even for ruid-only tags.
            uid = str(uid)
            tag["uid"] = uid
            tags[uid] = tag

        self._tags = tags
        self._tags_fetched_at = now
        self.logger.debug("Fetched %s playable tag(s) from TeddyCloud", len(tags))
        return tags

    @staticmethod
    def _is_playable(tag: dict[str, Any]) -> bool:
        """Return True if the tag points at real, downloadable local audio."""
        if tag.get("exists") is False or tag.get("valid") is False:
            return False
        if tag.get("live") is True:
            return False
        return bool(tag.get("audioUrl") or tag.get("source"))

    async def _get_tag(self, uid: str) -> dict[str, Any]:
        """Look up a single tag by uid, refetching the index if needed."""
        tags = await self._fetch_tags()
        if uid not in tags:
            tags = await self._fetch_tags(force=True)
        if uid not in tags:
            raise MediaNotFoundError(f"Tonie {uid} not found in TeddyCloud")
        return tags[uid]

    async def _total_seconds(self, tag: dict[str, Any]) -> int | None:
        """
        Derive the tonie's total duration from its TAF header (persistently cached).

        Reads only the 4096-byte header and combines num_bytes + the last chapter's page
        offset with its start time (the file's own byte-rate). The result is stored in
        ``mass.cache`` keyed by the immutable audio_id, so it survives restarts and each
        unique content is probed at most once. Best-effort: on any failure we return None
        and the caller falls back to the last chapter's start.
        """
        cache_key = self._audio_id(tag)
        if not cache_key:
            return None
        cached = await self.mass.cache.get(
            key=cache_key, provider=self.instance_id, category=CACHE_CATEGORY_DURATION
        )
        if cached is not None:
            return int(cached)
        seconds = tag.get("trackSeconds")
        if not isinstance(seconds, list) or not seconds or not seconds[-1]:
            return None
        try:
            timeout = aiohttp.ClientTimeout(total=20)
            async with self._session.get(self._content_url(tag), timeout=timeout) as resp:
                resp.raise_for_status()
                header = await resp.content.readexactly(TAF_HEADER_SIZE)
        except (aiohttp.ClientError, TimeoutError, EOFError) as err:
            self.logger.debug("Duration probe failed for %s: %s", cache_key, err)
            return None
        num_bytes, last_page = _parse_taf_header(header)
        if not num_bytes or not last_page:
            return None
        last_time = float(seconds[-1])
        total = round(num_bytes * last_time / (last_page * TAF_HEADER_SIZE))
        if total < last_time:
            return None
        await self.mass.cache.set(
            key=cache_key,
            data=total,
            provider=self.instance_id,
            category=CACHE_CATEGORY_DURATION,
            expiration=DURATION_CACHE_TTL,
        )
        return total

    # ------------------------------------------------------------------ #
    # parsing helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _audio_id(tag: dict[str, Any]) -> str:
        """
        Return the tonie's content id from its source path (e.g. .../audioID/1615475317.taf).

        This identifies the immutable audio content, so it's used as the duration cache key.
        Falls back to the tag uid when no source path is available.
        """
        audio_id = str(tag.get("source") or "").rsplit("/", 1)[-1].removesuffix(".taf")
        return audio_id or str(tag.get("uid") or "")

    @staticmethod
    def _info(tag: dict[str, Any], key: str) -> Any:
        """Read a field from tonieInfo, falling back to the top level."""
        info = tag.get("tonieInfo")
        if isinstance(info, dict) and info.get(key) not in (None, ""):
            return info[key]
        return tag.get(key)

    def _series_name(self, tag: dict[str, Any]) -> str:
        return str(self._info(tag, "series") or DEFAULT_ARTIST)

    def _title(self, tag: dict[str, Any]) -> str:
        episode = self._info(tag, "episode")
        series = self._info(tag, "series")
        return str(episode or series or tag.get("uid") or "Unknown Tonie")

    def _images(self, tag: dict[str, Any]) -> UniqueList[MediaItemImage]:
        picture = self._info(tag, "picture")
        if not picture:
            return UniqueList()
        return UniqueList(
            [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=self._abs_url(str(picture)),
                    provider=self.instance_id,
                    remotely_accessible=str(picture).startswith(("http://", "https://")),
                )
            ]
        )

    def _chapters(self, tag: dict[str, Any], total_seconds: int | None) -> list[MediaItemChapter]:
        """
        Build chapter markers from trackSeconds, aligned to tonieInfo.tracks names.

        ``trackSeconds`` are the TAF's real chapter marks (often uniform ~3-min splits);
        ``tonieInfo.tracks`` are the (fewer) story names from tonies.json, without
        timestamps. We only put a name where we're confident of its position:

          * counts match  -> one named chapter per mark (1:1);
          * marks are an exact multiple of names -> collapse to the named "stories",
            each starting at its evenly-grouped mark (e.g. 16 marks / 4 names -> 4
            chapters at marks 0,4,8,12);
          * otherwise -> expose every mark with generic "Chapter N" (never guess a name
            onto the wrong mark).

        The last chapter's end is the derived total duration so every chapter is bounded.
        """
        seconds = tag.get("trackSeconds")
        if not isinstance(seconds, list) or not seconds:
            return []
        names = self._info(tag, "tracks")
        names = names if isinstance(names, list) else []
        n_marks, n_names = len(seconds), len(names)

        if n_names and n_names == n_marks:
            # perfect 1:1 alignment
            mark_indices: list[int] = list(range(n_marks))
            labels: list[Any] | None = names
        elif n_names and n_marks > n_names and n_marks % n_names == 0:
            # collapse the uniform marks into the named stories
            group = n_marks // n_names
            mark_indices = [i * group for i in range(n_names)]
            labels = names
        else:
            # can't align names to marks reliably -> keep every mark, no (wrong) names
            mark_indices = list(range(n_marks))
            labels = None

        chapters: list[MediaItemChapter] = []
        for pos, idx in enumerate(mark_indices):
            start = seconds[idx]
            if pos + 1 < len(mark_indices):
                end: float | None = float(seconds[mark_indices[pos + 1]])
            elif total_seconds and total_seconds > start:
                end = float(total_seconds)
            else:
                end = None
            if labels is not None and pos < len(labels) and labels[pos]:
                name = str(labels[pos])
            else:
                name = f"Chapter {pos + 1}"
            chapters.append(
                MediaItemChapter(position=pos + 1, name=name, start=float(start), end=end)
            )
        return chapters

    def _parse_audiobook(self, tag: dict[str, Any], total_seconds: int | None = None) -> Audiobook:
        uid = str(tag["uid"])
        series = self._series_name(tag)
        book = Audiobook(
            item_id=uid,
            provider=self.instance_id,
            name=self._title(tag),
            publisher=None if series == DEFAULT_ARTIST else series,
            provider_mappings={
                ProviderMapping(
                    item_id=uid,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    available=True,
                    audio_format=AudioFormat(content_type=ContentType.OGG),
                )
            },
        )
        # authors must be plain strings (we do not declare AUTHOR_AUDIOBOOKS)
        book.authors.set([series])
        book.metadata.images = self._images(tag)
        language = self._info(tag, "language")
        if language:
            book.metadata.languages = UniqueList([str(language)])
        chapters = self._chapters(tag, total_seconds)
        if chapters:
            book.metadata.chapters = chapters
        if total_seconds:
            book.duration = total_seconds
        elif chapters:
            book.duration = int(chapters[-1].start)
        # The tonie's audio_id is a unix timestamp of when the content was created, which
        # makes a meaningful date_added.
        audio_id = self._audio_id(tag)
        if audio_id.isdigit():
            with suppress(ValueError, OSError, OverflowError):
                book.date_added = from_utc_timestamp(int(audio_id))
        return book

    def _stream_url(self, tag: dict[str, Any]) -> str:
        """Return an absolute URL that serves the tonie as clean, seekable OGG/Opus."""
        url = self._content_url(tag)
        if "skip_header=" not in url:
            url += ("&" if "?" in url else "?") + "skip_header=true"
        return url
