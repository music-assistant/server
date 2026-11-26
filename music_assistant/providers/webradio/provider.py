"""Web Broadcast player provider implementation."""

from __future__ import annotations

import asyncio
import time
from contextlib import aclosing, suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from aiohttp import web
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import (
    ConfigEntryType,
    MediaType,
    PlaybackState,
    PlayerFeature,
)
from music_assistant_models.errors import AudioError
from music_assistant_models.player import DeviceInfo, PlayerMedia, PlayerSource

from music_assistant.constants import (
    CONF_ENTRY_REQUIRES_FIXED_CONTENT_LENGTH,
    CONF_OUTPUT_CODEC,
    CONF_REQUIRES_FIXED_CONTENT_LENGTH,
    DEFAULT_STREAM_HEADERS,
    DLNA_CONTENT_FEATURES_REALTIME,
    ICY_HEADERS,
)
from music_assistant.helpers.audio import (
    calculate_content_length,
    format_icy_metadata_frame,
    get_bit_rate,
    get_mime_type,
)
from music_assistant.helpers.ffmpeg import get_ffmpeg_stream
from music_assistant.models.player_protocol import PlayerProtocol
from music_assistant.models.player_provider import PlayerProvider

from .constants import (
    CONF_ENTRY_ENABLE_ICY_METADATA_BASIC,
    CONF_ENTRY_OUTPUT_CODEC_WEBRADIO,
    CONF_STATIONS,
    CONF_STREAM_URL_LABEL,
    PLAYER_ID_PREFIX,
    URL_PATH_PREFIX,
)
from .helpers import slugify_station_name

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable

    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.enums import ProviderFeature
    from music_assistant_models.media_items import AudioFormat
    from music_assistant_models.player_queue import PlayerQueue
    from music_assistant_models.provider import ProviderManifest
    from music_assistant_models.queue_item import QueueItem

    from music_assistant.mass import MusicAssistant
    from music_assistant.models.player import Player


_ICY_FULL_INTERVAL: int = 256_000
_ICY_STANDARD_INTERVAL: int = 16_384
# Per-listener queue depth. A subscriber that can't keep up has its queue
# filled and is dropped instead of stalling the producer.
_SUBSCRIBER_QUEUE_SIZE: int = 32
# ICY "no-change" frame: a zero length byte with no payload.
_ICY_NO_CHANGE_FRAME: bytes = b"\x00"
_PRODUCER_READY_TIMEOUT: float = 10.0
# How long the producer waits for the play_media of the next queue session.
_NEXT_SESSION_TIMEOUT: float = 5.0
# Media without an end, which core never plays inside a flow stream.
_LIVE_MEDIA_TYPES = (MediaType.RADIO, MediaType.AUDIO_SOURCE)


@dataclass(eq=False)
class _Subscriber:
    """A single HTTP listener subscribed to a station's broadcast."""

    queue: asyncio.Queue[bytes | None]
    want_icy: bool
    last_metadata_seen: bytes = b""


@dataclass
class _Station:
    """Runtime registration data for a single web broadcast station."""

    slug: str
    protocol: WebRadioPlayer
    # Only set while the station is attached to its player: its URL exists only then.
    url_path: str = ""
    unregister_route: Callable[[], None] | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    subscribers: set[_Subscriber] = field(default_factory=set)
    producer_task: asyncio.Task[None] | None = None
    producer_ready: asyncio.Event = field(default_factory=asyncio.Event)
    # Cooperative stop flag. Asks the producer to exit at the next chunk
    # boundary so ffmpeg's context manager cleans up without being cancelled
    # mid-pipeline (avoids the _UnixReadPipeTransport race on Python 3.14).
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    # Latest PlayerMedia the queue asked us to broadcast (set by play_media,
    # cleared by stop and at the end of the queue). Right after play_media the
    # producer reads this instead of queue.current_item so it cannot race the
    # queue controller's mid-skip mutations.
    pending_media: PlayerMedia | None = None
    pending_media_event: asyncio.Event = field(default_factory=asyncio.Event)
    # Set when the broadcast ended because the last listener left: the queue has moved on
    # from pending_media by then, so the next broadcast starts from the queue's current item.
    resume_from_queue: bool = False
    # Populated by the producer once output config is known.
    output_format: AudioFormat | None = None
    icy_preference: str = "disabled"
    icy_chunk_size: int = 0
    current_metadata: bytes = b""

    @property
    def queue_id(self) -> str | None:
        """Return the id of the station's queue, None while it has no player."""
        return player.player_id if (player := self.protocol.player) else None


