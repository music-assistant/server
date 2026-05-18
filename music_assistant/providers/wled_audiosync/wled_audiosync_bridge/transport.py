"""
asyncio UDP transport for WLED V2 audio-sync packets.

One WledV2Transport per WLED Player. Opens an `asyncio` datagram endpoint
lazily, sets socket options appropriate to the destination address kind
(unicast / broadcast / multicast), and exposes a thin `send()` method
that optionally duplicates each frame to match the firmware's
back-to-back retransmission pattern observed in the reference capture.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import struct
from enum import StrEnum
from ipaddress import AddressValueError, IPv4Address
from typing import TYPE_CHECKING, Any, Protocol, cast, runtime_checkable

from .constants import (
    WLED_AUDIOSYNC_DEFAULT_MULTICAST_GROUP,
    WLED_AUDIOSYNC_DEFAULT_PORT,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    OnResetCallback = Callable[[], Coroutine[Any, Any, None]]


@runtime_checkable
class SocketLike(Protocol):
    """Subset of socket.socket / asyncio.trsock.TransportSocket we rely on."""

    def getsockopt(self, level: int, optname: int, buflen: int = ...) -> int: ...
    def getsockname(self) -> tuple[str, int] | tuple[str, ...] | tuple[int, ...]: ...


_LOGGER = logging.getLogger(__name__)

# Default IP TTL for outgoing multicast packets. The reference capture used
# 64, but TTL=1 is the conventional LAN-only choice; we expose it as
# per-player config so users with routed multicast can tune.
DEFAULT_MULTICAST_TTL = 1

# After this many consecutive send() failures we tear the transport down so
# the next send() forces a clean reopen. With our ~43 Hz emit rate that
# corresponds to roughly 7 seconds of failures, long enough to weather a brief
# Wi-Fi hiccup but short enough that a real reboot recovers quickly.
DEFAULT_RESET_AFTER_CONSECUTIVE_ERRORS = 300
# Log at most one "send failed" warning every N seconds while the transport
# is in a sustained-error state. Without this, a powered-off WLED would emit
# ~2,580 warnings per minute at our 43 Hz cadence.
DEFAULT_ERROR_LOG_INTERVAL_S = 30.0


class DestinationKind(StrEnum):
    """How packets are addressed at the IP layer."""

    UNICAST = "unicast"
    BROADCAST = "broadcast"
    MULTICAST = "multicast"


def classify_destination(address: str) -> DestinationKind:
    """
    Classify an IPv4 address as unicast / broadcast / multicast.

    :param address: Dotted-quad IPv4 string, e.g. "192.168.1.42" or "239.0.0.1".
    :return: The matching DestinationKind. Invalid addresses default to UNICAST
        so a misconfigured Player will at least try a sendto and surface the
        OS-level error rather than silently doing nothing.
    """
    if address == "255.255.255.255":
        return DestinationKind.BROADCAST
    try:
        ip = IPv4Address(address)
    except (AddressValueError, ValueError):
        return DestinationKind.UNICAST
    if ip.is_multicast:
        return DestinationKind.MULTICAST
    # Common "subnet broadcast" pattern: last octet is 255 (e.g. 192.168.1.255).
    if int(ip) & 0xFF == 0xFF:
        return DestinationKind.BROADCAST
    return DestinationKind.UNICAST


class _DiscardProtocol(asyncio.DatagramProtocol):
    """A datagram protocol that ignores incoming packets and routes errors."""

    def __init__(self, on_error: Callable[[Exception], None]) -> None:
        """Wire a callback for connection-level errors."""
        self._on_error = on_error

    def error_received(self, exc: Exception) -> None:
        """Forward any transport-level error to the owning Player."""
        self._on_error(exc)


class WledV2Transport:
    """
    Per-Player UDP transport that emits 44-byte WLED V2 audio-sync packets.

    The first call to send() opens the underlying datagram endpoint with the
    appropriate IP socket options for the destination. close() releases it.
    A transport that has been closed can be reopened by calling send() again.
    """

    def __init__(
        self,
        address: str,
        port: int = WLED_AUDIOSYNC_DEFAULT_PORT,
        *,
        duplicate_transmit: bool = True,
        multicast_ttl: int = DEFAULT_MULTICAST_TTL,
        reset_after_consecutive_errors: int = DEFAULT_RESET_AFTER_CONSECUTIVE_ERRORS,
        error_log_interval_s: float = DEFAULT_ERROR_LOG_INTERVAL_S,
        on_reset: OnResetCallback | None = None,
    ) -> None:
        """
        Build a transport for a single WLED destination.

        :param address: IPv4 unicast, broadcast, or multicast destination.
        :param port: UDP port; defaults to the WLED V2 audio-sync port.
        :param duplicate_transmit: Send each packet twice back-to-back
            (mirrors MoonModules firmware capture behaviour).
        :param multicast_ttl: IP_MULTICAST_TTL for multicast destinations only.
        :param reset_after_consecutive_errors: Close the endpoint after this
            many sequential failed sends so the next send forces a reopen.
        :param error_log_interval_s: Minimum seconds between repeat warnings
            while in a sustained-error state.
        :param on_reset: Optional async callback invoked (via
            asyncio.create_task) whenever the transport auto-resets due to a
            sustained-error streak. The player wires this up to a /json/info
            probe so users learn whether the device is genuinely offline.
        """
        self.address = address
        self.port = port
        self.kind = classify_destination(address)
        self._duplicate_transmit = duplicate_transmit
        self._multicast_ttl = multicast_ttl
        self._reset_after = max(1, reset_after_consecutive_errors)
        self._log_interval_s = max(0.0, error_log_interval_s)
        self._on_reset = on_reset
        self._transport: asyncio.DatagramTransport | None = None
        self._open_lock = asyncio.Lock()
        self._last_error: Exception | None = None
        self._consecutive_errors = 0
        self._last_error_log_ts: float = 0.0
        self._packets_sent = 0

    @property
    def is_open(self) -> bool:
        """Return True if the underlying datagram endpoint is alive."""
        return self._transport is not None and not self._transport.is_closing()

    @property
    def duplicate_transmit(self) -> bool:
        """Return whether each send() emits the packet twice."""
        return self._duplicate_transmit

    @property
    def socket(self) -> SocketLike | None:
        """
        Return a socket-like handle for the open endpoint (or None).

        asyncio actually returns an `asyncio.trsock.TransportSocket` wrapper
        rather than a raw `socket.socket`, but it exposes the same `getsockopt`
        / `getsockname` surface and is interchangeable for our purposes.
        """
        if self._transport is None:
            return None
        sock = self._transport.get_extra_info("socket")
        return cast("SocketLike | None", sock)

    @property
    def last_error(self) -> Exception | None:
        """Return the last transport-level error reported by the OS, if any."""
        return self._last_error

    @property
    def consecutive_errors(self) -> int:
        """Return how many sends in a row have failed since the last success."""
        return self._consecutive_errors

    @property
    def packets_sent(self) -> int:
        """Return how many packets have been successfully dispatched."""
        return self._packets_sent

    async def send(self, packet: bytes) -> None:
        """
        Send one V2 packet to the configured destination.

        If duplicate-transmit is enabled, the packet is dispatched twice
        in a row (the firmware does this for receiver-side loss resilience).

        Consecutive sendto() failures are counted; the warning log is
        throttled to once every ``error_log_interval_s``, and after
        ``reset_after_consecutive_errors`` failures the endpoint is closed
        so the next send() forces a clean reopen.

        :param packet: A 44-byte WLED V2 audio-sync payload from encode_v2.
        """
        if not self.is_open:
            await self._open()
        transport = self._transport
        if transport is None:  # _open() failed silently
            self._record_error(exc=self._last_error)
            return
        target = (self.address, self.port)
        try:
            transport.sendto(packet, target)
            if self._duplicate_transmit:
                transport.sendto(packet, target)
        except OSError as exc:
            self._record_error(exc=exc)
            return
        self._record_success()

    def _record_success(self) -> None:
        """Reset error counters after a successful send."""
        if self._consecutive_errors:
            _LOGGER.info(
                "WLED V2 transport for %s:%d recovered after %d failed sends",
                self.address,
                self.port,
                self._consecutive_errors,
            )
        self._consecutive_errors = 0
        self._last_error_log_ts = 0.0
        self._packets_sent += 1

    def _record_error(self, *, exc: Exception | None) -> None:
        """Tally a failure, throttle logging, and reset the endpoint if needed."""
        if exc is not None:
            self._last_error = exc
        self._consecutive_errors += 1
        loop = asyncio.get_running_loop()
        now = loop.time()
        if self._consecutive_errors == 1 or now - self._last_error_log_ts >= self._log_interval_s:
            _LOGGER.warning(
                "WLED V2 sendto(%s:%d) failed (%d consecutive): %s",
                self.address,
                self.port,
                self._consecutive_errors,
                exc or self._last_error,
            )
            self._last_error_log_ts = now
        if self._consecutive_errors >= self._reset_after:
            _LOGGER.warning(
                "WLED V2 transport for %s:%d resetting after %d consecutive errors",
                self.address,
                self.port,
                self._consecutive_errors,
            )
            transport = self._transport
            self._transport = None
            if self._on_reset is not None:
                # Fire-and-forget: the player wires this up to a /json/info
                # probe so we can surface "device offline" to the user.
                # If there's no running loop (e.g. shutdown) just skip.
                with contextlib.suppress(RuntimeError):
                    asyncio.create_task(self._on_reset())
            if transport is not None and not transport.is_closing():
                transport.close()

    async def close(self) -> None:
        """Close the underlying datagram endpoint, if any."""
        transport = self._transport
        self._transport = None
        if transport is not None and not transport.is_closing():
            transport.close()

    async def _open(self) -> None:
        """Open the datagram endpoint and apply per-kind socket options."""
        async with self._open_lock:
            if self.is_open:
                return
            loop = asyncio.get_running_loop()
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setblocking(False)
            try:
                if self.kind is DestinationKind.BROADCAST:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                elif self.kind is DestinationKind.MULTICAST:
                    sock.setsockopt(
                        socket.IPPROTO_IP,
                        socket.IP_MULTICAST_TTL,
                        max(1, int(self._multicast_ttl)),
                    )
                    # Joining the group ourselves (even as a sender) keeps
                    # IGMP-snooping switches refreshed: the kernel reissues
                    # IGMP membership reports periodically so the switch
                    # forwarding table doesn't prune our multicast traffic
                    # during long-running playback. The mreq's INADDR_ANY
                    # interface field defers the choice to the routing table.
                    # Use ``=4s4s`` for a stable 8-byte ip_mreq layout — the
                    # earlier ``4sl`` produced a platform-dependent 12-byte
                    # buffer on 64-bit Linux (long is 8 bytes plus
                    # alignment padding) that the kernel accepted only by
                    # leniency.
                    mreq = struct.pack(
                        "=4s4s",
                        socket.inet_aton(self.address),
                        socket.inet_aton("0.0.0.0"),
                    )
                    try:
                        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
                    except OSError as exc:
                        # Best-effort: some hosts (e.g. inside a network
                        # namespace without a multicast route) refuse the
                        # JOIN. The transport still works for sending, so
                        # don't fail the open — just record the warning.
                        _LOGGER.warning(
                            "Could not join multicast group %s for keepalive: %s",
                            self.address,
                            exc,
                        )
                self._transport, _protocol = await loop.create_datagram_endpoint(
                    lambda: _DiscardProtocol(self._on_error),
                    sock=sock,
                )
            except OSError as exc:
                self._last_error = exc
                sock.close()
                _LOGGER.warning(
                    "Failed to open WLED V2 transport for %s:%d (%s): %s",
                    self.address,
                    self.port,
                    self.kind,
                    exc,
                )

    def _on_error(self, exc: Exception) -> None:
        """Record errors reported by the datagram protocol."""
        self._last_error = exc
        _LOGGER.warning("WLED V2 transport error on %s:%d: %s", self.address, self.port, exc)


__all__ = [
    "DEFAULT_ERROR_LOG_INTERVAL_S",
    "DEFAULT_MULTICAST_TTL",
    "DEFAULT_RESET_AFTER_CONSECUTIVE_ERRORS",
    "WLED_AUDIOSYNC_DEFAULT_MULTICAST_GROUP",
    "DestinationKind",
    "WledV2Transport",
    "classify_destination",
]
