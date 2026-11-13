"""
Light targets: where a Hue Lights Sync player streams its light frames to.

A target is an entertainment area on the paired Hue bridge, or a set of Hue lamps on ZHA
that is streamed to through ZHA's Ziggurat server.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import TYPE_CHECKING, ClassVar, Final, Protocol

from hue_entertainment import (
    EntertainmentSession,
    LightChannel,
    LightsBusyError,
    ZigbeeEntertainmentSession,
    ZigbeeLight,
)

from .constants import CONF_CLIENTKEY, CONF_USERNAME
from .zigbee import ZhaUnavailableError, stream_info

if TYPE_CHECKING:
    from hue_entertainment import EntertainmentArea, LightColorCommand
    from hue_entertainment.api import HueEntertainmentAPI

    from .provider import HueEntertainmentProvider
    from .zigbee import ZhaDevice

LOGGER = logging.getLogger(__name__)

_ENTERTAINMENT_STALE_COOLDOWN_S: Final[float] = 1.0
# how often a lost Zigbee stream is restarted in place (e.g. while ZHA restarts)
_ZIGBEE_RECOVERY_INTERVAL_S: Final[float] = 2.0


class TargetUnavailableError(Exception):
    """The target can not stream now, and retrying right away will not change that."""


class LightTarget(Protocol):
    """Where a player streams its light frames to."""

    @property
    def client_id(self) -> str:
        """Return the stable Sendspin client id of the target's player."""

    @property
    def name(self) -> str:
        """Return the display name of the target."""

    @property
    def channels(self) -> list[LightChannel]:
        """Return the light channels to render, with their positions."""

    @property
    def product_name(self) -> str:
        """Return the product name shown for the target's player."""

    @property
    def is_streaming(self) -> bool:
        """Return True while frames reach the lights."""

    async def start(self) -> None:
        """
        Open the stream.

        :raises TargetUnavailableError: When the target can not stream now; any other error
            is worth a retry.
        """

    def send(self, commands: list[LightColorCommand]) -> None:
        """
        Queue a frame of channel colours; non-blocking, never raises, a no-op when not streaming.

        :param commands: Colour commands for the channels to update.
        """

    async def aclose(self) -> None:
        """Stop streaming and restore the lights; never raises and safe to call repeatedly."""


class HueAreaTarget:
    """An entertainment area on the paired Hue bridge, streamed to over DTLS."""

    def __init__(self, provider: HueEntertainmentProvider, area: EntertainmentArea) -> None:
        """
        Initialize the target (no network I/O until :meth:`start`).

        :param provider: The provider holding the bridge connection and credentials.
        :param area: The entertainment area to stream to.
        """
        self._provider = provider
        self._area = area
        self._session: EntertainmentSession | None = None

    @property
    def client_id(self) -> str:
        """Return the stable Sendspin client id of the area's player."""
        return f"hue-{self._area.id.replace('-', '')[:16]}"

    @property
    def name(self) -> str:
        """Return the name of the entertainment area."""
        return self._area.name

    @property
    def channels(self) -> list[LightChannel]:
        """Return the area's light channels."""
        return self._area.channels

    @property
    def product_name(self) -> str:
        """Return the product name shown for the area's player."""
        return "Hue Entertainment Area"

    @property
    def is_streaming(self) -> bool:
        """Return True while the DTLS stream is active."""
        return self._session is not None and self._session.is_streaming

    async def start(self) -> None:
        """
        Activate entertainment mode for the area and open the DTLS stream.

        A start after a failed one reuses the session, until :meth:`aclose`.

        :raises TargetUnavailableError: When the Hue bridge is not configured.
        """
        hue_api = self._provider.hue_api
        if hue_api is None:
            msg = "the Hue bridge is not configured"
            raise TargetUnavailableError(msg)
        if self._session is None:
            # idle_timeout=0: teardown is driven by the Sendspin stream start/end
            # events of the bridge, not by the session's own inactivity monitor. The
            # session stops any other active area on start (the bridge only allows one).
            self._session = EntertainmentSession(
                hue_api.host,
                str(self._provider.get_setup_value(CONF_USERNAME) or ""),
                str(self._provider.get_setup_value(CONF_CLIENTKEY) or ""),
                idle_timeout=0,
            )
            await self._clear_stale_entertainment(hue_api)
        await self._session.start(self._area.id)

    def send(self, commands: list[LightColorCommand]) -> None:
        """
        Queue a frame of channel colours for the active stream.

        :param commands: Colour commands for the channels to update.
        """
        if self._session is not None:
            self._session.send(commands)

    async def aclose(self) -> None:
        """Stop the stream, deactivate entertainment mode and release the session."""
        session, self._session = self._session, None
        if session is not None:
            with suppress(Exception):
                await session.aclose()

    async def _clear_stale_entertainment(self, hue_api: HueEntertainmentAPI) -> None:
        """
        Stop entertainment left active on the bridge from a prior failed handshake.

        :param hue_api: Authenticated Hue REST client for this bridge.
        """
        try:
            status, _rid = await hue_api.get_entertainment_status(self._area.id)
        except Exception:
            return
        if status != "active":
            return
        LOGGER.info(
            "Entertainment area '%s' still active on bridge, clearing before DTLS",
            self._area.name,
        )
        await hue_api.stop_entertainment(self._area.id)
        await asyncio.sleep(_ENTERTAINMENT_STALE_COOLDOWN_S)


