"""Helpers/utils for the Spotify musicprovider."""

from __future__ import annotations

import asyncio
import os
import platform
import time
from typing import TYPE_CHECKING, Any

from music_assistant_models.errors import LoginFailed

from music_assistant.helpers.process import check_output

if TYPE_CHECKING:
    import aiohttp


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
