"""Spotify musicprovider support for MusicAssistant with Podcast Support -Modular Implementation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlencode

import pkce
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType
from music_assistant_models.errors import SetupFailedError

from music_assistant.helpers.app_vars import app_var  # type: ignore[attr-defined]
from music_assistant.helpers.auth import AuthenticationHelper

from .constants import (
    CALLBACK_REDIRECT_URL,
    CONF_ACTION_AUTH,
    CONF_ACTION_CLEAR_AUTH,
    CONF_CLIENT_ID,
    CONF_ENABLE_PODCASTS,
    CONF_PLAYED_THRESHOLD,
    CONF_REFRESH_TOKEN,
    CONF_SYNC_PLAYED_STATUS,
    SCOPE,
)
from .provider import SpotifyProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant import MusicAssistant


async def handle_auth_action(mass: MusicAssistant, values: dict[str, Any]) -> None:
    """Handle the authentication action."""
    code_verifier, code_challenge = pkce.generate_pkce_pair()
    async with AuthenticationHelper(mass, cast("str", values["session_id"])) as auth_helper:
        params = {
            "response_type": "code",
            "client_id": values.get(CONF_CLIENT_ID) or app_var(2),
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

    # Get access token
    params = {
        "grant_type": "authorization_code",
        "code": authorization_code,
        "redirect_uri": CALLBACK_REDIRECT_URL,
        "client_id": values.get(CONF_CLIENT_ID) or app_var(2),
        "code_verifier": code_verifier,
    }
    async with mass.http_session.post(
        "https://accounts.spotify.com/api/token", data=params
    ) as response:
        result = await response.json()
        values[CONF_REFRESH_TOKEN] = result["refresh_token"]


def get_auth_label_text(auth_required: bool, action: str) -> str:
    """Get appropriate label text based on auth status."""
    if auth_required:
        return (
            "You need to authenticate to Spotify. Click the authenticate button below "
            "to start the authentication process which will open in a new (popup) window, "
            "so make sure to disable any popup blockers.\n\n"
            "Also make sure to perform this action from your local network"
        )
    elif action == CONF_ACTION_AUTH:
        return "Authenticated to Spotify. Press save to complete setup."
    else:
        return "Authenticated to Spotify. No further action required."


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,
    values: dict[str, Any] | None = None,
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    # Ensure values is not None
    if values is None:
        values = {}

    # Handle authentication action
    if action == CONF_ACTION_AUTH:
        await handle_auth_action(mass, values)
    elif action == CONF_ACTION_CLEAR_AUTH:
        values[CONF_REFRESH_TOKEN] = None

    auth_required = values.get(CONF_REFRESH_TOKEN) in (None, "")
    label_text = get_auth_label_text(auth_required, action or "")

    return (
        ConfigEntry(
            key="label_text",
            type=ConfigEntryType.LABEL,
            label=label_text,
        ),
        ConfigEntry(
            key=CONF_ACTION_CLEAR_AUTH,
            type=ConfigEntryType.ACTION,
            label="Clear authentication",
            description="Clear the current authentication details.",
            action=CONF_ACTION_CLEAR_AUTH,
            action_label="Clear authentication",
            required=False,
            hidden=auth_required,
        ),
        ConfigEntry(
            key=CONF_ENABLE_PODCASTS,
            type=ConfigEntryType.BOOLEAN,
            label="Enable Podcast Support",
            description="Enable support for Spotify podcasts and episodes. "
            "This will include podcasts in search results and library.",
            default_value=True,
            value=values.get(CONF_ENABLE_PODCASTS, True) if values else True,
        ),
        ConfigEntry(
            key=CONF_SYNC_PLAYED_STATUS,
            type=ConfigEntryType.BOOLEAN,
            label="Sync Played Status from Spotify",
            description="Automatically sync episode played status from Spotify to Music Assistant. "
            "Episodes marked as played in Spotify will be marked as played in MA.",
            default_value=True,
            value=values.get(CONF_SYNC_PLAYED_STATUS, True) if values else True,
        ),
        ConfigEntry(
            key=CONF_PLAYED_THRESHOLD,
            type=ConfigEntryType.INTEGER,
            label="Played Threshold (%)",
            description="Percentage of episode completion to consider it 'played' "
            "when not explicitly marked by Spotify (50 = 50%, 90 = 90%).",
            default_value=90,
            value=values.get(CONF_PLAYED_THRESHOLD, 90) if values else 90,
            range=(1, 100),
        ),
        ConfigEntry(
            key=CONF_REFRESH_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            label=CONF_REFRESH_TOKEN,
            hidden=True,
            required=True,
            value=values.get(CONF_REFRESH_TOKEN) if values else None,
        ),
        ConfigEntry(
            key=CONF_CLIENT_ID,
            type=ConfigEntryType.SECURE_STRING,
            label="Client ID (optional)",
            description="By default, a generic client ID is used which is (heavy) rate limited. "
            "To speedup performance, it is advised that you create your own Spotify Developer "
            "account and use that client ID here, but this comes at the cost of some features "
            "due to Spotify policies. For example Radio mode/recommendations and featured playlists"
            "will not work with a custom client ID. \n\n"
            f"Use {CALLBACK_REDIRECT_URL} as callback URL.",
            required=False,
            value=values.get(CONF_CLIENT_ID) if values else None,
            hidden=not auth_required,
        ),
        ConfigEntry(
            key=CONF_ACTION_AUTH,
            type=ConfigEntryType.ACTION,
            label="Authenticate with Spotify",
            description="This button will redirect you to Spotify to authenticate.",
            action=CONF_ACTION_AUTH,
            hidden=not auth_required,
        ),
    )


async def setup(
    mass: MusicAssistant,
    manifest: ProviderManifest,
    config: ProviderConfig,
) -> SpotifyProvider:
    """Initialize provider(instance) with given configuration."""
    if config.get_value(CONF_REFRESH_TOKEN) in (None, ""):
        msg = "Re-Authentication required"
        raise SetupFailedError(msg)
    return SpotifyProvider(mass, manifest, config)
