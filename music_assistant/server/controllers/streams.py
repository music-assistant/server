"""Controller to stream audio to players."""
from __future__ import annotations

import asyncio
import logging

import urllib.parse
from typing import TYPE_CHECKING, AsyncGenerator
from uuid import uuid4

from aiohttp import web

from music_assistant.common.models.enums import (
    ContentType,
    MetadataMode,
    StreamState,
)
from music_assistant.common.models.errors import (
    PlayerUnavailableError,
)

from music_assistant.common.models.media_items import StreamDetails
from music_assistant.common.models.player import Player


from music_assistant.constants import (
    DEFAULT_PORT,
    ROOT_LOGGER_NAME,
)
from music_assistant.server.helpers.audio import (
    check_audio_support,
    get_chunksize,
    get_media_stream,
    get_preview_stream,
    get_stream_details,
    strip_silence,
)
from music_assistant.server.helpers.process import AsyncProcess

from music_assistant.constants import CONF_WEB_HOST, CONF_WEB_PORT

if TYPE_CHECKING:
    # from music_assistant.common.models.queue_stream import QueueStream
    from music_assistant.server import MusicAssistant

LOGGER = logging.getLogger(f"{ROOT_LOGGER_NAME}.streams")

ICY_CHUNKSIZE = 8192


