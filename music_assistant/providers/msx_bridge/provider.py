"""MSX Bridge Player Provider implementation."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, cast

from music_assistant.models.player_provider import PlayerProvider

from .constants import (
    CONF_ENABLE_GROUPING,
    CONF_ENABLE_SENDSPIN_BRIDGE,
    CONF_GROUP_STREAM_MODE,
    CONF_HTTP_PORT,
    CONF_OUTPUT_FORMAT,
    CONF_PLAYER_IDLE_TIMEOUT,
    DEFAULT_ENABLE_GROUPING,
    DEFAULT_ENABLE_SENDSPIN_BRIDGE,
    DEFAULT_GROUP_STREAM_MODE,
    DEFAULT_HTTP_PORT,
    DEFAULT_OUTPUT_FORMAT,
    DEFAULT_PLAYER_IDLE_TIMEOUT,
    GROUP_STREAM_MODE_REDIRECT,
    GROUP_STREAM_MODE_SHARED,
    MSX_PLAYER_ID_PREFIX,
)
from .http_server import MSXHTTPServer
from .player import MSXPlayer

if TYPE_CHECKING:
    from music_assistant.controllers.streams.audio_processing import AudioOutputPlan

    from .sendspin_bridge import MSXSendspinBridgeManager

logger = logging.getLogger(__name__)


class SharedGroupStream:
    """
    Shared audio stream for a player group.

    One ffmpeg process produces audio, multiple TV clients read from a shared buffer.
    Late joiners receive buffered data first (catch-up), then live chunks.
    """

    def __init__(self, group_id: str, media_uri: str) -> None:
        """Initialize shared stream for a group."""
        self.group_id = group_id
        self.media_uri = media_uri
        self.buffer: deque[bytes] = deque(maxlen=512)  # ~15s @ 40KB/s MP3
        self.subscribers: dict[str, asyncio.Queue[bytes | None]] = {}
        self.producer_task: asyncio.Task[None] | None = None
        self.started = asyncio.Event()
        self.finished = False
        self.producer_error: Exception | None = None
        self.output_plan: AudioOutputPlan | None = None
        self._lock = asyncio.Lock()
        self._total_bytes = 0
        self._start_time: float = 0

        logger.info(
            "[SharedStream:%s] Created for media_uri=%s",
            self.group_id,
            self.media_uri[:80] if self.media_uri else "N/A",
        )

    async def start(
        self,
        audio_chunks: AsyncIterator[bytes],
    ) -> None:
        """Start producing audio from the given chunk iterator."""
        logger.info(
            "[SharedStream:%s] Starting producer task",
            self.group_id,
        )
        self._start_time = time.monotonic()
        self.producer_task = asyncio.create_task(self._produce(audio_chunks))

    async def subscribe(self, player_id: str) -> AsyncIterator[bytes]:
        """
        Subscribe to stream, get buffered + live chunks.

        Yields:
            Audio chunks (bytes). First yields catch-up buffer, then live chunks.
        """
        # Large queue to handle slow readers (TV with weak WiFi)
        q: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=512)

        bytes_sent = 0
        chunks_sent = 0

        try:
            # Wait for stream to start before registering so we don't receive
            # chunks that are already in the catch-up buffer via both paths.
            try:
                await asyncio.wait_for(self.started.wait(), timeout=15.0)
            except TimeoutError:
                logger.error(
                    "[SharedStream:%s] Timeout waiting for stream start for %s",
                    self.group_id,
                    player_id,
                )
                raise

            if self.producer_error is not None:
                logger.error(
                    "[SharedStream:%s] Producer failed before subscriber %s could join: %s",
                    self.group_id,
                    player_id,
                    self.producer_error,
                )
                raise self.producer_error

            # Phase 1: Snapshot buffer and register for live chunks atomically.
            # Holding the lock ensures the producer cannot distribute a new chunk
            # between the snapshot and the registration, eliminating the window
            # where the first chunk would appear in both the catch-up buffer and
            # the subscriber's live queue.
            # Also capture self.finished under the lock: if the producer already
            # completed before we registered, we must self-signal EOF so that
            # the live-stream loop below does not block forever.
            async with self._lock:
                buffer_snapshot = list(self.buffer)
                already_finished = self.finished
                self.subscribers[player_id] = q
                if already_finished:
                    q.put_nowait(None)
                subscriber_count = len(self.subscribers)

            logger.info(
                "[SharedStream:%s] Subscriber %s joined (total: %d)",
                self.group_id,
                player_id,
                subscriber_count,
            )

            buffer_bytes = sum(len(c) for c in buffer_snapshot)
            logger.debug(
                "[SharedStream:%s] Sending %d catch-up chunks (%d bytes) to %s",
                self.group_id,
                len(buffer_snapshot),
                buffer_bytes,
                player_id,
            )
            for chunk in buffer_snapshot:
                yield chunk
                bytes_sent += len(chunk)
                chunks_sent += 1

            # Phase 2: Live stream
            while True:
                next_chunk = await q.get()
                if next_chunk is None:
                    logger.debug(
                        "[SharedStream:%s] EOF received for subscriber %s",
                        self.group_id,
                        player_id,
                    )
                    break
                yield next_chunk
                bytes_sent += len(next_chunk)
                chunks_sent += 1

        finally:
            async with self._lock:
                self.subscribers.pop(player_id, None)
                remaining = len(self.subscribers)

            logger.info(
                "[SharedStream:%s] Subscriber %s left after %d chunks, %d bytes (remaining: %d)",
                self.group_id,
                player_id,
                chunks_sent,
                bytes_sent,
                remaining,
            )

    async def stop(self) -> None:
        """Stop the stream and clean up."""
        logger.info(
            "[SharedStream:%s] Stopping (total: %d bytes)",
            self.group_id,
            self._total_bytes,
        )
        if self.producer_task and not self.producer_task.done():
            self.producer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.producer_task
        self.finished = True

    @property
    def subscriber_count(self) -> int:
        """Return current subscriber count."""
        return len(self.subscribers)

    async def _produce(self, audio_chunks: AsyncIterator[bytes]) -> None:
        """Read from ffmpeg and distribute to all subscribers."""
        try:
            chunk_count = 0
            async for chunk in audio_chunks:
                chunk_count += 1
                self._total_bytes += len(chunk)
                async with self._lock:
                    self.buffer.append(chunk)
                    for player_id, q in list(self.subscribers.items()):
                        try:
                            q.put_nowait(chunk)
                        except asyncio.QueueFull:
                            logger.warning(
                                "[SharedStream:%s] Queue full for subscriber %s, dropping chunk %d",
                                self.group_id,
                                player_id,
                                chunk_count,
                            )

                if not self.started.is_set():
                    # Signal that stream has started (first chunk received)
                    self.started.set()
                    logger.debug(
                        "[SharedStream:%s] First chunk received, signaling started",
                        self.group_id,
                    )

            logger.info(
                "[SharedStream:%s] Producer finished: %d chunks, %d bytes, %.1fs",
                self.group_id,
                chunk_count,
                self._total_bytes,
                time.monotonic() - self._start_time,
            )
        except asyncio.CancelledError:
            logger.debug("[SharedStream:%s] Producer cancelled", self.group_id)
            raise
        except Exception as exc:
            logger.exception("[SharedStream:%s] Producer error", self.group_id)
            self.producer_error = exc
        finally:
            self.finished = True
            # Ensure subscribers waiting on `started` can proceed even if no chunks were produced
            if not self.started.is_set():
                self.started.set()
                logger.debug(
                    "[SharedStream:%s] Producer completed without first chunk, signaling started",
                    self.group_id,
                )
            # Signal EOF to all subscribers
            async with self._lock:
                for player_id, q in list(self.subscribers.items()):
                    with contextlib.suppress(asyncio.QueueFull):
                        q.put_nowait(None)
                    logger.debug(
                        "[SharedStream:%s] Sent EOF to subscriber %s",
                        self.group_id,
                        player_id,
                    )


class MSXBridgeProvider(PlayerProvider):
    """Player Provider that bridges Music Assistant to Smart TVs via MSX."""

    http_server: MSXHTTPServer | None = None
    grouping_enabled: bool = True
    group_stream_mode: str = DEFAULT_GROUP_STREAM_MODE
    sendspin_bridge_enabled: bool = False
    bridge_manager: MSXSendspinBridgeManager | None = None
    _player_last_activity: dict[str, float]
    _pending_unregisters: dict[str, asyncio.Event]
    _timeout_task: asyncio.Task[None] | None = None
    _owner_username: str | None = None
    _shared_streams: dict[str, SharedGroupStream]  # group_id -> SharedGroupStream
    _shared_stream_lock: asyncio.Lock
    _background_tasks: set[asyncio.Task[None]]  # fire-and-forget tasks (unregister, stream stop)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize the provider."""
        super().__init__(*args, **kwargs)
        self._player_last_activity = {}
        self._pending_unregisters = {}
        self._shared_streams = {}
        self._shared_stream_lock = asyncio.Lock()
        self._background_tasks = set()

    async def handle_async_init(self) -> None:
        """Handle async initialization — start embedded HTTP server."""
        raw_port = cast("int", self.config.get_value(CONF_HTTP_PORT, DEFAULT_HTTP_PORT))
        port = max(1, min(65535, int(raw_port)))
        self.grouping_enabled = bool(
            self.config.get_value(CONF_ENABLE_GROUPING, DEFAULT_ENABLE_GROUPING)
        )
        self.group_stream_mode = cast(
            "str",
            self.config.get_value(CONF_GROUP_STREAM_MODE, DEFAULT_GROUP_STREAM_MODE),
        )
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
        self.http_server = MSXHTTPServer(self, port)
        await self.http_server.start()
        self.logger.info(
            "MSX Bridge provider initialized, HTTP server on port %s, group_stream_mode=%s",
            port,
            self.group_stream_mode,
        )

    async def loaded_in_mass(self) -> None:
        """Start idle timeout task after provider is loaded."""
        await super().loaded_in_mass()
        self._timeout_task = self.mass.create_task(self._run_idle_timeout_loop())
        self.logger.info("MSX Bridge provider loaded — players register on demand")

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload — stop timeout task, HTTP server, then unregister players."""
        if self._timeout_task and not self._timeout_task.done():
            self._timeout_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._timeout_task
            self._timeout_task = None

        # Cancel and await any in-flight unregister tasks before proceeding
        for task in list(self._background_tasks):
            if not task.done():
                task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        self._background_tasks.clear()

        # Cleanup shared streams
        await self.cleanup_shared_streams()

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
        self.logger.info("MSX Bridge provider unloaded")

    async def get_owner_username(self) -> str | None:
        """Resolve and cache the first enabled user's username for playlog attribution."""
        if self._owner_username is None:
            try:
                users = await self.mass.webserver.auth.list_users()
                for user in users:
                    if user.enabled and user.username:
                        self._owner_username = user.username
                        self.logger.debug("Resolved owner username: %s", self._owner_username)
                        break
            except Exception as err:
                self.logger.warning("Could not resolve owner username: %s", err)
        return self._owner_username

    async def discover_players(self) -> None:
        """Discover players — MSX players are registered on demand when TVs connect."""

    async def get_or_register_player(
        self,
        player_id: str,
        display_name: str | None = None,
        ip_address: str | None = None,
    ) -> MSXPlayer | None:
        """
        Get or register an MSX player for the given player_id.

        Returns the player, or None if registration failed.
        """
        # Wait for any pending unregister to complete (race condition handling)
        if pending_event := self._pending_unregisters.get(player_id):
            self.logger.debug("Waiting for pending unregister of %s before registering", player_id)
            await pending_event.wait()
        existing = self.mass.players.get_player(player_id, raise_unavailable=False)
        if existing and isinstance(existing, MSXPlayer):
            if ip_address and not existing.device_info.ip_address:
                existing.device_info.ip_address = ip_address
            self.on_player_activity(player_id)
            return existing
        output_format = cast(
            "str", self.config.get_value(CONF_OUTPUT_FORMAT, DEFAULT_OUTPUT_FORMAT)
        )
        name = display_name or self._player_display_name_from_id(player_id)
        player = MSXPlayer(
            provider=self,
            player_id=player_id,
            name=name,
            output_format=output_format,
            grouping_enabled=self.grouping_enabled,
            ip_address=ip_address,
        )
        await self.mass.players.register(player)
        self._player_last_activity[player_id] = time.monotonic()
        self.logger.info("Registered MSX player: %s (%s)", name, player_id)
        if self.bridge_manager:
            await self.bridge_manager.evaluate_bridge(player)
        return player

    def on_player_activity(self, player_id: str) -> None:
        """Record activity for a player (extends idle timeout)."""
        # Monotonic: a wall-clock NTP step must not age players past the cutoff
        self._player_last_activity[player_id] = time.monotonic()

    def on_player_disabled(self, player_id: str) -> None:
        """
        Handle player disabled: do not unregister (base would unregister).

        MSX players are registered on demand; unregister on disable would remove them
        from the list. On enable, discovery is empty so the player would not come back
        until the TV reconnects. We keep the player registered but disabled so it stays
        visible in the list when re-enabled.

        Still stop playback on TV by broadcasting stop and cancelling streams.
        """
        if self.http_server:
            self.http_server.broadcast_stop(player_id)
            self.http_server.cancel_streams_for_player(player_id)
        # Do NOT call super() — base PlayerProvider unregisters the player here.

    def on_player_enabled(self, player_id: str) -> None:
        """Handle player enabled: no-op, player already registered."""
        # Player was never unregistered (see on_player_disabled), so nothing to do.

    async def remove_player(self, player_id: str) -> None:
        """
        Remove (delete) a player from this provider.

        Called when user chooses to remove the player from MA.
        This fully unregisters the player. It will reappear if the TV reconnects.
        """
        if self.http_server:
            self.http_server.broadcast_stop(player_id)
            self.http_server.cancel_streams_for_player(player_id)
        await self._handle_player_unregister(player_id)
        self.logger.info("Player %s removed by user", player_id)

    def notify_play_started(
        self,
        player_id: str,
        *,
        title: str | None = None,
        artist: str | None = None,
        image_url: str | None = None,
        duration: int | None = None,
        next_action: str | None = None,
        prev_action: str | None = None,
    ) -> None:
        """Notify WebSocket clients that playback started (for MA -> MSX push)."""
        if self.http_server:
            self.http_server.broadcast_play(
                player_id,
                title=title,
                artist=artist,
                image_url=image_url,
                duration=duration,
                next_action=next_action,
                prev_action=prev_action,
            )

    def notify_play_playlist(
        self,
        player_id: str,
        start_index: int = 0,
        queue_id: str | None = None,
    ) -> None:
        """Notify WebSocket clients to play an MSX native playlist from the MA queue."""
        if self.http_server:
            qid = queue_id or player_id
            url = f"/msx/queue-playlist/{player_id}.json?start={start_index}&queue_id={qid}"
            self.http_server.broadcast_playlist(player_id, url)

    def notify_goto_index(self, player_id: str, index: int) -> None:
        """Notify WebSocket clients to jump to a specific playlist index."""
        if self.http_server:
            self.http_server.broadcast_goto_index(player_id, index)

    def notify_play_paused(self, player_id: str) -> None:
        """Notify WebSocket clients that playback is paused (MA pause -> MSX)."""
        if self.http_server:
            self.http_server.broadcast_pause(player_id)

    def notify_play_resumed(self, player_id: str) -> None:
        """Notify WebSocket clients that playback resumed (MA resume -> MSX)."""
        if self.http_server:
            self.http_server.broadcast_resume(player_id)

    def notify_play_stopped(self, player_id: str) -> None:
        """
        Notify WebSocket clients that playback stopped (MA stop -> MSX).

        Sends broadcast_stop + cancel_streams twice — same as Disable flow, which
        stops playback on MSX instantly (vs single signal with ~30s delay).
        """
        server = self.http_server
        if not server:
            return

        def _send() -> None:
            server.broadcast_stop(player_id)
            server.cancel_streams_for_player(player_id)

        _send()
        _send()

    def notify_seek(self, player_id: str, position_seconds: int) -> None:
        """Notify WebSocket clients to seek to position (MA seek -> MSX)."""
        if self.http_server:
            self.http_server.broadcast_seek(player_id, position_seconds)

    # --- Group Stream Management ---

    def is_shared_stream_mode(self) -> bool:
        """Check if shared buffer stream mode is enabled."""
        return self.group_stream_mode == GROUP_STREAM_MODE_SHARED

    def is_redirect_stream_mode(self) -> bool:
        """
        Check if MA redirect stream mode is enabled.

        In redirect mode the TV is 302-redirected to the MA Streamserver
        (``resolve_stream_url``) instead of being served by the local
        proxy/ffmpeg pipeline. See also ``get_ma_stream_url()``.
        """
        return self.group_stream_mode == GROUP_STREAM_MODE_REDIRECT

    def get_group_id_for_player(self, player: MSXPlayer) -> str | None:
        """
        Get group ID if player is in a group (as leader or member).

        Returns:
            group_id if player is grouped, None if solo player.
        """
        # If player is synced to another (member), use leader's ID as group
        if player.synced_to:
            logger.debug(
                "[GroupStream] Player %s is member of group %s",
                player.player_id,
                player.synced_to,
            )
            return player.synced_to

        # If player has group members (is leader), use own ID as group
        if player.group_members and len(player.group_members) > 1:
            logger.debug(
                "[GroupStream] Player %s is leader of group with %d members",
                player.player_id,
                len(player.group_members),
            )
            return player.player_id

        # Solo player
        return None

    async def get_or_create_shared_stream(
        self,
        group_id: str,
        media_uri: str,
        audio_chunks: AsyncIterator[bytes],
    ) -> SharedGroupStream:
        """
        Get existing shared stream or create new one for the group.

        Args:
            group_id: ID of the group (leader's player_id)
            media_uri: URI of the media being streamed
            audio_chunks: Async iterator yielding encoded audio chunks

        Returns:
            SharedGroupStream instance
        """
        # Serialize check-and-create: without the lock, two concurrent callers
        # replacing an old stream both pass the "existing" check while awaiting
        # existing.stop(), creating two producers — one orphaned.
        async with self._shared_stream_lock:
            existing = self._shared_streams.get(group_id)

            # Reuse existing if same media and not finished
            if existing and not existing.finished and existing.media_uri == media_uri:
                logger.info(
                    "[GroupStream] Reusing existing shared stream for group %s (subscribers: %d)",
                    group_id,
                    existing.subscriber_count,
                )
                return existing

            # Clean up old stream if exists
            if existing:
                logger.info(
                    "[GroupStream] Replacing old shared stream for group %s "
                    "(old_uri=%s, new_uri=%s)",
                    group_id,
                    existing.media_uri[:50] if existing.media_uri else "N/A",
                    media_uri[:50] if media_uri else "N/A",
                )
                await existing.stop()

            # Create new shared stream
            logger.info(
                "[GroupStream] Creating new shared stream for group %s, uri=%s",
                group_id,
                media_uri[:80] if media_uri else "N/A",
            )
            stream = SharedGroupStream(group_id, media_uri)
            await stream.start(audio_chunks)
            self._shared_streams[group_id] = stream

            return stream

    def remove_shared_stream(self, group_id: str) -> None:
        """Remove and cleanup shared stream for a group."""
        if stream := self._shared_streams.pop(group_id, None):
            logger.info("[GroupStream] Removed shared stream for group %s", group_id)
            task = self.mass.create_task(stream.stop())
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

    async def get_ma_stream_url(self, player_id: str, media: Any) -> str | None:
        """
        Resolve the direct MA Streamserver URL for the given media.

        Used by redirect stream mode: the TV fetches audio straight from the
        MA Streamserver, which applies the player's own codec config and DSP —
        no local proxy/ffmpeg involved.

        :param player_id: The MSX player requesting the stream.
        :param media: PlayerMedia to resolve the stream URL for.
        :return: Direct URL to the MA Streamserver, or None when resolution
            fails (the caller falls back to the local proxy pipeline).
        """
        if not media:
            logger.debug("[MARedirect] No media provided")
            return None
        try:
            stream_url: str = await self.mass.streams.resolve_stream_url(player_id, media)
        except Exception as err:
            logger.warning("[MARedirect] Failed to resolve MA stream URL: %s", err, exc_info=True)
            return None
        # MA returns a flow URL (continuous whole-queue stream) when e.g. crossfade
        # is enabled and the player lacks gapless support. That breaks the MSX
        # per-track model (progress display, auto-advance re-enqueue), so serve
        # such tracks through the local per-track proxy instead.
        if "/flow/" in stream_url:
            logger.debug(
                "[MARedirect] Flow-mode URL not usable for MSX per-track playback, "
                "falling back to proxy: %s",
                stream_url,
            )
            return None
        logger.debug("[MARedirect] Resolved MA stream URL: %s", stream_url)
        return stream_url

    async def cleanup_shared_streams(self) -> None:
        """Cleanup all shared streams (called on unload)."""
        for group_id, stream in list(self._shared_streams.items()):
            logger.debug("[GroupStream] Cleaning up stream for group %s", group_id)
            await stream.stop()
        self._shared_streams.clear()

    def _player_display_name_from_id(
        self, player_id: str, prefix_label: str = "MSX TV", remote_ip: str | None = None
    ) -> str:
        """Build a unique display name from player_id for the MA UI."""
        prefix = MSX_PLAYER_ID_PREFIX
        suffix = player_id.removeprefix(prefix)
        if not suffix:
            return prefix_label
        # IP-based: msx_192_168_10_15 → "MSX TV (192.168.10.15)"
        if "_" in suffix:
            parts = suffix.split("_")
            if all(p.isdigit() for p in parts):
                return f"{prefix_label} ({'.'.join(parts)})"
        # UUID-based: msx_msx_bc93ce1d_491d_4d95_9430_2fbeabb5ce1b → "MSX TV (bc93)"
        # Show only first 4 chars of UUID for readability, plus IP if available
        if suffix.startswith("msx_") and len(suffix) > 12:
            uuid_part = suffix[4:8]  # First 4 chars after "msx_"
            if remote_ip:
                return f"{prefix_label} ({uuid_part}) [{remote_ip}]"
            return f"{prefix_label} ({uuid_part})"
        # Fallback: truncate long suffixes
        if len(suffix) > 12:
            if remote_ip:
                return f"{prefix_label} ({suffix[:8]}...) [{remote_ip}]"
            return f"{prefix_label} ({suffix[:8]}...)"
        if remote_ip:
            return f"{prefix_label} ({suffix}) [{remote_ip}]"
        return f"{prefix_label} ({suffix})"

    async def _handle_player_unregister(self, player_id: str) -> None:
        """Unregister a player with race-condition handling."""
        self.logger.debug("Unregistering MSX player %s", player_id)
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
                if not isinstance(player, MSXPlayer):
                    continue
                last = self._player_last_activity.get(player.player_id, 0)
                if last > 0 and last < cutoff:
                    self.logger.info(
                        "Unregistering idle MSX player %s (no activity for %d min)",
                        player.player_id,
                        timeout_minutes,
                    )
                    task = self.mass.create_task(self._handle_player_unregister(player.player_id))
                    self._background_tasks.add(task)
                    task.add_done_callback(self._background_tasks.discard)

    def _make_bridge_manager(self) -> MSXSendspinBridgeManager:
        """Import and construct the Sendspin bridge manager (raises ImportError if absent)."""
        from .sendspin_bridge import MSXSendspinBridgeManager  # noqa: PLC0415

        return MSXSendspinBridgeManager(self)
