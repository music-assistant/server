"""DLNA Receiver — Main provider implementation.

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
from typing import TYPE_CHECKING

import aiohttp
from music_assistant_models.config_entries import ConfigValueType  # noqa: F401
from music_assistant_models.enums import ContentType, ProviderFeature
from music_assistant_models.errors import SetupFailedError
from music_assistant_models.media_items.audio_format import AudioFormat
from music_assistant_models.streamdetails import StreamMetadata

from music_assistant.helpers.util import get_ip_addresses
from music_assistant.models.plugin import PluginProvider, PluginSource

from .constants import (
    CONF_BIND_IP,
    CONF_FRIENDLY_NAME,
    CONF_HTTP_PORT,
    CONF_TARGET_PLAYER,
    CONF_TARGET_PLAYERS,
    DEFAULT_FRIENDLY_NAME,
    DEFAULT_HTTP_PORT,
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
    """Return True iff ``value`` is a non-wildcard, non-loopback IPv4 literal.

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
    plugin_source: PluginSource | None = None


class DLNAReceiverProvider(PluginProvider):
    """DLNA Receiver plugin provider for Music Assistant.

    Exposes MA as one or more UPnP MediaRenderers on the local network
    so that external apps can send audio streams which are then played
    on the corresponding MA player.
    """

    SUPPORTED_FEATURES = {ProviderFeature.AUDIO_SOURCE}

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
    ) -> None:
        """Initialize provider state; renderer instances are created in loaded_in_mass."""
        super().__init__(mass, manifest, config, self.SUPPORTED_FEATURES)
        self._instances: dict[str, RendererInstance] = {}
        self._plugin_source: PluginSource | None = None
        # Playback state for elapsed time tracking
        self._active_player_id: str | None = None
        self._play_start_time: float | None = None
        self._elapsed_offset: int = 0
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

        if player_specs:
            for pid, pname in player_specs:
                await self._create_instance(
                    player_id=pid,
                    player_name=pname,
                    friendly_prefix=self._friendly_prefix,
                    bind_ip=self._bind_ip,
                    http_port=self._next_port,
                )
                self._next_port += 1
        else:
            # Fallback: single unbound renderer so DLNA discovery works
            # immediately. In target_players=* mode this is retired as soon
            # as the first real player renderer registers, so we don't keep
            # advertising an unroutable renderer once real ones exist.
            await self._create_instance(
                player_id="",
                player_name="",
                friendly_prefix=self._friendly_prefix,
                bind_ip=self._bind_ip,
                http_port=self._next_port,
            )
            self._next_port += 1

        # For target_players=*, keep looking for late-registering players in the
        # background instead of blocking startup for up to 72 seconds.
        if raw_target == "*":
            self._discovery_task = asyncio.create_task(self._adopt_late_players())

        LOGGER.info(
            "DLNA Receiver started: %d renderer(s) on %s (base port %s)",
            len(self._instances),
            self._bind_ip,
            self._base_port,
        )

    async def _adopt_late_players(self) -> None:
        """Poll for newly-registered MA players and spin up renderers for them.

        Runs only when ``target_players=*``. Caps the total wait at ~5 minutes
        so stale provider instances don't poll forever. The first time a real
        player renderer is created we retire the unbound ``__default__``
        fallback so we aren't advertising a renderer that can't route audio.
        """
        known_ids = {inst.player_id for inst in self._instances.values() if inst.player_id}
        default_retired = False
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
                    if not default_retired:
                        await self._retire_default_instance()
                        default_retired = True
        except asyncio.CancelledError:
            pass
        except Exception:
            LOGGER.debug("Late-player discovery loop error", exc_info=True)

    async def _retire_default_instance(self) -> None:
        """Stop and drop the unbound ``__default__`` renderer, if any.

        Called once from ``_adopt_late_players`` after the first real player
        renderer is created so control points no longer see a fallback
        renderer that would silently swallow Play commands.
        """
        default = self._instances.pop("__default__", None)
        if default is None:
            return
        await default.ssdp.stop()
        await default.renderer.stop()
        LOGGER.info("Retired unbound fallback renderer — real players registered")

    async def unload(self, is_removed: bool = False) -> None:
        """Unload the provider — stop all renderer instances.

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
    # Instance management
    # ------------------------------------------------------------------

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
        renderer.on_play = lambda: self._on_play(inst)
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

        key = player_id or "__default__"
        self._instances[key] = inst

        LOGGER.info(
            "Renderer '%s' → player '%s' on port %d (UDN: %s)",
            friendly_name,
            player_id or "(none)",
            http_port,
            udn,
        )
        return inst

    def _raw_target(self) -> str:
        """Return the configured target spec, honoring the legacy config key.

        Centralizes the CONF_TARGET_PLAYERS → CONF_TARGET_PLAYER fallback so
        every call site (late-player adoption check, spec resolution) sees
        the same value; otherwise a legacy ``"*"`` config would skip the
        _adopt_late_players background task.
        """
        raw = str(self.config.get_value(CONF_TARGET_PLAYERS) or "").strip()
        if not raw:
            raw = str(self.config.get_value(CONF_TARGET_PLAYER) or "").strip()
        return raw

    def _resolve_player_specs(self) -> list[tuple[str, str]]:
        """Resolve configured player targets to (player_id, display_name) pairs.

        Supports:
        - Empty / not set → empty list (single unbound renderer)
        - "*" → all currently known MA players
        - Comma-separated player_ids
        - Legacy CONF_TARGET_PLAYER (single player_id, backward compat)
        """
        raw = self._raw_target()

        if not raw:
            return []

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
        """Get all MA players as (player_id, display_name) pairs.

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
    # PluginProvider audio source interface
    # ------------------------------------------------------------------

    def get_source(self) -> PluginSource:
        """Return the plugin source descriptor for this DLNA receiver.

        Returns a persistent instance so metadata updates are visible
        to the MA controller between calls.
        """
        if self._plugin_source is None:
            # Upstream DLNA senders push arbitrary compressed formats
            # (FLAC/MP3/AAC/WAV/etc). Declare content_type/codec as UNKNOWN
            # so MA's ffmpeg input pipeline probes the actual codec from
            # stdin instead of misinterpreting compressed bytes as PCM.
            self._plugin_source = PluginSource(
                id=self.instance_id,
                name=self.name or "DLNA Receiver",
                passive=True,
                can_play_pause=True,
                audio_format=AudioFormat(
                    content_type=ContentType.UNKNOWN,
                    codec_type=ContentType.UNKNOWN,
                ),
            )
        return self._plugin_source

    async def get_audio_stream(
        self,
        player_id: str,
    ) -> AsyncGenerator[bytes, None]:
        """Yield audio bytes from the received DLNA stream.

        MA calls this when the plugin source is activated on a player.
        We proxy the external URL through aiohttp and yield raw bytes.
        """
        inst = self._instances.get(player_id) or self._instances.get("__default__")
        stream_url = inst.current_stream_url if inst else None

        if not stream_url:
            LOGGER.warning(
                "get_audio_stream(%s) called but no stream URL set",
                player_id,
            )
            return

        LOGGER.debug("Proxying DLNA stream for %s: %s", player_id, _redact_url(stream_url))
        # total=None: streams may be long-running; bound connect + per-chunk read only.
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=30)
        # Reuse MA's shared HTTP session (matches streams/audio.py) so we
        # don't open a fresh TCP connector + DNS cache per activation.
        try:
            async with self.mass.http_session.get(stream_url, timeout=timeout) as resp:
                # Re-validate after any redirects: the final URL still has to
                # be an http(s) endpoint, otherwise refuse to stream. (aiohttp
                # won't follow non-http schemes, but belt-and-suspenders.)
                final_url = str(resp.url)
                if _validate_stream_url(final_url) is None:
                    LOGGER.warning(
                        "Upstream DLNA source redirected to disallowed URL: %s",
                        _redact_url(final_url),
                    )
                    return
                # Accept any 2xx (e.g. 206 Partial Content is common for audio).
                if not 200 <= resp.status < 300:
                    LOGGER.warning(
                        "Upstream DLNA source returned HTTP %s for %s",
                        resp.status,
                        _redact_url(stream_url),
                    )
                    return
                async for chunk in resp.content.iter_any():
                    yield chunk
        except (aiohttp.ClientError, TimeoutError):
            LOGGER.warning(
                "Error proxying DLNA stream %s",
                _redact_url(stream_url),
                exc_info=True,
            )
            return
        LOGGER.debug("DLNA stream ended for %s", player_id)

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
        """Handle SetAVTransportURI for a specific renderer instance.

        Raises:
            ValueError: if the URI is not a safe http(s) stream URL. The
                renderer turns this into SOAP fault 716 so the control
                point sees the rejection instead of a silent 200 OK.
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

    async def _on_play(self, inst: RendererInstance) -> None:
        """Handle Play — start streaming to this instance's player."""
        target = inst.player_id
        if not target:
            LOGGER.warning("No target player bound — ignoring Play")
            return

        if not inst.current_stream_url:
            LOGGER.warning("Play received but no stream URL for %s", target)
            return

        LOGGER.info("Starting playback on player %s", target)
        meta = inst.current_metadata or {}
        LOGGER.debug("DIDL metadata for %s: %s", target, meta)
        duration = self._parse_duration(meta.get("duration"))

        # Update plugin source metadata for MA UI display
        source = self.get_source()
        source.in_use_by = target
        source.metadata = StreamMetadata(
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
        self._active_player_id = target
        self._play_start_time = time.time()
        self._elapsed_offset = 0
        self._ensure_metadata_task()

        # Route through the registered PluginSource so MA pulls bytes via
        # our get_audio_stream() proxy instead of handing the raw upstream
        # URI to the player (which would bypass the SSRF/redirect guards
        # and fail on players that require MA to serve the stream).
        await self.mass.players.select_source(target, self.instance_id)

    async def _on_pause(self, inst: RendererInstance) -> None:
        """Handle Pause for this instance's player."""
        if inst.player_id:
            self._freeze_elapsed()
            await self.mass.players.cmd_pause(inst.player_id)

    async def _on_stop(self, inst: RendererInstance) -> None:
        """Handle Stop for this instance's player."""
        if inst.player_id:
            await self.mass.players.cmd_stop(inst.player_id)
        inst.current_stream_url = None
        self._clear_playback_state()

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

    def _freeze_elapsed(self) -> None:
        """Freeze elapsed time at the current playback position."""
        if self._play_start_time:
            self._elapsed_offset += int(time.time() - self._play_start_time)
            self._play_start_time = None

    def _clear_playback_state(self) -> None:
        """Clear all playback state and metadata."""
        self._play_start_time = None
        self._elapsed_offset = 0
        self._active_player_id = None
        if self._plugin_source:
            self._plugin_source.metadata = None
            self._plugin_source.in_use_by = None
        # Drop the reference after cancel so the next playback cycle creates
        # a fresh task. Without this, a canceled-but-not-yet-done task still
        # reports done() == False to _ensure_metadata_task for a brief
        # window, and a rapid stop→play would skip restarting the loop.
        if self._metadata_task is not None:
            if not self._metadata_task.done():
                self._metadata_task.cancel()
            self._metadata_task = None

    def _ensure_metadata_task(self) -> None:
        """Start the metadata update loop if not already running."""
        if self._metadata_task and not self._metadata_task.done():
            return
        self._metadata_task = asyncio.create_task(self._metadata_update_loop())

    async def _metadata_update_loop(self) -> None:
        """Periodically update elapsed time and trigger UI refresh."""
        try:
            while True:
                await asyncio.sleep(2)
                source = self._plugin_source
                if not source or not source.metadata:
                    break

                now = time.time()

                if self._play_start_time:
                    elapsed = self._elapsed_offset + int(now - self._play_start_time)
                    source.metadata.elapsed_time = elapsed
                    source.metadata.elapsed_time_last_updated = now
                else:
                    # Paused — keep last_updated fresh to freeze UI display
                    source.metadata.elapsed_time = self._elapsed_offset
                    source.metadata.elapsed_time_last_updated = now

                # Check if player still exists and source is still active
                if self._active_player_id:
                    player = self.mass.players.get_player(self._active_player_id)
                    if not player:
                        LOGGER.debug("Metadata loop: player %s gone", self._active_player_id)
                        self._clear_playback_state()
                        break

                # Trigger player update so UI reflects metadata changes
                if self._active_player_id:
                    self.mass.players.trigger_player_update(self._active_player_id)
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
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _deterministic_udn(player_id: str) -> str:
        """Generate a deterministic UDN from the player_id.

        Uses UUID5 so the same player always gets the same UDN,
        keeping DLNA control point bookmarks stable across restarts.
        """
        namespace = uuid.uuid5(uuid.NAMESPACE_URL, UDN_NAMESPACE)
        return f"uuid:{uuid.uuid5(namespace, player_id or '__default__')}"
