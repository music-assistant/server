"""Controller to stream audio to players."""
from __future__ import annotations

import urllib.parse
from typing import TYPE_CHECKING, Dict

from aiohttp import web

from music_assistant.helpers.audio import (
    check_audio_support,
    create_wave_header,
    get_preview_stream,
    get_stream_details,
)
from music_assistant.models.enums import (
    ContentType,
    CrossFadeMode,
    EventType,
    ProviderType,
)
from music_assistant.models.errors import MediaNotFoundError, QueueEmpty
from music_assistant.models.event import MassEvent
from music_assistant.models.player_queue import PlayerQueue
from music_assistant.models.queue_stream import QueueStream

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


class StreamController:
    """Controller to stream audio to players."""

    def __init__(self, mass: MusicAssistant):
        """Initialize instance."""
        self.mass = mass
        self.logger = mass.logger.getChild("stream")
        self._port = mass.config.stream_port
        self._ip = mass.config.stream_ip
        self.queue_streams: Dict[str, QueueStream] = {}

    def get_stream_url(
        self,
        stream_id: str,
        client_id: str = "0",
        content_type: ContentType = ContentType.FLAC,
    ) -> str:
        """Generate unique stream url for the PlayerQueue Stream."""
        ext = content_type.value
        return f"http://{self._ip}:{self._port}/{stream_id}_{client_id}.{ext}"

    async def get_preview_url(self, provider: ProviderType, track_id: str) -> str:
        """Return url to short preview sample."""
        track = await self.mass.music.tracks.get_provider_item(track_id, provider)
        if preview := track.metadata.preview:
            return preview
        enc_track_id = urllib.parse.quote(track_id)
        return f"http://{self._ip}:{self._port}/preview?provider_id={provider.value}&item_id={enc_track_id}"

    def get_silence_url(self, duration: int = 600) -> str:
        """Return url to silence."""
        return f"http://{self._ip}:{self._port}/silence?duration={duration}"

    async def setup(self) -> None:
        """Async initialize of module."""
        app = web.Application()

        app.router.add_get("/preview", self.serve_preview)
        app.router.add_get("/silence", self.serve_silence)
        app.router.add_get(
            "/{stream_id}_{client_id}.{format}",
            self.serve_queue_stream,
        )

        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        # set host to None to bind to all addresses on both IPv4 and IPv6
        http_site = web.TCPSite(runner, host=None, port=self._port)
        await http_site.start()

        async def on_shutdown_event(*event: MassEvent):
            """Handle shutdown event."""
            await http_site.stop()
            await runner.cleanup()
            await app.shutdown()
            await app.cleanup()
            self.logger.info("Streamserver exited.")

        self.mass.subscribe(on_shutdown_event, EventType.SHUTDOWN)

        sox_present, ffmpeg_present = await check_audio_support(True)
        if not ffmpeg_present and not sox_present:
            self.logger.error(
                "SoX or FFmpeg binary not found on your system, "
                "playback will NOT work!."
            )
        elif not ffmpeg_present:
            self.logger.warning(
                "The FFmpeg binary was not found on your system, "
                "you might experience issues with playback. "
                "Please install FFmpeg with your OS package manager.",
            )
        elif not sox_present:
            self.logger.warning(
                "The SoX binary was not found on your system, FFmpeg is used as fallback."
            )

        self.logger.info("Started stream server on port %s", self._port)

    @staticmethod
    async def serve_silence(request: web.Request):
        """Serve silence."""
        resp = web.StreamResponse(
            status=200, reason="OK", headers={"Content-Type": "audio/wav"}
        )
        await resp.prepare(request)
        duration = int(request.query.get("duration", 600))
        await resp.write(create_wave_header(duration=duration))
        for _ in range(0, duration):
            await resp.write(b"\0" * 1764000)
        return resp

    async def serve_preview(self, request: web.Request):
        """Serve short preview sample."""
        provider_id = request.query["provider_id"]
        item_id = urllib.parse.unquote(request.query["item_id"])
        resp = web.StreamResponse(
            status=200, reason="OK", headers={"Content-Type": "audio/mp3"}
        )
        await resp.prepare(request)
        async for _, chunk in get_preview_stream(self.mass, provider_id, item_id):
            await resp.write(chunk)
        return resp

    async def serve_queue_stream(self, request: web.Request):
        """Serve queue audio stream to a single player."""
        self.logger.info(request)
        self.logger.info(request.headers)
        client_id = request.match_info["client_id"]
        stream_id = request.match_info["stream_id"]
        queue_stream = self.queue_streams.get(stream_id)

        if queue_stream is None:
            self.logger.warning("Got stream request for unknown id: %s", stream_id)
            return web.Response(status=404)

        # prepare request
        headers = {
            "Content-Type": f"audio/{queue_stream.output_format.value}",
            "transferMode.dlna.org": "Streaming",
            "Connection": "Close",
            "Server": "HairTunes",
            # "Content-Length": "-1",
            "contentFeatures.dlna.org": "DLNA.ORG_OP=00;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=0d500000000000000000000000000000",
        }
        resp = web.StreamResponse(headers=headers)
        await resp.prepare(request)
        await queue_stream.subscribe(client_id, resp.write)

        return resp

    async def start_queue_stream(
        self,
        queue: PlayerQueue,
        stream_id: str,
        expected_clients: set,
        start_index: int,
        seek_position: int,
        fade_in: bool,
        output_format: ContentType,
    ) -> None:
        """Start running a queue stream."""
        # determine the pcm details based on the first track we need to stream
        try:
            first_item = queue.items[start_index]
        except (IndexError, TypeError) as err:
            raise QueueEmpty() from err

        try:
            streamdetails = await get_stream_details(
                self.mass, first_item, queue.queue_id
            )
        except MediaNotFoundError:
            # something bad happened, try to recover by requesting the next track in the queue
            await queue.play_index(start_index + 1)
            return

        # work out pcm details
        if queue.settings.crossfade_mode == CrossFadeMode.ALWAYS:
            pcm_sample_rate = min(96000, queue.max_sample_rate)
            pcm_bit_depth = 24
            pcm_channels = 2
            pcm_resample = True
        else:
            pcm_sample_rate = streamdetails.sample_rate
            pcm_bit_depth = streamdetails.bit_depth
            pcm_channels = streamdetails.channels
            pcm_resample = False

        self.queue_streams[stream_id] = QueueStream(
            queue=queue,
            stream_id=stream_id,
            expected_clients=expected_clients,
            start_index=start_index,
            seek_position=seek_position,
            fade_in=fade_in,
            output_format=output_format,
            pcm_sample_rate=pcm_sample_rate,
            pcm_bit_depth=pcm_bit_depth,
            pcm_channels=pcm_channels,
            pcm_resample=pcm_resample,
        )
