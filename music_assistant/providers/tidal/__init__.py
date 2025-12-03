"""Tidal music provider support for MusicAssistant."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption, ConfigValueType
from music_assistant_models.enums import ConfigEntryType

from .auth_manager import ManualAuthenticationHelper, TidalAuthManager
from .constants import (
    CONF_ACTION_CLEAR_AUTH,
    CONF_ACTION_COMPLETE_PKCE_LOGIN,
    CONF_ACTION_START_PKCE_LOGIN,
    CONF_AUTH_TOKEN,
    CONF_EXPIRY_TIME,
    CONF_OOPS_URL,
    CONF_QUALITY,
    CONF_REFRESH_TOKEN,
    CONF_TEMP_SESSION,
    CONF_USER_ID,
    LABEL_COMPLETE_PKCE_LOGIN,
    LABEL_OOPS_URL,
    LABEL_START_PKCE_LOGIN,
)
from .provider import TidalProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return TidalProvider(mass, manifest, config)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """
    Return configuration entries required to set up the Tidal provider.

    Parameters:
        mass (MusicAssistant): The MusicAssistant instance.
        instance_id (str | None): Optional instance identifier for the provider.
        action (str | None): Optional action to perform (e.g., start or complete PKCE login).
        values (dict[str, ConfigValueType] | None): Dictionary of current configuration values.

    Returns:
        tuple[ConfigEntry, ...]: Tuple of ConfigEntry objects representing the configuration steps.

    The function handles authentication actions and returns the appropriate configuration entries
    based on the current state and provided values.
    """
    assert values is not None

    if action == CONF_ACTION_START_PKCE_LOGIN:
        async with ManualAuthenticationHelper(
            mass, cast("str", values["session_id"])
        ) as auth_helper:
            quality = str(values.get(CONF_QUALITY))
            base64_session = await TidalAuthManager.generate_auth_url(auth_helper, quality)
            values[CONF_TEMP_SESSION] = base64_session
            # Tidal is using the ManualAuthenticationHelper just to send the user to an URL
            # there is no actual oauth callback happening, instead the user is redirected
            # to a non-existent page and needs to copy the URL from the browser and paste it
            # we simply wait here to allow the user to start the auth
            await asyncio.sleep(15)

    if action == CONF_ACTION_COMPLETE_PKCE_LOGIN:
        quality = str(values.get(CONF_QUALITY))
        pkce_url = str(values.get(CONF_OOPS_URL))
        base64_session = str(values.get(CONF_TEMP_SESSION))
        auth_data = await TidalAuthManager.process_pkce_login(
            mass.http_session, base64_session, pkce_url
        )
        values[CONF_AUTH_TOKEN] = auth_data["access_token"]
        values[CONF_REFRESH_TOKEN] = auth_data["refresh_token"]
        values[CONF_EXPIRY_TIME] = auth_data["expires_at"]
        values[CONF_USER_ID] = auth_data["userId"]
        values[CONF_TEMP_SESSION] = ""

    if action == CONF_ACTION_CLEAR_AUTH:
        values[CONF_AUTH_TOKEN] = None
        values[CONF_REFRESH_TOKEN] = None
        values[CONF_EXPIRY_TIME] = None
        values[CONF_USER_ID] = None

    if values.get(CONF_AUTH_TOKEN):
        auth_entries: tuple[ConfigEntry, ...] = (
            ConfigEntry(
                key="label_ok",
                type=ConfigEntryType.LABEL,
                label="You are authenticated with Tidal",
            ),
            ConfigEntry(
                key=CONF_ACTION_CLEAR_AUTH,
                type=ConfigEntryType.ACTION,
                label="Reset authentication",
                description="Reset the authentication for Tidal",
                action=CONF_ACTION_CLEAR_AUTH,
                value=None,
            ),
            ConfigEntry(
                key=CONF_QUALITY,
                type=ConfigEntryType.STRING,
                label="Quality setting for Tidal:",
                description="High = 16bit 44.1kHz\n\nMax = Up to 24bit 192kHz",
                options=[
                    ConfigValueOption("High", "LOSSLESS"),
                    ConfigValueOption("Max", "HI_RES_LOSSLESS"),
                ],
                default_value="HI_RES_LOSSLESS",
            ),
        )
    else:
        auth_entries = (
            ConfigEntry(
                key=CONF_QUALITY,
                type=ConfigEntryType.STRING,
                label="Quality setting for Tidal:",
                required=True,
                description="High = 16bit 44.1kHz\n\nMax = Up to 24bit 192kHz",
                options=[
                    ConfigValueOption("High", "LOSSLESS"),
                    ConfigValueOption("Max", "HI_RES_LOSSLESS"),
                ],
                default_value="HI_RES_LOSSLESS",
            ),
            ConfigEntry(
                key=LABEL_START_PKCE_LOGIN,
                type=ConfigEntryType.LABEL,
                label="The button below will redirect you to Tidal.com to authenticate.\n\n"
                " After authenticating, you will be redirected to a page that prominently displays"
                " 'Page Not Found' at the top. That is normal, you need to copy that URL from the "
                "address bar and come back here",
                hidden=action == CONF_ACTION_START_PKCE_LOGIN,
            ),
            ConfigEntry(
                key=CONF_ACTION_START_PKCE_LOGIN,
                type=ConfigEntryType.ACTION,
                label="Starts the auth process via PKCE on Tidal.com",
                description="This button will redirect you to Tidal.com to authenticate."
                " After authenticating, you will be redirected to a page that prominently displays"
                " 'Page Not Found' at the top.",
                action=CONF_ACTION_START_PKCE_LOGIN,
                depends_on=CONF_QUALITY,
                action_label="Starts the auth process via PKCE on Tidal.com",
                value=cast("str", values.get(CONF_TEMP_SESSION)) if values else None,
                hidden=action == CONF_ACTION_START_PKCE_LOGIN,
            ),
            ConfigEntry(
                key=CONF_TEMP_SESSION,
                type=ConfigEntryType.STRING,
                label="Temporary session for Tidal",
                hidden=True,
                required=False,
                value=cast("str", values.get(CONF_TEMP_SESSION)) if values else None,
            ),
            ConfigEntry(
                key=LABEL_OOPS_URL,
                type=ConfigEntryType.LABEL,
                label="Copy the URL from the 'Page Not Found' page that you were previously"
                " redirected to and paste it in the field below",
                hidden=action != CONF_ACTION_START_PKCE_LOGIN,
            ),
            ConfigEntry(
                key=CONF_OOPS_URL,
                type=ConfigEntryType.STRING,
                label="Oops URL from Tidal redirect",
                description="This field should be filled manually by you after authenticating on"
                " Tidal.com and being redirected to a page that prominently displays"
                " 'Page Not Found' at the top.",
                depends_on=CONF_ACTION_START_PKCE_LOGIN,
                value=cast("str", values.get(CONF_OOPS_URL)) if values else None,
                hidden=action != CONF_ACTION_START_PKCE_LOGIN,
            ),
            ConfigEntry(
                key=LABEL_COMPLETE_PKCE_LOGIN,
                type=ConfigEntryType.LABEL,
                label="After pasting the URL in the field above, click the button below to complete"
                " the process.",
                hidden=action != CONF_ACTION_START_PKCE_LOGIN,
            ),
            ConfigEntry(
                key=CONF_ACTION_COMPLETE_PKCE_LOGIN,
                type=ConfigEntryType.ACTION,
                label="Complete the auth process via PKCE on Tidal.com",
                description="Click this after adding the 'Page Not Found' URL above, this will"
                " complete the authentication process.",
                action=CONF_ACTION_COMPLETE_PKCE_LOGIN,
                depends_on=CONF_OOPS_URL,
                action_label="Complete the auth process via PKCE on Tidal.com",
                value=None,
                hidden=action != CONF_ACTION_START_PKCE_LOGIN,
            ),
        )

    # return the auth_data config entry
    return (
        *auth_entries,
        ConfigEntry(
            key=CONF_AUTH_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            label="Authentication token for Tidal",
            description="You need to link Music Assistant to your Tidal account.",
            hidden=True,
            value=cast("str", values.get(CONF_AUTH_TOKEN)) if values else None,
        ),
        ConfigEntry(
            key=CONF_REFRESH_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            label="Refresh token for Tidal",
            description="You need to link Music Assistant to your Tidal account.",
            hidden=True,
            value=cast("str", values.get(CONF_REFRESH_TOKEN)) if values else None,
        ),
        ConfigEntry(
            key=CONF_EXPIRY_TIME,
            type=ConfigEntryType.STRING,
            label="Expiry time of auth token for Tidal",
            hidden=True,
            value=cast("str", values.get(CONF_EXPIRY_TIME)) if values else None,
        ),
        ConfigEntry(
            key=CONF_USER_ID,
            type=ConfigEntryType.STRING,
            label="Your Tidal User ID",
            description="This is your unique Tidal user ID.",
            hidden=True,
            value=cast("str", values.get(CONF_USER_ID)) if values else None,
        ),
    )
