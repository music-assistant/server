"""Spotify music provider support for Music Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from urllib.parse import urlencode

from music_assistant_models.config_entries import ConfigEntry, ConfigValueType
from music_assistant_models.enums import ConfigEntryType
from music_assistant_models.errors import SetupFailedError

from music_assistant.helpers.app_vars import app_var  # type: ignore[attr-defined]
from music_assistant.helpers.auth import AuthenticationHelper

from .constants import (
    CALLBACK_REDIRECT_URL,
    CONF_ACTION_AUTH,
    CONF_ACTION_CLEAR_AUTH,
    CONF_CLIENT_ID,
    CONF_REFRESH_TOKEN,
    SCOPE,
)
from .provider import SpotifyProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant import MusicAssistant
    from music_assistant.models import ProviderInstanceType


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    instance_id: id of an existing provider instance (None if new instance setup).
    action: [optional] action key called from config entries UI.
    values: the (intermediate) raw values for config entries sent with the action.
    """
    # ruff: noqa: ARG001

    if action == CONF_ACTION_AUTH:
        # spotify PKCE auth flow
        # https://developer.spotify.com/documentation/web-api/tutorials/code-pkce-flow
        import pkce

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
        # now get the access token
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

    # handle action clear authentication
    if action == CONF_ACTION_CLEAR_AUTH:
        assert values
        values[CONF_REFRESH_TOKEN] = None

    auth_required = values.get(CONF_REFRESH_TOKEN) in (None, "")

    if auth_required:
        values[CONF_CLIENT_ID] = None
        label_text = (
            "You need to authenticate to Spotify. Click the authenticate button below "
            "to start the authentication process which will open in a new (popup) window, "
            "so make sure to disable any popup blockers.\n\n"
            "Also make sure to perform this action from your local network"
        )
    elif action == CONF_ACTION_AUTH:
        label_text = "Authenticated to Spotify. Press save to complete setup."
    else:
        label_text = "Authenticated to Spotify. No further action required."

    return (
        ConfigEntry(
            key="label_text",
            type=ConfigEntryType.LABEL,
            label=label_text,
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
    )


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    if config.get_value(CONF_REFRESH_TOKEN) in (None, ""):
        msg = "Re-Authentication required"
        raise SetupFailedError(msg)
    return SpotifyProvider(mass, manifest, config)
