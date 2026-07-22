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
import uuid
import xml.etree.ElementTree as ET
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from html import unescape
from typing import TYPE_CHECKING, ClassVar

import aiohttp
from music_assistant_models.config_entries import ConfigValueType  # noqa: F401
from music_assistant_models.enums import (
    ContentType,
    MediaType,
    ProviderFeature,
    QueueOption,
    StreamType,
)
from music_assistant_models.errors import AudioError, MediaNotFoundError, SetupFailedError
from music_assistant_models.helpers import create_uri
from music_assistant_models.media_items import AudioSource, ProviderMapping
from music_assistant_models.media_items.audio_format import AudioFormat
from music_assistant_models.streamdetails import StreamDetails, StreamMetadata

from music_assistant.helpers.util import get_ip_addresses
from music_assistant.models.plugin import PluginProvider

from .constants import (
    CONF_BIND_IP,
    CONF_FRIENDLY_NAME,
    CONF_HTTP_PORT,
    CONF_TARGET_PLAYER,
    CONF_TARGET_PLAYERS,
    DEFAULT_FRIENDLY_NAME,
    DEFAULT_HTTP_PORT,
    TRANSPORT_STATE_PAUSED,
    TRANSPORT_STATE_PLAYING,
    UDN_NAMESPACE,
)
from .renderer import UPnPRenderer
from .ssdp import SSDPAdvertiser
from .urls import redact_url as _redact_url
from .urls import validate_stream_url as _validate_stream_url

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant

LOGGER = logging.getLogger(__name__)

# DIDL-Lite metadata is normally < 4 KiB; bound input (measured in characters)
# to guard CPU/memory on parse.
_MAX_DIDL_CHARS = 64 * 1024


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


