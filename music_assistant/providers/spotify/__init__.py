"""Spotify music provider support for Music Assistant."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import TYPE_CHECKING, cast

import pkce
from music_assistant_models.config_entries import ConfigEntry, ConfigValueType
from music_assistant_models.enums import ConfigEntryType, EventType, ProviderFeature
from music_assistant_models.errors import InvalidDataError, LoginFailed

from music_assistant.constants import CONF_ENTRY_UNOFFICIAL_PROVIDER
from music_assistant.helpers.app_vars import app_var  # type: ignore[attr-defined]

from .constants import (
    CALLBACK_REDIRECT_URL,
    CONF_ACTION_AUTH,
    CONF_ACTION_AUTH_DEV,
    CONF_ACTION_AUTH_PLAYBACK,
    CONF_ACTION_AUTH_PLAYBACK_BROWSER,
    CONF_ACTION_AUTH_PLAYBACK_SUBMIT,
    CONF_ACTION_CLEAR_AUTH,
    CONF_ACTION_CLEAR_AUTH_DEV,
    CONF_ACTION_CLEAR_AUTH_PLAYBACK,
    CONF_CLIENT_ID,
    CONF_LIBRESPOT_CREDENTIALS,
    CONF_PLAYBACK_CALLBACK_URL,
    CONF_REFRESH_TOKEN_DEPRECATED,
    CONF_REFRESH_TOKEN_DEV,
    CONF_REFRESH_TOKEN_GLOBAL,
    CONF_SYNC_AUDIOBOOK_PROGRESS,
    CONF_SYNC_PODCAST_PROGRESS,
    LIBRESPOT_REDIRECT_PATH,
    LIBRESPOT_REDIRECT_PORT,
    LOOPBACK_WAIT_TIMEOUT,
    PAIRING_DEVICE_NAME,
    PAIRING_TIMEOUT,
)
from .helpers import (
    authorization_code_from_params,
    authorization_code_from_url,
    await_loopback_authorization,
    get_librespot_binary,
    keymaster_access_token,
    keymaster_authorize_url,
    librespot_credentials_via_pairing,
    librespot_credentials_via_token,
    pkce_auth_flow,
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
    ProviderFeature.PLAYLIST_CREATE,
    ProviderFeature.PLAYLIST_TRACKS_EDIT,
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
    ProviderFeature.ARTIST_ALBUMS,
    ProviderFeature.ARTIST_TOPTRACKS,
    ProviderFeature.SIMILAR_TRACKS,
    ProviderFeature.LIBRARY_PODCASTS,
    ProviderFeature.LIBRARY_PODCASTS_EDIT,
}

# A browser sign-in that could not report back parks its PKCE verifier here, keyed by the config
# dialog's session id, until the user pastes the address their browser ended up on. Kept in
# memory only: the authorization code it belongs to expires within minutes anyway.
_PENDING_BROWSER_AUTH: dict[str, str] = {}
# The pairing daemon keeps advertising until it is selected or times out, so it is tracked to
# make a second attempt replace the first one.
_PAIRING_ATTEMPT: dict[str, asyncio.Task[str]] = {}


async def _handle_auth_actions(
    mass: MusicAssistant,
    action: str | None,
    values: dict[str, ConfigValueType] | None,
) -> None:
    """Handle authentication-related actions for config entries."""
    if values is None:
        return

    if action == CONF_ACTION_AUTH:
        refresh_token = await pkce_auth_flow(mass, cast("str", values["session_id"]), app_var(2))
        values[CONF_REFRESH_TOKEN_GLOBAL] = refresh_token
        values[CONF_REFRESH_TOKEN_DEV] = None  # Clear dev token on new global auth
        values[CONF_CLIENT_ID] = None  # Clear client ID on new global auth
        # the playback credential belongs to the account that was just replaced
        _clear_playback_auth_values(values)

    elif action == CONF_ACTION_AUTH_DEV:
        custom_client_id = values.get(CONF_CLIENT_ID)
        if not custom_client_id:
            raise InvalidDataError("Client ID is required for developer authentication")
        refresh_token = await pkce_auth_flow(
            mass, cast("str", values["session_id"]), cast("str", custom_client_id)
        )
        values[CONF_REFRESH_TOKEN_DEV] = refresh_token

    elif action == CONF_ACTION_CLEAR_AUTH:
        values[CONF_REFRESH_TOKEN_GLOBAL] = None
        _clear_playback_auth_values(values)

    elif action == CONF_ACTION_CLEAR_AUTH_DEV:
        values[CONF_REFRESH_TOKEN_DEV] = None
        values[CONF_CLIENT_ID] = None


async def _handle_playback_auth_actions(
    mass: MusicAssistant,
    action: str | None,
    values: dict[str, ConfigValueType] | None,
) -> None:
    """
    Handle the playback authorization actions for config entries.

    :param mass: MusicAssistant instance.
    :param action: Action key called from the config entries UI, if any.
    :param values: The intermediate config values, updated in place with the result.
    """
    if values is None:
        return

    if action == CONF_ACTION_CLEAR_AUTH_PLAYBACK:
        _clear_playback_auth_values(values)
        return

    if action not in (
        CONF_ACTION_AUTH_PLAYBACK,
        CONF_ACTION_AUTH_PLAYBACK_BROWSER,
        CONF_ACTION_AUTH_PLAYBACK_SUBMIT,
    ):
        return

    librespot_bin = await get_librespot_binary()
    credentials: str | None
    if action == CONF_ACTION_AUTH_PLAYBACK:
        try:
            credentials = await _pair_with_spotify_app(librespot_bin)
        except TimeoutError as err:
            raise LoginFailed(
                f"'{PAIRING_DEVICE_NAME}' was not selected in the Spotify app in time. "
                "Try again, or use the web browser option if it does not appear at all."
            ) from err
    elif action == CONF_ACTION_AUTH_PLAYBACK_BROWSER:
        credentials = await _authorize_playback_via_browser(mass, values, librespot_bin)
    else:
        credentials = await _complete_playback_auth_via_browser(mass, values, librespot_bin)

    if credentials is None:
        # the browser could not report back, so the user pastes the address instead
        return
    _clear_playback_auth_values(values)
    values[CONF_LIBRESPOT_CREDENTIALS] = credentials


async def _pair_with_spotify_app(librespot_bin: str) -> str:
    """
    Advertise Music Assistant to the Spotify app and return the credential once it is selected.

    A still-running attempt is cancelled first, so pressing the button again restarts pairing
    instead of advertising a second device under the same name.

    :param librespot_bin: Path to the librespot binary.
    :raises TimeoutError: When the device was not selected within PAIRING_TIMEOUT.
    """
    if attempt := _PAIRING_ATTEMPT.pop("task", None):
        attempt.cancel()
        # wait for it to actually stop advertising before starting the replacement
        with suppress(asyncio.CancelledError):
            await attempt
    attempt = asyncio.create_task(
        librespot_credentials_via_pairing(librespot_bin, PAIRING_DEVICE_NAME, PAIRING_TIMEOUT)
    )
    _PAIRING_ATTEMPT["task"] = attempt
    try:
        return await attempt
    finally:
        if _PAIRING_ATTEMPT.get("task") is attempt:
            del _PAIRING_ATTEMPT["task"]


async def _authorize_playback_via_browser(
    mass: MusicAssistant,
    values: dict[str, ConfigValueType],
    librespot_bin: str,
) -> str | None:
    """
    Start the playback sign-in in the user's browser.

    Returns the stored credential when the browser reported back to Music Assistant directly,
    or None when the user has to paste the address their browser ended up on.

    :param mass: MusicAssistant instance.
    :param values: The intermediate config values, updated in place with the pending state.
    :param librespot_bin: Path to the librespot binary.
    """
    session_id = cast("str", values["session_id"])
    code_verifier, code_challenge = pkce.generate_pkce_pair()
    # signalled directly instead of through AuthenticationHelper: Spotify only accepts a
    # loopback redirect for this client id, so the helper's callback URL can never be reached.
    # The frontend opens the URL right away, so the loopback target below is already served
    # by the time the user approves.
    mass.signal_event(EventType.AUTH_SESSION, session_id, keymaster_authorize_url(code_challenge))
    try:
        callback_params = await await_loopback_authorization(
            LIBRESPOT_REDIRECT_PORT, LIBRESPOT_REDIRECT_PATH, LOOPBACK_WAIT_TIMEOUT
        )
    except TimeoutError, OSError:
        # the loopback target is only reachable when the browser runs on this host; everyone
        # else copies the address from their browser into the config instead
        _clear_playback_auth_values(values)
        _PENDING_BROWSER_AUTH[session_id] = code_verifier
        return None

    access_token = await keymaster_access_token(
        mass.http_session, authorization_code_from_params(callback_params), code_verifier
    )
    return await librespot_credentials_via_token(librespot_bin, access_token)


async def _complete_playback_auth_via_browser(
    mass: MusicAssistant,
    values: dict[str, ConfigValueType],
    librespot_bin: str,
) -> str:
    """
    Redeem the address the user pasted back after approving playback in their browser.

    :param mass: MusicAssistant instance.
    :param values: The intermediate config values holding the pasted address.
    :param librespot_bin: Path to the librespot binary.
    :raises InvalidDataError: When the browser sign-in was not started first.
    """
    code_verifier = _PENDING_BROWSER_AUTH.pop(cast("str", values["session_id"]), None)
    if not code_verifier:
        raise InvalidDataError("Start the browser authorization first")
    code = authorization_code_from_url(str(values.get(CONF_PLAYBACK_CALLBACK_URL) or ""))
    access_token = await keymaster_access_token(mass.http_session, code, code_verifier)
    return await librespot_credentials_via_token(librespot_bin, access_token)


def _clear_playback_auth_values(values: dict[str, ConfigValueType]) -> None:
    """Reset the playback authorization values, including any half-finished browser sign-in."""
    _PENDING_BROWSER_AUTH.clear()
    values[CONF_LIBRESPOT_CREDENTIALS] = None
    values[CONF_PLAYBACK_CALLBACK_URL] = None


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    # Check if audiobooks are supported by existing provider instance
    audiobooks_supported = (
        instance_id
        and (prov_instance := mass.get_provider(instance_id))
        and getattr(prov_instance, "audiobooks_supported", False)
    )

    # Handle any authentication actions
    await _handle_auth_actions(mass, action, values)
    await _handle_playback_auth_actions(mass, action, values)

    # Determine authentication states from current values
    # Note: encrypted values are sent as placeholder text, which indicates value IS set
    global_token = (values or {}).get(CONF_REFRESH_TOKEN_GLOBAL)
    dev_token = (values or {}).get(CONF_REFRESH_TOKEN_DEV)
    global_authenticated = global_token not in (None, "")
    dev_authenticated = dev_token not in (None, "")
    playback_authorized = (values or {}).get(CONF_LIBRESPOT_CREDENTIALS) not in (None, "")
    # a browser sign-in waiting to be finished is what reveals the paste field
    browser_auth_pending = (values or {}).get("session_id") in _PENDING_BROWSER_AUTH

    # Build label text based on state - these are dynamic based on current values
    if not global_authenticated:
        label_text = (
            "You need to authenticate to Spotify. Click the authenticate button below "
            "to start the authentication process which will open in a new (popup) window, "
            "so make sure to disable any popup blockers.\n\n"
            "Also make sure to perform this action from your local network."
        )
    elif action == CONF_ACTION_AUTH:
        label_text = "Authenticated to Spotify. Don't forget to save to complete setup."
    else:
        label_text = "Authenticated to Spotify. No further action required."

    # Build playback authorization label text
    if browser_auth_pending:
        playback_label_text = (
            "Approve playback in the browser tab that was just opened. Your browser then ends "
            "up on a page that cannot be reached, which is expected.\n\n"
            "Copy the full address of that page into the field below and press "
            "'Complete authorization'."
        )
    elif not playback_authorized:
        playback_label_text = (
            "Spotify authorizes playback separately from the connection above, so one more "
            "step is needed before Music Assistant can play your music.\n\n"
            "1. Open the Spotify app on your phone, tablet or computer, signed in with this "
            "same account.\n"
            f"2. Press the button below, then pick '{PAIRING_DEVICE_NAME}' in the Spotify app, "
            "the way you would pick a speaker.\n\n"
            f"If '{PAIRING_DEVICE_NAME}' does not appear in the Spotify app, for example "
            "because Music Assistant runs in a container without access to your home network, "
            "use the web browser option instead."
        )
    elif action in (
        CONF_ACTION_AUTH_PLAYBACK,
        CONF_ACTION_AUTH_PLAYBACK_BROWSER,
        CONF_ACTION_AUTH_PLAYBACK_SUBMIT,
    ):
        playback_label_text = "Playback authorized. Don't forget to save to complete setup."
    else:
        playback_label_text = "Playback is authorized. No further action required."

    # Build dev label text
    if action == CONF_ACTION_AUTH_DEV:
        dev_label_text = "Developer session authenticated. Don't forget to save to complete setup."
    elif dev_authenticated:
        dev_label_text = (
            "Developer API session authenticated. "
            "This session will be used for most API requests to avoid rate limits."
        )
    else:
        dev_label_text = (
            "Optionally, enter your own Spotify Developer Client ID to speed up performance."
        )

    return (
        CONF_ENTRY_UNOFFICIAL_PROVIDER,
        # Global authentication section
        ConfigEntry(
            key="label_text",
            type=ConfigEntryType.LABEL,
            label=label_text,
        ),
        ConfigEntry(
            key=CONF_REFRESH_TOKEN_GLOBAL,
            type=ConfigEntryType.SECURE_STRING,
            label=CONF_REFRESH_TOKEN_GLOBAL,
            hidden=True,
            required=True,
            default_value="",
            value=values.get(CONF_REFRESH_TOKEN_GLOBAL, "") if values else "",
        ),
        ConfigEntry(
            key=CONF_ACTION_AUTH,
            type=ConfigEntryType.ACTION,
            label="Authenticate with Spotify",
            description="This button will redirect you to Spotify to authenticate.",
            action=CONF_ACTION_AUTH,
            # Show only when not authenticated
            hidden=global_authenticated,
        ),
        ConfigEntry(
            key=CONF_ACTION_CLEAR_AUTH,
            type=ConfigEntryType.ACTION,
            label="Clear authentication",
            description="Clear the current authentication details.",
            action=CONF_ACTION_CLEAR_AUTH,
            action_label="Clear authentication",
            required=False,
            # Show only when authenticated
            hidden=not global_authenticated,
        ),
        # Playback authorization section
        ConfigEntry(
            key="playback_label_text",
            type=ConfigEntryType.LABEL,
            label=playback_label_text,
            # Show only when global auth is complete
            hidden=not global_authenticated,
        ),
        ConfigEntry(
            key=CONF_LIBRESPOT_CREDENTIALS,
            type=ConfigEntryType.SECURE_STRING,
            label=CONF_LIBRESPOT_CREDENTIALS,
            hidden=True,
            # required without a default keeps the config from being saved (and the provider
            # from being loaded and failing) before playback has been authorized
            required=True,
            value=values.get(CONF_LIBRESPOT_CREDENTIALS) if values else None,
        ),
        ConfigEntry(
            key=CONF_ACTION_AUTH_PLAYBACK,
            type=ConfigEntryType.ACTION,
            label="Authorize playback with the Spotify app",
            description="Advertises Music Assistant in the Spotify app for a few minutes, "
            "so you can select it there to authorize playback.",
            action=CONF_ACTION_AUTH_PLAYBACK,
            action_label="Authorize playback",
            required=False,
            # Show only when the account is connected but playback is not authorized yet
            hidden=not global_authenticated or playback_authorized,
        ),
        ConfigEntry(
            key=CONF_ACTION_AUTH_PLAYBACK_BROWSER,
            type=ConfigEntryType.ACTION,
            label="Authorize playback with a web browser",
            description="Opens Spotify in a new browser tab to authorize playback. Use this "
            "when Music Assistant does not appear in the Spotify app.",
            action=CONF_ACTION_AUTH_PLAYBACK_BROWSER,
            action_label="Authorize in browser",
            required=False,
            hidden=not global_authenticated or playback_authorized,
        ),
        ConfigEntry(
            key=CONF_PLAYBACK_CALLBACK_URL,
            type=ConfigEntryType.STRING,
            label="Address from your browser",
            description="After approving playback, your browser ends up on a page that cannot "
            "be reached. That is expected: copy the full address of that page to here and press "
            "the button below.",
            required=False,
            default_value="",
            value=values.get(CONF_PLAYBACK_CALLBACK_URL, "") if values else "",
            # Show only while a browser sign-in is waiting to be finished
            hidden=not browser_auth_pending,
        ),
        ConfigEntry(
            key=CONF_ACTION_AUTH_PLAYBACK_SUBMIT,
            type=ConfigEntryType.ACTION,
            label="Complete the browser authorization",
            description="Finishes the authorization with the address you copied above.",
            action=CONF_ACTION_AUTH_PLAYBACK_SUBMIT,
            action_label="Complete authorization",
            required=False,
            hidden=not browser_auth_pending,
        ),
        ConfigEntry(
            key=CONF_ACTION_CLEAR_AUTH_PLAYBACK,
            type=ConfigEntryType.ACTION,
            label="Clear playback authorization",
            description="Clear the current playback authorization to authorize it again.",
            action=CONF_ACTION_CLEAR_AUTH_PLAYBACK,
            action_label="Clear playback authorization",
            required=False,
            # Show only when playback is authorized
            hidden=not playback_authorized,
        ),
        # Developer API section
        ConfigEntry(
            key="dev_label_text",
            type=ConfigEntryType.LABEL,
            label=dev_label_text,
            category="Developer Token",
            # Show only when global auth is complete
            hidden=not global_authenticated,
        ),
        ConfigEntry(
            key=CONF_CLIENT_ID,
            type=ConfigEntryType.SECURE_STRING,
            label="Client ID (optional)",
            description="Enter your own Spotify Developer Client ID to speed up performance "
            "by avoiding global rate limits. Some features like recommendations and similar "
            "tracks will continue to use the global session due to Spotify API restrictions.\n\n"
            f"Use {CALLBACK_REDIRECT_URL} as callback URL in your Spotify Developer app.",
            required=False,
            default_value="",
            value=values.get(CONF_CLIENT_ID, "") if values else "",
            category="Developer Token",
            # Show only when global auth is complete
            hidden=not global_authenticated or dev_authenticated,
        ),
        ConfigEntry(
            key=CONF_REFRESH_TOKEN_DEV,
            type=ConfigEntryType.SECURE_STRING,
            label=CONF_REFRESH_TOKEN_DEV,
            hidden=True,
            required=False,
            default_value="",
            value=values.get(CONF_REFRESH_TOKEN_DEV, "") if values else "",
        ),
        ConfigEntry(
            key=CONF_ACTION_AUTH_DEV,
            type=ConfigEntryType.ACTION,
            label="Authenticate Developer Session",
            description="Authenticate with your custom Client ID.",
            action=CONF_ACTION_AUTH_DEV,
            category="Developer Token",
            # Show only when global is authenticated and dev is NOT authenticated
            # The client_id dependency is checked at action time, not visibility
            hidden=not global_authenticated or dev_authenticated,
        ),
        ConfigEntry(
            key=CONF_ACTION_CLEAR_AUTH_DEV,
            type=ConfigEntryType.ACTION,
            label="Clear Developer Authentication",
            description="Clear the developer session authentication and client ID.",
            action=CONF_ACTION_CLEAR_AUTH_DEV,
            action_label="Clear developer authentication",
            required=False,
            category="Developer Token",
            # Show when dev token is set
            hidden=not global_authenticated or not dev_authenticated,
        ),
        # Sync options
        ConfigEntry(
            key=CONF_SYNC_PODCAST_PROGRESS,
            type=ConfigEntryType.BOOLEAN,
            label="Sync Podcast Progress from Spotify",
            description="Automatically sync episode played status from Spotify to Music Assistant. "
            "Episodes marked as played in Spotify will be marked as played in MA."
            "Only enable this if you use both the Spotify app and Music Assistant "
            "for podcast playback.",
            default_value=False,
            value=values.get(CONF_SYNC_PODCAST_PROGRESS, True) if values else True,
            category="sync_options",
            hidden=not global_authenticated,
        ),
        ConfigEntry(
            key=CONF_SYNC_AUDIOBOOK_PROGRESS,
            type=ConfigEntryType.BOOLEAN,
            label="Sync Audiobook Progress from Spotify",
            description="Automatically sync audiobook progress from Spotify to Music Assistant. "
            "Progress from Spotify app will sync to MA when audiobooks are accessed. "
            "Only enable this if you use both the Spotify app and Music Assistant "
            "for audiobook playback.",
            default_value=False,
            value=values.get(CONF_SYNC_AUDIOBOOK_PROGRESS, False) if values else False,
            category="sync_options",
            hidden=not global_authenticated or not audiobooks_supported,
        ),
    )


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    # Migration: handle legacy refresh_token
    legacy_token = config.get_value(CONF_REFRESH_TOKEN_DEPRECATED)
    global_token = config.get_value(CONF_REFRESH_TOKEN_GLOBAL)

    if legacy_token and not global_token:
        # Migrate legacy token to appropriate new key
        if config.get_value(CONF_CLIENT_ID):
            # Had custom client ID, migrate to dev token
            mass.config.set_raw_provider_config_value(
                config.instance_id, CONF_REFRESH_TOKEN_DEV, legacy_token, encrypted=True
            )
        else:
            # No custom client ID, migrate to global token
            mass.config.set_raw_provider_config_value(
                config.instance_id, CONF_REFRESH_TOKEN_GLOBAL, legacy_token, encrypted=True
            )
        # Remove the deprecated legacy token from config
        mass.config.set_raw_provider_config_value(
            config.instance_id, CONF_REFRESH_TOKEN_DEPRECATED, None
        )
        # Re-fetch the updated config value
        global_token = config.get_value(CONF_REFRESH_TOKEN_GLOBAL)

    if global_token in (None, ""):
        msg = "Re-Authentication required"
        raise LoginFailed(msg)
    return SpotifyProvider(mass, manifest, config, SUPPORTED_FEATURES)
