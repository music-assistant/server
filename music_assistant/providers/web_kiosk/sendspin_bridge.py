"""
Sendspin bridge: expose web kiosk players as external Sendspin clients.

Follows the Music Assistant Chromecast receiver model. The bridge registers
the kiosk as an external Sendspin client under the cryptographic client id that sendspin-js 5
minted in the browser. When a synchronized stream starts, the kiosk opens
itself in Sendspin mode and connects with that same identity, and the server
upgrades the client from the bridge role to a real player. Audio never flows
through this bridge.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING, cast
from urllib.parse import quote

from aiosendspin.models.core import ClientHelloPayload
from aiosendspin.models.core import DeviceInfo as SendspinDeviceInfo
from aiosendspin.models.player import ClientHelloPlayerSupport, SupportedAudioFormat
from aiosendspin.models.types import AudioCodec, PlayerCommand
from music_assistant_models.enums import IdentifierType
from music_assistant_models.errors import MusicAssistantError

from music_assistant.providers.sendspin.bridge_manager import SendspinBridgeManagerBase
from music_assistant.providers.sendspin.bridge_role import (
    BRIDGE_BIT_DEPTH,
    BRIDGE_CHANNELS,
    BRIDGE_ROLE_ID,
    BRIDGE_SAMPLE_RATE,
    BridgePlayerRole,
)

from .constants import SENDSPIN_CONNECT_TIMEOUT
from .player import WebKioskPlayer

if TYPE_CHECKING:
    from aiosendspin.server import SendspinClient, SendspinServer
    from aiosendspin.server.server import ExternalStreamStartRequest

    from music_assistant.models.player import Player
    from music_assistant.providers.sendspin.provider import SendspinProvider

    from .provider import WebKioskProvider


class WebKioskSendspinBridge:
    """Sendspin bridge for a single web kiosk player."""

    def __init__(
        self,
        provider: WebKioskProvider,
        kiosk_player: WebKioskPlayer,
        sendspin_server: SendspinServer,
        client_id: str,
    ) -> None:
        """
        Initialize the bridge (registration happens in start()).

        :param provider: The owning Web Kiosk provider.
        :param kiosk_player: The kiosk player this bridge rides on.
        :param sendspin_server: The Sendspin server to register with.
        :param client_id: Stable Sendspin client id for this kiosk.
        """
        self.provider = provider
        self.kiosk_player = kiosk_player
        self.sendspin_server = sendspin_server
        self.client_id = client_id
        self.logger = provider.logger.getChild("sendspin_bridge")
        self._client: SendspinClient | None = None
        self._connect_watchdog: asyncio.Task[None] | None = None
        self._watchdog_tasks: set[asyncio.Task[None]] = set()

    @property
    def is_registered(self) -> bool:
        """Return whether the bridge is registered with Sendspin."""
        return self._client is not None

    @property
    def hello_payload(self) -> ClientHelloPayload:
        """Return the identity and commands this browser bridge supports."""
        return ClientHelloPayload(
            client_id=self.client_id,
            name=f"{self.kiosk_player.display_name} (Sendspin)",
            version=1,
            supported_roles=["player@v1"],
            device_info=SendspinDeviceInfo(
                product_name=f"Web Kiosk / {self.kiosk_player.device_info.model}",
                manufacturer=self.kiosk_player.device_info.manufacturer,
            ),
            player_support=ClientHelloPlayerSupport(
                supported_formats=[
                    SupportedAudioFormat(
                        codec=AudioCodec.PCM,
                        channels=BRIDGE_CHANNELS,
                        sample_rate=BRIDGE_SAMPLE_RATE,
                        bit_depth=BRIDGE_BIT_DEPTH,
                    )
                ],
                buffer_capacity=1_000,
                supported_commands=[PlayerCommand.VOLUME],
            ),
        )

    async def start(self) -> None:
        """Register the kiosk as an external Sendspin client."""
        if sendspin_prov := cast(
            "SendspinProvider | None", self.provider.mass.get_provider("sendspin")
        ):
            sendspin_prov.register_bridge_identifiers(
                self.client_id,
                {
                    IdentifierType.UUID: self.kiosk_player.device_info.identifiers[
                        IdentifierType.UUID
                    ]
                },
            )
            sendspin_prov.register_bridge_underlying_player(
                self.client_id, self.kiosk_player.player_id
            )
        hello = self.hello_payload
        existing = self.sendspin_server.get_client(self.client_id)
        if sendspin_prov is not None and existing is not None and existing.is_connected:
            await sendspin_prov.apply_bridge_claim(
                self.client_id,
                {
                    IdentifierType.UUID: self.kiosk_player.device_info.identifiers[
                        IdentifierType.UUID
                    ]
                },
                hello,
                underlying_player_id=self.kiosk_player.player_id,
            )
            self._client = existing
            return
        self._client = self.sendspin_server.register_external_player(
            replace(hello, supported_roles=[BRIDGE_ROLE_ID]), on_stream_start=self._on_stream_start
        )
        # The bridge role is a no-op audio sink until the kiosk's JS client
        # connects and takes over; audio requirements make the server treat
        # the client as stream-ready in the meantime.
        for role in self._client.roles_by_family("player"):
            if isinstance(role, BridgePlayerRole):
                role.setup_audio_requirements()
        self.logger.info(
            "Sendspin bridge registered for %s (client_id=%s)",
            self.kiosk_player.display_name,
            self.client_id,
        )

    async def stop(self) -> None:
        """Stop and unregister the bridge."""
        self._cancel_watchdog()
        tasks = tuple(self._watchdog_tasks)
        for task in tasks:
            if not task.cancelling():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._client is not None:
            await self.sendspin_server.remove_client(self.client_id)
            self._client = None

    def _on_stream_start(self, request: ExternalStreamStartRequest) -> None:
        """Push the kiosk into Sendspin mode via the provider's WebSocket."""
        http_server = self.provider.http_server
        if http_server is None:
            self.logger.warning(
                "Sendspin stream start for %s but the HTTP server is not running",
                self.kiosk_player.display_name,
            )
            return
        self.logger.debug(
            "Sendspin stream start for %s (reason=%s) — opening kiosk in Sendspin mode",
            self.kiosk_player.display_name,
            getattr(request, "connection_reason", None),
        )
        kiosk_url = f"/kiosk?sendspin=1&sendspin_client_id={quote(self.client_id, safe='')}"
        http_server.broadcast_sendspin(self.kiosk_player.player_id, kiosk_url)
        self._cancel_watchdog()
        self._connect_watchdog = self.provider.mass.create_task(self._watch_connect())
        self._watchdog_tasks.add(self._connect_watchdog)
        self._connect_watchdog.add_done_callback(self._watchdog_tasks.discard)

    async def _watch_connect(self) -> None:
        """Fall back to the regular HTTP player when the kiosk never connects."""
        await asyncio.sleep(SENDSPIN_CONNECT_TIMEOUT)
        client = self._client
        if client is not None and client.is_connected:
            return
        self.logger.warning(
            "Kiosk %s did not connect its Sendspin client within %.0fs — "
            "transferring playback to the regular HTTP player",
            self.kiosk_player.display_name,
            SENDSPIN_CONNECT_TIMEOUT,
        )
        try:
            await self.provider.mass.player_queues.transfer_queue(
                self.client_id, self.kiosk_player.player_id, auto_play=True
            )
        except MusicAssistantError, TimeoutError:
            self.logger.warning("Fallback transfer to the HTTP player failed", exc_info=True)

    def _cancel_watchdog(self) -> None:
        if (
            self._connect_watchdog
            and not self._connect_watchdog.done()
            and not self._connect_watchdog.cancelling()
        ):
            self._connect_watchdog.cancel()
        self._connect_watchdog = None


