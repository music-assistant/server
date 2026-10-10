"""Audio pipeline for redirect and independent stream delivery."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlsplit, urlunsplit

from aiohttp import web
from music_assistant_models.enums import ContentType, MediaType
from music_assistant_models.errors import MusicAssistantError
from music_assistant_models.media_items import AudioFormat

from music_assistant.controllers.streams.audio_processing import get_media_session_id
from music_assistant.controllers.streams.constants import PacingProfile, output_pacing_args
from music_assistant.helpers.ffmpeg import get_ffmpeg_stream

from .constants import PRE_BUFFER_BYTES

if TYPE_CHECKING:
    from music_assistant_models.player import PlayerMedia

    from music_assistant.helpers.dsp import ComplexFilter

    from .player import MSXPlayer
    from .provider import MSXBridgeProvider

logger = logging.getLogger(__name__)


class AudioPipeline:
    """Serve encoded audio for one request using redirect or independent mode."""

    def __init__(self, provider: MSXBridgeProvider) -> None:
        """Initialize the pipeline."""
        self.provider = provider
        self.active_stream_tasks: dict[str, set[asyncio.Task[None]]] = {}
        self.active_stream_transports: dict[str, set[Any]] = {}

    def cancel_streams_for_player(self, player_id: str) -> None:
        """Cancel stream tasks and abort connections for the given player."""
        tasks = self.active_stream_tasks.pop(player_id, set())
        transports = self.active_stream_transports.pop(player_id, set())
        for task in tasks:
            if not task.done():
                task.cancel()
        for transport in transports:
            with contextlib.suppress(OSError, RuntimeError):
                transport.abort()
        if tasks or transports:
            logger.debug(
                "Cancelled %d task(s), aborted %d transport(s) for player %s",
                len(tasks),
                len(transports),
                player_id,
            )

    async def serve(
        self,
        request: web.Request,
        player: MSXPlayer,
        media: PlayerMedia,
    ) -> web.StreamResponse:
        """Serve this player's current media on this request."""
        player_id = player.player_id
        if not player.config.enabled:
            raise web.HTTPNotFound(text="Player not found")

        if self.provider.is_redirect_stream_mode():
            redirect_url = await self.provider.get_ma_stream_url(player_id, media)
            if not player.config.enabled:
                raise web.HTTPNotFound(text="Player not found")
            if redirect_url:
                redirect_url = rewrite_stream_host(request, redirect_url)
                logger.info(
                    "[StreamMode:redirect] Player %s -> MA Streamserver: %s",
                    player_id,
                    redirect_url,
                )
                raise web.HTTPFound(location=redirect_url)
            logger.warning(
                "[StreamMode:redirect] Failed to get MA URL for %s, "
                "falling back to independent mode",
                player_id,
            )

        effective_format = cast(
            "str",
            player.config.get_value("output_codec", player.output_format),
        )

        pcm_format, out_format, headers = build_audio_params(effective_format)

        logger.debug(
            "[StreamMode:independent] Serving audio %s: format=%s",
            player_id,
            effective_format,
        )
        return await self.serve_independent(request, player, media, pcm_format, out_format, headers)

    async def serve_independent(
        self,
        request: web.Request,
        player: MSXPlayer,
        media: PlayerMedia,
        pcm_format: AudioFormat,
        out_format: AudioFormat,
        headers: dict[str, str],
    ) -> web.StreamResponse:
        """Serve audio via independent ffmpeg stream."""
        player_id = player.player_id
        audio_source = self.provider.mass.streams.get_stream(
            media,
            pcm_format,
            force_flow_mode=False,
        )
        output_plan = self.provider.mass.streams.audio.get_player_output_plan(
            player_id,
            pcm_format,
            out_format,
            queue_id=media.source_id,
            session_id=get_media_session_id(media),
            queue_item_id=media.queue_item_id,
        )

        response = web.StreamResponse(status=200, headers=headers)
        stream_task: asyncio.Task[None] = asyncio.create_task(
            self.stream_with_prebuffer(
                request,
                response,
                player,
                audio_source,
                pcm_format,
                out_format,
                output_plan.filter_params,
                self._get_pacing_profile(media),
            )
        )
        transport = request.transport
        try:
            await self.run_stream_task(player_id, stream_task, transport)
        except MusicAssistantError, OSError:
            if not response.prepared:
                raise web.HTTPServiceUnavailable(text="Unable to start audio stream") from None
            raise
        finally:
            if (
                response.prepared
                and stream_task.done()
                and not stream_task.cancelled()
                and stream_task.exception() is not None
                and transport is not None
            ):
                transport.abort()
        return response

    async def stream_with_prebuffer(
        self,
        request: web.Request,
        response: web.StreamResponse,
        player: MSXPlayer,
        audio_source: Any,
        pcm_format: AudioFormat,
        out_format: AudioFormat,
        filter_params: Sequence[str | ComplexFilter],
        pacing_profile: PacingProfile,
    ) -> None:
        """Pre-buffer audio chunks, then send HTTP headers and stream remaining data."""
        player_id = player.player_id
        chunk_queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=32)
        producer_done = asyncio.Event()

        async def producer() -> None:
            try:
                async for chunk in get_ffmpeg_stream(
                    audio_input=audio_source,
                    input_format=pcm_format,
                    output_format=out_format,
                    filter_params=filter_params,
                    extra_input_args=output_pacing_args(pacing_profile),
                ):
                    await chunk_queue.put(chunk)
            finally:
                producer_done.set()
                _signal_eof(chunk_queue)

        producer_task: asyncio.Task[None] | None = None
        total_bytes = 0
        try:
            producer_task = asyncio.create_task(producer())
            pre_buffer, ended = await _collect_prebuffer(chunk_queue, producer_done, producer_task)

            if not player.current_media and not pre_buffer:
                return

            await response.prepare(request)
            for buf_chunk in pre_buffer:
                await response.write(buf_chunk)
                total_bytes += len(buf_chunk)

            if ended:
                return

            while True:
                try:
                    chunk = await asyncio.wait_for(chunk_queue.get(), timeout=0.5)
                except TimeoutError:
                    if producer_done.is_set():
                        break
                    continue
                if chunk is None:
                    break
                await response.write(chunk)
                total_bytes += len(chunk)
        except ConnectionResetError, BrokenPipeError, ConnectionAbortedError:
            logger.debug("Client disconnected from stream %s", player_id)
        except asyncio.CancelledError:
            logger.debug("Stream cancelled for player %s", player_id)
            raise
        finally:
            if producer_task is not None:
                if not producer_task.done():
                    producer_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await producer_task
            logger.debug("Stream %s finished: wrote %d bytes", player_id, total_bytes)

    async def run_stream_task(
        self,
        player_id: str,
        stream_task: asyncio.Task[None],
        transport: Any,
    ) -> None:
        """Run a stream task with registration and error handling."""
        self.register_stream(player_id, stream_task, transport)
        try:
            await stream_task
        except asyncio.CancelledError:
            raise
        except MusicAssistantError, OSError:
            logger.exception("Stream error for player %s", player_id)
            raise
        finally:
            self.unregister_stream(player_id, stream_task, transport)

    def register_stream(self, player_id: str, task: asyncio.Task[None], transport: Any) -> None:
        """Register active stream task and transport for cancel on stop."""
        if player_id not in self.active_stream_tasks:
            self.active_stream_tasks[player_id] = set()
            self.active_stream_transports[player_id] = set()
        if task:
            self.active_stream_tasks[player_id].add(task)
        if transport:
            self.active_stream_transports[player_id].add(transport)

    def unregister_stream(self, player_id: str, task: asyncio.Task[None], transport: Any) -> None:
        """Unregister stream when done (from finally block)."""
        if player_id not in self.active_stream_tasks:
            return
        if task:
            self.active_stream_tasks[player_id].discard(task)
        if transport:
            self.active_stream_transports[player_id].discard(transport)
        if not self.active_stream_tasks[player_id]:
            del self.active_stream_tasks[player_id]
            del self.active_stream_transports[player_id]

    def _get_pacing_profile(self, media: PlayerMedia) -> PacingProfile:
        """Return the pacing profile for the audio delivered to the TV."""
        if media.media_type == MediaType.AUDIO_SOURCE:
            return PacingProfile.LOW_LATENCY
        if media.media_type in (MediaType.RADIO, MediaType.FLOW_STREAM):
            return PacingProfile.NEAR_REALTIME
        if media.source_id and media.queue_item_id:
            item = self.provider.mass.player_queues.get_item(media.source_id, media.queue_item_id)
            if item and item.streamdetails and item.streamdetails.is_realtime:
                return PacingProfile.NEAR_REALTIME
        return PacingProfile.DEFAULT


