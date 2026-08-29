"""Helpers/utils for the Spotify musicprovider."""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import re
import tempfile
import time
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import web
from music_assistant_models.errors import LoginFailed

from music_assistant.helpers.json import json_loads
from music_assistant.helpers.process import AsyncProcess, check_output
from music_assistant.providers.spotify_connect.soloist import SoloistBinaryManager

from .constants import (
    CHECK_AUTH_TIMEOUT,
    CREDENTIALS_FILE,
    PAIRING_DEVICE_NAME,
    SOLOIST_USER_DIR_SUFFIX,
)

# how long the pairing daemon's log reader is given to drain after it exits
PAIR_LOG_DRAIN_TIMEOUT = 2.0

LOGGER = logging.getLogger(__name__)
PAIRING_LOG_TIMESTAMP = re.compile(r"^\[\d{4}-\d{2}-\d{2}T[^ ]+ ")

LOOPBACK_RESPONSE_HTML = """
<html>
<body onload="window.close();">
    Playback approved, you may now close this window and return to Music Assistant.
</body>
</html>
"""

if TYPE_CHECKING:
    import aiohttp

    from music_assistant.mass import MusicAssistant


async def get_librespot_binary() -> str:
    """Find the correct librespot binary belonging to the platform."""

    async def check_librespot(librespot_path: str) -> str | None:
        try:
            returncode, output = await check_output(librespot_path, "--version")
            if returncode == 0 and b"librespot" in output:
                return librespot_path
            return None
        except OSError:
            return None

    base_path = os.path.join(os.path.dirname(__file__), "bin")
    system = platform.system().lower().replace("darwin", "macos")
    architecture = platform.machine().lower()

    if librespot_binary := await check_librespot(
        os.path.join(base_path, f"librespot-{system}-{architecture}")
    ):
        return librespot_binary

    msg = f"Unable to locate Librespot for {system}/{architecture}"
    raise RuntimeError(msg)


async def librespot_credentials_via_pairing(librespot_bin: str, device_name: str) -> str:
    """
    Advertise a Spotify Connect device and return the credential librespot stores once paired.

    Blocks until the user selects the device in the official Spotify app; the caller is expected
    to bound the wait (the setup flow's step deadline cancels it).

    :param librespot_bin: Path to the librespot binary.
    :param device_name: Device name to advertise to the Spotify app.
    """
    with tempfile.TemporaryDirectory() as cache_dir:
        args = [
            librespot_bin,
            "--cache",
            cache_dir,
            "--disable-audio-cache",
            "--backend",
            "pipe",
            "--name",
            device_name,
        ]
        # stdout carries decoded audio once the user hits play; discard it so the pairing
        # daemon never blocks on a pipe nobody reads
        async with AsyncProcess(
            args, stdout=asyncio.subprocess.DEVNULL, stderr=True, name="librespot-pairing"
        ) as librespot_proc:
            # librespot advertises over mDNS, which fails silently in host-network-less
            # containers; without its log the user would just watch the step time out
            librespot_proc.attach_stderr_reader(
                asyncio.create_task(_log_pairing_output(librespot_proc))
            )
            return await _await_credentials_file(cache_dir)


async def librespot_credentials_via_token(librespot_bin: str, access_token: str) -> str:
    """
    Exchange a keymaster access token for librespot's reusable stored credential.

    :param librespot_bin: Path to the librespot binary.
    :param access_token: Spotify access token minted with the keymaster client id.
    :raises LoginFailed: When librespot could not turn the token into a stored credential.
    """
    with tempfile.TemporaryDirectory() as cache_dir:
        returncode, output = await check_output(
            librespot_bin,
            "--cache",
            cache_dir,
            "--check-auth",
            "--access-token",
            access_token,
            timeout=CHECK_AUTH_TIMEOUT,
        )
        if returncode != 0:
            raise LoginFailed(
                f"Librespot rejected the playback authorization: {output.decode().strip()}"
            )
        credentials_file = os.path.join(cache_dir, CREDENTIALS_FILE)
        if not Path(credentials_file).exists():
            raise LoginFailed("Librespot did not store a playback credential")
        return await asyncio.to_thread(_read_credentials_file, credentials_file)


