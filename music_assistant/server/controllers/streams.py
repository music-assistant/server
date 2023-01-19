"""Controller to stream audio to players."""
from __future__ import annotations

import asyncio
import logging
import os
import urllib.parse
from typing import TYPE_CHECKING, AsyncGenerator, Dict, List, Optional, Tuple
from uuid import uuid4

from aiohttp import web

from music_assistant.common.models.enums import (
    ContentType,
    CrossFadeMode,
    EventType,
    MediaType,
    MetadataMode,
    ProviderType,
    StreamState,
)
from music_assistant.common.models.errors import (
    MediaNotFoundError,
    PlayerUnavailableError,
    QueueEmpty,
)
from music_assistant.common.models.event import MassEvent
from music_assistant.common.models.player import Player
from music_assistant.common.models.player_queue import PlayerQueue
from music_assistant.common.models.queue_item import QueueItem
from music_assistant.constants import BASE_URL_OVERRIDE_ENVNAME, ROOT_LOGGER_NAME
from music_assistant.server.helpers.audio import (
    check_audio_support,
    crossfade_pcm_parts,
    get_chunksize,
    get_media_stream,
    get_preview_stream,
    get_stream_details,
    strip_silence,
)
from music_assistant.server.helpers.process import AsyncProcess

if TYPE_CHECKING:
    from music_assistant.common.models.queue_stream import QueueStream
    from music_assistant.server import MusicAssistant

LOGGER = logging.getLogger(f"{ROOT_LOGGER_NAME}.streams")

ICY_CHUNKSIZE = 8192


