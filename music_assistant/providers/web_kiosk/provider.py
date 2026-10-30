"""Web Kiosk Player Provider implementation."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import logging
import secrets
import time
from collections.abc import Coroutine
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlsplit

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType, MediaType, PlaybackState
from music_assistant_models.errors import (
    InvalidDataError,
    MusicAssistantError,
    PlayerUnavailableError,
)

from music_assistant.constants import ATTR_ENABLED
from music_assistant.models.player_provider import PlayerProvider

from .constants import (
    CONF_ENABLE_PARTY_QR,
    CONF_ENABLE_SENDSPIN_BRIDGE,
    CONF_HTTP_PORT,
    CONF_KIOSK_URL,
    CONF_SHOW_STOP_NOTIFICATION,
    CONF_URL_BUILDER_REQUIRE_AUTH,
    DEFAULT_ENABLE_PARTY_QR,
    DEFAULT_ENABLE_SENDSPIN_BRIDGE,
    DEFAULT_HTTP_PORT,
    DEFAULT_PLAYER_IDLE_TIMEOUT,
    DEFAULT_SHOW_STOP_NOTIFICATION,
    DEFAULT_URL_BUILDER_REQUIRE_AUTH,
    PLAYER_DISCONNECT_GRACE_SECONDS,
    WEB_KIOSK_PLAYER_ID_PREFIX,
)
from .http_server import WebKioskHTTPServer
from .lyrics import parse_track_lyrics
from .ownership import SendspinIdentityConflictError, VerifiedSendspinClaim
from .player import END_MARGIN, PlaybackClaim, WebKioskPlayer, playback_lock, resume_position
from .sendspin_bridge import WebKioskSendspinBridgeManager

if TYPE_CHECKING:
    from music_assistant_models.player import PlayerMedia
    from music_assistant_models.player_queue import PlayerQueue
    from music_assistant_models.queue_item import QueueItem

logger = logging.getLogger(__name__)


class WebKioskProvider(PlayerProvider):
    """Player Provider that turns a web browser into a Music Assistant kiosk player."""

    http_server: WebKioskHTTPServer | None = None
    sendspin_bridge_enabled: bool = False
    party_qr_enabled: bool = False
    bridge_manager: WebKioskSendspinBridgeManager | None = None
    _player_last_activity: dict[str, float]
    _disconnect_tasks: dict[str, asyncio.Task[None]]
    _stream_token_secret: bytes
    _timeout_task: asyncio.Task[None] | None = None
    _wave_seq: dict[str, int]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize the provider."""
        super().__init__(*args, **kwargs)
        self._unloading = False
        self._player_lock = asyncio.Lock()
        self._sendspin_owners: dict[str, WebKioskPlayer] = {}
        self._player_last_activity = {}
        self._disconnect_tasks = {}
        self._background_tasks: set[asyncio.Future[Any]] = set()
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
                key=CONF_ENABLE_PARTY_QR,
                type=ConfigEntryType.BOOLEAN,
                required=False,
                default_value=DEFAULT_ENABLE_PARTY_QR,
            ),
            ConfigEntry(
                key=CONF_URL_BUILDER_REQUIRE_AUTH,
                type=ConfigEntryType.BOOLEAN,
                required=False,
                default_value=DEFAULT_URL_BUILDER_REQUIRE_AUTH,
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
        self.party_qr_enabled = bool(
            self.config.get_value(CONF_ENABLE_PARTY_QR, DEFAULT_ENABLE_PARTY_QR)
        )
        try:
            self.bridge_manager = WebKioskSendspinBridgeManager(self)
            self.http_server = WebKioskHTTPServer(self, port)
            await self.http_server.start()
        except Exception, asyncio.CancelledError:
            try:
                if self.http_server:
                    await self.http_server.stop()
            finally:
                self.http_server = None
                try:
                    if self.bridge_manager:
                        await self.bridge_manager.close()
                finally:
                    self.bridge_manager = None
            raise
        self.logger.info("Web Kiosk provider initialized, HTTP server on port %s", port)

    async def loaded_in_mass(self) -> None:
        """Start idle timeout task after provider is loaded."""
        await super().loaded_in_mass()
        self._timeout_task = self.mass.create_task(self._run_idle_timeout_loop())
        self.logger.info("Web Kiosk provider loaded — players register on demand")

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload — stop timeout task, HTTP server, then unregister players."""
        self._unloading = True
        # Completion and metadata work must not outlive this instance and
        # dispatch to a replacement kiosk with the same player id.
        background = list(self._background_tasks)
        for task in background:
            task.cancel()
        if background:
            await asyncio.gather(*background, return_exceptions=True)
        disconnect_tasks = list(self._disconnect_tasks.values())
        self.cancel_all_player_closes()
        if disconnect_tasks:
            await asyncio.gather(*disconnect_tasks, return_exceptions=True)
        if self._timeout_task and not self._timeout_task.done():
            self._timeout_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._timeout_task
            self._timeout_task = None

        async with self._player_lock:
            pass

        if self.bridge_manager:
            await self.bridge_manager.close()
            self.bridge_manager = None

        if self.http_server:
            await self.http_server.stop()
        async with self._player_lock:
            for player in list(
                self.mass.players.iter_players(
                    provider_filter=self.instance_id,
                    return_disabled=True,
                    return_protocol_players=True,
                )
            ):
                try:
                    self.logger.debug("Unloading player %s", player.display_name)
                    await self.mass.players.unregister(player.player_id)
                except MusicAssistantError:
                    self.logger.exception("Error unregistering player %s", player.player_id)
        self._player_last_activity.clear()
        self._sendspin_owners.clear()
        self.logger.info("Web Kiosk provider unloaded")

    async def discover_players(self) -> None:
        """Discover players — kiosk players are registered on demand when browsers connect."""

    async def get_or_register_player(
        self,
        player_id: str,
        display_name: str | None = None,
        ip_address: str | None = None,
        sendspin_client_id: str | None = None,
        sendspin_claim: VerifiedSendspinClaim | None = None,
        user_agent: str | None = None,
    ) -> WebKioskPlayer | None:
        """
        Get or register a kiosk player for the given player_id.

        Returns the player, or None if registration failed.
        """
        async with self._player_lock:
            if self._unloading:
                return None
            existing = self.mass.players.get_player(player_id, raise_unavailable=False)
            if existing and isinstance(existing, WebKioskPlayer):
                if user_agent:
                    existing.update_browser_info(user_agent)
                if ip_address and not existing.device_info.ip_address:
                    existing.device_info.ip_address = ip_address
                self.on_player_activity(player_id)
                if sendspin_client_id:
                    await self._set_sendspin_owner(existing, sendspin_client_id, sendspin_claim)
                    if self.bridge_manager:
                        await self.bridge_manager.prepare_player(existing)
                        await self.bridge_manager.evaluate_bridge(existing)
                return existing
            if not self.mass.config.get_raw_player_config_value(player_id, ATTR_ENABLED, True):
                return None
            name = display_name or self.player_display_name(player_id)
            player = WebKioskPlayer(
                provider=self,
                player_id=player_id,
                name=name,
                ip_address=ip_address,
            )
            if user_agent:
                player.update_browser_info(user_agent)
            if sendspin_client_id:
                await self._set_sendspin_owner(player, sendspin_client_id, sendspin_claim)
                if self.bridge_manager:
                    await self.bridge_manager.prepare_player(player)
            await self.mass.players.register(player)
            self._player_last_activity[player_id] = time.monotonic()
            self.logger.info("Registered kiosk player: %s (%s)", name, player_id)
            if self.bridge_manager:
                await self.bridge_manager.evaluate_bridge(player)
            return player

    async def note_sendspin_client_id(
        self,
        player: WebKioskPlayer,
        client_id: str,
        *,
        sendspin_claim: VerifiedSendspinClaim | None = None,
    ) -> None:
        """
        Store the browser's Sendspin identity and reconcile the bridge.

        sendspin-js 5 mints a cryptographic client id. Music Assistant
        (aiosendspin 10) only lets that browser take over the bridge when the
        external player was registered under the same id. Identity ownership
        transfers between kiosks without deleting saved Sendspin settings.
        """
        async with self._player_lock:
            if self._unloading:
                return
            await self._set_sendspin_owner(player, client_id, sendspin_claim)
            if self.bridge_manager:
                await self.bridge_manager.evaluate_bridge(player)

    def schedule_player_close(self, player_id: str) -> None:
        """
        Remove the player unless a browser reconnects within the grace window.

        The last socket closing is not enough to tell a closed tab from a reload.
        A reload opens a new socket and cancels this. A closed page does not.
        """
        if self._unloading:
            return
        self.cancel_player_close(player_id)
        self._disconnect_tasks[player_id] = self.mass.create_task(
            self._unregister_if_browser_gone(player_id)
        )

    def cancel_player_close(self, player_id: str) -> None:
        """Keep the player. A browser socket for it is open again."""
        task = self._disconnect_tasks.get(player_id)
        if task is asyncio.current_task():
            return
        self._disconnect_tasks.pop(player_id, None)
        if task is not None and not task.done():
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
        """Stop the kiosk page and unregister the player while keeping its browser connected."""
        if self.http_server:
            self.http_server.broadcast_stop(player_id)
        self._spawn(self._handle_player_unregister(player_id))

    def on_player_enabled(self, player_id: str) -> None:
        """Register the enabled kiosk again when its browser is still connected."""
        if self.http_server and self.http_server.client_count(player_id):
            self._spawn(self.http_server.reattach_player(player_id))

    async def remove_player(self, player_id: str) -> None:
        """
        Remove (delete) a player from this provider.

        Called when user chooses to remove the player from MA.
        This fully unregisters the player. It will reappear if the kiosk reconnects.
        """
        if self.http_server:
            self.http_server.broadcast_stop(player_id)
        await self._handle_player_unregister(player_id, permanent=True)
        self.logger.info("Player %s removed by user", player_id)

    def next_wave_seq(self, player_id: str) -> int:
        """Return the next energy-curve generation for this player."""
        seq = self._wave_seq.get(player_id, 0) + 1
        self._wave_seq[player_id] = seq
        return seq

    def spawn_wave(self, player_id: str, seq: int, media: PlayerMedia | None = None) -> None:
        """
        Look up the track's energy curve and send it to the page.

        :param player_id: The kiosk player to send the curve to.
        :param seq: The energy-curve generation the page expects.
        :param media: The dispatched media; the active queue item is used when omitted.
        """
        self._spawn(self._publish_wave(player_id, seq, media))

    def spawn_lyrics(self, player_id: str, seq: int, media: PlayerMedia | None = None) -> None:
        """
        Look up the track's lyrics and send them to the page.

        :param player_id: The kiosk player to send the lyrics to.
        :param seq: The energy-curve generation the page expects.
        :param media: The dispatched media; the active queue item is used when omitted.
        """
        # Lyrics lookups can refresh metadata and call external providers.
        if self.http_server is None or not self.http_server.lyrics_requested(player_id):
            return
        self._spawn(self._publish_lyrics(player_id, seq, media))

    async def fallback_sendspin(
        self, player_id: str, *, expected_client_id: str | None = None
    ) -> None:
        """Resume the active MA queue through the kiosk's native HTML5 output."""
        # MA serializes its own playback commands per player (and its group)
        # with this lock. Queue pause and stop do not take it, so the kiosk takes
        # over the output before the first await instead (see below).
        async with playback_lock(self.mass, player_id):
            async with self._player_lock:
                if self._unloading:
                    return
                player = self.mass.players.get_player(player_id)
                if not isinstance(player, WebKioskPlayer) or not player.sendspin_client_id:
                    return
                client_id = player.sendspin_client_id
                if expected_client_id is not None and client_id != expected_client_id:
                    return
                if player.sendspin_fallback:
                    return
                queue = self.mass.player_queues.get_active_queue(player_id)
                if queue is None:
                    raise PlayerUnavailableError("No active queue for Sendspin fallback")
                # A startup failure also requests fallback. Only playback that was
                # running moves to HTML5; a paused or stopped queue keeps its state.
                was_playing = queue.state == PlaybackState.PLAYING
                # A sync follower's active queue belongs to its leader. Leaving the
                # group ends the kiosk's part in it, as an ungroup does in Music
                # Assistant: the leader keeps playing and nothing is carried over.
                protocol = self.mass.players.get_player(client_id)
                follower = bool(protocol and protocol.synced_to and protocol.synced_to != client_id)
                identity = player.sendspin_claim
                player.sendspin_fallback = True
                # Commands issued during the teardown now reach the kiosk itself:
                # a pause (a stop on a Sendspin output) or a stop is then kept
                # instead of being undone by the resume.
                claim = player.claim(take_over=True) if was_playing and not follower else None
                try:
                    # Detach only this owned transport, through the players
                    # controller so it can resolve the protocol member and keep a
                    # captured group's own membership handling intact.
                    if protocol and (protocol.synced_to or len(protocol.group_members) > 1):
                        await self.mass.players.cmd_set_members(
                            player.player_id, player_ids_to_remove=[client_id]
                        )
                    # Release the kiosk's own bridge instead of unregistering the
                    # Sendspin provider's player: removing the client lets that
                    # provider drop its player itself, and a later verified claim
                    # sets the bridge up again so the provider registers it anew.
                    if self.bridge_manager:
                        await self.bridge_manager.remove_bridge(player_id)
                    player.set_active_output_protocol("native")
                except BaseException as err:
                    player.sendspin_fallback = False
                    if claim is not None:
                        await self._hand_back_output(player, client_id, claim, err)
                    raise
            # Stream preparation can take time. Release the registration lock while
            # resuming, and do not clear a newer connection's state on failure.
            if claim is None:
                return
            try:
                # A newer verified connection owns the output choice now.
                if player.sendspin_claim is not identity:
                    return
                # A pause during the teardown or the resume is kept: the queue
                # resumes paused. The user's last command wins.
                await claim.start(lambda: self._resume_inside_track(queue))
            except BaseException:
                if player.sendspin_claim is identity:
                    player.sendspin_fallback = False
                raise
            finally:
                claim.release()

    async def run_owned[T](self, coro: Coroutine[Any, Any, T]) -> T:
        """
        Run work that must not depend on its caller as provider work, and await it.

        Cancelling the caller (a page that closes) leaves the work running to its
        end; unload cancels it. Its failure reaches the caller, or is logged once
        the caller is gone.

        :param coro: The work to run.
        :raises asyncio.CancelledError: When the provider is unloading.
        """
        task = self._own(coro)
        if task is None:
            raise asyncio.CancelledError
        try:
            return cast("T", await asyncio.shield(task))
        except asyncio.CancelledError:
            # The caller went away; whoever finishes last reports a failure.
            if task.done():
                self._report_abandoned_work(task)
            else:
                task.add_done_callback(self._report_abandoned_work)
            raise

    def request_sendspin_fallback(self, player_id: str, client_id: str) -> None:
        """
        Schedule native playback for a browser whose Sendspin output has no audio.

        :param player_id: The kiosk player that asked for the fallback.
        :param client_id: The Sendspin client the request was verified for.
        """
        self._spawn(self._fallback_sendspin_safely(player_id, client_id))

    def track_ended(self, player_id: str, playback_generation: str) -> None:
        """
        End the playback after the browser finished its stream.

        :param player_id: The kiosk player that finished playback.
        :param playback_generation: The playback instance the browser finished.
        """
        self._spawn(self._finish_playback(player_id, playback_generation))

    def playback_failed(self, player_id: str, playback_generation: str) -> None:
        """
        Stop playback the browser could not load or continue.

        :param player_id: The kiosk player that reported the failure.
        :param playback_generation: The playback instance that failed.
        """
        self._spawn(self._stop_failed_playback(player_id, playback_generation))

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
        except MusicAssistantError as err:
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
            if (
                self._unloading
                or self._disconnect_tasks.get(player_id) is not asyncio.current_task()
            ):
                return
            server = self.http_server
            if server is not None and server.client_count(player_id) > 0:
                return
            player = self.mass.players.get_player(player_id, raise_unavailable=False)
            if not isinstance(player, WebKioskPlayer):
                return
            self.logger.info("Unregistering kiosk player %s because its page closed", player_id)
            await self._handle_player_unregister(player_id, only_if_disconnected=True)
        finally:
            if self._disconnect_tasks.get(player_id) is asyncio.current_task():
                self._disconnect_tasks.pop(player_id, None)

    def _spawn(self, coro: Any) -> None:
        """Schedule coro as provider-owned work that unload cancels."""
        self._own(coro)

    def _own(self, coro: Any) -> asyncio.Future[Any] | None:
        """Start coro as provider-owned work; None once the provider is unloading."""
        if self._unloading:
            coro.close()
            return None
        task: asyncio.Future[Any] = self.mass.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    def _report_abandoned_work(self, task: asyncio.Future[Any]) -> None:
        """Log the failure of provider work whose caller went away."""
        if not task.cancelled() and (error := task.exception()) is not None:
            self.logger.error("Kiosk background work failed", exc_info=error)

    async def _publish_wave(
        self, player_id: str, seq: int, media: PlayerMedia | None = None
    ) -> None:
        """Send the stored RMS curve for the player's current queue item."""
        if self.http_server is None:
            return
        try:
            bins = await self._current_wave(player_id, media)
        except MusicAssistantError:
            logger.debug("Energy curve lookup failed for %s", player_id, exc_info=True)
            bins = None
        if self.http_server is not None:
            self.http_server.broadcast_wave(player_id, seq, bins)

    async def _current_wave(
        self, player_id: str, media: PlayerMedia | None = None
    ) -> list[float] | None:
        """Return the stored energy bins for the playing item, if analysis exists."""
        media_item = getattr(self._queue_item(player_id, media), "media_item", None)
        mappings = getattr(media_item, "provider_mappings", None) or []
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

    async def _publish_lyrics(
        self, player_id: str, seq: int, media: PlayerMedia | None = None
    ) -> None:
        """Send the current track's lyric lines to the kiosk page."""
        if self.http_server is None:
            return
        try:
            lines = await self._current_lyrics(player_id, media)
        except MusicAssistantError:
            logger.debug("Lyrics lookup failed for %s", player_id, exc_info=True)
            lines = []
        if self.http_server is not None:
            self.http_server.broadcast_lyrics(player_id, seq, lines)

    async def _current_lyrics(
        self, player_id: str, media: PlayerMedia | None = None
    ) -> list[dict[str, Any]]:
        """Return parsed lyric lines for the playing item."""
        media_item = getattr(self._queue_item(player_id, media), "media_item", None)
        if media_item is None or media_item.media_type != MediaType.TRACK:
            return []
        plain, lrc = await self.mass.metadata.get_track_lyrics(media_item)
        return parse_track_lyrics(plain, lrc)

    def _queue_item(self, player_id: str, media: PlayerMedia | None) -> QueueItem | None:
        """Return the queue item of the dispatched media, else the active queue's item."""
        # A queue updates its current item only after dispatch returns, so the
        # identity carried by the media is the reliable one during play_media.
        if media is not None and media.source_id and media.queue_item_id:
            return self.mass.player_queues.get_item(media.source_id, media.queue_item_id)
        queue = self.mass.player_queues.get_active_queue(player_id)
        return getattr(queue, "current_item", None)

    async def _fallback_sendspin_safely(self, player_id: str, client_id: str) -> None:
        """Handle expected fallback failures without losing the kiosk socket."""
        try:
            await self.fallback_sendspin(player_id, expected_client_id=client_id)
        except MusicAssistantError, TimeoutError:
            logger.warning("Native playback fallback failed for %s", player_id, exc_info=True)

    async def _stop_failed_playback(self, player_id: str, playback_generation: str) -> None:
        """End the current playback when the browser reports that it failed."""
        player = self.mass.players.get_player(player_id)
        if (
            not isinstance(player, WebKioskPlayer)
            or playback_generation != player.playback_generation
            or player.playback_state == PlaybackState.IDLE
        ):
            return
        # Report IDLE like a finished stream; the queue stays where it is.
        await player.stop()

    async def _finish_playback(self, player_id: str, playback_generation: str) -> None:
        """Report IDLE once the browser has played its stream to the end."""
        # These reports only change the kiosk's own state and issue no Music
        # Assistant command, so they take no playback lock: an announcement holds
        # that lock until this player reports IDLE. The generation check and the
        # state change below run without an await in between, so a replacement
        # that starts meanwhile is never mistaken for the finished stream.
        player = self.mass.players.get_player(player_id)
        if not isinstance(player, WebKioskPlayer) or player.playback_state != PlaybackState.PLAYING:
            return
        if playback_generation != player.playback_generation:
            return
        # A queue plays as one flow stream, so its end is the end of the queue;
        # other media (radio, an announcement) is a stream of its own. Either
        # way the kiosk reports IDLE and Music Assistant's end-of-queue and
        # announcement-restore handling decides what follows.
        await player.stop()

    async def _hand_back_output(
        self, player: WebKioskPlayer, client_id: str, claim: PlaybackClaim, err: BaseException
    ) -> None:
        """Return playback commands to the Sendspin output after a failed fallback teardown."""
        commanded = not claim.owns_player or player.playback_state == PlaybackState.PAUSED
        claim.release()
        player.set_active_output_protocol(client_id)
        # A pause or stop reached the kiosk during the teardown, while the
        # Sendspin output kept playing. That output has no pause, so stop it as
        # Music Assistant would. A cancelled teardown awaits nothing more.
        if commanded and isinstance(err, Exception):
            if output := self.mass.players.get_player(client_id):
                try:
                    await output.stop()
                except MusicAssistantError:
                    logger.warning("Cannot stop the Sendspin output %s", client_id, exc_info=True)

    async def _resume_inside_track(self, queue: PlayerQueue) -> None:
        """Resume a playing queue, starting the last seconds of a track it ran past."""
        item = queue.current_item
        duration = item.duration if item else None
        # The position comes from a clock that ran on without a browser playing
        # it. Resuming past the end would serve an empty stream; the last
        # seconds let the track finish and the queue move on as usual.
        if (
            item is not None
            and duration
            and item.media_type != MediaType.RADIO
            and queue.corrected_elapsed_time >= duration - END_MARGIN
        ):
            await self.mass.player_queues.play_index(
                queue.queue_id,
                item.queue_item_id,
                resume_position(duration, queue.corrected_elapsed_time),
            )
            return
        await self.mass.player_queues.resume(queue.queue_id)

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
            return f"http://{host}:{port}/kiosk"
        return f"http://<ma-host>:{port}/kiosk"

    async def _set_sendspin_owner(
        self, player: WebKioskPlayer, client_id: str, claim: VerifiedSendspinClaim | None
    ) -> None:
        """Transfer identity ownership while the player lifecycle lock is held."""
        if claim is None or claim.player_id != player.player_id or claim.client_id != client_id:
            raise InvalidDataError("Sendspin identity ownership has not been verified")
        # One device keeps the identity it proved first. Handshakes overlap, so this
        # is checked here, under the lifecycle lock, not only before the proof.
        if player.sendspin_client_id and player.sendspin_client_id != client_id:
            raise SendspinIdentityConflictError(player.player_id)
        player.sendspin_claim = claim
        player.sendspin_fallback = False
        owner = self._sendspin_owners.get(client_id)
        if owner is not None and owner is not player:
            owner.sendspin_client_id = None
            if self.bridge_manager:
                await self.bridge_manager.release_bridge(owner.player_id)
        player.sendspin_client_id = client_id
        self._sendspin_owners[client_id] = player

    async def _handle_player_unregister(
        self, player_id: str, *, only_if_disconnected: bool = False, permanent: bool = False
    ) -> None:
        """Unregister a player while excluding concurrent registration."""
        async with self._player_lock:
            if self._unloading:
                return
            if (
                only_if_disconnected
                and self.http_server
                and self.http_server.client_count(player_id)
            ):
                return
            self.cancel_player_close(player_id)
            self.logger.debug("Unregistering kiosk player %s", player_id)
            try:
                if self.bridge_manager:
                    await self.bridge_manager.release_bridge(player_id, permanent=permanent)
                if permanent:
                    await self.mass.players.unregister(player_id, permanent=True)
                else:
                    await self.mass.players.unregister(player_id)
            finally:
                for client_id, owner in list(self._sendspin_owners.items()):
                    if owner.player_id == player_id:
                        owner.sendspin_client_id = None
                        self._sendspin_owners.pop(client_id, None)
                self._player_last_activity.pop(player_id, None)

    async def _run_idle_timeout_loop(self) -> None:
        """Background task: clean up idle players registered without a browser socket."""
        timeout_minutes = DEFAULT_PLAYER_IDLE_TIMEOUT
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
                if self.http_server and self.http_server.client_count(player.player_id) > 0:
                    continue
                last = self._player_last_activity.get(player.player_id, 0)
                if last > 0 and last < cutoff:
                    self.logger.info(
                        "Unregistering idle kiosk player %s (no activity for %d min)",
                        player.player_id,
                        timeout_minutes,
                    )
                    await self._handle_player_unregister(
                        player.player_id, only_if_disconnected=True
                    )
