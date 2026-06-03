"""AmpliPi Player Provider for Music Assistant."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import TYPE_CHECKING, cast

from music_assistant_models.errors import SetupFailedError
from pyamplipi.amplipi import AmpliPi

from music_assistant.models.player_provider import PlayerProvider

from .constants import CONF_HOST, POLL_INTERVAL
from .player import AmpliPiZonePlayer

if TYPE_CHECKING:
    from pyamplipi.models import Status


class AmpliPiPlayerProvider(PlayerProvider):
    """Player provider for AmpliPi multi-zone audio controllers."""

    api: AmpliPi
    _poll_task: asyncio.Task[None] | None = None
    _status: Status
    _players: dict[int, AmpliPiZonePlayer]

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self._players = {}
        host = cast("str", self.config.get_value(CONF_HOST))
        endpoint = host if host.startswith("http") else f"http://{host}/api"
        self.api = AmpliPi(
            endpoint=endpoint,
            timeout=10,
            http_session=self.mass.http_session,
        )
        try:
            self._status = await self.api.get_status()
        except Exception as err:
            raise SetupFailedError(f"Unable to connect to AmpliPi at {host}: {err}") from err

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        await self.discover_players()
        self._poll_task = self.mass.create_task(self._poll_loop())

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
        for player in list(getattr(self, "_players", {}).values()):
            await self.mass.players.unregister(player.player_id)
        if hasattr(self, "_players"):
            self._players.clear()
        with suppress(Exception):
            await self.api.close()

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
            except Exception as err:
                self.logger.warning("Failed to poll AmpliPi controller: %s", err)
                for player in self._players.values():
                    player.set_unavailable()
                continue
            # register any newly enabled zones that appeared
            await self.discover_players()
            for player in self._players.values():
                player.update_from_status(self._status)