class ZigbeeTarget:
    """
    Hue lamps on ZHA, streamed to through ZHA's Ziggurat server.

    Music Assistant streams to one Zigbee target at a time: a start takes the stream over
    from the target that has it, which restores its lamps and stays parked until closed.
    A stream only starts once every closed stream has restored its lamps. While another app
    streams to one of the lamps the target waits, and starts once they are free. A lost
    stream is restarted in place while the target has the stream.
    """

    # the target that has the Zigbee stream, shared by all ZHA instances in the process
    _holder: ClassVar[ZigbeeTarget | None] = None
    # closed sessions still restoring their lamps, of any target: the lamps may overlap
    _restoring: ClassVar[set[asyncio.Task[None]]] = set()

    def __init__(
        self, provider: HueEntertainmentProvider, lights: list[tuple[str, int]], name: str
    ) -> None:
        """
        Initialize the target (no network I/O until :meth:`start`).

        :param provider: The provider the target's player belongs to.
        :param lights: The lamps as (IEEE, zones) in channel order; each zone of a gradient
            lamp gets a channel of its own.
        :param name: Display name of the target's player.
        """
        self._provider = provider
        self._name = name
        self._zones = [(ieee, zone) for ieee, zones in lights for zone in range(zones)]
        last = len(self._zones) - 1
        # the channels on an evenly spaced row from left (-1) to right (1)
        self._channels = [
            LightChannel(
                channel_id=channel_id,
                service_id=ieee,
                name=ieee if zone == 0 else f"{ieee} zone {zone}",
                position=(2 * channel_id / last - 1 if last else 0.0, 0.0, 0.0),
            )
            for channel_id, (ieee, zone) in enumerate(self._zones)
        ]
        self._session: ZigbeeEntertainmentSession | None = None
        self._recovery_task: asyncio.Task[None] | None = None

    @property
    def client_id(self) -> str:
        """Return the stable Sendspin client id of the target's player."""
        return f"hue-zb-{self._provider.instance_id.rsplit('--', 1)[-1]}"

    @property
    def name(self) -> str:
        """Return the display name of the target's player."""
        return self._name

    @property
    def channels(self) -> list[LightChannel]:
        """Return a channel per lamp, or per zone of a gradient lamp."""
        return self._channels

    @property
    def product_name(self) -> str:
        """Return the product name shown for the target's player."""
        return "Hue Lights (ZHA)"

    @property
    def is_streaming(self) -> bool:
        """Return True while this target has the Zigbee stream and it is up."""
        return self._session is not None and self._session.is_streaming

    async def start(self) -> None:
        """
        Take the Zigbee stream over and open it to the lamps.

        Lamps that are no longer in ZHA are skipped. When a newer start takes the stream over
        before this one completes, this returns without streaming. While another app streams
        to one of the lamps, this returns without streaming and the stream starts by itself
        once the lamps are free.

        :raises TargetUnavailableError: When the Home Assistant plugin is not connected, ZHA
            does not run on Ziggurat, or none of the lamps is in ZHA.
        :raises ZigguratError: When Ziggurat can not be reached.
        """
        try:
            url, devices = await stream_info(self._provider.mass)
        except ZhaUnavailableError as err:
            raise TargetUnavailableError(str(err)) from err
        lights = self._zigbee_lights(devices)
        previous, ZigbeeTarget._holder = ZigbeeTarget._holder, self
        if previous is not None and previous is not self:
            LOGGER.info("'%s' takes the Zigbee stream over from '%s'", self._name, previous.name)
            previous._release()
        await self._wait_restored()
        if ZigbeeTarget._holder is not self:
            return
        session = ZigbeeEntertainmentSession(url, lights, idle_timeout=0)
        self._session = session
        try:
            await session.start()
        except LightsBusyError as err:
            if self._session is not session:
                return
            LOGGER.info("Waiting to stream to '%s': %s", self._name, err)
        except Exception:
            if self._session is not session:
                # taken over meanwhile: the newer start closes this session
                return
            self._session = None
            raise
        if self._session is session:
            self._recovery_task = asyncio.create_task(self._recover(session))

    def send(self, commands: list[LightColorCommand]) -> None:
        """
        Queue a frame of channel colours for the active stream.

        :param commands: Colour commands for the channels to update.
        """
        if self._session is not None:
            self._session.send(commands)

    async def aclose(self) -> None:
        """Stop streaming, restore the lamps and free the Zigbee stream."""
        if ZigbeeTarget._holder is self:
            ZigbeeTarget._holder = None
        self._release()
        await self._wait_restored()

    def _release(self) -> None:
        """Stop the recovery and close the session in a task that outlives a cancelled caller."""
        if self._recovery_task is not None:
            self._recovery_task.cancel()
            self._recovery_task = None
        session, self._session = self._session, None
        if session is not None:
            task = asyncio.create_task(session.aclose())
            ZigbeeTarget._restoring.add(task)
            task.add_done_callback(ZigbeeTarget._restoring.discard)

    @staticmethod
    async def _wait_restored() -> None:
        """Wait until every closed session restored its lamps; cancelling the wait leaves them be."""
        if ZigbeeTarget._restoring:
            await asyncio.wait(set(ZigbeeTarget._restoring))

    async def _recover(self, session: ZigbeeEntertainmentSession) -> None:
        """Keep starting a waiting or lost stream in place, until cancelled."""
        while True:
            await asyncio.sleep(_ZIGBEE_RECOVERY_INTERVAL_S)
            if session.is_streaming:
                continue
            try:
                await session.start()
            except Exception as err:
                LOGGER.debug("Zigbee stream of '%s' not up yet: %s", self._name, err)
                continue
            LOGGER.info("Zigbee stream of '%s' is up", self._name)

    def _zigbee_lights(self, devices: dict[str, ZhaDevice]) -> list[ZigbeeLight]:
        """
        Return the channels with their lamp's current network address and name.

        :param devices: The ZHA devices by IEEE.
        :raises TargetUnavailableError: When none of the lamps is in ZHA.
        """
        lights: list[ZigbeeLight] = []
        missing: list[str] = []
        for channel_id, (ieee, zone) in enumerate(self._zones):
            device = devices.get(ieee)
            if device is not None:
                lights.append(ZigbeeLight(channel_id, ieee, device.nwk, device.name, zone))
            elif zone == 0:
                missing.append(ieee)
        if not lights:
            msg = "none of the lamps is in ZHA"
            raise TargetUnavailableError(msg)
        if missing:
            LOGGER.warning(
                "Lamps of '%s' no longer in ZHA, skipped: %s", self._name, ", ".join(missing)
            )
        return lights
