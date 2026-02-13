"""MSX Bridge Player Provider implementation."""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any, cast

from music_assistant.models.player_provider import PlayerProvider

from .constants import (
    CONF_ABORT_STREAM_FIRST,
    CONF_ENABLE_GROUPING,
    CONF_HTTP_PORT,
    CONF_OUTPUT_FORMAT,
    CONF_PLAYER_IDLE_TIMEOUT,
    DEFAULT_ABORT_STREAM_FIRST,
    DEFAULT_ENABLE_GROUPING,
    DEFAULT_HTTP_PORT,
    DEFAULT_OUTPUT_FORMAT,
    DEFAULT_PLAYER_IDLE_TIMEOUT,
    MSX_PLAYER_ID_PREFIX,
)
from .http_server import MSXHTTPServer
from .player import MSXPlayer


class MSXBridgeProvider(PlayerProvider):
    """Player Provider that bridges Music Assistant to Smart TVs via MSX."""

    http_server: MSXHTTPServer | None = None
    grouping_enabled: bool = True
    _player_last_activity: dict[str, float]
    _pending_unregisters: dict[str, asyncio.Event]
    _timeout_task: asyncio.Task[None] | None = None
    _owner_username: str | None = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize the provider."""
        super().__init__(*args, **kwargs)
        self._player_last_activity = {}
        self._pending_unregisters = {}

    async def handle_async_init(self) -> None:
        """Handle async initialization — start embedded HTTP server."""
        port = cast("int", self.config.get_value(CONF_HTTP_PORT, DEFAULT_HTTP_PORT))
        self.grouping_enabled = bool(
            self.config.get_value(CONF_ENABLE_GROUPING, DEFAULT_ENABLE_GROUPING)
        )
        self.http_server = MSXHTTPServer(self, port)
        await self.http_server.start()
        self.logger.info("MSX Bridge provider initialized, HTTP server on port %s", port)

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
        if self.http_server:
            await self.http_server.stop()
        for player in list(self.players):
            self.logger.debug("Unloading player %s", player.display_name)
            await self.mass.players.unregister(player.player_id)
        self._player_last_activity.clear()
        self.logger.info("MSX Bridge provider unloaded")

    async def get_owner_username(self) -> str | None:
        """Resolve and cache the first non-system user's username for playlog attribution."""
        if self._owner_username is None:
            try:
                users = await self.mass.webserver.auth.list_users()
                if users:
                    self._owner_username = users[0].username
                    self.logger.debug("Resolved owner username: %s", self._owner_username)
            except Exception as err:
                self.logger.warning("Could not resolve owner username: %s", err)
        return self._owner_username

    async def discover_players(self) -> None:
        """Discover players — MSX players are registered on demand when TVs connect."""

    async def get_or_register_player(
        self,
        player_id: str,
        display_name: str | None = None,
    ) -> MSXPlayer | None:
        """
        Get or register an MSX player for the given player_id.

        Returns the player, or None if registration failed.
        """
        # Wait for any pending unregister to complete (race condition handling)
        if pending_event := self._pending_unregisters.get(player_id):
            self.logger.debug("Waiting for pending unregister of %s before registering", player_id)
            await pending_event.wait()
        existing = self.mass.players.get(player_id, raise_unavailable=False)
        if existing and isinstance(existing, MSXPlayer):
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
        )
        await self.mass.players.register(player)
        self._player_last_activity[player_id] = time.time()
        self.logger.info("Registered MSX player: %s (%s)", name, player_id)
        return player

    def _player_display_name_from_id(self, player_id: str) -> str:
        """Build a unique display name from player_id for the MA UI."""
        prefix = MSX_PLAYER_ID_PREFIX
        suffix = player_id.removeprefix(prefix)
        if not suffix:
            return "MSX TV"
        # IP-based: msx_192_168_10_15 → "MSX TV (192.168.10.15)"
        if "_" in suffix and all(p.isdigit() for p in suffix.replace("_", " ").split()):
            return f"MSX TV ({suffix.replace('_', '.')})"
        return f"MSX TV ({suffix})"

    def on_player_activity(self, player_id: str) -> None:
        """Record activity for a player (extends idle timeout)."""
        self._player_last_activity[player_id] = time.time()

    def on_player_disabled(self, player_id: str) -> None:
        """Handle player disabled: do not unregister (base would unregister).

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
    ) -> None:
        """Notify WebSocket clients to play an MSX native playlist from the MA queue."""
        if self.http_server:
            url = f"/msx/queue-playlist/{player_id}.json?start={start_index}"
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
        """Notify WebSocket clients that playback stopped (MA stop -> MSX).

        Sends broadcast_stop + cancel_streams twice — same as Disable flow, which
        stops playback on MSX instantly (vs single signal with ~30s delay).
        """
        server = self.http_server
        if not server:
            return
        abort_first = cast(
            "bool",
            self.config.get_value(CONF_ABORT_STREAM_FIRST, DEFAULT_ABORT_STREAM_FIRST),
        )

        def _send() -> None:
            if abort_first:
                server.cancel_streams_for_player(player_id)
                server.broadcast_stop(player_id)
            else:
                server.broadcast_stop(player_id)
                server.cancel_streams_for_player(player_id)

        _send()
        _send()

    async def _handle_player_unregister(self, player_id: str) -> None:
        """Unregister a player with race-condition handling."""
        self.logger.debug("Unregistering MSX player %s", player_id)
        unregister_event = asyncio.Event()
        self._pending_unregisters[player_id] = unregister_event
        try:
            await self.mass.players.unregister(player_id)
        finally:
            self._pending_unregisters.pop(player_id, None)
            self._player_last_activity.pop(player_id, None)
            unregister_event.set()

    async def _run_idle_timeout_loop(self) -> None:
        """Background task: unregister players idle longer than configured timeout."""
        timeout_minutes = cast(
            "int",
            self.config.get_value(CONF_PLAYER_IDLE_TIMEOUT, DEFAULT_PLAYER_IDLE_TIMEOUT),
        )
        interval_seconds = 60
        while not self.mass.closing:
            try:
                await asyncio.sleep(interval_seconds)
            except asyncio.CancelledError:
                break
            now = time.time()
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
                    self.mass.create_task(self._handle_player_unregister(player.player_id))