class WebKioskSendspinBridgeManager(SendspinBridgeManagerBase[WebKioskSendspinBridge]):
    """Manages Sendspin bridges for all web kiosk players."""

    async def prepare_player(self, player: WebKioskPlayer) -> None:
        """Declare the browser identity before MA registers its native kiosk player."""
        client_id = player.sendspin_client_id
        sendspin = self.sendspin_provider
        if (
            not self._should_have_bridge(player)
            or not client_id
            or sendspin is None
            or self.sendspin_server is None
        ):
            return
        identifiers = {IdentifierType.UUID: player.device_info.identifiers[IdentifierType.UUID]}
        sendspin.register_bridge_identifiers(client_id, identifiers)
        sendspin.register_bridge_underlying_player(client_id, player.player_id)
        existing = self.mass.players.get_player(client_id)
        if existing is None:
            return
        parent = self.mass.players.get_player(existing.protocol_parent_id or "")
        if (
            parent is not None
            and parent.provider.domain == "universal_player"
            and {link.output_protocol_id for link in parent.linked_output_protocols} == {client_id}
        ):
            # Give MA's one-output wrapper the browser identity before native registration.
            # MA then owns the wrapper replacement and configuration/group migration.
            parent.device_info.add_identifier(IdentifierType.UUID, identifiers[IdentifierType.UUID])
        await sendspin.apply_bridge_claim(
            client_id,
            identifiers,
            self._create_bridge(player).hello_payload,
            underlying_player_id=player.player_id,
        )

    async def release_bridge(self, player_id: str, permanent: bool = False) -> None:
        """Detach a kiosk's bridge and relinquish its identity ownership."""
        await self.remove_bridge(player_id, permanent=permanent)
        self._client_ids.pop(player_id, None)

    def _bridge_client_id(self, player: Player) -> str | None:
        """Return the browser Sendspin identity bridging the given player."""
        if not isinstance(player, WebKioskPlayer):
            return None
        return player.sendspin_client_id

    def _create_bridge(self, player: Player) -> WebKioskSendspinBridge:
        """Create a (not yet started) bridge for a kiosk player."""
        sendspin_server = self.sendspin_server
        assert sendspin_server is not None  # guaranteed by _lifecycle_allows_bridge
        kiosk_player = cast("WebKioskPlayer", player)
        client_id = kiosk_player.sendspin_client_id
        if not client_id:
            raise RuntimeError(f"Kiosk {kiosk_player.player_id} has no Sendspin client id")
        return WebKioskSendspinBridge(
            cast("WebKioskProvider", self.provider),
            kiosk_player,
            sendspin_server,
            client_id,
        )

    def _should_have_bridge(self, player: Player) -> bool:
        """Bridge policy: the option is on, and the browser has announced an identity."""
        provider = cast("WebKioskProvider", self.provider)
        return (
            provider.sendspin_bridge_enabled
            and isinstance(player, WebKioskPlayer)
            and bool(player.sendspin_client_id)
        )
