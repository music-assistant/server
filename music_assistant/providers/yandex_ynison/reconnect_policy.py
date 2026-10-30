"""Ynison reconnect policy modelled on the official client's retry manager."""

from __future__ import annotations

import math
from typing import Any

# gRPC statuses the official client retries on its ladder: RESOURCE_EXHAUSTED,
# ABORTED and UNAVAILABLE. Any other reported status is not retried there.
_RETRYABLE_GRPC_CODES = frozenset({8, 10, 14})


def parse_seconds(value: Any) -> float:
    """Return a positive number of seconds from a Ynison error detail, or 0."""
    try:
        seconds = float(value)
    except TypeError, ValueError:
        return 0.0
    return seconds if seconds > 0 and math.isfinite(seconds) else 0.0


def parse_backoff_ladder(value: Any) -> tuple[float, ...]:
    """Return a sorted, de-duplicated ladder in seconds from ``ynison-backoff-millis``."""
    if not isinstance(value, str):
        return ()
    millis = {int(part) for part in value.split(":") if part.strip().isdigit()}
    return tuple(ms / 1000 for ms in sorted(millis))


class ReconnectPolicy:
    """
    Decide how long to wait before each Ynison reconnect attempt.

    Mirrors the official client: a go-away waits exactly the server window, a
    server-supplied ladder replaces the default one, the ladder restarts when
    the error kind changes or a session is healthy, and statuses the official
    client does not retry are not retried on the ladder. Where the official
    client stops until an external trigger, this policy waits ``idle_delay``.
    """

    def __init__(self, default_ladder: tuple[float, ...], idle_delay: float) -> None:
        """
        Create a policy.

        :param default_ladder: Delays in seconds used when the server supplies none.
        :param idle_delay: Delay once the ladder is exhausted or retry is not allowed.
        """
        self._default_ladder = default_ladder
        self._idle_delay = idle_delay
        self._ladder: tuple[float, ...] = ()
        self._index = 0
        self._key: tuple[str, str] | None = None
        self._go_away = 0.0
        self._retryable = True

    def on_server_error(
        self, error_code: str, *, grpc_code: int | None, go_away: float, ladder: Any
    ) -> None:
        """
        Adopt the retry directive of a Ynison error response.

        :param error_code: Ynison error code of the response.
        :param grpc_code: gRPC status code of the response, when present.
        :param go_away: Seconds the server asked the client to stay away, or 0.
        :param ladder: Raw ``ynison-backoff-millis`` value, if supplied.
        """
        self._adopt(("go_away" if go_away else "basic", error_code))
        self._ladder = parse_backoff_ladder(ladder)
        self._go_away = go_away
        self._retryable = bool(go_away) or grpc_code is None or grpc_code in _RETRYABLE_GRPC_CODES

    def on_transport_failure(self) -> None:
        """Record a transport-level failure that carried no server directive."""
        self._adopt(("transport", ""))
        self._ladder = ()
        self._retryable = True

    def on_healthy(self) -> None:
        """Record a session that delivered state; the next failure starts afresh."""
        self._key = None
        self._index = 0
        self._ladder = ()
        self._go_away = 0.0
        self._retryable = True

    @property
    def go_away_pending(self) -> bool:
        """Return whether a go-away window has not been waited for yet."""
        return bool(self._go_away)

    def next_delay(self) -> float:
        """Return the wait in seconds before the next attempt and advance the ladder."""
        if self._go_away:
            delay = self._go_away
            self._go_away = 0.0
            self._index = 0
            return delay
        if not self._retryable:
            return self._idle_delay
        ladder = self._ladder or self._default_ladder
        if self._index >= len(ladder):
            return self._idle_delay
        delay = ladder[self._index]
        self._index += 1
        return delay

    def _adopt(self, key: tuple[str, str]) -> None:
        """Restart the ladder when the error kind changes."""
        if key != self._key:
            self._key = key
            self._index = 0
