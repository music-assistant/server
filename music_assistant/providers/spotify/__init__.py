"""Spotify music provider support for Music Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from urllib.parse import urlencode

import pkce
from music_assistant_models.config_entries import ConfigEntry, ConfigValueType
from music_assistant_models.enums import ConfigEntryType, ProviderFeature
from music_assistant_models.errors import InvalidDataError, SetupFailedError

from music_assistant.helpers.app_vars import app_var  # type: ignore[attr-defined]
from music_assistant.helpers.auth import AuthenticationHelper

from .constants import (
    CALLBACK_REDIRECT_URL,
    CONF_ACTION_AUTH,
    CONF_ACTION_CLEAR_AUTH,
    CONF_CLIENT_ID,
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
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES = {
    ProviderFeature.LIBRARY_ARTISTS,
    ProviderFeature.LIBRARY_ALBUMS,
    ProviderFeature.LIBRARY_TRACKS,
    ProviderFeature.LIBRARY_PLAYLISTS,
    ProviderFeature.LIBRARY_ARTISTS_EDIT,
    ProviderFeature.LIBRARY_ALBUMS_EDIT,
    ProviderFeature.LIBRARY_PLAYLISTS_EDIT,
    ProviderFeature.LIBRARY_TRACKS_EDIT,
    ProviderFeature.PLAYLIST_TRACKS_EDIT,
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
    ProviderFeature.ARTIST_ALBUMS,
    ProviderFeature.ARTIST_TOPTRACKS,
    ProviderFeature.SIMILAR_TRACKS,
    ProviderFeature.LIBRARY_PODCASTS,
    ProviderFeature.LIBRARY_PODCASTS_EDIT,
}


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

        if values is None:
            raise InvalidDataError("values cannot be None for authentication action")

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
        if values is None:
            raise InvalidDataError("values cannot be None for clear auth action")
        values[CONF_REFRESH_TOKEN] = None

    auth_required = (values or {}).get(CONF_REFRESH_TOKEN) in (None, "")

    if auth_required and values is not None:
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
        ConfigEntry(
            key=CONF_SYNC_PLAYED_STATUS,
            type=ConfigEntryType.BOOLEAN,
            label="Sync Played Status from Spotify",
            description="Automatically sync episode played status from Spotify to Music Assistant. "
            "Episodes marked as played in Spotify will be marked as played in MA."
            "Only enable this if you use both the Spotify app and Music Assistant "
            "for podcast playback.",
            default_value=False,
            value=values.get(CONF_SYNC_PLAYED_STATUS, True) if values else True,
            category="sync_options",
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
            depends_on=CONF_SYNC_PLAYED_STATUS,
            category="sync_options",
        ),
    )


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    if config.get_value(CONF_REFRESH_TOKEN) in (None, ""):
        msg = "Re-Authentication required"
        raise SetupFailedError(msg)
    return SpotifyProvider(mass, manifest, config, SUPPORTED_FEATURES)