@dataclass
class RendererInstance:
    """Per-player renderer state: UPnP renderer + SSDP + streaming context."""

    player_id: str
    player_name: str
    renderer: UPnPRenderer
    ssdp: SSDPAdvertiser
    current_stream_url: str | None = None
    current_metadata: dict[str, str | None] | None = None
    stream_metadata: StreamMetadata | None = None
    play_start_time: float | None = None
    elapsed_offset: int = 0
    # metadata_dirty marks a real change (new track / pause / resume) that must
    # be pushed to the queue item; elapsed-only ticks are resynced periodically.
    metadata_dirty: bool = False
    last_metadata_push: float = 0.0


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
        """Initialize provider state; renderer instances are created in loaded_in_mass."""
        super().__init__(mass, manifest, config, self.SUPPORTED_FEATURES)
        self._instances: dict[str, RendererInstance] = {}
        # Exclusive stream claims: source_id -> (queue_id, stream_session_id).
        # Claimed in on_source_selected (never in get_stream_details, which
        # must stay side-effect-free for queue preload), released in
        # on_source_unselected when the session token matches.
        self._claims: dict[str, tuple[str, str]] = {}
        self._metadata_task: asyncio.Task[None] | None = None
        self._discovery_task: asyncio.Task[None] | None = None
        # Monotonically bumped per renderer; assigned lazily in loaded_in_mass.
        self._next_port: int = 0

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return supported features."""
        return {ProviderFeature.AUDIO_SOURCE}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def loaded_in_mass(self) -> None:
        """Initialize renderer instances when loaded in Music Assistant."""
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
        self._base_port = int(
            self.config.get_value(CONF_HTTP_PORT) or DEFAULT_HTTP_PORT  # type: ignore[arg-type]
        )
        self._next_port = self._base_port

        raw_target = self._raw_target()
        player_specs = self._resolve_player_specs()

        for pid, pname in player_specs:
            await self._create_instance(
                player_id=pid,
                player_name=pname,
                friendly_prefix=self._friendly_prefix,
                bind_ip=self._bind_ip,
                http_port=self._next_port,
            )
            self._next_port += 1

        if not player_specs:
            LOGGER.info(
                "No target players are available yet; waiting for player registration",
            )

        # For target_players=*, keep looking for late-registering players in the
        # background instead of blocking startup.
        if raw_target == "*":
            self._discovery_task = asyncio.create_task(self._adopt_late_players())

        LOGGER.info(
            "DLNA Receiver started: %d renderer(s) on %s (base port %s)",
            len(self._instances),
            self._bind_ip,
            self._base_port,
        )

    async def unload(self, is_removed: bool = False) -> None:
        """
        Unload the provider — stop all renderer instances.

        Cancels and *awaits* background tasks before touching ``_instances``:
        otherwise ``_adopt_late_players`` could still be mid-``_create_instance``
        and append a new entry to the dict while we're iterating it, which
        raises ``RuntimeError`` and leaks sockets.
        """
        for task in (self._metadata_task, self._discovery_task):
            if task is None or task.done():
                continue
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._metadata_task = None
        self._discovery_task = None
        # Snapshot before iterating: no more background mutation possible now,
        # but cheap defense against future concurrent shutdown paths.
        for inst in list(self._instances.values()):
            await inst.ssdp.stop()
            await inst.renderer.stop()
        self._instances.clear()
        LOGGER.info("DLNA Receiver provider unloaded")

    # ------------------------------------------------------------------
    # PluginProvider audio source interface
    # ------------------------------------------------------------------

    async def get_audio_sources(self) -> list[AudioSource]:
        """Return one AudioSource per virtual DLNA renderer."""
        return [
            self._audio_source_for(source_id, inst) for source_id, inst in self._instances.items()
        ]

    async def get_stream_details(self, source_id: str, queue_id: str) -> StreamDetails:
        """
        Return StreamDetails for streaming the received DLNA audio.

        :param source_id: The AudioSource.item_id requested for playback.
        :param queue_id: The queue that owns this playback session.
        :raises MediaNotFoundError: If the source id matches no renderer.
        :raises AudioError: If no DLNA sender has pushed a stream URL yet.
        """
        del queue_id  # ownership is claimed in on_source_selected
        inst = self._instances.get(source_id)
        if inst is None:
            raise MediaNotFoundError(f"Unknown AudioSource: {source_id}")
        if not inst.current_stream_url:
            raise AudioError(
                "DLNA renderer has no active stream — start casting from the sender app first"
            )
        return StreamDetails(
            provider=self.instance_id,
            item_id=source_id,
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
        # don't open a fresh TCP connector + DNS cache per activation.
        try:
            async with self.mass.http_session.get(stream_url, timeout=timeout) as resp:
                # Re-validate after any redirects: the final URL still has to
                # be an http(s) endpoint, otherwise refuse to stream. (aiohttp
                # won't follow non-http schemes, but belt-and-suspenders.)
                final_url = str(resp.url)
                if _validate_stream_url(final_url) is None:
                    raise AudioError(
                        f"Upstream DLNA source redirected to disallowed URL: "
                        f"{_redact_url(final_url)}"
                    )
                # Accept any 2xx (e.g. 206 Partial Content is common for audio).
                if not 200 <= resp.status < 300:
                    raise AudioError(
                        f"Upstream DLNA source returned HTTP {resp.status} for "
                        f"{_redact_url(stream_url)}"
                    )
                async for chunk in resp.content.iter_any():
                    bytes_streamed = True
                    yield chunk
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
            except Exception as err:
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
            self._clear_instance_playback(inst)

    # ------------------------------------------------------------------
    # Instance management
    # ------------------------------------------------------------------

    async def _adopt_late_players(self) -> None:
        """
        Poll for newly-registered MA players and spin up renderers for them.

        Runs only when ``target_players=*``. Caps the total wait at ~5 minutes
        so stale provider instances don't poll forever.
        """
        known_ids = {inst.player_id for inst in self._instances.values() if inst.player_id}
        try:
            for _ in range(60):  # ~5 minutes at 5s intervals
                await asyncio.sleep(5)
                specs = self._resolve_player_specs()
                new = [(pid, name) for pid, name in specs if pid and pid not in known_ids]
                for pid, name in new:
                    await self._create_instance(
                        player_id=pid,
                        player_name=name,
                        friendly_prefix=self._friendly_prefix,
                        bind_ip=self._bind_ip,
                        http_port=self._next_port,
                    )
                    self._next_port += 1
                    known_ids.add(pid)
        except asyncio.CancelledError:
            pass
        except Exception:
            LOGGER.debug("Late-player discovery loop error", exc_info=True)

    async def _create_instance(
        self,
        player_id: str,
        player_name: str,
        friendly_prefix: str,
        bind_ip: str,
        http_port: int,
    ) -> RendererInstance:
        """Create and start a single renderer instance for a player."""
        friendly_name = f"{friendly_prefix} — {player_name}" if player_name else friendly_prefix

        udn = self._deterministic_udn(player_id)

        renderer = UPnPRenderer(
            friendly_name=friendly_name,
            bind_ip=bind_ip,
            http_port=http_port,
            udn=udn,
            # Reuse MA's shared HTTP session so every GENA NOTIFY across
            # all three services on every renderer shares one connector.
            session=self.mass.http_session,
        )

        inst = RendererInstance(
            player_id=player_id,
            player_name=player_name,
            renderer=renderer,
            ssdp=SSDPAdvertiser(
                udn=udn,
                description_url=renderer.description_url,
                bind_ip=bind_ip,
            ),
        )

        # Wire SOAP callbacks bound to this instance
        renderer.on_set_av_transport_uri = lambda uri, meta: self._on_set_transport_uri(
            inst, uri, meta
        )
        renderer.on_play = lambda previous_state: self._on_play(inst, previous_state)
        renderer.on_pause = lambda: self._on_pause(inst)
        renderer.on_stop = lambda: self._on_stop(inst)
        renderer.on_seek = lambda unit, target: self._on_seek(inst, unit, target)
        renderer.on_set_volume = lambda vol: self._on_set_volume(inst, vol)
        renderer.on_set_mute = lambda m: self._on_set_mute(inst, m)

        # Roll back a partially-started instance so a failed SSDP start
        # (port collision, multicast permission denied, etc.) doesn't
        # leave the HTTP runner holding the port across reloads.
        await renderer.start()
        # If http_port was 0, renderer.start() just learned the ephemeral
        # port from the bound socket; refresh the cached SSDP LOCATION so
        # alive/M-SEARCH responses advertise the real port instead of
        # whatever description_url resolved to at construction time.
        inst.ssdp.description_url = renderer.description_url
        try:
            await inst.ssdp.start()
        except Exception:
            with contextlib.suppress(Exception):
                await inst.ssdp.stop()
            with contextlib.suppress(Exception):
                await renderer.stop()
            raise

        self._instances[self._source_id_for(inst)] = inst

        LOGGER.info(
            "Renderer '%s' → player '%s' on port %d (UDN: %s)",
            friendly_name,
            player_id or "(none)",
            http_port,
            udn,
        )
        return inst

    def _raw_target(self) -> str:
        """
        Return the configured target spec, defaulting to all players.

        Centralizes the CONF_TARGET_PLAYERS → CONF_TARGET_PLAYER fallback so
        every call site (late-player adoption check, spec resolution) sees
        the same value; otherwise a legacy ``"*"`` config would skip the
        _adopt_late_players background task.
        """
        raw = str(self.config.get_value(CONF_TARGET_PLAYERS) or "").strip()
        if not raw:
            raw = str(self.config.get_value(CONF_TARGET_PLAYER) or "").strip()
        return raw or "*"

    def _resolve_player_specs(self) -> list[tuple[str, str]]:
        """
        Resolve configured player targets to (player_id, display_name) pairs.

        Supports:
        - Empty / not set → all currently known MA players
        - "*" → all currently known MA players
        - Comma-separated player_ids
        - Legacy CONF_TARGET_PLAYER (single player_id, backward compat)
        """
        raw = self._raw_target()

        if raw == "*":
            return self._get_all_players()

        # Comma-separated list (order-preserving dedupe: repeated ids would
        # collide on the same instance key and leak sockets on each rebind).
        specs: list[tuple[str, str]] = []
        seen: set[str] = set()
        for raw_pid in raw.split(","):
            pid = raw_pid.strip()
            if not pid or pid in seen:
                continue
            seen.add(pid)
            name = self._get_player_name(pid)
            specs.append((pid, name))
        return specs

    def _get_all_players(self) -> list[tuple[str, str]]:
        """
        Get all MA players as (player_id, display_name) pairs.

        Includes unavailable players (they may come online later).
        Filters out protocol players and our own DLNA Receiver renderers
        (which the UPnP player provider discovers as "up<udn_hex>" players).
        """
        try:
            players = self.mass.players.all_players(
                return_unavailable=True,
                return_protocol_players=False,
            )
            all_pids = {p.player_id for p in players}

            # For every player_id, compute the UPnP player_id that would
            # result from discovering our deterministic-UDN renderer.
            # This catches ALL recursion levels without needing _instances.
            own_renderer_pids: set[str] = set()
            for pid in all_pids:
                udn = self._deterministic_udn(pid)
                upnp_pid = "up" + udn.replace("uuid:", "").replace("-", "")
                own_renderer_pids.add(upnp_pid)

            # Also filter already-created instances (belt-and-suspenders)
            own_renderer_pids.update(
                inst.player_id for inst in self._instances.values() if inst.player_id
            )

            result = []
            for p in players:
                if p.player_id in own_renderer_pids:
                    LOGGER.debug(
                        "Filtering out own renderer player: %s (%s)",
                        p.player_id,
                        p.display_name or p.name,
                    )
                    continue
                result.append((p.player_id, p.display_name or p.name or p.player_id))
            return result
        except Exception:
            LOGGER.warning("Could not enumerate MA players", exc_info=True)
            return []

    def _get_player_name(self, player_id: str) -> str:
        """Get the display name for a player, falling back to the id."""
        try:
            player = self.mass.players.get_player(player_id)
            if player:
                return player.display_name or player.name or player_id
            return player_id
        except Exception:
            return player_id

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
    # DIDL-Lite metadata parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_didl_metadata(metadata: str | None) -> dict[str, str | None]:
        """Parse DIDL-Lite XML and extract title, artist, album, image_url, duration."""
        result: dict[str, str | None] = {
            "title": None,
            "artist": None,
            "album": None,
            "image_url": None,
            "duration": None,
        }
        if not metadata:
            return result

        # Bound untrusted input before parsing (normal DIDL is well under this).
        if len(metadata) > _MAX_DIDL_CHARS:
            LOGGER.info(
                "DIDL metadata truncated from %d to %d chars",
                len(metadata),
                _MAX_DIDL_CHARS,
            )
            metadata = metadata[:_MAX_DIDL_CHARS]

        # SOAP bodies may contain XML-escaped DIDL-Lite content
        metadata = unescape(metadata)

        # Defence-in-depth: reject DOCTYPE/ENTITY declarations before parsing
        # with the stdlib XML parser (defusedxml is not yet a dependency) to
        # avoid billion-laughs / external-entity DoS on untrusted LAN input.
        lowered = metadata.lower()
        if "<!doctype" in lowered or "<!entity" in lowered:
            LOGGER.info("DIDL metadata rejected: DOCTYPE/ENTITY declaration present")
            return result

        try:
            root = ET.fromstring(metadata)  # noqa: S314
        except Exception:
            LOGGER.info("Failed to parse DIDL-Lite metadata: %s", metadata[:300])
            return result

        ns = {
            "dc": "http://purl.org/dc/elements/1.1/",
            "upnp": "urn:schemas-upnp-org:metadata-1-0/upnp/",
            "didl": "urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/",
        }

        item = root.find("didl:item", ns)
        if item is None:
            item = root

        title_el = item.find("dc:title", ns)
        if title_el is not None and title_el.text:
            result["title"] = title_el.text

        artist_el = item.find("upnp:artist", ns)
        if artist_el is None:
            artist_el = item.find("dc:creator", ns)
        if artist_el is not None and artist_el.text:
            result["artist"] = artist_el.text

        album_el = item.find("upnp:album", ns)
        if album_el is not None and album_el.text:
            result["album"] = album_el.text

        art_el = item.find("upnp:albumArtURI", ns)
        if art_el is not None and art_el.text:
            result["image_url"] = art_el.text

        # Extract duration from <res duration="H:MM:SS"> element
        res_el = item.find("didl:res", ns)
        if res_el is not None:
            dur = res_el.get("duration")
            if dur:
                result["duration"] = dur

        return result

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
        safe_url = _validate_stream_url(uri)
        if safe_url is None:
            LOGGER.warning(
                "Rejecting transport URI for '%s' (unsupported scheme/host): %s",
                inst.player_name or "(default)",
                _redact_url(uri),
            )
            raise ValueError("unsupported URI scheme or missing host")
        LOGGER.info(
            "Received transport URI for '%s': %s",
            inst.player_name or "(default)",
            _redact_url(safe_url),
        )
        inst.current_stream_url = safe_url
        inst.current_metadata = self._parse_didl_metadata(metadata)

    async def _on_play(self, inst: RendererInstance, previous_state: str) -> None:
        """Handle Play — start streaming to this instance's player."""
        target = inst.player_id
        if not target:
            LOGGER.warning("No target player bound — ignoring Play")
            return

        if not inst.current_stream_url:
            LOGGER.warning("Play received but no stream URL for %s", target)
            return

        if previous_state == TRANSPORT_STATE_PLAYING:
            LOGGER.debug("Player %s is already playing; ignoring duplicate Play", target)
            return

        if previous_state == TRANSPORT_STATE_PAUSED:
            LOGGER.info("Resuming playback on player %s", target)
            await self.mass.players.cmd_play(target)
            inst.play_start_time = time.time()
            inst.metadata_dirty = True
            self._ensure_metadata_task()
            return

        LOGGER.info("Starting playback on player %s", target)
        meta = inst.current_metadata or {}
        LOGGER.debug("DIDL metadata for %s: %s", target, meta)
        duration = self._parse_duration(meta.get("duration"))

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
        self._ensure_metadata_task()

        # Route through the AudioSource item so MA pulls bytes via our
        # get_audio_stream() proxy instead of handing the raw upstream
        # URI to the player (which would bypass the SSRF/redirect guards
        # and fail on players that require MA to serve the stream).
        # QueueOption.PLAY overrides any user-configured default enqueue
        # option — the sender expects immediate playback, not enqueueing.
        source_uri = create_uri(MediaType.AUDIO_SOURCE, self.instance_id, source_id)
        await self.mass.player_queues.play_media(target, source_uri, option=QueueOption.PLAY)

    async def _on_pause(self, inst: RendererInstance) -> None:
        """Handle Pause for this instance's player."""
        if inst.player_id:
            self._freeze_elapsed(inst)
            inst.metadata_dirty = True
            await self.mass.players.cmd_pause(inst.player_id)

    async def _on_stop(self, inst: RendererInstance) -> None:
        """Handle Stop for this instance's player."""
        if inst.player_id:
            await self.mass.players.cmd_stop(inst.player_id)
        inst.current_stream_url = None
        self._clear_instance_playback(inst)

    async def _on_seek(self, inst: RendererInstance, unit: str, target: str) -> None:
        """Handle Seek for this instance's player."""
        if not inst.player_id:
            return
        position = self._parse_duration(target)
        if position is not None:
            LOGGER.info("Seeking player %s to %ds", inst.player_id, position)
            await self.mass.players.cmd_seek(inst.player_id, position)
        else:
            LOGGER.warning("Could not parse seek target: %s", target)

    async def _on_set_volume(self, inst: RendererInstance, volume: int) -> None:
        """Handle volume change for this instance's player."""
        if inst.player_id:
            await self.mass.players.cmd_volume_set(inst.player_id, volume)

    async def _on_set_mute(self, inst: RendererInstance, mute: bool) -> None:
        """Handle mute change for this instance's player."""
        if inst.player_id:
            await self.mass.players.cmd_volume_mute(inst.player_id, mute)

    # ------------------------------------------------------------------
    # Playback state & metadata helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _freeze_elapsed(inst: RendererInstance) -> None:
        """Freeze the instance's elapsed time at the current playback position."""
        if inst.play_start_time:
            inst.elapsed_offset += int(time.time() - inst.play_start_time)
            inst.play_start_time = None

    @staticmethod
    def _clear_instance_playback(inst: RendererInstance) -> None:
        """Clear one renderer instance's playback state and metadata."""
        inst.play_start_time = None
        inst.elapsed_offset = 0
        inst.stream_metadata = None
        inst.metadata_dirty = False

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
        self._metadata_task = asyncio.create_task(self._metadata_update_loop())

    async def _metadata_update_loop(self) -> None:
        """
        Periodically update elapsed time and push it to the claiming queues.

        Exits on its own when no renderer instance has active stream metadata
        left; restarted on demand by _ensure_metadata_task.
        """
        try:
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
                    if inst.play_start_time:
                        metadata.elapsed_time = inst.elapsed_offset + int(
                            now - inst.play_start_time
                        )
                    else:
                        # Paused — keep last_updated fresh to freeze UI display
                        metadata.elapsed_time = inst.elapsed_offset
                    metadata.elapsed_time_last_updated = now

                    if inst.player_id and not self.mass.players.get_player(inst.player_id):
                        LOGGER.debug("Metadata loop: player %s gone", inst.player_id)
                        self._clear_instance_playback(inst)
                        continue

                    # Push through to the queue item's streamdetails so the UI
                    # reflects title/elapsed changes without restarting the stream.
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
        except asyncio.CancelledError:
            pass
        except Exception:
            LOGGER.debug("Metadata update loop error", exc_info=True)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_duration(value: str | None) -> int | None:
        """Parse UPnP duration string (H:MM:SS or H:MM:SS.xxx) to seconds."""
        if not value:
            return None
        try:
            parts = value.split(":")
            if len(parts) == 3:
                h, m, s = parts
                return int(h) * 3600 + int(m) * 60 + int(float(s))
            if len(parts) == 2:
                m, s = parts
                return int(m) * 60 + int(float(s))
            return int(float(value))
        except ValueError, TypeError:
            return None

    @staticmethod
    def _deterministic_udn(player_id: str) -> str:
        """
        Generate a deterministic UDN from the player_id.

        Uses UUID5 so the same player always gets the same UDN,
        keeping DLNA control point bookmarks stable across restarts.
        """
        namespace = uuid.uuid5(uuid.NAMESPACE_URL, UDN_NAMESPACE)
        return f"uuid:{uuid.uuid5(namespace, player_id or '__default__')}"
