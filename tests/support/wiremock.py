"""WireMock testcontainers fixture for provider HTTP API replay tests."""

from __future__ import annotations

from collections.abc import Generator

import pytest
from testcontainers.core.container import DockerContainer
from testcontainers.core.waiting_utils import wait_for_logs

__all__ = ["WireMockContainer"]

WIREMOCK_IMAGE = "wiremock/wiremock:3.9.2"
WIREMOCK_PORT = 8080


class WireMockContainer(DockerContainer):  # type: ignore[misc]
    """Minimal WireMock container built on top of DockerContainer.

    WireMock is an HTTP mock server with a REST admin API for registering
    response stubs and verifying received requests.
    """

    def __init__(self) -> None:
        """Initialize the WireMock container."""
        super().__init__(WIREMOCK_IMAGE)
        self.with_exposed_ports(WIREMOCK_PORT)

    def start(self) -> WireMockContainer:
        """Start the container and wait for WireMock to be ready.

        WireMock 3.x does not emit a "Started WireMock" log line — it emits
        configuration output instead. Wait for the extensions line which appears
        at the end of startup output.
        """
        super().start()
        # WireMock 3.x startup ends with the extensions line
        wait_for_logs(self, "extensions:")
        return self

    def get_base_url(self) -> str:
        """Return the base HTTP URL for the WireMock server."""
        host = self.get_container_host_ip()
        port = self.get_exposed_port(WIREMOCK_PORT)
        return f"http://{host}:{port}"


@pytest.fixture(scope="session")
def wiremock() -> Generator[WireMockContainer]:
    """Start a WireMock container for the test session.

    The container is started once and shared across all tests in the session.
    Use wiremock.get_base_url() to get the base URL for HTTP requests.
    """
    with WireMockContainer() as wm:
        yield wm
