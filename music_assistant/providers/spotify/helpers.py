"""Helpers/utils for the Spotify musicprovider."""

from __future__ import annotations

import asyncio
import os
import platform
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

import pkce
from music_assistant_models.errors import LoginFailed

from music_assistant.helpers.auth import AuthenticationHelper
from music_assistant.helpers.process import check_output

from .constants import CALLBACK_REDIRECT_URL, SCOPE

if TYPE_CHECKING:
    import aiohttp

    from music_assistant import MusicAssistant


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
                if "revoked" in err:
                    raise LoginFailed(f"Token revoked for {session_name}: {err}")
                # the token failed to refresh, we allow one retry
                await asyncio.sleep(2)
                continue
            # if we reached this point, the token has been successfully refreshed
            auth_info: dict[str, Any] = await response.json()
            auth_info["expires_at"] = int(auth_info["expires_in"] + time.time())
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