class WebRadioProvider(PlayerProvider):
    """Player provider that publishes each configured station as an HTTP audio stream."""

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
        supported_features: set[ProviderFeature] | None = None,
    ) -> None:
        """
        Initialize the provider.

        :param mass: The owning Music Assistant instance.
        :param manifest: Loaded provider manifest.
        :param config: This provider instance's config.
        :param supported_features: Optional feature set; falls back to the
            features declared in the module-level setup helper.
        """
        super().__init__(mass, manifest, config, supported_features)
        self._stations: dict[str, _Station] = {}

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return the config entries of the provider, holding its list of stations."""
        return (
            ConfigEntry(
                key=CONF_STATIONS,
                type=ConfigEntryType.STRING,
                default_value=[],
                required=False,
                multi_value=True,
            ),
        )

    async def loaded_in_mass(self) -> None:
        """Forget the stations that are no longer in the stations config."""
        self._rename_stations_sharing_a_url()
        for name in self._raw_station_names():
            if not slugify_station_name(name):
                self.logger.warning(
                    "Ignoring station %r: name has no alphanumeric characters", name
                )
        configured = {f"{PLAYER_ID_PREFIX}{slug}" for slug in self._configured_stations()}
        for protocol_id in self.mass.config.get_protocol_ids_of_provider(self.instance_id):
            if protocol_id not in configured:
                await self.mass.players.remove_protocol(protocol_id)

    async def discover_players(self) -> None:
        """Register one player per configured station that is not attached."""
        for slug, name in self._configured_stations().items():
            protocol_id = f"{PLAYER_ID_PREFIX}{slug}"
            if self.get_protocol(protocol_id):
                continue
            if (station := self._stations.get(slug)) is None:
                station = _Station(
                    slug=slug,
                    protocol=WebRadioPlayer(
                        provider=self,
                        protocol_id=protocol_id,
                        station_slug=slug,
                        display_name=name,
                    ),
                )
                self._stations[slug] = station
            await self.mass.players.register(station.protocol)

    async def on_protocol_removed(self, protocol_id: str) -> None:
        """
        Take the station of a removed player out of the stations config.

        :param protocol_id: ID of the removed station's protocol.
        """
        slug = protocol_id.removeprefix(PLAYER_ID_PREFIX)
        self._stations.pop(slug, None)
        raw_names = self._raw_station_names()
        new_names = [name for name in raw_names if slugify_station_name(name) != slug]
        if new_names != raw_names:
            self._update_config_value(CONF_STATIONS, new_names)

    async def unload(self, is_removed: bool = False) -> None:
        """
        Stop all stations on provider unload.

        :param is_removed: True when the provider instance is being removed.
        """
        del is_removed
        for slug in list(self._stations):
            await self.close_station(slug)
        self._stations.clear()

    def open_station(self, slug: str) -> None:
        """Start serving a station on its URL."""
        station = self._stations.get(slug)
        if station is None or station.unregister_route is not None:
            return
        self._register_route(station)

    async def close_station(self, slug: str) -> None:
        """End a station's broadcast and stop serving its URL."""
        station = self._stations.get(slug)
        if station is None:
            return
        await self._cancel_producer(station)
        self.clear_pending_media(slug)
        if station.unregister_route is None:
            return
        station.unregister_route()
        station.unregister_route = None
        station.url_path = ""
        station.protocol.set_stream_url(None)

    def stop_producer(self, slug: str) -> None:
        """Terminate a station's broadcast."""
        station = self._stations.get(slug)
        if station is None:
            return
        station.stop_event.set()

    def set_pending_media(self, slug: str, media: PlayerMedia) -> None:
        """Hand a new PlayerMedia to a station's producer for broadcast."""
        station = self._stations.get(slug)
        if station is None:
            return
        station.pending_media = media
        station.resume_from_queue = False
        station.pending_media_event.set()

    def clear_pending_media(self, slug: str) -> None:
        """Drop a station's stored PlayerMedia."""
        station = self._stations.get(slug)
        if station is None:
            return
        station.pending_media = None
        station.pending_media_event.clear()

    def refresh_station_route(self, slug: str) -> None:
        """Re-register a station's HTTP route after its codec config changed."""
        station = self._stations.get(slug)
        if station is None or station.unregister_route is None:
            return
        if self._station_url_path(station) == station.url_path:
            return
        station.unregister_route()
        self._register_route(station)

    # ---------------------------------------------------------------
    # Station / route lifecycle
    # ---------------------------------------------------------------

    def _raw_station_names(self) -> list[str]:
        """Return the raw list of station names from provider config."""
        value = self.config.get_value(CONF_STATIONS)
        if not isinstance(value, list):
            return []
        return [str(name) for name in value if str(name).strip()]

    def _rename_stations_sharing_a_url(self) -> None:
        """Give a station whose name yields the URL of an earlier one a numbered name."""
        raw_names = self._raw_station_names()
        names: list[str] = []
        taken: set[str] = set()
        for raw_name in raw_names:
            name, number = raw_name, 1
            while (slug := slugify_station_name(name)) in taken:
                number += 1
                name = f"{raw_name} ({number})"
            if slug:
                taken.add(slug)
            names.append(name)
        if names != raw_names:
            # Stored, so the name in the settings, the player and the URL all agree.
            self._update_config_value(CONF_STATIONS, names)

    def _configured_stations(self) -> dict[str, str]:
        """Return the name of each configured station by its slug."""
        return {
            slug: name for name in self._raw_station_names() if (slug := slugify_station_name(name))
        }

    def _station_url_path(self, station: _Station) -> str:
        """Return the URL path a station is served on with its configured codec."""
        return f"{URL_PATH_PREFIX}{station.slug}.{self._station_codec(station)}"

    def _register_route(self, station: _Station) -> None:
        """Register the HTTP route of a station and publish its URL."""
        station.url_path = self._station_url_path(station)
        station.unregister_route = self.mass.streams.register_dynamic_route(
            station.url_path, self._handle_stream_request, "*"
        )
        station.protocol.set_stream_url(self._public_url(station.url_path))

    async def _cancel_producer(self, station: _Station) -> None:
        """Stop a station's producer task and wait for it to finish."""
        task = station.producer_task
        if task is None or task.done():
            return
        # Prefer the cooperative stop; only force-cancel if ffmpeg lingers.
        station.stop_event.set()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=3.0)
        except TimeoutError:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        except asyncio.CancelledError:
            pass

    def _station_codec(self, station: _Station) -> str:
        """Return the configured output codec extension for a station, e.g. ``"mp3"``."""
        return station.protocol.get_config_value(CONF_OUTPUT_CODEC, "mp3", return_type=str)

    def _public_url(self, url_path: str) -> str:
        """Build the externally reachable HTTP URL for a station path."""
        return f"{self.mass.streams.base_url}{url_path}"

    def _station_for_request(self, request: web.Request) -> _Station:
        """
        Resolve the station whose route matches the incoming request.

        :param request: Inbound aiohttp request.
        :return: The matching ``_Station``.
        :raises web.HTTPNotFound: When no station matches the request path.
        """
        for station in self._stations.values():
            if station.url_path == request.path:
                return station
        raise web.HTTPNotFound(reason=f"Unknown web broadcast station: {request.path}")

    # ---------------------------------------------------------------
    # HTTP stream handler and per-station producer
    # ---------------------------------------------------------------

    async def _handle_stream_request(self, request: web.Request) -> web.StreamResponse:
        """
        Subscribe an HTTP listener to a station's shared broadcast.

        :param request: Inbound aiohttp request.
        """
        station = self._station_for_request(request)

        if request.method == "HEAD":
            return _head_response(self._station_codec(station))

        if station.pending_media is None:
            # Answered right away, so a listener that keeps retrying does not start a
            # producer each time only for it to find nothing to broadcast.
            raise web.HTTPServiceUnavailable(reason=f"Station {station.slug!r} has nothing playing")

        subscriber = _Subscriber(
            queue=asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE_SIZE),
            want_icy=request.headers.get("Icy-MetaData", "") == "1",
        )

        async with station.lock:
            station.subscribers.add(subscriber)
            if station.producer_task is None or station.producer_task.done():
                station.output_format = None
                station.current_metadata = b""
                station.producer_ready.clear()
                station.stop_event.clear()
                station.producer_task = self.mass.create_task(self._run_producer(station))

        try:
            try:
                await asyncio.wait_for(
                    station.producer_ready.wait(), timeout=_PRODUCER_READY_TIMEOUT
                )
            except TimeoutError as err:
                raise web.HTTPServiceUnavailable(
                    reason=f"Station {station.slug!r} did not start in time"
                ) from err
            if station.output_format is None:
                raise web.HTTPServiceUnavailable(
                    reason=f"Station {station.slug!r} has nothing playing"
                )
            return await self._serve_subscriber(request, station, subscriber)
        finally:
            async with station.lock:
                station.subscribers.discard(subscriber)

    async def _run_producer(self, station: _Station) -> None:  # noqa: PLR0915
        """
        Encode the station's queue and broadcast each chunk to every subscriber.

        :param station: Station whose queue should be broadcast.
        """
        clean_exit = False
        try:
            queue, start_item = self._resolve_pending_media(station)
            if queue is None or start_item is None:
                station.producer_ready.set()
                return

            flow_pcm_format = await self.mass.streams.audio.select_flow_pcm_format(station.protocol)
            output_format = await self.mass.streams.audio.get_output_format(
                output_format_str=station.url_path.rsplit(".", 1)[-1],
                player=station.protocol,
                content_sample_rate=flow_pcm_format.sample_rate,
                content_bit_depth=flow_pcm_format.bit_depth,
                media_type=start_item.media_type,
            )

            icy_preference = station.protocol.get_config_value(
                CONF_ENTRY_ENABLE_ICY_METADATA_BASIC.key, "basic", return_type=str
            )
            chunk_size = _producer_chunk_size(icy_preference, output_format)

            station.output_format = output_format
            station.icy_preference = icy_preference
            station.icy_chunk_size = chunk_size
            station.current_metadata = b""
            station.producer_ready.set()

            self.logger.debug(
                "Producer started for station %s: output=%s icy=%s chunk_size=%d",
                station.slug,
                output_format.output_format_str,
                icy_preference,
                chunk_size,
            )

            first_iteration = True
            while True:
                queue, start_item = self._resolve_pending_media(station)
                if queue is None or start_item is None:
                    break
                queue_data = self.mass.player_queues.queue_data_or_none(queue.queue_id)
                session_id_at_start = queue_data.session_id if queue_data else None
                # Only when the broadcast carries on from the queue: catch up to
                # where it would be now for a late-joining listener. After a
                # play_media MA already set streamdetails.seek_position to the
                # intended value (e.g. a resume point). A live item has no
                # position to catch up to.
                if (
                    first_iteration
                    and station.resume_from_queue
                    and start_item.media_type not in _LIVE_MEDIA_TYPES
                ):
                    _join_at_live_position(queue, start_item)
                first_iteration = False
                # Clear so a stale event can't satisfy the post-session wait.
                station.pending_media_event.clear()

                exit_reason = await self._run_one_flow_session(
                    station=station,
                    queue=queue,
                    start_item=start_item,
                    flow_pcm_format=flow_pcm_format,
                    output_format=output_format,
                    icy_preference=icy_preference,
                    chunk_size=chunk_size,
                )

                if exit_reason == "stopped":
                    break
                if exit_reason == "no_subscribers":
                    station.resume_from_queue = True
                    async with station.lock:
                        if not station.subscribers:
                            station.producer_task = None
                            station.producer_ready.clear()
                            clean_exit = True
                            return
                    # A subscriber raced back in while we were exiting.
                    first_iteration = True
                    continue

                # exit_reason == "ended". If the queue's session_id moved on we
                # were skipped past.
                queue_data = self.mass.player_queues.queue_data_or_none(queue.queue_id)
                if queue_data is None:
                    break
                if queue_data.session_id == session_id_at_start:
                    # The queue ran out, or core ended the flow early (a format change or
                    # a live item) and plays the next item once the player reports idle.
                    exhausted = session_id_at_start is not None and (
                        self.mass.player_queues.flow_queue_exhausted(
                            queue.queue_id, session_id_at_start
                        )
                    )
                    if exhausted:
                        # A listener that reconnects on its own must not start the queue over.
                        self.clear_pending_media(station.slug)
                    station.protocol.mark_idle()
                    if exhausted:
                        break
                # Wait for the new play_media that owns the new session.
                with suppress(TimeoutError):
                    await asyncio.wait_for(
                        station.pending_media_event.wait(), timeout=_NEXT_SESSION_TIMEOUT
                    )
                if station.protocol.playback_state != PlaybackState.PLAYING:
                    # No next item arrived, so there is nothing to carry on with.
                    self.clear_pending_media(station.slug)
                    break
                self.logger.debug(
                    "Producer for station %s restarting on new queue session",
                    station.slug,
                )
        finally:
            # Unblock waiters even on error: they will then see output_format
            # is None and return HTTP 503.
            if not station.producer_ready.is_set():
                station.producer_ready.set()
            if not clean_exit:
                await self._reset_producer(station)
            self.logger.debug("Producer ended for station %s", station.slug)

    async def _run_one_flow_session(
        self,
        *,
        station: _Station,
        queue: PlayerQueue,
        start_item: QueueItem,
        flow_pcm_format: AudioFormat,
        output_format: AudioFormat,
        icy_preference: str,
        chunk_size: int,
    ) -> str:
        """
        Run one ffmpeg pipeline to completion, fed by the queue flow or by a single live item.

        :param station: Station whose subscribers should receive the audio.
        :param queue: Active queue being streamed.
        :param start_item: Queue item from which the stream starts.
        :param flow_pcm_format: PCM format coming out of the queue flow.
        :param output_format: Encoded format pushed to subscribers.
        :param icy_preference: Per-station ICY mode (``disabled``/``basic``/``full``).
        :param chunk_size: Encoded chunk size in bytes; also the icy-metaint value.
        :return: ``"stopped"`` (user pressed stop/pause), ``"no_subscribers"``
            (last listener left), or ``"ended"`` (queue exhausted or session
            superseded by a skip).
        """
        audio_input = (
            self._live_item_stream(station, start_item, flow_pcm_format)
            if start_item.media_type in _LIVE_MEDIA_TYPES
            else self.mass.streams.audio.get_queue_flow_stream(
                queue=queue,
                start_queue_item=start_item,
                pcm_format=flow_pcm_format,
                protocol_player=station.protocol,
            )
        )
        stream = get_ffmpeg_stream(
            audio_input=audio_input,
            input_format=flow_pcm_format,
            output_format=output_format,
            # 1.0x avoids long-broadcast drift (core uses 1.1 which clients
            # absorb past TCP backpressure). 2s burst per restart cushions
            # listeners across the producer-restart gap on skip; bigger
            # bursts close the gap further at the cost of more skip latency.
            extra_input_args=["-readrate", "1.0", "-readrate_initial_burst", "2"],
            chunk_size=chunk_size,
        )
        session_started = False
        async with aclosing(stream):
            async for chunk in stream:
                if not session_started:
                    # The queue measures its position against the audio of this session.
                    station.protocol.restart_elapsed_time()
                    session_started = True
                # iter_chunked yields a short chunk only at ffmpeg EOF. Emitting
                # one would desync the client's icy-metaint counter for the rest
                # of the connection, so drop it (we lose <1s of audio at session
                # end).
                if len(chunk) < chunk_size:
                    continue

                if icy_preference != "disabled":
                    title, image_url = _icy_metadata_for_queue(queue, icy_preference)
                    new_metadata = format_icy_metadata_frame(title, image_url)
                    if new_metadata != station.current_metadata:
                        self.logger.debug("Station %s ICY metadata: %s", station.slug, title)
                        station.current_metadata = new_metadata

                slow = self._broadcast_chunk(station, chunk)
                for sub in slow:
                    self.logger.debug(
                        "Dropping slow listener on station %s (queue backlog full)",
                        station.slug,
                    )
                    station.subscribers.discard(sub)
                    _disconnect_subscriber(sub)

                if not station.subscribers:
                    return "no_subscribers"

                if station.stop_event.is_set():
                    return "stopped"

        return "stopped" if station.stop_event.is_set() else "ended"

    async def _live_item_stream(
        self, station: _Station, queue_item: QueueItem, pcm_format: AudioFormat
    ) -> AsyncGenerator[bytes]:
        """Yield the PCM audio of a live queue item, ending when its source fails."""
        media = station.pending_media
        if media is None or media.queue_item_id != queue_item.queue_item_id:
            # The broadcast resumes from the queue, on an item no play_media named.
            media = await self.mass.player_queues.player_media_from_queue_item(queue_item)
        stream = self.mass.streams.get_stream(media, pcm_format, station.protocol.protocol_id)
        try:
            async with aclosing(stream):
                async for chunk in stream:
                    yield chunk
        except AudioError as err:
            self.logger.warning(
                "Live stream of %s failed on station %s: %s", queue_item.name, station.slug, err
            )

    def _resolve_pending_media(
        self, station: _Station
    ) -> tuple[PlayerQueue | None, QueueItem | None]:
        """
        Return the queue and start item the producer should consume next.

        :param station: Station whose pending PlayerMedia should be resolved.
        :return: Tuple of (queue, start_item); both may be ``None`` if the
            station's queue is missing, no media is pending, or the pending
            item has been removed from the queue.
        """
        media = station.pending_media
        if media is None:
            return None, None
        queue_id = station.queue_id
        queue = self.mass.player_queues.get(queue_id) if queue_id else None
        if queue is None:
            return None, None
        if station.resume_from_queue and queue.current_item is not None:
            return queue, queue.current_item
        # Right after play_media the PlayerMedia is the source of truth:
        # queue.current_item races queue.session_id during PLAY_NOW/seek.
        return queue, self.mass.player_queues.get_item(queue.queue_id, media.queue_item_id)

    def _broadcast_chunk(self, station: _Station, chunk: bytes) -> list[_Subscriber]:
        """
        Push a chunk to every subscriber of a station.

        :param station: Station whose subscribers should receive the chunk.
        :param chunk: Encoded audio bytes from the ffmpeg pipeline.
        :return: Subscribers whose backlog overflowed and should be dropped.
        """
        slow: list[_Subscriber] = []
        for sub in station.subscribers:
            try:
                sub.queue.put_nowait(chunk)
            except asyncio.QueueFull:
                slow.append(sub)
        return slow

    async def _reset_producer(self, station: _Station) -> None:
        """
        Disconnect every subscriber and reset the station's producer slot.

        :param station: Station to reset.
        """
        # Clearing subscribers and resetting producer_task happen under the
        # same lock so a new subscriber arriving here cannot join a producer
        # that is already dying; instead it sees producer_task is None and
        # starts a fresh one.
        async with station.lock:
            subs = list(station.subscribers)
            station.subscribers.clear()
            station.producer_task = None
            station.producer_ready.clear()
        for sub in subs:
            _disconnect_subscriber(sub)

    async def _serve_subscriber(
        self,
        request: web.Request,
        station: _Station,
        subscriber: _Subscriber,
    ) -> web.StreamResponse:
        """
        Stream the station's broadcast to one HTTP listener.

        :param request: Inbound aiohttp request.
        :param station: Resolved station record.
        :param subscriber: Subscription belonging to this connection.
        :return: The completed aiohttp response.
        """
        output_format = station.output_format
        assert output_format is not None  # guaranteed by producer_ready

        enable_icy = subscriber.want_icy and station.icy_preference != "disabled"

        station_name = _sanitize_header(station.protocol.display_name)
        headers = {
            **DEFAULT_STREAM_HEADERS,
            **ICY_HEADERS,
            "contentFeatures.dlna.org": DLNA_CONTENT_FEATURES_REALTIME,
            "Content-Type": get_mime_type(output_format.output_format_str),
            "icy-name": station_name,
            "icy-description": f"Music Assistant station: {station_name}",
            "icy-br": str(get_bit_rate(output_format)),
            "icy-pub": "0",
        }
        if enable_icy:
            headers["icy-metaint"] = str(station.icy_chunk_size)

        # Without a Content-Length an HTTP/1.1 listener gets chunked transfer encoding
        # and an HTTP/1.0 one a plain stream until the connection closes. A listener
        # that requires a fixed content length gets a (very high) one.
        resp = web.StreamResponse(status=200, reason="OK", headers=headers)
        if station.protocol.get_config_value(
            CONF_REQUIRES_FIXED_CONTENT_LENGTH, False, return_type=bool
        ):
            resp.content_length = calculate_content_length(output_format, 12 * 3600)
        # Closing the connection is what tells every kind of listener the broadcast ended.
        resp.force_close()
        await resp.prepare(request)
        # A forced Content-Length runs out at some point on an endless broadcast.
        bytes_left = resp.content_length

        self.logger.debug(
            "Listener subscribed: station=%s output=%s icy=%s",
            station.slug,
            output_format.output_format_str,
            enable_icy,
        )

        try:
            while True:
                chunk = await subscriber.queue.get()
                if chunk is None:
                    break
                try:
                    await resp.write(chunk)
                except BrokenPipeError, ConnectionResetError, ConnectionError:
                    break

                frame = b""
                if enable_icy:
                    current = station.current_metadata
                    if current and current != subscriber.last_metadata_seen:
                        frame = current
                        subscriber.last_metadata_seen = current
                    else:
                        frame = _ICY_NO_CHANGE_FRAME
                    try:
                        await resp.write(frame)
                    except BrokenPipeError, ConnectionResetError, ConnectionError:
                        break

                if bytes_left is not None:
                    bytes_left -= len(chunk) + len(frame)
                    if bytes_left <= 0:
                        # End the response, so the listener reconnects for a new one.
                        break
        finally:
            self.logger.debug("Listener unsubscribed: station=%s", station.slug)

        return resp


