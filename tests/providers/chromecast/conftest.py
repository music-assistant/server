"""Shared fixtures for the chromecast provider tests."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator

import pychromecast
import pytest


class FakeSocketClient(threading.Thread):
    """A Cast socket client thread that stays alive, as a real one does right after disconnect()."""

    def __init__(self, released: threading.Event) -> None:
        """Initialize with the event that lets the thread finish."""
        super().__init__(daemon=True)
        self._released = released
        self.is_connected = False
        self.disconnect_called = False

    def run(self) -> None:
        """Stay alive until the test releases the thread."""
        self._released.wait(10)

    def disconnect(self) -> None:
        """Signal the socket thread to stop, as the real client does."""
        self.disconnect_called = True


class FakeChromecast:
    """Cast connection running pychromecast's real disconnect/join over a live socket thread."""

    disconnect = pychromecast.Chromecast.disconnect
    join = pychromecast.Chromecast.join

    def __init__(self, socket_client: FakeSocketClient) -> None:
        """Initialize with the socket client this connection runs on."""
        self.socket_client = socket_client

    def wait(self, timeout: float | None = None) -> None:
        """Stand in for the blocking wait on the device's first status."""


@pytest.fixture(name="live_chromecast_factory")
def live_chromecast_factory_fixture() -> Iterator[Callable[[], FakeChromecast]]:
    """Yield a factory for Cast connections whose socket thread is running."""
    released = threading.Event()
    socket_clients: list[FakeSocketClient] = []

    def _create() -> FakeChromecast:
        socket_client = FakeSocketClient(released)
        socket_client.start()
        socket_clients.append(socket_client)
        return FakeChromecast(socket_client)

    try:
        yield _create
    finally:
        released.set()
        for socket_client in socket_clients:
            socket_client.join(5)
