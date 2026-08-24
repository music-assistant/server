"""AmpliPi Player Provider for Music Assistant."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import TYPE_CHECKING, cast
from urllib.parse import urlparse

from music_assistant_models.errors import PlayerCommandFailed, SetupFailedError
from pyamplipi.amplipi import AmpliPi
from pyamplipi.models import Stream, StreamUpdate

from music_assistant.models.player_provider import PlayerProvider

from .constants import (
    AMPLIPI_API_ERRORS,
    CONF_HOST,
    EXCLUDED_SELECTABLE_STREAM_TYPES,
    MA_STREAM_NAME,
    MA_STREAM_TYPE,
    POLL_INTERVAL,
)
from .player import AmpliPiZonePlayer

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry
    from pyamplipi.models import Status
    from pyamplipi.models import Stream as AmpliPiStream


def _ma_stream_source_id(stream: AmpliPiStream) -> int | None:
    """
    Return the AmpliPi source id a Music Assistant stream is bound to, or None.

    Requires both the exact "<MA_STREAM_NAME> <source_id>" naming scheme and
    MA_STREAM_TYPE, so a user-created stream that merely starts with the same
    name prefix (e.g. "Music Assistant Radio") is never mistaken for one of ours.

    :param stream: The AmpliPi stream to inspect.
    """
    if stream.id is None or stream.type != MA_STREAM_TYPE:
        return None
    name = stream.name or ""
    if not name.startswith(f"{MA_STREAM_NAME} "):
        return None
    suffix = name[len(MA_STREAM_NAME) :].strip()
    try:
        return int(suffix)
    except ValueError:
        return None


class AmpliPiPlayerProvider(PlayerProvider):
    """Player provider for AmpliPi multi-zone audio controllers."""

    api: AmpliPi
    _poll_task: asyncio.Task[None] | None = None
    _status: Status
    _players: dict[int, AmpliPiZonePlayer]
    # AmpliPi source id -> id of the Music Assistant internetradio stream bound to it
    _ma_streams: dict[int, int]
    # last-polled AmpliPi streams (native streams + RCA inputs), used to build source lists
    _streams: list[AmpliPiStream]
    # per-source locks for ensure_stream: concurrent callers for the same source cannot
    # create duplicate streams, while different sources still proceed in parallel
    _stream_locks: dict[int, asyncio.Lock]

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to configure this provider."""
        return ()

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self._players = {}
        self._ma_streams = {}
        self._streams = []
        self._stream_locks = {}
        host = cast("str", self.get_setup_value(CONF_HOST))
        if host.startswith(("http://", "https://")):
            # a full URL is used as-is, but a schemed host with no path (e.g.
            # "https://amplipi.local") still needs the AmpliPi "/api" base appended
            endpoint = host.rstrip("/")
            if urlparse(endpoint).path in ("", "/"):
                endpoint = f"{endpoint}/api"
        else:
            endpoint = f"http://{host}/api"
        self.api = AmpliPi(
            endpoint=endpoint,
            timeout=10,
            http_session=self.mass.http_session,
        )
        try:
            self._status = await self.api.get_status()
        except AMPLIPI_API_ERRORS as err:
            raise SetupFailedError(f"Unable to connect to AmpliPi at {host}: {err}") from err

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        with suppress(*AMPLIPI_API_ERRORS):
            self._streams = await self.api.get_streams()
        # adopt (reuse) any Music Assistant streams left by a previous run instead of
        # recreating them, so a reload does not interrupt active playback
        self._adopt_ma_streams(self._streams)
        await self.discover_players()
        self._poll_task = self.mass.create_task(self._poll_loop())

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            # await the cancelled task so it finishes cleanly (avoids "Task was destroyed
            # but it is pending" warnings and surfaces any cleanup errors)
            with suppress(asyncio.CancelledError):
                await self._poll_task
        for player in list(self._players.values()):
            await self.mass.players.unregister(player.player_id)
        self._players.clear()
        # only delete our streams when the provider is being removed; on a plain reload
        # they are kept (and re-adopted) so playback survives and they can be reused
        if is_removed:
            await self._remove_ma_streams()
        # NOTE: we deliberately do not call api.close(): the AmpliPi client was created with
        # Music Assistant's shared http_session, and pyamplipi's close() would close that
        # session, tearing down networking for the rest of Music Assistant.

    async def ensure_stream(self, source_id: int, url: str) -> int:
        """
        Ensure a Music Assistant internetradio stream exists for the given AmpliPi source.

        Creates the stream on first use (one per source) and (re)points it at the supplied
        Music Assistant stream URL on every call. Returns the AmpliPi stream id.

        :param source_id: The AmpliPi source id the stream is bound to.
        :param url: The Music Assistant stream URL the AmpliPi stream should play.
        """
        # serialize per source so two concurrent callers for the same source cannot each
        # miss the cache and create duplicate streams; other sources are unaffected
        lock = self._stream_locks.setdefault(source_id, asyncio.Lock())
        async with lock:
            try:
                if (stream_id := self._ma_streams.get(source_id)) is not None:
                    await self.api.set_stream(stream_id, StreamUpdate(url=url))
                    return stream_id
                name = f"{MA_STREAM_NAME} {source_id}"
                await self.api.create_stream(Stream(name=name, type=MA_STREAM_TYPE, url=url))
                # create_stream's response is not a reliable carrier of the new id, so look it up
                streams = await self.api.get_streams()
            except AMPLIPI_API_ERRORS as err:
                # surface a clean MA error instead of leaking the transport-layer exception
                raise PlayerCommandFailed(
                    f"AmpliPi stream operation failed for source {source_id}: {err}"
                ) from err
            new_stream_id: int | None = next(
                (
                    s.id
                    for s in streams
                    if s.name == name and s.type == MA_STREAM_TYPE and s.id is not None
                ),
                None,
            )
            if new_stream_id is None:
                raise PlayerCommandFailed(f"Failed to create AmpliPi stream for source {source_id}")
            self._ma_streams[source_id] = new_stream_id
            return new_stream_id

    async def discover_players(self) -> None:
        """Discover (register) the zones of the AmpliPi controller as players."""
        for zone in self._status.zones:
            if zone.disabled or zone.id is None or zone.id in self._players:
                continue
            player = AmpliPiZonePlayer(self, zone.id)
            self._players[zone.id] = player
            await self.mass.players.register_or_update(player)
            player.update_from_status(self._status)

    @property
    def status(self) -> Status:
        """Return the last polled AmpliPi status."""
        return self._status

    def selectable_streams(self) -> list[AmpliPiStream]:
        """
        Return the AmpliPi streams a user can select as a source (native streams + RCA inputs).

        Excludes the internetradio streams Music Assistant creates for its own playback and
        the built-in announcement player (fileplayer).
        """
        return [
            stream
            for stream in self._streams
            if stream.id is not None
            and _ma_stream_source_id(stream) is None
            and stream.type not in EXCLUDED_SELECTABLE_STREAM_TYPES
        ]

    def zone_id_for(self, player_id: str) -> int | None:
        """Return the AmpliPi zone id for the given Music Assistant player_id."""
        for zone_id, player in self._players.items():
            if player.player_id == player_id:
                return zone_id
        return None

    async def _poll_loop(self) -> None:
        """Poll the AmpliPi controller for state updates and propagate them to the players."""
        while True:
            await asyncio.sleep(POLL_INTERVAL)
            try:
                self._status = await self.api.get_status()
            except asyncio.CancelledError:
                raise
            except AMPLIPI_API_ERRORS as err:
                self.logger.warning("Failed to poll AmpliPi controller: %s", err)
                for player in self._players.values():
                    player.set_unavailable()
                continue
            # refresh the selectable stream list (native streams + RCA inputs); best-effort
            with suppress(*AMPLIPI_API_ERRORS):
                self._streams = await self.api.get_streams()
            # register any newly enabled zones that appeared
            await self.discover_players()
            for player in self._players.values():
                player.update_from_status(self._status)

    def _adopt_ma_streams(self, streams: list[AmpliPiStream]) -> None:
        """
        Rebuild the source -> stream id map from the controller's existing MA streams.

        Music Assistant names its per-source streams "<MA_STREAM_NAME> <source_id>", so the
        owning source id can be recovered from the name and the stream reused across reloads.

        :param streams: The streams currently present on the AmpliPi controller.
        """
        self._ma_streams = {}
        for stream in streams:
            source_id = _ma_stream_source_id(stream)
            if source_id is not None and stream.id is not None:
                self._ma_streams[source_id] = stream.id

    async def _remove_ma_streams(self) -> None:
        """Delete every Music Assistant stream from the AmpliPi controller."""
        with suppress(*AMPLIPI_API_ERRORS):
            for stream in await self.api.get_streams():
                if _ma_stream_source_id(stream) is not None:
                    with suppress(*AMPLIPI_API_ERRORS):
                        await self.api.delete_stream(stream.id)
        self._ma_streams = {}