def _producer_chunk_size(icy_preference: str, output_format: AudioFormat) -> int:
    """
    Return the producer's encoded-chunk size in bytes.

    With ICY enabled this value also serves as ``icy-metaint``.

    :param icy_preference: Per-station ICY preference value.
    :param output_format: Encoded output format.
    """
    if icy_preference == "full":
        return _ICY_FULL_INTERVAL
    if icy_preference == "basic":
        return _ICY_STANDARD_INTERVAL
    return calculate_content_length(output_format)


def _head_response(codec: str) -> web.Response:
    """Build a minimal response for a HEAD probe on a station URL."""
    headers = {**DEFAULT_STREAM_HEADERS, "Content-Type": get_mime_type(codec)}
    return web.Response(status=200, headers=headers)


def _disconnect_subscriber(subscriber: _Subscriber) -> None:
    """Signal end-of-stream to a subscriber so its serving loop exits."""
    while not subscriber.queue.empty():
        try:
            subscriber.queue.get_nowait()
        except asyncio.QueueEmpty:
            break
    with suppress(asyncio.QueueFull):
        subscriber.queue.put_nowait(None)


def _sanitize_header(value: str) -> str:
    """Return a header-safe form of an arbitrary string."""
    return value.replace("\n", " ").replace("\r", " ").replace("\t", " ")


