"""WireMock testcontainers fixture for provider HTTP API replay tests."""

from __future__ import annotations

from collections.abc import Generator

import pytest
from testcontainers.wiremock import WireMockContainer


@pytest.fixture(scope="session")
def wiremock() -> Generator[WireMockContainer]:
    """Start a WireMock container for the test session.

    The container is started once and shared across all tests in the session.
    Use wiremock.base_url to get the base URL for HTTP requests.
    """
    with WireMockContainer(verify_ssl_certs=False) as wm:
        yield wm