class StreamsController:
    """Controller to stream audio to players."""

    def __init__(self, mass: MusicAssistant):
        """Initialize instance."""
        self.mass = mass

    @property
    def base_url(self) -> str:
        """Return the base url for the stream engine."""

        host = self.mass.config.get(CONF_WEB_HOST, "127.0.0.1")
        port = self.mass.config.get(CONF_WEB_PORT, DEFAULT_PORT)
        return f"http://{host}:{port}"

    def get_stream_url(
        self,
        queue_id: str,
        queue_item_id: str,
        fmt: str = "pcm",
        seek_position: int | None = None,
        player_id: str | None = None,
    ) -> str:
        """Get stream url for the QueueItem Stream."""
        url = f"{self.base_url}/stream/{queue_id}/{queue_item_id}?fmt={fmt}"
        if seek_position:
            url += f"&seek={seek_position}"
        if player_id:
            url += f"&player_id={player_id}"
        return url

    async def get_preview_url(self, provider: str, track_id: str) -> str:
        """Return url to short preview sample."""
        enc_track_id = urllib.parse.quote(track_id)
        return f"{self.base_url}/preview?provider={provider}&item_id={enc_track_id}"

    async def setup(self) -> None:
        """Async initialize of module."""

        self.mass.webapp.router.add_get("/stream/preview", self.serve_preview)
        self.mass.webapp.router.add_get(
            "/stream/announce/{player_id}.{fmt}", self.serve_announcement
        )
        self.mass.webapp.router.add_get(
            "/stream/{queue_id}/{queue_item_id}", self.serve_queue_item
        )

        ffmpeg_present, libsoxr_support = await check_audio_support(True)
        if not ffmpeg_present:
            LOGGER.error(
                "FFmpeg binary not found on your system, playback will NOT work!."
            )
        elif not libsoxr_support:
            LOGGER.warning(
                "FFmpeg version found without libsoxr support, "
                "highest quality audio not available. "
            )

        LOGGER.info("Started stream controller")

    async def serve_queue_item(self, request: web.Request) -> web.Response:
        """Stream a single queue item."""
        queue_id = request.match_info["queue_id"]
        queue_item_id = request.match_info["queue_item_id"]
        player_id = request.query.get("player_id", queue_id)
        player = self.mass.players.get(player_id)
        player_queue = self.mass.players.queues.get(queue_id)
        if not player or not player_queue:
            raise web.HTTPNotFound(reason="player or queue not found")

        LOGGER.debug("Stream request for %s", player.name)

        queue_item = self.mass.players.queues.get_item(queue_id, queue_item_id)
        if not queue_item:
            raise web.HTTPNotFound(reason="invalid queue_item_id")

        streamdetails = await get_stream_details(self.mass, queue_item, queue_id)
        output_format_str = request.query.get("fmt", "wav").lower()
        output_format = ContentType.try_parse(output_format_str)
        if output_format_str == "wav":
            output_format_str = f"x-wav;codec=pcm;rate={streamdetails.sample_rate};"
            output_format_str += f"bitrate={streamdetails.bit_depth};channels={streamdetails.channels}"

        # prepare request
        resp = web.StreamResponse(
            status=200,
            reason="OK",
            headers={"Content-Type": f"audio/{output_format_str}"},
        )
        await resp.prepare(request)

        # start streaming
        LOGGER.debug(
            "Start streaming %s (%s) on player %s (queue %s)",
            queue_item_id,
            queue_item.name,
            player.name,
            queue_id,
        )

        ffmpeg_args = self._get_player_ffmpeg_args(streamdetails, player, output_format)
        seek_position = int(request.query.get("seek", "0"))
        pcm_fmt = ContentType.from_bit_depth(streamdetails.bit_depth)
        async with AsyncProcess(ffmpeg_args, True) as ffmpeg_proc:

            # feed stdin with pcm audio chunks from origin
            async def read_audio():
                async for _, audio_chunk in get_media_stream(
                    mass,
                    streamdetails,
                    pcm_fmt,
                    streamdetails.sample_rate,
                    streamdetails.channels,
                    seek_position=seek_position,
                ):
                    await resp.write(audio_chunk)

            ffmpeg_proc.attach_task(read_audio())

            # read final chunks from stdout
            async for chunk in ffmpeg_proc.iter_any():
                await resp.write(chunk)

        LOGGER.debug(
            "Finished streaming %s (%s) on player %s (queue %s)",
            queue_item_id,
            queue_item.name,
            player.name,
            queue_id,
        )

        return

    async def serve_preview(self, request: web.Request):
        """Serve short preview sample."""
        provider_mapping = request.query["provider_mapping"]
        item_id = urllib.parse.unquote(request.query["item_id"])
        resp = web.StreamResponse(
            status=200, reason="OK", headers={"Content-Type": "audio/mp3"}
        )
        await resp.prepare(request)
        async for chunk in get_preview_stream(self.mass, provider_mapping, item_id):
            await resp.write(chunk)
        return resp

    async def serve_announcement(self, request: web.Request):
        """Serve announcement broadcast."""
        player_id = request.match_info["player_id"]
        fmt = ContentType.try_parse(request.match_info["fmt"])
        urls = self.announcements[player_id]

        ffmpeg_args = ["ffmpeg", "-hide_banner", "-loglevel", "quiet"]
        for url in urls:
            ffmpeg_args += ["-i", url]
        if len(urls) > 1:
            ffmpeg_args += [
                "-filter_complex",
                f"[0:a][1:a]concat=n={len(urls)}:v=0:a=1",
            ]
        ffmpeg_args += ["-f", fmt, "-"]

        async with AsyncProcess(ffmpeg_args) as ffmpeg_proc:
            output, _ = await ffmpeg_proc.communicate()

        return web.Response(body=output, headers={"Content-Type": f"audio/{fmt}"})

    async def _get_player_stream(
        self,
        stream: QueueStreamInfo,
        player: Player,
        output_format: ContentType,
        chunksize: int,
    ) -> AsyncGenerator[bytes, None]:
        """Get the (encoded) player specific audiostream."""
        client_id = player.player_id
        if client_id in stream.listeners:
            # multiple connections from the same player
            # this probably means a client which does multiple GET requests (e.g. Kodi, Vlc)
            LOGGER.warning(
                "Simultanuous connections detected from %s, playback may be disturbed!",
                client_id,
            )
            client_id += uuid4().hex

        done = asyncio.Event()
        ffmpeg_args = self._get_player_ffmpeg_args(stream, player, output_format)

        async with AsyncProcess(ffmpeg_args, True) as ffmpeg_proc:
            # the player subscribes to the callback of raw PCM audio chunks
            # which are fed into an ffmpeg instance for the player to do all player-specific
            # adjustments like DSP/equalizer/convolution etc. which are just effects in the
            # ffmpeg chain.
            async def audio_callback(chunk: bytes) -> None:
                """Handle incoming pcm audio chunk from the queue stream."""
                if chunk == b"":
                    # empty chunk means EOF
                    ffmpeg_proc.write_eof()
                    done.set()
                    return
                # TODO: implement sync corrections here
                await ffmpeg_proc.write(chunk)

            try:
                # register our callback func to the listeners so we receive the pcm chunks
                stream.listeners[client_id] = audio_callback
                # start yielding chunks from the stdout of the player-specific ffmpeg
                async for output_chunk in ffmpeg_proc.iter_chunked(chunksize):
                    yield output_chunk
            finally:
                # make sure the callback is removed from the listeners
                stream.listeners.pop(client_id, None)

    def _get_player_ffmpeg_args(
        self,
        streamdetails: StreamDetails,
        player: Player,
        output_format: ContentType,
        input_is_pcm: bool = True,
    ) -> list[str]:
        """Get player specific arguments for the given stream and output details."""
        # generic args
        ffmpeg_args = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "quiet",
            "-ignore_unknown",
        ]
        # pcm input args,
        if input_is_pcm:
            input_fmt = ContentType.from_bit_depth(streamdetails.bit_depth)
        else:
            input_fmt = streamdetails.content_type
        ffmpeg_args += [
            "-f",
            input_fmt,
            "-ac",
            str(streamdetails.channels),
            "-ar",
            str(streamdetails.sample_rate),
            "-i",
            "-",
        ]
        # TODO: insert filters/convolution/DSP here!
        # output args
        ffmpeg_args += [
            # output args
            "-f",
            output_format.value,
            "-compression_level",
            "0",
            "-",
        ]
        return ffmpeg_args

    async def cleanup_stale(self) -> None:
        """Cleanup stale/done stream tasks."""
        stale = set()
        for stream_id, stream in self.queue_streams.items():
            if stream.done.is_set() and not stream.connected_clients:
                stale.add(stream_id)
        for stream_id in stale:
            self.queue_streams.pop(stream_id, None)
