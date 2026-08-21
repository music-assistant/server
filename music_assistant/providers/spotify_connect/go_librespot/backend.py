"""
go-librespot backend for the Spotify Connect provider.

go-librespot is driven entirely over its local HTTP+WebSocket API: the WebSocket
``/events`` stream feeds player/session/metadata/volume state into Music Assistant,
and transport + volume commands are issued via the REST endpoints. Playback control
works without a configured Spotify *music* provider or the Spotify Web API.
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import suppress
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from music_assistant_models.enums import ContentType, StreamType
from music_assistant_models.media_items import AudioFormat

from music_assistant.helpers.process import AsyncProcess
from music_assistant.helpers.util import (
    interface_name_for_ip,
    is_port_in_use,
    select_free_port,
)
from music_assistant.providers.spotify_connect.base import (
    AUDIO_QUALITY_HIGH,
    AUDIO_QUALITY_LOSSLESS,
    AUDIO_QUALITY_NORMAL,
    AUDIO_QUALITY_VERY_HIGH,
    SpotifyConnectBackend,
)
from music_assistant.providers.spotify_connect.helpers import (
    generate_device_id,
    get_go_librespot_binary,
)
from music_assistant.providers.spotify_connect.models import (
    BackendEvent,
    BackendEventType,
    BackendStreamSource,
    BackendTrackMetadata,
)

from .client import GoLibrespotClient

if TYPE_CHECKING:
    import logging

    from music_assistant.mass import MusicAssistant
    from music_assistant.providers.spotify_connect.models import (
        AudioChunkReader,
        BackendEventCallback,
    )

# go-librespot volume scale; we pin volume_steps to this so the daemon's 0..max
# volume maps 1:1 to a 0-100 percentage.
VOLUME_STEPS = 100

# Read size for pulling PCM off the daemon's stdout.
STREAM_READ_CHUNK = 16384

# Port range the go-librespot API server binds to (loopback only, one per instance).
API_PORT_RANGE_START = 38800
API_PORT_RANGE_END = 38900

# Quality tier -> go-librespot's `bitrate` setting, which only accepts these
# three values. go-librespot cannot do lossless, so that tier gets the 320 kbps
# ceiling rather than nothing.
_MAX_BITRATE: Final = 320
_BITRATES: Final[dict[str, int]] = {
    AUDIO_QUALITY_NORMAL: 96,
    AUDIO_QUALITY_HIGH: 160,
    AUDIO_QUALITY_VERY_HIGH: _MAX_BITRATE,
    AUDIO_QUALITY_LOSSLESS: _MAX_BITRATE,
}


class GoLibrespotBackend(SpotifyConnectBackend):
    """Spotify Connect backend wrapping a supervised go-librespot daemon."""

    def __init__(
        self,
        mass: MusicAssistant,
        *,
        instance_id: str,
        publish_name: str,
        name: str,
        logger: logging.Logger,
        event_callback: BackendEventCallback,
        crossfade_ms: int = 0,
        loudness_normalization: bool = True,
        audio_quality: str = AUDIO_QUALITY_LOSSLESS,
    ) -> None:
        """
        Initialize the backend (cheap; the daemon is launched in ``start``).

        :param mass: The MusicAssistant instance.
        :param instance_id: The owning provider's instance id; keys the
            credential/cache dir and the stable Spotify device id.
        :param publish_name: Device name advertised to the Spotify app.
        :param name: Display name of the owning provider instance (log messages).
        :param logger: Logger to use for diagnostics.
        :param event_callback: Awaited with a normalized BackendEvent for every
            state change the daemon reports.
        :param crossfade_ms: Crossfade duration between tracks in milliseconds
            (0 disables crossfade).
        :param loudness_normalization: Whether Spotify's loudness normalization
            should be applied to the audio.
        :param audio_quality: Ceiling for the streaming quality Spotify is asked
            to deliver (one of the AUDIO_QUALITY_* tiers).
        """
        self.mass = mass
        self.logger = logger
        self.name = name
        self._instance_id = instance_id
        self._publish_name = publish_name
        self._event_callback = event_callback
        self._crossfade_ms = crossfade_ms
        self._loudness_normalization = loudness_normalization
        self._audio_quality = audio_quality
        self.cache_dir = os.path.join(self.mass.cache_path, instance_id)
        self._binary: str | None = None
        self._api_port: int = 0
        self._client: GoLibrespotClient | None = None
        self._stop_called: bool = False
        self._daemon_task: asyncio.Task[None] | None = None
        self._events_task: asyncio.Task[None] | None = None
        self._proc: AsyncProcess | None = None
        self._restart_error_count = 0
        # _audio_format is the original Spotify source codec (Ogg Vorbis 320 kbps),
        # advertised to clients for display. _decoded_audio_format is the raw PCM
        # go-librespot actually writes to its stdout after decoding — what the
        # audio reader yields and what the streams controller hands ffmpeg as
        # the input format. We always emit the source's own format here; MA is
        # responsible for converting it to whatever each player needs.
        self._audio_format = AudioFormat(
            content_type=ContentType.OGG,
            codec_type=ContentType.VORBIS,
            sample_rate=44100,
            bit_depth=16,
            channels=2,
            bit_rate=320,
        )
        self._decoded_audio_format = AudioFormat(
            content_type=ContentType.PCM_S16LE,
            codec_type=ContentType.PCM_S16LE,
            sample_rate=44100,
            bit_depth=16,
            channels=2,
        )

    @property
    def audio_format(self) -> AudioFormat:
        """Return the source audio format (advertised to clients for display)."""
        return self._audio_format

    @property
    def decoded_audio_format(self) -> AudioFormat:
        """Return the decoded PCM format the audio reader actually delivers."""
        return self._decoded_audio_format

    async def start(self) -> None:
        """Start the backend and its supervised go-librespot daemon."""
        self._binary = get_go_librespot_binary()
        self._api_port = await select_free_port(
            API_PORT_RANGE_START, API_PORT_RANGE_END, host="127.0.0.1"
        )
        self._client = GoLibrespotClient(
            self.mass, f"http://127.0.0.1:{self._api_port}", self.logger
        )
        # Two self-healing supervisors: one keeps the daemon process alive, the
        # other keeps the events websocket connected (reconnecting across daemon
        # restarts). The events runner resets the daemon's restart backoff once
        # the websocket is healthy again.
        self._daemon_task = self.mass.create_task(self._daemon_runner())
        self._events_task = self.mass.create_task(self._events_runner())

    async def stop(self) -> None:
        """Stop the daemon and all supervisor tasks."""
        self._stop_called = True
        for task in (self._events_task, self._daemon_task):
            if task and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    async def get_stream_source(self) -> BackendStreamSource:
        """Return the CUSTOM stream source, consumed through the audio reader."""
        # CUSTOM: the core pulls PCM through get_audio_reader. `-fflags nobuffer`
        # keeps ffmpeg's own input buffering low so the controller's realtime
        # pacer owns the (small, bounded) read-ahead.
        return BackendStreamSource(
            stream_type=StreamType.CUSTOM,
            extra_input_args=["-fflags", "nobuffer"],
        )

    def get_audio_reader(self) -> AudioChunkReader | None:
        """
        Return a PCM chunk reader bound to the currently running daemon.

        The reader stays bound to this daemon process: once it exits, the
        reader returns b"" (clean EOF) even if a restarted daemon is already
        up. None is returned when no daemon is running.
        """
        if (proc := self._proc) is None:
            return None
        return partial(proc.read, STREAM_READ_CHUNK)

    async def play(self, uri: str, *, skip_to_uri: str | None = None) -> None:
        """
        Start playing a Spotify URI/context, making this device the active one.

        :param uri: Spotify URI (track, album, playlist, ...) — typically a context.
        :param skip_to_uri: Optional track URI within the context to start at.
        """
        assert self._client is not None
        await self._client.play(uri, skip_to_uri=skip_to_uri)

    async def resume(self) -> None:
        """Resume playback on the active session."""
        assert self._client is not None
        await self._client.resume()

    async def pause(self) -> None:
        """Pause playback on the active session."""
        assert self._client is not None
        await self._client.pause()

    async def deactivate(self) -> None:
        """Release this device as the active Spotify Connect device."""
        assert self._client is not None
        await self._client.stop()

    async def next(self) -> None:
        """Skip to the next track."""
        assert self._client is not None
        await self._client.next()

    async def previous(self) -> None:
        """Skip to the previous track (or rewind the current one)."""
        assert self._client is not None
        await self._client.prev()

    async def seek(self, position_ms: int) -> None:
        """
        Seek to an absolute position in the current track.

        :param position_ms: Target position in milliseconds.
        """
        assert self._client is not None
        await self._client.seek(position_ms)

    async def set_volume(self, volume: int) -> None:
        """
        Set the daemon's playback volume.

        :param volume: Absolute volume as a 0-100 percentage (translated to
            go-librespot's own volume scale).
        """
        assert self._client is not None
        await self._client.set_volume(round(volume / 100 * VOLUME_STEPS))

    def _write_config(self, source_ip: str | None) -> None:
        """
        Write the go-librespot ``config.yml`` for this instance.

        go-librespot reads a YAML config; JSON is valid YAML, so we emit JSON to
        sidestep an extra dependency and any string-quoting pitfalls (the device
        name is user-provided). The config dir doubles as the credential/device
        cache so the Spotify Connect device stays paired across restarts.

        :param source_ip: Local address of the player-facing interface, or None to
            advertise the Spotify Connect device on all interfaces.
        """
        Path(self.cache_dir).mkdir(parents=True, exist_ok=True)
        config: dict[str, Any] = {
            "device_name": self._publish_name,
            "device_type": "speaker",
            "device_id": generate_device_id(self._instance_id),
            "bitrate": _BITRATES.get(self._audio_quality, _MAX_BITRATE),
            "audio_backend": "pipe",
            # write decoded PCM to the daemon's stdout, which we capture and
            # forward (the process pipe is always attached, so the daemon's
            # non-blocking pipe open never fails for lack of a reader). s16le is
            # the Spotify source representation; MA converts it per player.
            "audio_output_pipe": "/dev/stdout",
            "audio_output_pipe_format": "s16le",
            # external_volume: don't let go-librespot attenuate the PCM — MA / the
            # target player owns the actual volume. We still receive 'volume'
            # events and push volume back so the Spotify app slider stays in sync.
            # No initial_volume: go-librespot ignores it with external_volume set;
            # the provider pushes the player's volume instead.
            "external_volume": True,
            "volume_steps": VOLUME_STEPS,
            # normalisation is applied by go-librespot itself (-14 LUFS target);
            # crossfade_duration is in milliseconds, 0 disables it. The crossfade
            # key needs go-librespot >= 0.8.0; older daemons ignore unknown keys.
            "normalisation_disabled": not self._loudness_normalization,
            "crossfade_duration": self._crossfade_ms,
            "zeroconf_enabled": True,
            "credentials": {"type": "zeroconf", "zeroconf": {"persist_credentials": True}},
            "server": {"enabled": True, "address": "127.0.0.1", "port": self._api_port},
        }
        # Advertise the Spotify Connect device only on the interface the streams
        # server binds to, so it lands on the right network on multi-homed hosts.
        # go-librespot selects advertise interfaces by name, so map the IP to one.
        if source_ip:
            if iface_name := interface_name_for_ip(source_ip):
                config["zeroconf_interfaces_to_advertise"] = [iface_name]
            else:
                self.logger.debug(
                    "No interface found for stream bind IP %s; advertising on all interfaces",
                    source_ip,
                )
        config_file = os.path.join(self.cache_dir, "config.yml")
        with open(config_file, "w", encoding="utf-8") as fileobj:
            json.dump(config, fileobj, indent=2)

    async def _daemon_runner(self) -> None:
        """Run and supervise the go-librespot daemon, restarting it if it exits."""
        assert self._binary
        assert self._client
        # Loop forever; stop() cancels this task and the explicit stop-check below
        # handles a graceful exit without a restart.
        while True:
            # If the API port was taken while the daemon was down, move to a
            # fresh port instead of crash-looping on a bind error.
            if await is_port_in_use(self._api_port, host="127.0.0.1"):
                self._api_port = await select_free_port(
                    API_PORT_RANGE_START, API_PORT_RANGE_END, host="127.0.0.1"
                )
                self._client.base_url = f"http://127.0.0.1:{self._api_port}"
                self.logger.warning(
                    "API port in use by another process; switching to port %s", self._api_port
                )
            self._write_config(await self.mass.streams.get_source_ip())
            proc: AsyncProcess | None = None
            try:
                # stdout carries the decoded PCM (audio_output_pipe=/dev/stdout) and
                # is consumed through get_audio_reader; stderr carries the daemon's
                # logs, read here. Because the process pipe owns stdout from spawn
                # there is always a reader, so go-librespot's non-blocking pipe open
                # never fails for lack of a consumer.
                self._proc = proc = AsyncProcess(
                    [self._binary, "--config_dir", self.cache_dir],
                    stdout=True,
                    stderr=True,
                    name=f"go-librespot[{self.name}]",
                )
                await proc.start()
                self.logger.info("Started Spotify Connect background daemon [%s]", self.name)
                async for line in proc.iter_stderr():
                    self.logger.debug("[%s] %s", self.name, line)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                self.logger.warning("go-librespot daemon error [%s]: %s", self.name, err)
            finally:
                if proc:
                    await proc.close()
                # The daemon — and thus the Spotify session — is gone. Tell the
                # provider so a dead/restarting daemon isn't treated as active and
                # controllable; a fresh 'active' event re-establishes it on reconnect.
                self._proc = None
                try:
                    await self._event_callback(BackendEvent(BackendEventType.CONNECTION_LOST))
                except Exception:
                    # never let a callback error replace a propagating
                    # cancellation or kill the daemon supervisor
                    self.logger.exception("Error while handling daemon exit")
            if self._stop_called:
                break
            self.logger.info("Spotify Connect background daemon stopped for %s", self.name)
            self._restart_error_count += 1
            if self._restart_error_count >= 5:
                await self._event_callback(
                    BackendEvent(
                        BackendEventType.FATAL_ERROR,
                        error="go-librespot daemon failed to start multiple times.",
                    )
                )
                return
            await asyncio.sleep(2)

    async def _events_runner(self) -> None:
        """Keep the go-librespot events websocket connected, reconnecting as needed."""
        assert self._client is not None
        while not self._stop_called:
            try:
                if not await self._client.wait_until_ready():
                    await asyncio.sleep(2)
                    continue
                # A live websocket means the daemon is healthy: reset the restart
                # backoff counter the daemon supervisor uses.
                self._restart_error_count = 0
                await self._client.listen_events(self._handle_event)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                self.logger.debug("go-librespot events websocket dropped: %s", err)
            if not self._stop_called:
                await asyncio.sleep(2)

    async def _handle_event(self, event_type: str, data: dict[str, Any]) -> None:
        """Translate a single go-librespot websocket event and emit it normalized."""
        self.logger.debug("Received event [%s]: %s %s", self.name, event_type, data)
        await self._event_callback(self._translate_event(event_type, data))

    def _translate_event(self, event_type: str, data: dict[str, Any]) -> BackendEvent:
        """Map a raw go-librespot event onto the normalized BackendEvent model."""
        # Every event carries the latest context/track uris when present, so the
        # provider can take playback back when the user moves the active device
        # away in the Spotify app.
        context_uri: str | None = data.get("context_uri") or None
        track_uri: str | None = data.get("uri") or None
        if event_type == "active":
            return BackendEvent(
                BackendEventType.SESSION_ACTIVE, context_uri=context_uri, track_uri=track_uri
            )
        if event_type == "inactive":
            return BackendEvent(
                BackendEventType.SESSION_INACTIVE, context_uri=context_uri, track_uri=track_uri
            )
        if event_type == "playing":
            return BackendEvent(
                BackendEventType.PLAYING, context_uri=context_uri, track_uri=track_uri
            )
        if event_type == "paused":
            return BackendEvent(
                BackendEventType.PAUSED, context_uri=context_uri, track_uri=track_uri
            )
        if event_type == "stopped":
            return BackendEvent(
                BackendEventType.STOPPED, context_uri=context_uri, track_uri=track_uri
            )
        if event_type == "metadata":
            artists = data.get("artist_names") or []
            duration_ms = data.get("duration")
            return BackendEvent(
                BackendEventType.METADATA,
                context_uri=context_uri,
                track_uri=track_uri,
                metadata=BackendTrackMetadata(
                    track_uri=data.get("uri"),
                    title=data.get("name") or None,
                    artist=artists[0] if artists else None,
                    album=data.get("album_name"),
                    image_url=data.get("album_cover_url"),
                    duration=duration_ms // 1000 if duration_ms else None,
                    position=int(data.get("position", 0)) // 1000,
                ),
            )
        if event_type == "seek":
            return BackendEvent(
                BackendEventType.POSITION,
                context_uri=context_uri,
                track_uri=track_uri,
                position=int(data.get("position", 0)) // 1000,
            )
        if event_type == "volume" and data.get("value") is not None:
            # translate the daemon's 0..max scale to a 0-100 percentage
            max_value = data.get("max") or VOLUME_STEPS
            return BackendEvent(
                BackendEventType.VOLUME,
                context_uri=context_uri,
                track_uri=track_uri,
                volume=int(int(data["value"]) / int(max_value) * 100),
            )
        return BackendEvent(BackendEventType.OTHER, context_uri=context_uri, track_uri=track_uri)