def rewrite_stream_host(request: web.Request, url: str) -> str:
    """Point a stream URL at the host the client already uses to reach us."""
    client_host = request.url.host
    if not client_host:
        return url
    parts = urlsplit(url)
    if ":" in client_host:
        client_host = f"[{client_host}]"
    netloc = f"{client_host}:{parts.port}" if parts.port else client_host
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def build_audio_params(output_format_str: str) -> tuple[AudioFormat, AudioFormat, dict[str, str]]:
    """Build PCM input format, encoded output format, and HTTP headers."""
    pcm_format = AudioFormat(
        content_type=ContentType.PCM_S16LE,
        sample_rate=44100,
        bit_depth=16,
        channels=2,
    )
    content_type_map: dict[str, tuple[ContentType, str]] = {
        "mp3": (ContentType.MP3, "audio/mpeg"),
        "aac": (ContentType.AAC, "audio/aac"),
        "flac": (ContentType.FLAC, "audio/flac"),
    }
    codec, mime_type = content_type_map.get(output_format_str, (ContentType.MP3, "audio/mpeg"))
    out_format = AudioFormat(
        content_type=codec,
        sample_rate=44100,
        bit_depth=16,
        channels=2,
    )
    headers: dict[str, str] = {
        "Content-Type": mime_type,
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "Accept-Ranges": "none",
    }
    # Streaming encoders add headers/padding and AAC may use variable bitrate,
    # so the encoded length is unknown: never advertise a Content-Length.
    return pcm_format, out_format, headers


async def _collect_prebuffer(
    chunk_queue: asyncio.Queue[bytes | None],
    done: asyncio.Event | None = None,
    producer_task: asyncio.Task[None] | None = None,
) -> tuple[list[bytes], bool]:
    """Collect audio until the buffer threshold or EOF, propagating producer failures."""
    pre_buffer: list[bytes] = []
    pre_buffer_size = 0
    ended = False
    while pre_buffer_size < PRE_BUFFER_BYTES:
        if done is None:
            chunk = await chunk_queue.get()
        else:
            try:
                chunk = await asyncio.wait_for(chunk_queue.get(), timeout=0.2)
            except TimeoutError:
                if done.is_set() and chunk_queue.empty():
                    ended = True
                    break
                continue
        if chunk is None:
            ended = True
            break
        pre_buffer.append(chunk)
        pre_buffer_size += len(chunk)
    if producer_task is not None and producer_task.done():
        producer_task.result()
    return pre_buffer, ended


def _signal_eof(queue: asyncio.Queue[bytes | None]) -> None:
    """Signal EOF to the consumer unless the queue is full."""
    with contextlib.suppress(asyncio.QueueFull):
        queue.put_nowait(None)
