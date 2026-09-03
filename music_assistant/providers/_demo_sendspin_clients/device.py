"""A single fake Sendspin client, wired to one scenario's pairing profile."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import replace
from typing import TYPE_CHECKING

from aiohttp import ClientError
from aiosendspin.client import PairingSupport, SendspinClient
from aiosendspin.models.core import DeviceInfo
from aiosendspin.models.player import ClientHelloPlayerSupport, SupportedAudioFormat
from aiosendspin.models.source import ClientHelloSourceFeatures, ClientHelloSourceSupport
from aiosendspin.models.types import AudioCodec, Roles
from aiosendspin.noise.driver import HandshakeAbortedError
from aiosendspin.noise.keys import Identity, generate_psk, psk_id_for
from aiosendspin.noise.trust_store import FileClientPairingStore, PairingPsk

from .constants import DEVICE_MANUFACTURER, RECONNECT_INTERVAL, STATIC_PIN

if TYPE_CHECKING:
    from pathlib import Path

    from aiosendspin.models.types import PairAbortReason

    from .scenarios import Scenario

LOGGER = logging.getLogger(__name__)

# Buffer a real speaker would advertise; never filled, since the audio is dropped.
BUFFER_CAPACITY = 1024 * 1024

SUPPORTED_FORMATS = [
    SupportedAudioFormat(codec=AudioCodec.FLAC, channels=2, sample_rate=48000, bit_depth=16),
    SupportedAudioFormat(codec=AudioCodec.PCM, channels=2, sample_rate=48000, bit_depth=16),
]


class FakeSendspinDevice:
    """
    A fake Sendspin client that connects to this server and pairs like real hardware.

    Audio is decoded and dropped, so the player is a usable playback target without
    needing a sound card.
    """

    def __init__(self, scenario: Scenario, storage_dir: Path, server_url: str) -> None:
        """
        Build the device for a scenario, without connecting it yet.

        :param scenario: The pairing profile this device presents.
        :param storage_dir: Directory holding one pairing-store file per device.
        :param server_url: WebSocket URL of this server's Sendspin endpoint.
        """
        self.scenario = scenario
        self.identity = _scenario_identity(scenario.scenario_id)
        self.dynamic_pin: str | None = None
        self.awaiting_button: bool = False
        self.last_abort: PairAbortReason | None = None
        self._storage_path = storage_dir / f"{scenario.scenario_id}.json"
        self._server_url = server_url
        self._client: SendspinClient | None = None
        self._stopped = False
        self._task: asyncio.Task[None] | None = None
        self._disconnected = asyncio.Event()

    @property
    def client_id(self) -> str:
        """The device's Sendspin client id, which is also its Music Assistant player id."""
        return self.identity.peer_id

    @property
    def connected(self) -> bool:
        """Whether the device currently holds a connection to the server."""
        return self._client is not None and self._client.connected

    @property
    def static_pin(self) -> str | None:
        """The fixed PIN this device accepts, or None when it offers no static PIN."""
        return STATIC_PIN if self.scenario.static_pin else None

    async def start(self) -> None:
        """Build the client from the scenario profile and keep it connected."""
        self._stopped = False
        store = await FileClientPairingStore.open(self._storage_path)
        config = await store.get_pairing_config()
        await store.store_pairing_config(
            replace(
                config,
                pairing_psk_enabled=self.scenario.pairing_psk,
                static_pin_enabled=self.scenario.static_pin,
                dynamic_pin_enabled=self.scenario.dynamic_pin,
                unpaired_access_enabled=self.scenario.unpaired_access,
                dynamic_pin_min_length=self.scenario.min_pin_length,
            )
        )
        if self.scenario.static_pin:
            await store.set_static_pin(STATIC_PIN)
        if self.scenario.pairing_psk:
            await _ensure_pairing_psk(store)

        roles = [Roles.PLAYER]
        if self.scenario.source_role:
            roles.append(Roles.SOURCE)
        client = SendspinClient(
            self.identity,
            self.scenario.name,
            roles,
            pairing_store=store,
            device_info=DeviceInfo(
                product_name=self.scenario.product_name,
                manufacturer=DEVICE_MANUFACTURER,
            ),
            player_support=ClientHelloPlayerSupport(
                supported_formats=SUPPORTED_FORMATS,
                buffer_capacity=BUFFER_CAPACITY,
                # No volume/mute: aiosendspin hands those commands to a callback rather than
                # applying them, and reporting the result back needs the connection's own
                # (non-public) state API. Advertising them would leave the control inert.
                supported_commands=[],
            ),
            source_support=(
                ClientHelloSourceSupport(features=ClientHelloSourceFeatures(line_sense=True))
                if self.scenario.source_role
                else None
            ),
            pairing_support=self._pairing_support(),
        )
        if self._stopped:
            # ``stop`` ran while this was still coming up, and it had no task to cancel
            # yet; starting one now would reconnect forever under an identity the next
            # load reuses, and the two would displace each other on every attempt.
            return
        self._client = client
        client.add_disconnect_listener(self._disconnected.set)
        client.add_pairing_abort_listener(self._on_pairing_abort)
        self._task = asyncio.create_task(self._connect_loop())
        self._task.add_done_callback(self._log_task_result)

    async def stop(self) -> None:
        """Disconnect the device and stop its reconnect loop, including one still starting."""
        self._stopped = True
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self._client is not None:
            await self._client.disconnect()
            self._client = None

    def press_pairing_button(self) -> None:
        """Perform the operator gesture that admits one gated pairing attempt."""
        if self._client is not None:
            self._client.open_pairing_window()

    async def reset(self) -> None:
        """Forget everything this device knows about the server and reconnect as new."""
        await self.stop()
        self.dynamic_pin = None
        self.awaiting_button = False
        self.last_abort = None
        self._disconnected.clear()
        await asyncio.to_thread(self._storage_path.unlink, missing_ok=True)
        await self.start()

    def _pairing_support(self) -> PairingSupport:
        """Wire the operator channels the scenario's pairing methods need."""
        return PairingSupport(
            gesture_prompt=self._on_gesture_prompt,
            pin_display=(self._on_pin_display if self.scenario.pin_channel.has_display else None),
            pin_speaker=(self._on_pin_speaker if self.scenario.pin_channel.has_speaker else None),
            offer_static_pin=self.scenario.static_pin,
            secret_locations=self.scenario.secret_locations,
        )

    async def _connect_loop(self) -> None:
        """Keep the device connected, retrying while the Sendspin server is unreachable."""
        assert self._client is not None
        while True:
            self._disconnected.clear()
            try:
                await self._client.connect(self._server_url)
            except (ClientError, OSError, TimeoutError, HandshakeAbortedError) as err:
                LOGGER.debug("%s could not connect: %s", self.scenario.name, err)
                await asyncio.sleep(RECONNECT_INTERVAL)
                continue
            LOGGER.info("%s connected as %s", self.scenario.name, self.client_id)
            await self._disconnected.wait()
            LOGGER.info("%s disconnected", self.scenario.name)
            await asyncio.sleep(RECONNECT_INTERVAL)

    async def _on_gesture_prompt(self, waiting: bool) -> None:
        """Track whether the device is waiting for its pairing button to be pressed."""
        self.awaiting_button = waiting
        if waiting:
            LOGGER.info("%s is waiting for its pairing button", self.scenario.name)

    async def _on_pin_display(self, pin: str | None) -> None:
        """Show (or clear) the derived dynamic PIN on the device's display."""
        self.dynamic_pin = pin
        if pin is not None:
            LOGGER.info("%s displays PIN %s", self.scenario.name, pin)

    async def _on_pin_speaker(self, pin: str | None, *, languages: tuple[str, ...]) -> None:
        """Speak (or stop speaking) the derived dynamic PIN."""
        self.dynamic_pin = pin
        if pin is not None:
            LOGGER.info("%s speaks PIN %s (languages: %s)", self.scenario.name, pin, languages)

    def _log_task_result(self, task: asyncio.Task[None]) -> None:
        """Log a connect loop that ended on an unexpected error rather than a cancellation."""
        if task.cancelled():
            return
        if (err := task.exception()) is not None:
            LOGGER.error("%s stopped: %s", self.scenario.name, err, exc_info=err)

    def _on_pairing_abort(self, reason: PairAbortReason) -> None:
        """Remember why the last pairing attempt was aborted, for the status entry."""
        self.last_abort = reason
        LOGGER.info("%s aborted pairing: %s", self.scenario.name, reason.value)


def _scenario_identity(scenario_id: str) -> Identity:
    """
    Return the fixed identity for a scenario, so its player id survives a restart.

    Derived rather than generated on purpose: these are throwaway devices on loopback and
    a stable client id keeps the player config (and any pairing) attached across reloads.
    """
    seed = hashlib.sha256(f"music-assistant-demo-sendspin-client:{scenario_id}".encode()).digest()
    return Identity.from_private_bytes(seed)


async def _ensure_pairing_psk(store: FileClientPairingStore) -> None:
    """
    Mint the Pairing PSK once, so the device can advertise the token method.

    The token itself is never shown: Music Assistant pairs by token only when enrolling
    its own web player, and never offers it as something an operator can carry out.
    """
    if await store.pairing_psk() is not None:
        return
    psk = generate_psk()
    await store.set_pairing_psk(PairingPsk(psk_id=psk_id_for(psk), psk=psk))
