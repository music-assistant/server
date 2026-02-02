"""Optional e2e tests: Music Assistant server in a separate process, tests via HTTP.

Requires port 8095 to be free (no other Music Assistant instance running).
Skip with: pytest -k "not e2e" to run only in-process tests.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from collections.abc import Generator
from pathlib import Path

import aiohttp
import pytest

E2E_SERVER_PORT = 8095
E2E_READINESS_TIMEOUT = 45
E2E_POLL_INTERVAL = 0.5
E2E_SHUTDOWN_TIMEOUT = 10


async def _wait_for_server(port: int, timeout: float) -> bool:
    """Poll GET /info until 200 or timeout."""
    url = f"http://127.0.0.1:{port}/info"
    deadline = time.monotonic() + timeout
    async with aiohttp.ClientSession() as session:
        while time.monotonic() < deadline:
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=2)) as resp:
                    if resp.status == 200:
                        return True
            except (TimeoutError, aiohttp.ClientError, OSError):
                pass
            await asyncio.sleep(E2E_POLL_INTERVAL)
    return False


@pytest.fixture(scope="module")
def e2e_server(
    tmp_path_factory: pytest.TempPathFactory,
) -> Generator[subprocess.Popen[bytes], None, None]:
    """Start Music Assistant in a subprocess; yield when ready; terminate on teardown."""
    data_dir = tmp_path_factory.mktemp("ma_data")
    cache_dir = tmp_path_factory.mktemp("ma_cache")
    data_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["PYTHONDEVMODE"] = "0"

    proc = subprocess.Popen(  # noqa: S603 (trusted test helper)
        [
            sys.executable,
            "-m",
            "music_assistant",
            "--data-dir",
            str(data_dir),
            "--cache-dir",
            str(cache_dir),
            "--log-level",
            "debug",
        ],
        cwd=str(Path(__file__).resolve().parents[4]),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    try:
        ready = asyncio.run(_wait_for_server(E2E_SERVER_PORT, E2E_READINESS_TIMEOUT))
        if not ready:
            proc.terminate()
            proc.wait(timeout=E2E_SHUTDOWN_TIMEOUT)
            pytest.skip(
                f"Server did not become ready on port {E2E_SERVER_PORT} within "
                f"{E2E_READINESS_TIMEOUT}s. Ensure port 8095 is free."
            )
        yield proc
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=E2E_SHUTDOWN_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_e2e_server_info(
    e2e_server: subprocess.Popen[bytes],  # noqa: ARG001 (fixture ensures server is up)
) -> None:
    """E2E: GET /info returns server info when server runs in separate process."""
    url = f"http://127.0.0.1:{E2E_SERVER_PORT}/info"
    async with aiohttp.ClientSession() as session, session.get(url) as resp:
        assert resp.status == 200
        data = await resp.json()
        assert "schema_version" in data or "server_version" in data
