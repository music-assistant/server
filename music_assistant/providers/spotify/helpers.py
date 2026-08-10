"""Helpers/utils for the Spotify musicprovider."""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import tempfile
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlencode, urlparse

import pkce
from aiohttp import web
from music_assistant_models.errors import InvalidDataError, LoginFailed

from music_assistant.helpers.auth import AuthenticationHelper
from music_assistant.helpers.json import json_loads
from music_assistant.helpers.process import AsyncProcess, check_output

from .constants import (
    CALLBACK_REDIRECT_URL,
    CHECK_AUTH_TIMEOUT,
    CREDENTIALS_FILE,
    KEYMASTER_CLIENT_ID,
    LIBRESPOT_REDIRECT_URI,
    LIBRESPOT_SCOPE,
    SCOPE,
)

if TYPE_CHECKING:
    import aiohttp

    from music_assistant import MusicAssistant

LOGGER = logging.getLogger(__name__)

LOOPBACK_RESPONSE_HTML = """
<html>
<body onload="window.close();">
    Playback approved, you may now close this window and return to Music Assistant.
</body>
</html>
"""


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


async def librespot_credentials_via_pairing(
    librespot_bin: str, device_name: str, timeout: float
) -> str:
    """
    Advertise a Spotify Connect device and return the credential librespot stores once paired.

    :param librespot_bin: Path to the librespot binary.
    :param device_name: Device name to advertise to the Spotify app.
    :param timeout: Seconds to wait for the user to select the device.
    :raises TimeoutError: When the device was not selected within the timeout.
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
            # containers; without its log the user would just watch the action time out
            librespot_proc.attach_stderr_reader(
                asyncio.create_task(_log_pairing_output(librespot_proc))
            )
            # bound the wait inside the context manager so the daemon is always reaped
            async with asyncio.timeout(timeout):
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
        if not os.path.exists(credentials_file):
            raise LoginFailed("Librespot did not store a playback credential")
        return await asyncio.to_thread(_read_credentials_file, credentials_file)


async def await_loopback_authorization(port: int, path: str, timeout: float) -> dict[str, str]:
    """
    Serve the loopback redirect target and return the OAuth params the browser arrives with.

    Only reachable when the browser runs on the same host as Music Assistant; callers are
    expected to offer a manual fallback for everyone else.

    :param port: Loopback port to listen on.
    :param path: Request path the redirect URI points at.
    :param timeout: Seconds to wait for the browser to arrive.
    :raises OSError: When the port cannot be bound.
    :raises TimeoutError: When no browser arrived within the timeout.
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
        async with asyncio.timeout(timeout):
            return await received
    finally:
        await runner.cleanup()


def keymaster_authorize_url(code_challenge: str) -> str:
    """
    Return the Spotify authorize URL that grants a credential librespot can use for playback.

    :param code_challenge: PKCE code challenge derived from the verifier kept for the exchange.
    """
    params = {
        "response_type": "code",
        "client_id": KEYMASTER_CLIENT_ID,
        "scope": " ".join(LIBRESPOT_SCOPE),
        "code_challenge_method": "S256",
        "code_challenge": code_challenge,
        "redirect_uri": LIBRESPOT_REDIRECT_URI,
    }
    return f"https://accounts.spotify.com/authorize?{urlencode(params)}"


async def keymaster_access_token(
    http_session: aiohttp.ClientSession, code: str, code_verifier: str
) -> str:
    """
    Redeem an authorization code from the playback sign-in for an access token.

    :param http_session: aiohttp client session.
    :param code: Authorization code returned by Spotify.
    :param code_verifier: PKCE code verifier belonging to the authorize request.
    :raises LoginFailed: When Spotify refuses the code.
    """
    token_params = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": LIBRESPOT_REDIRECT_URI,
        "client_id": KEYMASTER_CLIENT_ID,
        "code_verifier": code_verifier,
    }
    async with http_session.post(
        "https://accounts.spotify.com/api/token", data=token_params
    ) as response:
        if response.status != 200:
            raise LoginFailed(f"Failed to get playback access token: {await response.text()}")
        token_result: dict[str, Any] = await response.json()
    if not (access_token := token_result.get("access_token")):
        raise LoginFailed("Spotify returned no playback access token")
    return str(access_token)