def _join_at_live_position(queue: PlayerQueue, start_item: QueueItem) -> None:
    """
    Set ``start_item.streamdetails.seek_position`` to the station's live point.

    Always writes a value (either the live offset within the track or 0) so a
    stale seek_position from a previous producer run cannot persist.

    :param queue: Active queue for the station.
    :param start_item: Queue item from which the flow stream will start.
    """
    streamdetails = start_item.streamdetails
    if streamdetails is None or not streamdetails.allow_seek:
        return
    seek_seconds = int(queue.elapsed_time or 0)
    if seek_seconds <= 0 or not streamdetails.duration or seek_seconds >= streamdetails.duration:
        streamdetails.seek_position = 0
        return
    streamdetails.seek_position = seek_seconds


def _icy_metadata_for_queue(queue: PlayerQueue, icy_preference: object) -> tuple[str, str | None]:
    """
    Return the title (and optional image URL) to advertise via ICY metadata.

    :param queue: Active queue for the station.
    :param icy_preference: Per-player ICY preference value; ``"full"`` enables
        the StreamURL field in addition to StreamTitle.
    :return: Tuple of (title, image_url-or-None).
    """
    current_item = queue.current_item
    title = "Music Assistant"
    image_url: str | None = None
    if current_item is None:
        return title, image_url
    if current_item.streamdetails and current_item.streamdetails.stream_title:
        title = current_item.streamdetails.stream_title
    elif current_item.name:
        title = current_item.name
    if icy_preference == "full" and current_item.image is not None:
        image_url = current_item.image.path
    return title, image_url


