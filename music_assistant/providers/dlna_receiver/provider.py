"""
DLNA Receiver — Main provider implementation.

Registers as a PluginProvider with AUDIO_SOURCE feature so that audio
received from external DLNA control points is routed through the MA
streaming pipeline to any configured player.

Supports multi-player mode: one virtual DLNA renderer per MA player,
each with a unique UDN, HTTP port, and SSDP advertisement.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import time
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, ClassVar
from urllib.parse import urljoin

import aiohttp
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    IdentifierType,
    MediaType,
    ProviderFeature,
    QueueOption,
    StreamType,
)
from music_assistant_models.errors import (
    AudioError,
    MediaNotFoundError,
    MusicAssistantError,
    SetupFailedError,
)
from music_assistant_models.helpers import create_uri
from music_assistant_models.media_items import AudioSource, ProviderMapping
from music_assistant_models.media_items.audio_format import AudioFormat
from music_assistant_models.streamdetails import StreamDetails, StreamMetadata

from music_assistant.constants import CONF_BIND_IP
from music_assistant.helpers.util import get_ip_addresses
from music_assistant.models.plugin import PluginProvider

from .constants import (
    CONF_FRIENDLY_NAME,
    CONF_HTTP_PORT,
    CONF_TARGET_PLAYERS,
    DEFAULT_FRIENDLY_NAME,
    DEFAULT_HTTP_PORT,
    TRANSPORT_STATE_PAUSED,
    TRANSPORT_STATE_PLAYING,
    TRANSPORT_STATE_STOPPED,
)
from .lifecycle import (
    RendererCallbacks,
    RendererRegistry,
    deterministic_udn,
    normalize_udn_uuid,
)
from .metadata import (
    clear_playback,
    freeze_elapsed,
    parse_didl_metadata,
    parse_duration,
    position_for,
)
from .models import RendererInstance
from .urls import redact_url as _redact_url
from .urls import validate_outbound_url as _validate_outbound_url

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models.player import Player

LOGGER = logging.getLogger(__name__)


def _is_concrete_ipv4(value: str) -> bool:
    """
    Return True iff ``value`` is a non-wildcard, non-loopback IPv4 literal.

    SSDP uses ``socket.inet_aton`` + ``IP_ADD_MEMBERSHIP`` which require a
    concrete IPv4 interface; ``0.0.0.0`` joins the multicast group on the
    wrong interface (silently dropping alive packets on multi-homed hosts),
    ``127.0.0.1`` joins on the loopback interface where SSDP multicast
    never reaches real DLNA control points, and IPv6 / hostnames fail
    outright.
    """
    if not value:
        return False
    try:
        addr = ipaddress.ip_address(value)
    except ValueError:
        return False
    return (
        isinstance(addr, ipaddress.IPv4Address) and not addr.is_unspecified and not addr.is_loopback
    )


class DLNAReceiverProvider(PluginProvider):
    """
    DLNA Receiver plugin provider for Music Assistant.

    Exposes MA as one or more UPnP MediaRenderers on the local network
    so that external apps can send audio streams which are then played
    on the corresponding MA player.
    """

    SUPPORTED_FEATURES: ClassVar[set[ProviderFeature]] = {ProviderFeature.AUDIO_SOURCE}

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
    ) -> None:
        """Initialize provider state before asynchronous setup."""
        super().__init__(mass, manifest, config, self.SUPPORTED_FEATURES)
        self._instances: dict[str, RendererInstance] = {}
        self._registry: RendererRegistry | None = None
        # Exclusive stream claims: source_id -> (queue_id, stream_session_id).
        # Claimed in on_source_selected (never in get_stream_details, which
        # must stay side-effect-free for queue preload), released in
        # on_source_unselected when the session token matches.
        self._claims: dict[str, tuple[str, str]] = {}
        self._metadata_task: asyncio.Task[None] | None = None

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return editable options for this provider instance."""
        return (
            ConfigEntry(
                key=CONF_FRIENDLY_NAME,
                type=ConfigEntryType.STRING,
                default_value=DEFAULT_FRIENDLY_NAME,
                required=True,
            ),
            ConfigEntry(
                key=CONF_TARGET_PLAYERS,
                type=ConfigEntryType.STRING,
                default_value=[],
                required=False,
                options=self._target_player_options(),
                multi_value=True,
            ),
            ConfigEntry(
                key=CONF_BIND_IP,
                type=ConfigEntryType.STRING,
                required=False,
            ),
            ConfigEntry(
                key=CONF_HTTP_PORT,
                type=ConfigEntryType.INTEGER,
                default_value=DEFAULT_HTTP_PORT,
                required=True,
            ),
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def handle_async_init(self) -> None:
        """Initialize renderer instances during the awaited provider load phase."""
        self._friendly_prefix = str(
            self.config.get_value(CONF_FRIENDLY_NAME) or DEFAULT_FRIENDLY_NAME,
        )
        configured_ip = str(self.config.get_value(CONF_BIND_IP) or "")
        if configured_ip:
            self._bind_ip = configured_ip
        else:
            # Reuse MA's shared primary-IP helper (threaded probe + ranked
            # interface scan) instead of rolling our own. Prefer the first
            # IPv4 entry: SSDP needs a routable IPv4 for the multicast
            # interface and the LOCATION header.
            ip_addresses = await get_ip_addresses()
            self._bind_ip = next(
                (ip for ip in ip_addresses if _is_concrete_ipv4(ip)),
                "",
            )
        # Fail fast with a clear message instead of letting SSDP
        # inet_aton / IP_ADD_MEMBERSHIP explode on 0.0.0.0, an IPv6
        # literal, or a typo from the user.
        if not _is_concrete_ipv4(self._bind_ip):
            msg = (
                "DLNA Receiver requires a concrete IPv4 bind address; got "
                f"{self._bind_ip!r}. Configure a valid IPv4 for this host, "
                "or run MA on a network that exposes an IPv4 interface."
            )
            raise SetupFailedError(msg)
        base_port = int(
            self.config.get_value(CONF_HTTP_PORT) or DEFAULT_HTTP_PORT  # type: ignore[arg-type]
        )
        self._registry = RendererRegistry(
            mass=self.mass,
            target_player_ids=self._configured_target_player_ids(),
            friendly_prefix=self._friendly_prefix,
            bind_ip=self._bind_ip,
            base_port=base_port,
            callbacks=RendererCallbacks(
                on_set_av_transport_uri=self._on_set_transport_uri,
                on_play=self._on_play,
                on_pause=self._on_pause,
                on_stop=self._on_stop,
                on_get_position=position_for,
                on_set_volume=self._on_set_volume,
                on_set_mute=self._on_set_mute,
                on_instance_removed=self._on_instance_removed,
            ),
        )
        self._instances = self._registry.instances
        await self._registry.start()

        if not self._instances:
            LOGGER.info(
                "No target players are available yet; waiting for player registration",
            )

        LOGGER.info(
            "DLNA Receiver started: %d renderer(s) on %s (base port %s)",
            len(self._instances),
            self._bind_ip,
            base_port,
        )

    async def unload(self, is_removed: bool = False) -> None:
        """
        Unload the provider — stop all renderer instances.

        Cancels the metadata task before renderer shutdown.
        """
        if self._metadata_task is not None and not self._metadata_task.done():
            self._metadata_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._metadata_task
        self._metadata_task = None
        if self._registry is not None:
            await self._registry.stop()
            self._registry = None
        LOGGER.info("DLNA Receiver provider unloaded")

    # ------------------------------------------------------------------
    # PluginProvider audio source interface
    # ------------------------------------------------------------------

    async def get_audio_sources(self) -> list[AudioSource]:
        """Return one AudioSource per virtual DLNA renderer."""
        return [
            self._audio_source_for(source_id, inst) for source_id, inst in self._instances.items()
        ]

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """
        Return StreamDetails for streaming the received DLNA audio.

        :param item_id: The AudioSource.item_id requested for playback.
        :param media_type: The requested media type, which must be AUDIO_SOURCE.
        :raises MediaNotFoundError: If the media type or source id is unsupported.
        :raises AudioError: If no DLNA sender has pushed a stream URL yet.
        """
        if media_type is not MediaType.AUDIO_SOURCE:
            raise MediaNotFoundError(f"Unsupported media type: {media_type}")
        inst = self._instances.get(item_id)
        if inst is None:
            raise MediaNotFoundError(f"Unknown AudioSource: {item_id}")
        if not inst.current_stream_url:
            raise AudioError(
                "DLNA renderer has no active stream — start casting from the sender app first"
            )
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=self._probe_audio_format(),
            media_type=MediaType.AUDIO_SOURCE,
            stream_type=StreamType.CUSTOM,
            stream_metadata=inst.stream_metadata,
        )

    async def get_audio_stream(
        self,
        streamdetails: StreamDetails,
        seek_position: int = 0,
    ) -> AsyncGenerator[bytes]:
        """
        Yield the audio bytes for an active DLNA stream.

        :param streamdetails: The StreamDetails previously returned by get_stream_details.
        :param seek_position: Ignored — the incoming DLNA stream cannot be seeked.
        :raises AudioError: If the source has no active stream or the upstream
            URL cannot be fetched.
        """
        del seek_position  # live source — no seeking through the bytestream
        source_id = streamdetails.item_id
        inst = self._instances.get(source_id)
        stream_url = inst.current_stream_url if inst else None

        # Raise instead of yielding nothing: the server may reuse cached
        # StreamDetails and skip get_stream_details' guard, so this is the
        # last place a stale replay can be surfaced as a proper error.
        if not stream_url:
            raise AudioError(
                f"DLNA source {source_id} has no active stream — "
                "start casting from the sender app first"
            )

        LOGGER.debug("Proxying DLNA stream for %s: %s", source_id, _redact_url(stream_url))
        # total=None: streams may be long-running; bound connect + per-chunk read only.
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=30)
        bytes_streamed = False
        # Reuse MA's shared HTTP session (matches streams/audio.py) so we
        # don't open a fresh TCP connector + DNS cache per activation. The
        # policy lookup and the shared connector's lookup are not pinned to
        # one DNS answer, so a residual DNS-rebinding TOCTOU window remains.
        try:
            current_url = stream_url
            for redirect_count in range(6):
                safe_url = await _validate_outbound_url(current_url)
                if safe_url is None:
                    raise AudioError(
                        f"Outbound DLNA source destination is not allowed: "
                        f"{_redact_url(current_url)}"
                    )
                async with self.mass.http_session.get(
                    safe_url,
                    timeout=timeout,
                    allow_redirects=False,
                ) as resp:
                    if 300 <= resp.status < 400:
                        if redirect_count >= 5:
                            raise AudioError("Upstream DLNA source exceeded five redirects")
                        location = resp.headers.get("Location")
                        if not location:
                            raise AudioError("Upstream DLNA source redirect has no Location")
                        try:
                            current_url = urljoin(safe_url, location)
                        except ValueError as err:
                            raise AudioError("Upstream DLNA source redirect is invalid") from err
                        continue
                    # Accept any 2xx (e.g. 206 Partial Content is common for audio).
                    if not 200 <= resp.status < 300:
                        raise AudioError(
                            f"Upstream DLNA source returned HTTP {resp.status} for "
                            f"{_redact_url(safe_url)}"
                        )
                    async for chunk in resp.content.iter_any():
                        bytes_streamed = True
                        yield chunk
                    break
        except (aiohttp.ClientError, TimeoutError) as err:
            # A drop mid-stream just ends the stream (the sender went away);
            # a failure before the first byte is a real error to surface.
            if not bytes_streamed:
                raise AudioError(f"Could not fetch DLNA stream {_redact_url(stream_url)}") from err
            LOGGER.warning(
                "Error proxying DLNA stream %s",
                _redact_url(stream_url),
                exc_info=True,
            )
            return
        LOGGER.debug("DLNA stream ended for %s", source_id)

    async def on_source_selected(
        self,
        source_id: str,
        player_id: str,
        queue_id: str,
        stream_session_id: str,
    ) -> None:
        """
        Claim the renderer's stream for the queue that starts consuming it.

        :param source_id: The AudioSource.item_id that was selected.
        :param player_id: The player that will receive the stream.
        :param queue_id: The queue that owns this playback session.
        :param stream_session_id: Controller token paired with the matching
            on_source_unselected call.
        """
        del player_id
        if source_id not in self._instances:
            return
        # The source is exclusive: on a cross-queue takeover stop the previous
        # consumer before replacing its claim (its late on_source_unselected
        # is rejected by the session-id guard).
        previous = self._claims.get(source_id)
        if previous and previous[0] != queue_id:
            try:
                await self.mass.players.cmd_stop(previous[0])
            except MusicAssistantError as err:
                LOGGER.debug("Could not stop previous consumer %s: %s", previous[0], err)
        self._claims[source_id] = (queue_id, stream_session_id)
        self._ensure_metadata_task()

    async def on_source_unselected(
        self,
        source_id: str,
        queue_id: str,
        stream_session_id: str,
    ) -> None:
        """
        Release the stream claim when MA tears down the queue's stream.

        :param source_id: The AudioSource.item_id whose stream ended.
        :param queue_id: The queue whose stream is being torn down.
        :param stream_session_id: Token paired with on_source_selected; stale
            tokens from a superseded request are ignored.
        """
        claim = self._claims.get(source_id)
        if claim != (queue_id, stream_session_id):
            return
        del self._claims[source_id]
        # MA stopped consuming: stop elapsed tracking so the metadata loop
        # doesn't tick forever on state nobody reads. The next SOAP Play
        # rebuilds the stream metadata from the sender's DIDL.
        inst = self._instances.get(source_id)
        if inst:
            await inst.renderer.set_transport_state(TRANSPORT_STATE_STOPPED)
            clear_playback(inst)

    def _configured_target_player_ids(self) -> frozenset[str]:
        """Return the configured exact player allowlist, or empty for all players."""
        value = self.config.get_value(CONF_TARGET_PLAYERS)
        if not isinstance(value, list):
            return frozenset()
        return frozenset(item for item in value if isinstance(item, str) and item)

    def _target_player_options(self) -> list[ConfigValueOption]:
        """Return selectable MA players plus unavailable saved selections."""
        selected_ids = self._configured_target_player_ids()
        players: list[Player] = self.mass.players.all_players(
            return_unavailable=True,
            return_protocol_players=False,
        )
        candidate_uuids = {
            normalize_udn_uuid(deterministic_udn(player_id))
            for player_id in ({player.player_id for player in players} | set(selected_ids))
        }
        available_players = [
            player
            for player in players
            if not (
                (identifier := player.device_info.identifiers.get(IdentifierType.UUID))
                and normalize_udn_uuid(identifier) in candidate_uuids
            )
        ]
        options = [
            ConfigValueOption(player.player_id, title=player.display_name)
            for player in available_players
        ]
        available_ids = {player.player_id for player in available_players}
        options.extend(
            ConfigValueOption(player_id, title=player_id, disabled=True)
            for player_id in selected_ids - available_ids
        )
        return sorted(options, key=lambda option: (str(option.title).casefold(), str(option.value)))

    # ------------------------------------------------------------------
    # AudioSource helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _probe_audio_format() -> AudioFormat:
        # Upstream DLNA senders push arbitrary compressed formats
        # (FLAC/MP3/AAC/WAV/etc). Declare content_type/codec as UNKNOWN
        # so MA's ffmpeg input pipeline probes the actual codec from
        # stdin instead of misinterpreting compressed bytes as PCM.
        return AudioFormat(
            content_type=ContentType.UNKNOWN,
            codec_type=ContentType.UNKNOWN,
        )

    @staticmethod
    def _source_id_for(inst: RendererInstance) -> str:
        """Return the AudioSource item_id for a renderer instance."""
        return inst.player_id or "__default__"

    def _audio_source_for(self, source_id: str, inst: RendererInstance) -> AudioSource:
        """Build the AudioSource media item exposed for a renderer instance."""
        return AudioSource(
            item_id=source_id,
            provider=self.instance_id,
            name=inst.renderer.friendly_name,
            provider_mappings={
                ProviderMapping(
                    item_id=source_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    audio_format=self._probe_audio_format(),
                )
            },
            # Playback is initiated by the external DLNA sender through the
            # renderer bound to this player.
            can_initiate=False,
            allow_external_trigger=True,
            exclusive=True,
        )

    # ------------------------------------------------------------------
    # Renderer callbacks (per-instance)
    # ------------------------------------------------------------------

    async def _on_set_transport_uri(
        self,
        inst: RendererInstance,
        uri: str,
        metadata: str | None,
    ) -> None:
        """
        Handle SetAVTransportURI for a specific renderer instance.

        :param inst: Renderer instance owning the transport.
        :param uri: Stream URI received from the control point.
        :param metadata: Optional DIDL-Lite metadata attached to the
            transport URI.
        :raises ValueError: If the URI is not a safe ``http(s)`` stream
            URL. The renderer turns this into SOAP fault 716 so the
            control point sees the rejection instead of a silent
            200 OK.
        """
        safe_url = await _validate_outbound_url(uri)
        if safe_url is None:
            LOGGER.warning(
                "Rejecting transport URI for '%s' (destination not allowed): %s",
                inst.player_name or "(default)",
                _redact_url(uri),
            )
            raise ValueError("unsupported or disallowed stream destination")
        LOGGER.info(
            "Received transport URI for '%s': %s",
            inst.player_name or "(default)",
            _redact_url(safe_url),
        )
        inst.current_stream_url = safe_url
        inst.current_metadata = parse_didl_metadata(metadata)

    async def _on_play(self, inst: RendererInstance, previous_state: str) -> bool:
        """Handle Play — start streaming to this instance's player."""
        target = inst.player_id
        if not target:
            LOGGER.warning("No target player bound — ignoring Play")
            return False

        if not inst.current_stream_url:
            LOGGER.warning("Play received but no stream URL for %s", target)
            return False

        if previous_state == TRANSPORT_STATE_PLAYING:
            LOGGER.debug("Player %s is already playing; ignoring duplicate Play", target)
            return True

        if previous_state == TRANSPORT_STATE_PAUSED:
            LOGGER.info("Resuming playback on player %s", target)
            try:
                await self.mass.players.cmd_play(target)
            except MusicAssistantError as err:
                LOGGER.warning("Could not resume playback on %s: %s", target, err)
                return False
            inst.play_start_time = time.time()
            inst.metadata_dirty = True
            self._ensure_metadata_task()
            return True

        LOGGER.info("Starting playback on player %s", target)
        meta = inst.current_metadata or {}
        LOGGER.debug("DIDL metadata for %s: %s", target, meta)
        duration = parse_duration(meta.get("duration"))

        # Stream metadata for MA UI display; travels with the StreamDetails
        # and is refreshed via update_stream_metadata by the metadata loop.
        inst.stream_metadata = StreamMetadata(
            title=meta.get("title") or "DLNA Stream",
            artist=meta.get("artist"),
            album=meta.get("album"),
            image_url=meta.get("image_url"),
            duration=duration,
            uri=inst.current_stream_url,
            elapsed_time=0,
            elapsed_time_last_updated=time.time(),
        )

        # Track playback state for elapsed time
        source_id = self._source_id_for(inst)
        inst.play_start_time = time.time()
        inst.elapsed_offset = 0
        inst.metadata_dirty = True

        # Route through the AudioSource item so MA pulls bytes via our
        # get_audio_stream() proxy instead of handing the raw upstream
        # URI to the player (which would bypass the SSRF/redirect guards
        # and fail on players that require MA to serve the stream).
        # QueueOption.PLAY overrides any user-configured default enqueue
        # option — the sender expects immediate playback, not enqueueing.
        source_uri = create_uri(MediaType.AUDIO_SOURCE, self.instance_id, source_id)
        try:
            await self.mass.player_queues.play_media(target, source_uri, option=QueueOption.PLAY)
        except MusicAssistantError as err:
            clear_playback(inst)
            LOGGER.warning("Could not start playback on %s: %s", target, err)
            return False
        self._ensure_metadata_task()
        return True

    async def _on_pause(self, inst: RendererInstance) -> None:
        """Handle Pause for this instance's player."""
        if inst.player_id:
            freeze_elapsed(inst)
            inst.metadata_dirty = True
            await self.mass.players.cmd_pause(inst.player_id)

    async def _on_stop(self, inst: RendererInstance) -> None:
        """Handle Stop for this instance's player."""
        if inst.player_id:
            await self.mass.players.cmd_stop(inst.player_id)
        clear_playback(inst)

    async def _on_set_volume(self, inst: RendererInstance, volume: int) -> None:
        """Handle volume change for this instance's player."""
        if inst.player_id:
            await self.mass.players.cmd_volume_set(inst.player_id, volume)

    async def _on_set_mute(self, inst: RendererInstance, mute: bool) -> None:
        """Handle mute change for this instance's player."""
        if inst.player_id:
            await self.mass.players.cmd_volume_mute(inst.player_id, mute)

    def _on_instance_removed(self, source_id: str, inst: RendererInstance) -> None:
        """Clear provider-owned state when a player renderer disappears."""
        self._claims.pop(source_id, None)
        clear_playback(inst)

    @staticmethod
    def _should_push_metadata(inst: RendererInstance, now: float) -> bool:
        """
        Return True when the instance's metadata warrants a queue push.

        Real changes (new track, pause/resume) push immediately; elapsed-only
        ticks are covered by a periodic resync since clients extrapolate the
        position from elapsed_time_last_updated locally.
        """
        return inst.metadata_dirty or (now - inst.last_metadata_push) >= 30

    def _ensure_metadata_task(self) -> None:
        """Start the metadata update loop if not already running."""
        if self._metadata_task and not self._metadata_task.done():
            return
        self._metadata_task = self.mass.create_task(
            self._metadata_update_loop,
            task_id="dlna_receiver_metadata_updates",
        )

    async def _metadata_update_loop(self) -> None:
        """
        Periodically update elapsed time and push it to the claiming queues.

        Exits on its own when no renderer instance has active stream metadata
        left; restarted on demand by _ensure_metadata_task.
        """
        while True:
            await asyncio.sleep(2)
            active = [
                (source_id, inst)
                for source_id, inst in self._instances.items()
                if inst.stream_metadata is not None
            ]
            if not active:
                break

            now = time.time()
            for source_id, inst in active:
                metadata = inst.stream_metadata
                if metadata is None:
                    continue
                metadata.elapsed_time, _duration = position_for(inst, now)
                metadata.elapsed_time_last_updated = now

                if inst.player_id and not self.mass.players.get_player(inst.player_id):
                    LOGGER.debug("Metadata loop: player %s gone", inst.player_id)
                    clear_playback(inst)
                    continue

                claim = self._claims.get(source_id)
                if claim and self._should_push_metadata(inst, now):
                    self.mass.streams.update_stream_metadata(
                        claim[0],
                        source_id,
                        self.instance_id,
                        metadata,
                    )
                    inst.metadata_dirty = False
                    inst.last_metadata_push = now