async def pair_soloist_session(mass: MusicAssistant, api_key: str, data_dir: Path) -> None:
    """
    Pair a Spotify account with soloist and store the session in the given data dir.

    Advertises a Spotify Connect device and blocks until the user selects it in the
    official Spotify app; the caller is expected to bound the wait (the setup flow's
    step deadline cancels it).

    :param mass: The MusicAssistant instance.
    :param api_key: The user's personal Soloist API key (secret, kept out of all logs).
    :param data_dir: Directory the paired session is stored in.
    :raises LoginFailed: When pairing did not complete with a stored session.
    """
    binary = await SoloistBinaryManager(mass).ensure_fresh(consent=True)

    def _prepare() -> None:
        data_dir.mkdir(parents=True, exist_ok=True)
        # the paired session holds the Spotify device identity and login session
        data_dir.chmod(0o700)

    await asyncio.to_thread(_prepare)
    with tempfile.TemporaryDirectory() as cache_dir:
        args = [
            str(binary),
            "--pair",
            "--device-name",
            PAIRING_DEVICE_NAME,
            "--api-key",
            api_key,
            "--data-dir",
            str(data_dir),
            "--cache-dir",
            cache_dir,
        ]
        # the daemon writes all of its logging to stdout and only ever puts
        # argument-parsing complaints on stderr, so the two are merged into one
        # captured stream. Capturing is also what makes the redaction below
        # reachable: an unset stdout is inherited, which would leak the daemon's
        # output - argv included - straight to the server console.
        async with AsyncProcess(
            args,
            stdout=True,
            stderr=asyncio.subprocess.STDOUT,
            name="soloist-pair",
        ) as pair_proc:
            log_task = asyncio.create_task(_log_soloist_pairing_output(pair_proc, api_key))
            try:
                # watched together: nothing else drains the daemon's stdout, so a
                # reader that died would leave it blocked on a full pipe until the
                # setup step expires
                wait_task = asyncio.ensure_future(pair_proc.wait())
                await asyncio.wait({wait_task, log_task}, return_when=asyncio.FIRST_COMPLETED)
                if log_task.done() and not log_task.cancelled() and log_task.exception():
                    wait_task.cancel()
                    raise LoginFailed(
                        "Soloist pairing could not be monitored"
                    ) from log_task.exception()
                returncode = await wait_task
                # an exited daemon still has its last (and most telling) lines in
                # the stream buffer; the shield keeps the reader alive across the
                # timeout so a pairing failure stays diagnosable
                with suppress(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(log_task), PAIR_LOG_DRAIN_TIMEOUT)
            finally:
                log_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await log_task
    if returncode != 0:
        raise LoginFailed(f"Soloist pairing failed (exit code {returncode})")
    if not await asyncio.to_thread(soloist_session_present, data_dir):
        raise LoginFailed("Soloist did not store a paired session")


def soloist_session_account(data_dir: Path) -> str | None:
    """
    Return the Spotify username a stored soloist session belongs to (blocking).

    Answers None when it cannot be told apart: no session yet, or state for more
    than one account.

    :param data_dir: The soloist data directory to inspect.
    """
    accounts = _soloist_session_accounts(data_dir)
    return accounts[0] if len(accounts) == 1 else None


def soloist_session_present(data_dir: Path) -> bool:
    """
    Return whether a soloist data dir holds a stored (paired) session (blocking).

    :param data_dir: The soloist data directory to inspect.
    """
    return bool(_soloist_session_accounts(data_dir))


async def await_loopback_authorization(port: int, path: str) -> dict[str, str]:
    """
    Serve the loopback redirect target and return the OAuth params the browser arrives with.

    Only reachable when the browser runs on the same host as Music Assistant; callers are
    expected to offer a manual fallback for everyone else.

    :param port: Loopback port to listen on.
    :param path: Request path the redirect URI points at.
    :raises OSError: When the port cannot be bound.
    """
    received: asyncio.Future[dict[str, str]] = asyncio.get_running_loop().create_future()

    async def handle(request: web.Request) -> web.Response:
        if not received.done():
            received.set_result(dict(request.query))
        return web.Response(text=LOOPBACK_RESPONSE_HTML, content_type="text/html")

    app = web.Application()
    app.router.add_get(path, handle)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        await web.TCPSite(runner, "127.0.0.1", port).start()
        return await received
    finally:
        await runner.cleanup()


