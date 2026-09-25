"""Web Radio Broadcast player provider implementation."""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from aiohttp import web
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import (
    ConfigEntryType,
    PlaybackState,
    PlayerFeature,
    ProviderFeature,
)
from music_assistant_models.errors import SetupFailedError
from music_assistant_models.player import DeviceInfo, PlayerMedia, PlayerSource

from music_assistant.constants import (
    CONF_OUTPUT_CODEC,
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
from music_assistant.models.player import Player
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
    from collections.abc import Callable

    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.media_items import AudioFormat
    from music_assistant_models.player_queue import PlayerQueue
    from music_assistant_models.provider import ProviderManifest
    from music_assistant_models.queue_item import QueueItem

    from music_assistant.mass import MusicAssistant


_ICY_FULL_INTERVAL: int = 256_000
_ICY_STANDARD_INTERVAL: int = 16_384
# Per-listener queue depth. A subscriber that can't keep up has its queue
# filled and is dropped instead of stalling the producer.
_SUBSCRIBER_QUEUE_SIZE: int = 32
# ICY "no-change" frame: a zero length byte with no payload.
_ICY_NO_CHANGE_FRAME: bytes = b"\x00"
_PRODUCER_READY_TIMEOUT: float = 10.0


@dataclass(eq=False)
class _Subscriber:
    """A single HTTP listener subscribed to a station's broadcast."""

    queue: asyncio.Queue[bytes | None]
    want_icy: bool
    last_metadata_seen: bytes = b""


@dataclass
class _Station:
    """Runtime registration data for a single web radio station."""

    slug: str
    player_id: str
    player: WebRadioPlayer
    url_path: str
    unregister_route: Callable[[], None]
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    subscribers: set[_Subscriber] = field(default_factory=set)
    producer_task: asyncio.Task[None] | None = None
    producer_ready: asyncio.Event = field(default_factory=asyncio.Event)
    # Cooperative stop flag. Asks the producer to exit at the next chunk
    # boundary so ffmpeg's context manager cleans up without being cancelled
    # mid-pipeline (avoids the _UnixReadPipeTransport race on Python 3.14).
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    # Latest PlayerMedia the queue asked us to broadcast (set by play_media,
    # cleared by stop). The producer reads this instead of queue.current_item
    # so it cannot race the queue controller's mid-skip mutations.
    pending_media: PlayerMedia | None = None
    pending_media_event: asyncio.Event = field(default_factory=asyncio.Event)
    # Populated by the producer once output config is known.
    output_format: AudioFormat | None = None
    icy_preference: str = "disabled"
    icy_chunk_size: int = 0
    current_metadata: bytes = b""


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

    async def loaded_in_mass(self) -> None:
        """Register players and HTTP routes for all configured stations."""
        await self._sync_stations()

    async def unload(self, is_removed: bool = False) -> None:
        """
        Unregister all stations and stop their producers on provider unload.

        :param is_removed: True when the provider instance is being removed.
        """
        del is_removed
        for station in list(self._stations.values()):
            await self._cancel_producer(station)
            station.unregister_route()
            await self.mass.players.unregister(station.player_id)
        self._stations.clear()

    async def remove_player(self, player_id: str) -> None:
        """
        Remove a station and persist its removal in the provider config.

        :param player_id: MA player_id of the station to remove.
        """
        station = next(
            (s for s in self._stations.values() if s.player_id == player_id),
            None,
        )
        if station is None:
            await self.mass.players.unregister(player_id, True)
            return

        raw_names = self._raw_station_names()
        new_names = [name for name in raw_names if slugify_station_name(name) != station.slug]
        if new_names != raw_names:
            self._update_config_value(CONF_STATIONS, new_names)

        await self._cancel_producer(station)
        station.unregister_route()
        del self._stations[station.slug]
        await self.mass.players.unregister(player_id, True)

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
        if station is None:
            return
        codec = self._station_codec(station.player_id)
        new_path = f"{URL_PATH_PREFIX}{slug}.{codec}"
        if new_path == station.url_path:
            return
        station.unregister_route()
        station.url_path = new_path
        station.unregister_route = self.mass.streams.register_dynamic_route(
            new_path, self._handle_stream_request, "*"
        )
        station.player.set_stream_url(self._public_url(new_path))

    # ---------------------------------------------------------------
    # Station / route lifecycle
    # ---------------------------------------------------------------

    def _raw_station_names(self) -> list[str]:
        """Return the raw list of station names from provider config."""
        value = self.config.get_value(CONF_STATIONS)
        if not isinstance(value, list):
            return []
        return [str(name) for name in value if str(name).strip()]

    async def _sync_stations(self) -> None:
        """
        Reconcile registered stations with the provider config.

        Stations whose slug disappears from config are unregistered; new
        stations are created. Existing stations are left untouched.
        """
        configured: dict[str, str] = {}
        for name in self._raw_station_names():
            slug = slugify_station_name(name)
            if not slug:
                self.logger.warning(
                    "Ignoring station %r: name has no alphanumeric characters", name
                )
                continue
            if slug in configured:
                raise SetupFailedError(
                    f"Duplicate station slug {slug!r} from name {name!r}; "
                    "rename one of the stations so each has a unique URL."
                )
            configured[slug] = name

        for slug in list(self._stations):
            if slug not in configured:
                station = self._stations.pop(slug)
                await self._cancel_producer(station)
                station.unregister_route()
                await self.mass.players.unregister(station.player_id)

        for slug, name in configured.items():
            if slug in self._stations:
                continue
            await self._register_station(slug, name)

    async def _register_station(self, slug: str, display_name: str) -> None:
        """
        Register a virtual player and HTTP route for a single station.

        :param slug: URL-safe slug derived from the display name.
        :param display_name: Human-readable station name shown in the UI.
        """
        player_id = f"{PLAYER_ID_PREFIX}{slug}"
        player = WebRadioPlayer(
            provider=self,
            player_id=player_id,
            station_slug=slug,
            display_name=display_name,
        )
        await self.mass.players.register(player)

        codec = self._station_codec(player_id)
        url_path = f"{URL_PATH_PREFIX}{slug}.{codec}"
        unregister = self.mass.streams.register_dynamic_route(
            url_path, self._handle_stream_request, "*"
        )
        self._stations[slug] = _Station(
            slug=slug,
            player_id=player_id,
            player=player,
            url_path=url_path,
            unregister_route=unregister,
        )
        player.set_stream_url(self._public_url(url_path))

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

    def _station_codec(self, player_id: str) -> str:
        """Return the configured output codec extension for a player, e.g. ``"mp3"``."""
        codec = self.mass.config.get_raw_player_config_value(
            player_id, CONF_OUTPUT_CODEC, CONF_ENTRY_OUTPUT_CODEC_WEBRADIO.default_value
        )
        return str(codec or "mp3")

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
        raise web.HTTPNotFound(reason=f"Unknown web radio station: {request.path}")

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
            return _head_response(self._station_codec(station.player_id))

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
            # Source from the latest PlayerMedia handed to play_media;
            # queue.current_item races queue.session_id during PLAY_NOW/seek.
            queue, start_item = self._resolve_pending_media(station)
            if queue is None or start_item is None:
                station.producer_ready.set()
                return

            flow_pcm_format = await self.mass.streams.audio.select_flow_pcm_format(station.player)
            output_format = await self.mass.streams.audio.get_output_format(
                output_format_str=station.url_path.rsplit(".", 1)[-1],
                player=station.player,
                content_sample_rate=flow_pcm_format.sample_rate,
                content_bit_depth=flow_pcm_format.bit_depth,
                media_type=start_item.media_type,
            )

            icy_preference = str(
                self.mass.config.get_raw_player_config_value(
                    station.player_id,
                    CONF_ENTRY_ENABLE_ICY_METADATA_BASIC.key,
                    CONF_ENTRY_ENABLE_ICY_METADATA_BASIC.default_value,
                )
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
                # Only on first start: catch up to where the broadcast would
                # be now for a late-joining listener. On a restart MA already
                # set streamdetails.seek_position to the intended value.
                if first_iteration:
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
                    async with station.lock:
                        if not station.subscribers:
                            station.producer_task = None
                            station.producer_ready.clear()
                            clean_exit = True
                            return
                    # A subscriber raced back in while we were exiting.
                    continue

                # exit_reason == "ended". If the queue's session_id moved on we
                # were skipped past; otherwise the queue actually ran out.
                queue_data = self.mass.player_queues.queue_data_or_none(station.player_id)
                if queue_data is None:
                    break
                if queue_data.session_id == session_id_at_start:
                    break
                # Wait for the new play_media that owns the new session.
                with suppress(TimeoutError):
                    await asyncio.wait_for(station.pending_media_event.wait(), timeout=5.0)
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
        Run one ffmpeg flow-stream pipeline to completion.

        :param station: Station whose subscribers should receive the audio.
        :param queue: Active queue being streamed.
        :param start_item: Queue item from which the flow stream starts.
        :param flow_pcm_format: PCM format coming out of the queue flow.
        :param output_format: Encoded format pushed to subscribers.
        :param icy_preference: Per-station ICY mode (``disabled``/``basic``/``full``).
        :param chunk_size: Encoded chunk size in bytes; also the icy-metaint value.
        :return: ``"stopped"`` (user pressed stop/pause), ``"no_subscribers"``
            (last listener left), or ``"ended"`` (queue exhausted or session
            superseded by a skip).
        """
        async for chunk in get_ffmpeg_stream(
            audio_input=self.mass.streams.audio.get_queue_flow_stream(
                queue=queue,
                start_queue_item=start_item,
                pcm_format=flow_pcm_format,
            ),
            input_format=flow_pcm_format,
            output_format=output_format,
            # 1.0x avoids long-broadcast drift (core uses 1.1 which clients
            # absorb past TCP backpressure). 2s burst per restart cushions
            # listeners across the producer-restart gap on skip; bigger
            # bursts close the gap further at the cost of more skip latency.
            extra_input_args=["-readrate", "1.0", "-readrate_initial_burst", "2"],
            chunk_size=chunk_size,
        ):
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
        queue = self.mass.player_queues.get(station.player_id)
        if queue is None:
            return None, None
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

        station_name = _sanitize_header(station.player.display_name)
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

        # Shoutcast/Icecast-style framing: no Content-Length, no chunked
        # transfer encoding. DEFAULT_STREAM_HEADERS sets Connection: close.
        resp = web.StreamResponse(status=200, reason="OK", headers=headers)
        await resp.prepare(request)

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

                if not enable_icy:
                    continue

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


class WebRadioPlayer(Player):
    """A single web radio "station" exposed as a Music Assistant player."""

    def __init__(
        self,
        provider: WebRadioProvider,
        player_id: str,
        station_slug: str,
        display_name: str,
    ) -> None:
        """
        Initialize the station player.

        :param provider: Owning ``WebRadioProvider`` instance.
        :param player_id: Unique MA player_id (``webradio_<slug>``).
        :param station_slug: URL-safe slug for this station.
        :param display_name: Human-readable station name.
        """
        super().__init__(provider, player_id)
        self._station_slug = station_slug
        self._stream_url: str | None = None
        self._attr_name = display_name
        self._attr_supported_features = {PlayerFeature.PLAY_MEDIA}
        self._attr_device_info = DeviceInfo(
            model="Web Radio Station",
            manufacturer="Music Assistant",
        )
        # Override the auto-injected queue source so the UI gates seek and
        # pause off: seek is meaningless on a live broadcast (and was
        # observed to confuse some clients), and pause has no real meaning
        # on a broadcast either - the UI shows stop instead. Next/previous
        # remain available.
        self._attr_source_list = [
            PlayerSource(
                id=player_id,
                name="Music Assistant Queue",
                passive=False,
                can_play_pause=False,
                can_seek=False,
                can_next_previous=True,
            ),
        ]

    @property
    def requires_flow_mode(self) -> bool:
        """A station is always consumed via a flow stream, listener or not."""
        return True

    @property
    def station_slug(self) -> str:
        """Return the URL-safe slug of this station."""
        return self._station_slug

    def set_stream_url(self, url: str) -> None:
        """Cache the public URL so it can be shown in the player's settings."""
        self._stream_url = url

    async def get_config_entries(
        self,
        action: str | None = None,
        values: dict[str, ConfigValueType] | None = None,
    ) -> list[ConfigEntry]:
        """Return player-level config entries."""
        del action, values
        return [
            CONF_ENTRY_OUTPUT_CODEC_WEBRADIO,
            CONF_ENTRY_ENABLE_ICY_METADATA_BASIC,
            ConfigEntry(
                key=CONF_STREAM_URL_LABEL,
                type=ConfigEntryType.LABEL,
                translation_params=[self._stream_url or "(unavailable)"],
            ),
        ]

    async def on_config_updated(self) -> None:
        """Refresh the published URL when the codec config changes."""
        provider = self.provider
        if isinstance(provider, WebRadioProvider):
            provider.refresh_station_route(self._station_slug)

    async def play_media(self, media: PlayerMedia) -> None:
        """Mark the station as playing and start the wallclock progress anchor."""
        # The station has no external sink: audio only leaves the box when an
        # HTTP listener connects. We still anchor elapsed_time here so MA's
        # queue controller can wallclock-advance current_item (and therefore
        # the artwork/title shown in the UI) as if the queue were playing.
        self._attr_current_media = media
        self._attr_playback_state = PlaybackState.PLAYING
        self._attr_elapsed_time = 0
        self._attr_elapsed_time_last_updated = time.time()
        # Pre-set queue.flow_mode so the queue controller does not schedule
        # enqueue_next_media during the listener-not-yet-connected window
        # (PlayerFeature.ENQUEUE is intentionally not advertised by this
        # player). get_queue_flow_stream sets the same flag once a listener
        # attaches and the producer runs for real.
        queue = self.mass.player_queues.get(self.player_id)
        if queue is not None:
            queue.flow_mode = True
        self.update_state()
        provider = self.provider
        if isinstance(provider, WebRadioProvider):
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
        self._attr_playback_state = PlaybackState.IDLE
        self._attr_current_media = None
        self._attr_elapsed_time = None
        self._attr_elapsed_time_last_updated = None
        self.update_state()
        provider = self.provider
        if isinstance(provider, WebRadioProvider):
            provider.clear_pending_media(self._station_slug)
            provider.stop_producer(self._station_slug)
