"""Fixtures for Zvuk Music provider integration tests."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncGenerator
from typing import Any
from uuid import uuid4

import aiohttp
import pytest
import pytest_asyncio

from music_assistant.mass import MusicAssistant

BASE_URL = "http://localhost:8095"
SETUP_USERNAME = "testadmin"
SETUP_PASSWORD = "testpassword123"


class MATestClient:
    """HTTP client for Music Assistant JSON-RPC API."""

    def __init__(self, token: str) -> None:
        """Initialize with bearer token.

        :param token: Bearer token for API authentication.
        """
        self.token = token

    async def command(self, cmd: str, **kwargs: Any) -> Any:
        """Send a JSON-RPC command to the MA server.

        :param cmd: The API command path (e.g. "music/search").
        :param kwargs: Command arguments.
        """
        payload: dict[str, Any] = {
            "command": cmd,
            "message_id": str(uuid4()),
        }
        if kwargs:
            payload["args"] = kwargs
        async with aiohttp.ClientSession() as session:
            resp = await session.post(
                f"{BASE_URL}/api",
                json=payload,
                headers={"Authorization": f"Bearer {self.token}"},
            )
            if resp.status != 200:
                text = await resp.text()
                msg = f"API error {resp.status}: {text}"
                raise RuntimeError(msg)
            return await resp.json()


@pytest.fixture(scope="session")
def zvuk_token() -> str:
    """Get Zvuk auth token from environment."""
    token = os.environ.get("ZVUK_TEST_TOKEN")
    if not token:
        pytest.skip("ZVUK_TEST_TOKEN not set")
    return token


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def mass(
    tmp_path_factory: pytest.TempPathFactory,
) -> AsyncGenerator[MusicAssistant, None]:
    """Start a Music Assistant server instance.

    :param tmp_path_factory: Pytest factory for session-scoped temp directories.
    """
    base_tmp = tmp_path_factory.mktemp("mass")
    storage_path = base_tmp / "data"
    cache_path = base_tmp / "cache"
    storage_path.mkdir(parents=True)
    cache_path.mkdir(parents=True)

    logging.getLogger("aiosqlite").level = logging.INFO

    mass_instance = MusicAssistant(str(storage_path), str(cache_path))
    await mass_instance.start()

    try:
        yield mass_instance
    finally:
        await mass_instance.stop()


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def ma_token(mass: MusicAssistant) -> str:  # noqa: ARG001
    """Create admin user via /setup and return bearer token.

    :param mass: Running MusicAssistant instance (ensures server is started).
    """
    async with aiohttp.ClientSession() as session:
        resp = await session.post(
            f"{BASE_URL}/setup",
            json={
                "username": SETUP_USERNAME,
                "password": SETUP_PASSWORD,
                "device_name": "integration-test",
            },
        )
        data = await resp.json()
        if not data.get("success"):
            msg = f"Setup failed: {data.get('error', 'unknown')}"
            raise RuntimeError(msg)
        return str(data["token"])


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def api_client(ma_token: str) -> MATestClient:
    """Create an authenticated API test client.

    :param ma_token: Bearer token for admin user.
    """
    return MATestClient(ma_token)


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def zvuk_provider_instance_id(
    mass: MusicAssistant,  # noqa: ARG001
    api_client: MATestClient,
    zvuk_token: str,
) -> str:
    """Add Zvuk provider and wait for it to become available.

    :param mass: Running MusicAssistant instance (ensures server is started).
    :param api_client: Authenticated API client.
    :param zvuk_token: Zvuk authentication token.
    """
    await api_client.command("config/onboard_complete")

    config = await api_client.command(
        "config/providers/save",
        provider_domain="zvuk_music",
        values={"token": zvuk_token, "quality": "high"},
    )
    instance_id: str = config["instance_id"]

    # Wait for provider to become available by polling
    for _ in range(30):
        providers = await api_client.command(
            "config/providers",
            provider_domain="zvuk_music",
        )
        for prov in providers:
            if prov["instance_id"] == instance_id and prov.get("available"):
                return instance_id
        await asyncio.sleep(1)

    msg = f"Zvuk provider {instance_id} did not become available"
    raise TimeoutError(msg)