def authorization_code_from_params(params: dict[str, str]) -> str:
    """
    Return the authorization code from the query params Spotify redirected with.

    :param params: Query params as received on the redirect.
    :raises InvalidDataError: When the params carry no usable authorization code.
    """
    if code := params.get("code"):
        return code
    error = params.get("error") or "no authorization code was returned"
    raise InvalidDataError(f"Playback authorization failed: {error}")


def authorization_code_from_url(url: str) -> str:
    """
    Return the authorization code from a redirect URL the user pasted back into the config.

    Spotify only accepts a loopback redirect for the playback client id, so the browser cannot
    reach Music Assistant and the user copies the URL it landed on instead.

    :param url: The full redirect URL, as pasted by the user.
    :raises InvalidDataError: When the URL carries no usable authorization code.
    """
    query = parse_qs(urlparse(url.strip()).query)
    return authorization_code_from_params(
        {key: values[0] for key, values in query.items() if values}
    )


async def get_spotify_token(
    http_session: aiohttp.ClientSession,
    client_id: str,
    refresh_token: str,
    session_name: str = "spotify",
) -> dict[str, Any]:
    """Refresh Spotify access token using refresh token.

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
                    raise LoginFailed(f"Refresh token no longer valid for {session_name}: {err}")
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


async def pkce_auth_flow(
    mass: MusicAssistant,
    session_id: str,
    client_id: str,
) -> str:
    """Perform Spotify PKCE auth flow and return refresh token.

    :param mass: MusicAssistant instance.
    :param session_id: Session ID for the authentication helper.
    :param client_id: The client ID to use for authentication.
    :return: Refresh token string.
    """
    # spotify PKCE auth flow
    # https://developer.spotify.com/documentation/web-api/tutorials/code-pkce-flow
    code_verifier, code_challenge = pkce.generate_pkce_pair()
    async with AuthenticationHelper(mass, session_id) as auth_helper:
        params = {
            "response_type": "code",
            "client_id": client_id,
            "scope": " ".join(SCOPE),
            "code_challenge_method": "S256",
            "code_challenge": code_challenge,
            "redirect_uri": CALLBACK_REDIRECT_URL,
            "state": auth_helper.callback_url,
        }
        query_string = urlencode(params)
        url = f"https://accounts.spotify.com/authorize?{query_string}"
        result = await auth_helper.authenticate(url)
        authorization_code = result["code"]

    # now get the access token
    token_params = {
        "grant_type": "authorization_code",
        "code": authorization_code,
        "redirect_uri": CALLBACK_REDIRECT_URL,
        "client_id": client_id,
        "code_verifier": code_verifier,
    }
    async with mass.http_session.post(
        "https://accounts.spotify.com/api/token", data=token_params
    ) as response:
        if response.status != 200:
            error_text = await response.text()
            raise LoginFailed(f"Failed to get access token: {error_text}")
        token_result = await response.json()

    return str(token_result["refresh_token"])


async def _log_pairing_output(librespot_proc: AsyncProcess) -> None:
    """Log the pairing daemon's output so a failure to advertise is diagnosable."""
    async for line in librespot_proc.iter_stderr():
        if "ERROR" in line or "WARN" in line:
            LOGGER.warning("[librespot-pairing] %s", line)
        else:
            LOGGER.debug("[librespot-pairing] %s", line)


async def _await_credentials_file(cache_dir: str) -> str:
    """Poll librespot's cache directory until it holds a complete credential file."""
    credentials_file = os.path.join(cache_dir, CREDENTIALS_FILE)
    while True:
        if os.path.exists(credentials_file):
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