async def get_spotify_token(
    http_session: aiohttp.ClientSession,
    client_id: str,
    refresh_token: str,
    session_name: str = "spotify",
) -> dict[str, Any]:
    """
    Refresh Spotify access token using refresh token.

    :param http_session: aiohttp client session.
    :param client_id: Spotify client ID.
    :param refresh_token: Spotify refresh token.
    :param session_name: Name for logging purposes.
    :return: Auth info dict with access_token, refresh_token, expires_at.
    :raises LoginFailed: If token refresh fails.
    """
    params = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }
    err = "Unknown error"
    for _ in range(2):
        async with http_session.post(
            "https://accounts.spotify.com/api/token", data=params
        ) as response:
            if response.status != 200:
                err = await response.text()
                # invalid_grant means the refresh token is revoked or expired (Spotify
                # enforces a 6-month lifetime); retrying won't recover it, so fail now and
                # let the caller clear the stored token and prompt re-authentication.
                if "invalid_grant" in err or "revoked" in err:
                    raise LoginFailed(
                        f"Refresh token no longer valid for {session_name}: {err}",
                        translation_key="refresh_token_invalid",
                        translation_owner="provider.spotify",
                    )
                # the token failed to refresh, we allow one retry
                await asyncio.sleep(2)
                continue
            # if we reached this point, the token has been successfully refreshed
            auth_info: dict[str, Any] = await response.json()
            auth_info["expires_at"] = int(auth_info["expires_in"] + time.time())
            # Spotify only returns a refresh_token when it rotates one; when the response
            # omits it, keep using the existing token (per Spotify's refresh-token docs).
            auth_info.setdefault("refresh_token", refresh_token)
            return auth_info

    raise LoginFailed(f"Failed to refresh {session_name} access token: {err}")


async def _log_soloist_pairing_output(pair_proc: AsyncProcess, api_key: str) -> None:
    """Log the pairing daemon's output (API key redacted) so failures are diagnosable."""
    async for line in pair_proc.iter_stdout():
        # the third-party binary's own output may echo argv (which carries the
        # api key), so redact it before logging
        text = line.replace(api_key, "<redacted>") if api_key else line
        LOGGER.debug("[soloist-pair] %s", text)


async def _log_pairing_output(librespot_proc: AsyncProcess) -> None:
    """Log the pairing daemon's output so a failure to advertise is diagnosable."""
    reported_warnings: set[str] = set()
    async for line in librespot_proc.iter_stderr():
        warning_key = PAIRING_LOG_TIMESTAMP.sub("[", line, count=1)
        if ("ERROR" in line or "WARN" in line) and warning_key not in reported_warnings:
            reported_warnings.add(warning_key)
            LOGGER.warning("[librespot-pairing] %s", line)
        else:
            LOGGER.debug("[librespot-pairing] %s", line)


async def _await_credentials_file(cache_dir: str) -> str:
    """Poll librespot's cache directory until it holds a complete credential file."""
    credentials_file = os.path.join(cache_dir, CREDENTIALS_FILE)
    while True:
        if Path(credentials_file).exists():
            try:
                return await asyncio.to_thread(_read_credentials_file, credentials_file)
            except OSError, ValueError:
                # the file was caught mid-write; fall through and retry
                pass
        await asyncio.sleep(1)


def _read_credentials_file(credentials_file: str) -> str:
    """Read and validate librespot's credential file, returning its raw contents."""
    with open(credentials_file, encoding="utf-8") as fileobj:
        contents = fileobj.read()
    if not json_loads(contents).get("auth_data"):
        msg = "Incomplete librespot credential file"
        raise ValueError(msg)
    return contents


def _soloist_session_accounts(data_dir: Path) -> list[str]:
    """Return the Spotify accounts a soloist data dir holds paired state for (blocking)."""
    # The per-account state under settings/Users/<username>-user is the only thing
    # a pairing leaves behind: the prefs stores rewritten before every spawn, the
    # pid and lock files and the WebSocket endpoint all outlive one, so a data
    # directory that ever ran a daemon never looks empty again.
    users_dir = data_dir / "settings" / "Users"
    try:
        return [
            entry.name.removesuffix(SOLOIST_USER_DIR_SUFFIX)
            for entry in users_dir.iterdir()
            if entry.is_dir() and entry.name.endswith(SOLOIST_USER_DIR_SUFFIX)
        ]
    except OSError:
        return []