class WebRadioPlayer(PlayerProtocol):
    """A single web broadcast "station" exposed as a Music Assistant player."""

    def __init__(
        self,
        provider: WebRadioProvider,
        protocol_id: str,
        station_slug: str,
        display_name: str,
    ) -> None:
        """
        Initialize the station player.

        :param provider: Owning ``WebRadioProvider`` instance.
        :param protocol_id: Unique protocol id (``webradio_<slug>``).
        :param station_slug: URL-safe slug for this station.
        :param display_name: Human-readable station name.
        """
        super().__init__(provider, protocol_id)
        self._station_slug = station_slug
        self._stream_url: str | None = None
        self._attr_name = display_name
        self._attr_supported_features = {PlayerFeature.PLAY_MEDIA}
        self._attr_device_info = DeviceInfo(
            model="Web Broadcast Station",
            manufacturer="Music Assistant",
        )

    @property
    def source_list(self) -> list[PlayerSource]:
        """Return the Music Assistant queue as the only source of this station."""
        # Override the auto-injected queue source so the UI gates seek and
        # pause off: seek is meaningless on a live broadcast (and was
        # observed to confuse some clients), and pause has no real meaning
        # on a broadcast either - the UI shows stop instead. A station that
        # is not playing can be started. Next/previous remain available.
        if (player := self.player) is None:
            return []
        queue = self.mass.player_queues.get(player.player_id)
        return [
            PlayerSource(
                id=player.player_id,
                name="Music Assistant Queue",
                passive=False,
                can_play_pause=(
                    self._attr_playback_state != PlaybackState.PLAYING
                    and bool(queue and queue.items)
                ),
                can_seek=False,
                can_next_previous=True,
            ),
        ]

    @property
    def station_slug(self) -> str:
        """Return the URL-safe slug of this station."""
        return self._station_slug

    def set_stream_url(self, url: str | None) -> None:
        """Cache the public URL so it can be shown in the player's settings."""
        self._stream_url = url

    async def get_config_entries(self) -> list[ConfigEntry]:
        """Return player-level config entries."""
        return [
            CONF_ENTRY_OUTPUT_CODEC_WEBRADIO,
            CONF_ENTRY_ENABLE_ICY_METADATA_BASIC,
            CONF_ENTRY_REQUIRES_FIXED_CONTENT_LENGTH,
            ConfigEntry(
                key=CONF_STREAM_URL_LABEL,
                type=ConfigEntryType.LABEL,
                translation_params=[self._stream_url or "(unavailable)"],
            ),
        ]

    async def on_config_updated(self) -> None:
        """Refresh the published URL when the codec config changes."""
        provider = cast("WebRadioProvider", self.provider)
        provider.refresh_station_route(self._station_slug)

    async def on_attach(self, player: Player) -> None:
        """Start serving the station on its URL."""
        del player
        provider = cast("WebRadioProvider", self.provider)
        provider.open_station(self._station_slug)

    async def on_detach(self) -> None:
        """End the broadcast and stop serving the station on its URL."""
        await super().on_detach()
        self.mark_idle()
        provider = cast("WebRadioProvider", self.provider)
        await provider.close_station(self._station_slug)

    def restart_elapsed_time(self) -> None:
        """Restart the elapsed time, called when a broadcast session starts sending audio."""
        if self._attr_playback_state != PlaybackState.PLAYING:
            return
        self._attr_elapsed_time = 0
        self._attr_elapsed_time_last_updated = time.time()
        self.update_state()

    def mark_idle(self) -> None:
        """Report the station as idle, with nothing being broadcast."""
        self._attr_playback_state = PlaybackState.IDLE
        self._attr_current_media = None
        self._attr_elapsed_time = None
        self._attr_elapsed_time_last_updated = None
        self.update_state()

    async def play_media(self, media: PlayerMedia) -> None:
        """Mark the station as playing and queue the media for its broadcast."""
        # The station has no external sink: audio only leaves the box when an
        # HTTP listener connects. Until then the queue holds at the start item
        # and the broadcast begins from it when the first listener tunes in.
        self._attr_current_media = media
        self._attr_playback_state = PlaybackState.PLAYING
        self._attr_elapsed_time = 0
        self._attr_elapsed_time_last_updated = time.time()
        # Pre-set queue.flow_mode so the queue controller does not schedule
        # enqueue_next_media during the listener-not-yet-connected window
        # (PlayerFeature.ENQUEUE is intentionally not advertised by this
        # player). get_queue_flow_stream sets the same flag once a listener
        # attaches and the producer runs for real. A live item is never
        # played as a flow.
        queue = self.mass.player_queues.get(self.player.player_id) if self.player else None
        if queue is not None and media.media_type not in _LIVE_MEDIA_TYPES:
            queue.flow_mode = True
        self.update_state()
        provider = cast("WebRadioProvider", self.provider)
        provider.set_pending_media(self._station_slug, media)

    async def play(self) -> None:
        """Resume playback and re-anchor the wallclock elapsed time."""
        self._attr_playback_state = PlaybackState.PLAYING
        if self._attr_elapsed_time_last_updated is None:
            self._attr_elapsed_time = self._attr_elapsed_time or 0
            self._attr_elapsed_time_last_updated = time.time()
        self.update_state()

    async def stop(self) -> None:
        """Stop playback and terminate the active broadcast, if any."""
        self.mark_idle()
        provider = cast("WebRadioProvider", self.provider)
        provider.clear_pending_media(self._station_slug)
        provider.stop_producer(self._station_slug)