class StreamsController:
    """Controller to stream audio to players."""

    def __init__(self, mass: MusicAssistant):
        """Initialize instance."""
        self.mass = mass
        self._port = mass.config.stream_port
        self._ip = mass.config.stream_ip
        self.queue_streams: Dict[str, QueueStream] = {}
        self.announcements: Dict[str, Tuple[str]] = {}

    @property
    def base_url(self) -> str:
        """Return the base url for the stream engine."""

        if BASE_URL_OVERRIDE_ENVNAME in os.environ:
            # This is a purpously undocumented feature to override the automatic
            # generated base_url used by the streaming-devices.
            # If you need this, you know it, but you should probably try to not set it!
            # Also see https://github.com/music-assistant/hass-music-assistant/issues/802
            # and https://github.com/music-assistant/hass-music-assistant/discussions/794#discussioncomment-3331209
            return os.environ[BASE_URL_OVERRIDE_ENVNAME]

        return f"http://{self._ip}:{self._port}"

    def get_stream_url(self, queue_id: str, player_id: Optional[str] = None) -> str:
        """Get stream url for the PlayerQueue Stream."""
        player = self.mass.players.get_player(player_id or queue_id)
        content_type = player.settings.stream_type
        ext = content_type.value
        if player_id:
            return f"{self.base_url}/stream/{queue_id}/{player_id}.{ext}"
        return f"{self.base_url}/stream/{queue_id}.{ext}"

    def get_announcement_url(
        self,
        player_id: str,
        urls: Tuple[str],
    ) -> str:
        """Get url to announcement stream."""
        self.announcements[player_id] = urls
        player = self.mass.players.get_player(player_id)
        content_type = player.settings.stream_type
        ext = content_type.value
        return f"{self.base_url}/announce/{player_id}.{ext}"

    async def get_preview_url(self, provider: ProviderType, track_id: str) -> str:
        """Return url to short preview sample."""
        track = await self.mass.music.tracks.get_provider_item(track_id, provider)
        if preview := track.metadata.preview:
            return preview
        enc_track_id = urllib.parse.quote(track_id)
        return f"{self.base_url}/preview?provider={provider}&item_id={enc_track_id}"

    async def setup(self) -> None:
        """Async initialize of module."""
        app = web.Application()

        app.router.add_get("/preview", self.serve_preview)
        app.router.add_get("/announce/{player_id}.{fmt}", self.serve_announcement)
        app.router.add_get(
            "/stream/{queue_id}/{client_id}.{fmt}", self.serve_queue_stream
        )

        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        # set host to None to bind to all addresses on both IPv4 and IPv6
        http_site = web.TCPSite(runner, host=None, port=self._port)
        await http_site.start()

        async def on_shutdown_event(event: MassEvent):
            """Handle shutdown event."""
            await http_site.stop()
            await runner.cleanup()
            await app.shutdown()
            await app.cleanup()
            LOGGER.debug("Streamserver exited.")

        self.mass.subscribe(on_shutdown_event, EventType.SHUTDOWN)

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

        LOGGER.info("Started stream server on port %s", self._port)

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

    async def serve_queue_stream(self, request: web.Request):
        """Serve queue audio stream to a player."""
        LOGGER.debug(
            "Got %s request to %s from %s\nheaders: %s\n",
            request.method,
            request.path,
            request.remote,
            request.headers,
        )
        fmt = request.match_info["fmt"]
        queue_id = request.match_info["queue_id"]
        queue_player = self.mass.players.get_player(queue_id)
        if not queue_player:
            raise PlayerUnavailableError(f"PlayerQueue {queue_id} is not available!")
        queue = queue_player.queue

        # player_id is optional and only used for multi-client streams
        player_id = request.match_info.get("player_id", queue_id)
        player = self.mass.players.get_player(player_id) or queue_player
        output_format = ContentType.try_parse(fmt)

        # handle situation where the player requests a stream but we did not initiate the request
        if queue.stream.state in (StreamState.IDLE, StreamState.PENDING_STOP):
            LOGGER.warning(
                "Got unsollicited stream request for %s",
                queue_id,
            )
            # issue resume request to player
            await queue.resume(passive=True)
        # handle scenario where the player (re)requests the stream while it is already running
        elif queue.stream.state == StreamState.RUNNING:
            LOGGER.warning(
                "Got stream request for %s while already running, playback may be disturbed!",
                queue_id,
            )

        # prepare request, add some DLNA/UPNP compatible headers
        headers = {
            "Content-Type": f"audio/{output_format.value}",
            "transferMode.dlna.org": "Streaming",
            "contentFeatures.dlna.org": "DLNA.ORG_OP=00;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=0d500000000000000000000000000000",
            "Cache-Control": "no-cache",
        }

        # Append ICY-metadata headers depending on player settings
        metadata_mode = player.settings.metadata_mode
        if metadata_mode == MetadataMode.LEGACY:
            # in legacy mode, use the very small default chunk size of 8192 bytes
            output_chunksize = ICY_CHUNKSIZE
        else:
            # otherwise calculate a sane default chunk size depending on the content
            output_chunksize = get_chunksize(
                output_format,
                sample_rate=queue.stream.pcm_sample_rate,
                bit_depth=queue.stream.pcm_bit_depth,
                seconds=2,
            )
        if metadata_mode != MetadataMode.DISABLED:
            headers["icy-name"] = "Music Assistant"
            headers["icy-pub"] = "1"
            headers["icy-metaint"] = str(output_chunksize)

        # prepare request/headers
        resp = web.StreamResponse(headers=headers)
        await resp.prepare(request)

        # do not start stream on HEAD request
        if request.method != "GET":
            return resp

        # if the client sets the 'Icy-MetaData' header,
        # it means it supports (and wants) ICY metadata
        enable_icy = request.headers.get("Icy-MetaData", "") == "1"

        # start of actual audio streaming logic

        # regular streaming - each chunk is sent to the callback here
        # this chunk is already encoded to the requested audio format of choice.
        # optional ICY metadata can be sent to the client if it supports that
        async def audio_callback(chunk: bytes) -> None:
            """Call when a new audio chunk arrives."""
            # write audio
            await resp.write(chunk)

            # ICY metadata support
            if not enable_icy:
                return

            # if icy metadata is enabled, send the icy metadata after the chunk
            item_in_buf = queue_stream.queue.get_item(queue_stream.index_in_buffer)
            if item_in_buf and item_in_buf.name:
                title = item_in_buf.name
                if item_in_buf.image and not item_in_buf.image.is_file:
                    image = item_in_buf.media_item.image.url
                else:
                    image = ""
            else:
                title = "Music Assistant"
                image = ""
            metadata = f"StreamTitle='{title}';StreamUrl='&picture={image}';".encode()
            while len(metadata) % 16 != 0:
                metadata += b"\x00"
            length = len(metadata)
            length_b = chr(int(length / 16)).encode()
            await resp.write(length_b + metadata)

        await queue_stream.subscribe(client_id, audio_callback)
        await resp.write_eof()

        return resp

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
        self, stream: QueueStreamInfo, player: Player, output_format: ContentType
    ) -> List[str]:
        """Get arguments for ffmpeg for the player specific oputput stream."""
        # generic args
        ffmpeg_args = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "quiet",
            "-ignore_unknown",
        ]
        # pcm input args
        ffmpeg_args += [
            "-f",
            stream.pcm_format,
            "-ac",
            str(stream.pcm_channels),
            "-ar",
            str(stream.pcm_sample_rate),
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
