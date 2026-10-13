"""Web Kiosk Player Provider implementation."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import logging
import secrets
import time
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlsplit

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType, PlaybackState

from music_assistant.models.player_provider import PlayerProvider

from .constants import (
    CONF_ENABLE_SENDSPIN_BRIDGE,
    CONF_HTTP_PORT,
    CONF_KIOSK_URL,
    CONF_PLAYER_IDLE_TIMEOUT,
    CONF_SHOW_STOP_NOTIFICATION,
    DEFAULT_ENABLE_SENDSPIN_BRIDGE,
    DEFAULT_HTTP_PORT,
    DEFAULT_PLAYER_IDLE_TIMEOUT,
    DEFAULT_SHOW_STOP_NOTIFICATION,
    PLAYER_DISCONNECT_GRACE_SECONDS,
    WEB_KIOSK_PLAYER_ID_PREFIX,
)
from .http_server import WebKioskHTTPServer
from .lyrics import parse_track_lyrics
from .player import WebKioskPlayer

if TYPE_CHECKING:
    from .sendspin_bridge import WebKioskSendspinBridgeManager

logger = logging.getLogger(__name__)


class WebKioskProvider(PlayerProvider):
    """Player Provider that turns a web browser into a Music Assistant kiosk player."""

    http_server: WebKioskHTTPServer | None = None
    sendspin_bridge_enabled: bool = False
    bridge_manager: WebKioskSendspinBridgeManager | None = None
    _player_last_activity: dict[str, float]
    _pending_unregisters: dict[str, asyncio.Event]
    _disconnect_tasks: dict[str, asyncio.Task[None]]
    _stream_token_secret: bytes
    _timeout_task: asyncio.Task[None] | None = None
    _wave_seq: dict[str, int]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize the provider."""
        super().__init__(*args, **kwargs)
        self._player_last_activity = {}
        self._pending_unregisters = {}
        self._disconnect_tasks = {}
        self._wave_seq = {}
        # one secret per provider instance; the per-player tokens derive from it
        self._stream_token_secret = secrets.token_bytes(32)

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to configure this provider."""
        return (
            ConfigEntry(
                key=CONF_HTTP_PORT,
                type=ConfigEntryType.INTEGER,
                required=True,
                default_value=str(DEFAULT_HTTP_PORT),
            ),
            ConfigEntry(
                key=CONF_PLAYER_IDLE_TIMEOUT,
                type=ConfigEntryType.INTEGER,
                required=False,
                default_value=str(DEFAULT_PLAYER_IDLE_TIMEOUT),
            ),
            ConfigEntry(
                key=CONF_SHOW_STOP_NOTIFICATION,
                type=ConfigEntryType.BOOLEAN,
                required=False,
                default_value=DEFAULT_SHOW_STOP_NOTIFICATION,
            ),
            ConfigEntry(
                key=CONF_ENABLE_SENDSPIN_BRIDGE,
                type=ConfigEntryType.BOOLEAN,
                required=False,
                default_value=DEFAULT_ENABLE_SENDSPIN_BRIDGE,
            ),
            ConfigEntry(
                key=CONF_KIOSK_URL,
                type=ConfigEntryType.STRING,
                required=False,
                default_value=self._compose_kiosk_url(),
                read_only=True,
            ),
        )

    async def handle_async_init(self) -> None:
        """Handle async initialization — start embedded HTTP server."""
        raw_port = cast("int", self.config.get_value(CONF_HTTP_PORT, DEFAULT_HTTP_PORT))
        port = max(1, min(65535, int(raw_port)))
        self.sendspin_bridge_enabled = bool(
            self.config.get_value(CONF_ENABLE_SENDSPIN_BRIDGE, DEFAULT_ENABLE_SENDSPIN_BRIDGE)
        )
        # The Sendspin bridge rides on MA's Sendspin provider; an install that
        # ships no Sendspin provider can't import the manager. That's a valid
        # setup — degrade to "no bridge" rather than failing to load.
        try:
            self.bridge_manager = self._make_bridge_manager()
        except ImportError:
            self.bridge_manager = None
            if self.sendspin_bridge_enabled:
                self.logger.warning(
                    "Sendspin bridge enabled but the Sendspin provider is not available; "
                    "the bridge is disabled"
                )
        self.http_server = WebKioskHTTPServer(self, port)
        await self.http_server.start()
        self.logger.info("Web Kiosk provider initialized, HTTP server on port %s", port)

    async def loaded_in_mass(self) -> None:
        """Start idle timeout task after provider is loaded."""
        await super().loaded_in_mass()
        self._timeout_task = self.mass.create_task(self._run_idle_timeout_loop())
        self.logger.info("Web Kiosk provider loaded — players register on demand")

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload — stop timeout task, HTTP server, then unregister players."""
        self.cancel_all_player_closes()
        if self._timeout_task and not self._timeout_task.done():
            self._timeout_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._timeout_task
            self._timeout_task = None

        if self.bridge_manager:
            await self.bridge_manager.close()
            self.bridge_manager = None

        if self.http_server:
            await self.http_server.stop()
        for player in list(self.players):
            try:
                self.logger.debug("Unloading player %s", player.display_name)
                await self.mass.players.unregister(player.player_id)
            except Exception:
                self.logger.exception("Error unregistering player %s", player.player_id)
        self._player_last_activity.clear()
        self.logger.info("Web Kiosk provider unloaded")

    async def discover_players(self) -> None:
        """Discover players — kiosk players are registered on demand when browsers connect."""

    async def get_or_register_player(
        self,
        player_id: str,
        display_name: str | None = None,
        ip_address: str | None = None,
    ) -> WebKioskPlayer | None:
        """
        Get or register a kiosk player for the given player_id.

        Returns the player, or None if registration failed.
        """
        if pending_event := self._pending_unregisters.get(player_id):
            self.logger.debug("Waiting for pending unregister of %s before registering", player_id)
            await pending_event.wait()
        existing = self.mass.players.get_player(player_id, raise_unavailable=False)
        if existing and isinstance(existing, WebKioskPlayer):
            if ip_address and not existing.device_info.ip_address:
                existing.device_info.ip_address = ip_address
            self.on_player_activity(player_id)
            return existing
        name = display_name or self.player_display_name(player_id)
        player = WebKioskPlayer(
            provider=self,
            player_id=player_id,
            name=name,
            ip_address=ip_address,
        )
        await self.mass.players.register(player)
        self._player_last_activity[player_id] = time.monotonic()
        self.logger.info("Registered kiosk player: %s (%s)", name, player_id)
        if self.bridge_manager:
            await self.bridge_manager.evaluate_bridge(player)
        return player

    async def note_sendspin_client_id(self, player: WebKioskPlayer, client_id: str) -> None:
        """
        Store the browser's Sendspin identity and reconcile the bridge.

        sendspin-js 5 mints a cryptographic client id. Music Assistant 2.11
        (aiosendspin 10) only lets that browser take over the bridge when the
        external player was registered under the same id. A changed identity
        drops the previous bridge player so a stale Sendspin player does not
        linger.
        """
        previous = player.sendspin_client_id
        if previous == client_id:
            if self.bridge_manager:
                await self.bridge_manager.evaluate_bridge(player)
            return
        player.sendspin_client_id = client_id
        if self.bridge_manager and previous:
            await self.bridge_manager.remove_bridge(player.player_id, permanent=True)
        if self.bridge_manager:
            await self.bridge_manager.evaluate_bridge(player)

    def schedule_player_close(self, player_id: str) -> None:
        """
        Remove the player unless a browser reconnects within the grace window.

        The last socket closing is not enough to tell a closed tab from a reload.
        A reload opens a new socket and cancels this. A closed page does not.
        """
        self.cancel_player_close(player_id)
        self._disconnect_tasks[player_id] = self.mass.create_task(
            self._unregister_if_browser_gone(player_id)
        )

    def cancel_player_close(self, player_id: str) -> None:
        """Keep the player. A browser socket for it is open again."""
        task = self._disconnect_tasks.pop(player_id, None)
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()

    def cancel_all_player_closes(self) -> None:
        """Drop every pending close. Used when the provider itself is stopping."""
        for player_id in list(self._disconnect_tasks):
            self.cancel_player_close(player_id)

    def on_player_activity(self, player_id: str) -> None:
        """Record activity for a player (extends idle timeout)."""
        # Monotonic: a wall-clock NTP step must not age players past the cutoff
        self._player_last_activity[player_id] = time.monotonic()

    def on_player_disabled(self, player_id: str) -> None:
        """
        Handle player disabled: do not unregister (base would unregister).

        Kiosk players are registered on demand; unregister on disable would remove them
        from the list. On enable, discovery is empty so the player would not come back
        until the kiosk reconnects. We keep the player registered but disabled so it stays
        visible in the list when re-enabled.

        Still stop playback on the kiosk by broadcasting stop.
        """
        if self.http_server:
            self.http_server.broadcast_stop(player_id)

    def on_player_enabled(self, player_id: str) -> None:
        """Handle player enabled: no-op, player already registered."""

    async def remove_player(self, player_id: str) -> None:
        """
        Remove (delete) a player from this provider.

        Called when user chooses to remove the player from MA.
        This fully unregisters the player. It will reappear if the kiosk reconnects.
        """
        if self.http_server:
            self.http_server.broadcast_stop(player_id)
        await self._handle_player_unregister(player_id)
        self.logger.info("Player %s removed by user", player_id)

    def notify_play_started(
        self,
        player_id: str,
        *,
        title: str | None = None,
        artist: str | None = None,
        image_url: str | None = None,
        duration: float | None = None,
        start: float = 0,
    ) -> None:
        """Notify the kiosk WebSocket client that playback started."""
        if self.http_server:
            seq = self.next_wave_seq(player_id)
            self.http_server.broadcast_play(
                player_id,
                title=title,
                artist=artist,
                image_url=image_url,
                duration=duration,
                start=start,
                wave_seq=seq,
            )
            # The browser may have no Music Assistant token. The curve still
            # belongs to the track this player is already decoding.
            self.spawn_wave(player_id, seq)
            self.spawn_lyrics(player_id, seq)

    def next_wave_seq(self, player_id: str) -> int:
        """Return the next energy-curve generation for this player."""
        seq = self._wave_seq.get(player_id, 0) + 1
        self._wave_seq[player_id] = seq
        return seq

    def spawn_wave(self, player_id: str, seq: int) -> None:
        """Look up the current track's energy curve and send it to the page."""
        self._spawn(self._publish_wave(player_id, seq))

    def spawn_lyrics(self, player_id: str, seq: int) -> None:
        """Look up the current track's lyrics and send them to the page."""
        self._spawn(self._publish_lyrics(player_id, seq))

    def track_ended(self, player_id: str) -> None:
        """Advance the queue after the browser finishes its audio file."""
        self._spawn(self._advance_after_track(player_id))

    def notify_play_paused(self, player_id: str) -> None:
        """Notify the kiosk WebSocket client that playback is paused."""
        if self.http_server:
            self.http_server.broadcast_pause(player_id)

    def notify_play_resumed(self, player_id: str) -> None:
        """Notify the kiosk WebSocket client that playback resumed."""
        if self.http_server:
            self.http_server.broadcast_resume(player_id)

    def notify_play_stopped(self, player_id: str) -> None:
        """Notify the kiosk WebSocket client that playback stopped."""
        if self.http_server:
            self.http_server.broadcast_stop(player_id)

    def notify_seek(self, player_id: str, position_seconds: int) -> None:
        """Notify the kiosk WebSocket client to seek to position."""
        if self.http_server:
            self.http_server.broadcast_seek(player_id, position_seconds)

    def notify_volume(self, player_id: str, volume_level: int) -> None:
        """Notify the kiosk WebSocket client of a volume change."""
        if self.http_server:
            self.http_server.broadcast_volume(player_id, volume_level)

    def get_stream_token(self, player_id: str) -> str:
        """
        Return the token that authorizes the audio route for the given player.

        Derived rather than stored, so a caller cannot grow provider state by asking for
        tokens under new player ids. It stays the same for the provider's lifetime: an
        idle kiosk is unregistered after the configured timeout, and changing the token
        there would strand the URLs a long-running kiosk has already cached.

        :param player_id: The player to build an audio URL for.
        """
        digest = hmac.new(self._stream_token_secret, player_id.encode(), hashlib.sha256)
        return digest.hexdigest()[:32]

    async def get_ma_stream_url(self, player_id: str, media: Any) -> str | None:
        """
        Resolve the direct MA Streamserver URL for the given media.

        The kiosk fetches audio straight from the MA Streamserver, which applies the
        player's own codec config and DSP — no local proxy/ffmpeg involved.

        :param player_id: The kiosk player requesting the stream.
        :param media: PlayerMedia to resolve the stream URL for.
        :return: Direct URL to the MA Streamserver, or None when resolution fails.
        """
        if not media:
            logger.debug("No media provided")
            return None
        try:
            return await self.mass.streams.resolve_stream_url(player_id, media)
        except Exception as err:
            logger.warning("Failed to resolve MA stream URL: %s", err, exc_info=True)
            return None

    def player_display_name(
        self, player_id: str, prefix_label: str = "Web Kiosk", remote_ip: str | None = None
    ) -> str:
        """Build a unique display name from player_id for the MA UI."""
        prefix = WEB_KIOSK_PLAYER_ID_PREFIX
        suffix = player_id.removeprefix(prefix)
        if not suffix:
            return prefix_label
        # IP-based: wk_192_168_10_15 → "Web Kiosk (192.168.10.15)"
        if "_" in suffix:
            parts = suffix.split("_")
            if all(p.isdigit() for p in parts):
                return f"{prefix_label} ({'.'.join(parts)})"
        # Fallback: truncate long suffixes
        if len(suffix) > 12:
            if remote_ip:
                return f"{prefix_label} ({suffix[:8]}...) [{remote_ip}]"
            return f"{prefix_label} ({suffix[:8]}...)"
        if remote_ip:
            return f"{prefix_label} ({suffix}) [{remote_ip}]"
        return f"{prefix_label} ({suffix})"

    async def _unregister_if_browser_gone(self, player_id: str) -> None:
        """Unregister after the grace window if no browser socket came back."""
        try:
            await asyncio.sleep(PLAYER_DISCONNECT_GRACE_SECONDS)
        except asyncio.CancelledError:
            return
        current = self._disconnect_tasks.get(player_id)
        if current is not None and current is not asyncio.current_task():
            return
        self._disconnect_tasks.pop(player_id, None)
        server = self.http_server
        if server is not None and server.client_count(player_id) > 0:
            return
        player = self.mass.players.get_player(player_id, raise_unavailable=False)
        if not isinstance(player, WebKioskPlayer):
            return
        self.logger.info("Unregistering kiosk player %s because its page closed", player_id)
        await self._handle_player_unregister(player_id)

    def _spawn(self, coro: Any) -> None:
        """Schedule coro. A test double that hands the coroutine back is closed."""
        task = self.mass.create_task(coro)
        if asyncio.iscoroutine(task):
            task.close()

    async def _publish_wave(self, player_id: str, seq: int) -> None:
        """Send the stored RMS curve for the player's current queue item."""
        if self.http_server is None:
            return
        try:
            bins = await self._current_wave(player_id)
        except Exception:
            logger.debug("Energy curve lookup failed for %s", player_id, exc_info=True)
            bins = None
        if self.http_server is not None:
            self.http_server.broadcast_wave(player_id, seq, bins)

    async def _current_wave(self, player_id: str) -> list[float] | None:
        """Return the stored energy bins for the playing item, if analysis exists."""
        queue = self.mass.player_queues.get_active_queue(player_id)
        item = getattr(queue, "current_item", None)
        media = getattr(item, "media_item", None)
        mappings = getattr(media, "provider_mappings", None) or []
        analysis = self.mass.streams.audio_analysis
        for mapping in mappings:
            item_id = getattr(mapping, "item_id", None)
            provider = getattr(mapping, "provider_instance", None) or getattr(
                mapping, "provider_domain", None
            )
            if not item_id or not provider:
                continue
            bins = await analysis.get_wave_form(str(item_id), str(provider))
            if bins:
                return [float(value) for value in bins]
        return None

    async def _publish_lyrics(self, player_id: str, seq: int) -> None:
        """Send the current track's lyric lines to the kiosk page."""
        if self.http_server is None:
            return
        try:
            lines = await self._current_lyrics(player_id)
        except Exception:
            logger.debug("Lyrics lookup failed for %s", player_id, exc_info=True)
            lines = []
        if self.http_server is not None:
            self.http_server.broadcast_lyrics(player_id, seq, lines)

    async def _current_lyrics(self, player_id: str) -> list[dict[str, Any]]:
        """Return parsed lyric lines for the playing item."""
        queue = self.mass.player_queues.get_active_queue(player_id)
        item = getattr(queue, "current_item", None)
        media = getattr(item, "media_item", None)
        if media is None:
            return []
        plain, lrc = await self.mass.metadata.get_track_lyrics(media)
        return parse_track_lyrics(plain, lrc)

    async def _advance_after_track(self, player_id: str) -> None:
        """Play the next queue item, or stop when this track was the last one."""
        player = self.mass.players.get_player(player_id)
        if not isinstance(player, WebKioskPlayer) or player.playback_state != PlaybackState.PLAYING:
            return
        if player._track_end_handled or not player.playback_reached_end():
            return
        # One end event per play. play_media clears this when the next item starts.
        player._track_end_handled = True
        try:
            queues = self.mass.player_queues
            queue = queues.get_active_queue(player_id)
            if queue is None:
                await self.mass.players.cmd_stop(player_id)
                return
            index = queue.current_index
            nxt = queues.get_next_item(queue.queue_id, index) if index is not None else None
            next_index = (
                queues.index_by_id(queue.queue_id, nxt.queue_item_id) if nxt is not None else None
            )
            if next_index is None:
                await self.mass.players.cmd_stop(player_id)
                return
            await queues.play_index(queue.queue_id, next_index)
        except Exception:
            logger.exception("Advancing after track end failed for %s", player_id)
            with contextlib.suppress(Exception):
                await self.mass.players.cmd_stop(player_id)

    def _compose_kiosk_url(self) -> str:
        """Compose the base kiosk URL from the MA webserver base URL and our port."""
        raw_port = self.config.get_value(CONF_HTTP_PORT, None)
        port = DEFAULT_HTTP_PORT
        if isinstance(raw_port, (int, str)):
            try:
                port = max(1, min(65535, int(raw_port)))
            except TypeError, ValueError:
                port = DEFAULT_HTTP_PORT
        base = getattr(getattr(self.mass, "webserver", None), "base_url", None)
        if isinstance(base, str) and base.startswith("http"):
            parts = urlsplit(base)
            hostname = parts.hostname or "ma-host"
            host = f"[{hostname}]" if ":" in hostname else hostname
            return f"{parts.scheme}://{host}:{port}/web?kiosk=1&ma_url={base}"
        return f"http://<ma-host>:{port}/web?kiosk=1"

    async def _handle_player_unregister(self, player_id: str) -> None:
        """Unregister a player with race-condition handling."""
        self.cancel_player_close(player_id)
        self.logger.debug("Unregistering kiosk player %s", player_id)
        unregister_event = asyncio.Event()
        self._pending_unregisters[player_id] = unregister_event
        try:
            if self.bridge_manager:
                await self.bridge_manager.remove_bridge(player_id, permanent=True)
            await self.mass.players.unregister(player_id)
        finally:
            self._pending_unregisters.pop(player_id, None)
            self._player_last_activity.pop(player_id, None)
            unregister_event.set()

    async def _run_idle_timeout_loop(self) -> None:
        """Background task: unregister players idle longer than configured timeout."""
        timeout_minutes = max(
            1,
            min(
                1440,
                int(
                    cast(
                        "int",
                        self.config.get_value(
                            CONF_PLAYER_IDLE_TIMEOUT, DEFAULT_PLAYER_IDLE_TIMEOUT
                        ),
                    )
                ),
            ),
        )
        interval_seconds = 60
        while not self.mass.closing:
            try:
                await asyncio.sleep(interval_seconds)
            except asyncio.CancelledError:
                break
            now = time.monotonic()
            cutoff = now - (timeout_minutes * 60)
            for player in list(self.players):
                if not isinstance(player, WebKioskPlayer):
                    continue
                last = self._player_last_activity.get(player.player_id, 0)
                if last > 0 and last < cutoff:
                    self.logger.info(
                        "Unregistering idle kiosk player %s (no activity for %d min)",
                        player.player_id,
                        timeout_minutes,
                    )
                    self.mass.create_task(self._handle_player_unregister(player.player_id))

    def _make_bridge_manager(self) -> WebKioskSendspinBridgeManager:
        """Import and construct the Sendspin bridge manager (raises ImportError if absent)."""
        from .sendspin_bridge import WebKioskSendspinBridgeManager  # noqa: PLC0415

        return WebKioskSendspinBridgeManager(self)
