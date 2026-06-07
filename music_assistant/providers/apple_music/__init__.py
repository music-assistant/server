"""
Apple Music musicprovider support for MusicAssistant.

TODO MUSIC_APP_TOKEN expires after 6 months so should have a distribution mechanism outside
  compulsory application updates. It is only a semi-private key in JWT format so could be
  refreshed daily by a GitHub action and downloaded by the provider each initialise.
TODO Widevine keys can be obtained dynamically from Apple Music API rather than copied into Docker
  build. This is undocumented but @maxlyth has a working example.
TODO MUSIC_USER_TOKEN must be refreshed (~min 180 days) and needs mechanism to prompt user to
  re-authenticate in browser.
TODO Current provider ignores private tracks that are not available in the storefront catalog as
  streamable url is derived from the catalog id. It is undocumented but @maxlyth has a working
  example to get a streamable url from the library id.
"""

from __future__ import annotations

import pathlib
import re
import time
from typing import TYPE_CHECKING, cast

from aiohttp import ClientTimeout, web
from music_assistant_models.config_entries import ConfigEntry, ConfigValueType
from music_assistant_models.enums import ConfigEntryType
from music_assistant_models.errors import LoginFailed

from music_assistant.helpers.auth import AuthenticationHelper

from .constants import (
    CONF_MUSIC_APP_TOKEN,
    CONF_MUSIC_USER_MANUAL_TOKEN,
    CONF_MUSIC_USER_TOKEN,
    CONF_MUSIC_USER_TOKEN_TIMESTAMP,
    MUSIC_APP_TOKEN,
)
from .provider import AppleMusicProvider

__all__ = ["AppleMusicProvider", "get_config_entries", "setup"]

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant import MusicAssistant
    from music_assistant.models import ProviderInstanceType


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return AppleMusicProvider(mass, manifest, config)


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
    values = values or {}

    def validate_user_token(token: ConfigValueType) -> bool:
        if not isinstance(token, str):
            return False
        valid = re.findall(r"[a-zA-Z0-9=/+]{32,}==$", token)
        return bool(valid)

    # Check for valid app token (first with regex and then API check)
    default_app_token_valid = False
    async with (
        mass.http_session.get(
            "https://api.music.apple.com/v1/test",
            headers={"Authorization": f"Bearer {MUSIC_APP_TOKEN}"},
            ssl=True,
            timeout=ClientTimeout(total=10),
        ) as response,
    ):
        if response.status == 200:
            values[CONF_MUSIC_APP_TOKEN] = f"{MUSIC_APP_TOKEN}"
            default_app_token_valid = True

    # Action is to launch MusicKit flow
    if action == "CONF_ACTION_AUTH" and default_app_token_valid:
        callback_method = "POST"
        async with AuthenticationHelper(
            mass, cast("str", values["session_id"]), callback_method
        ) as auth_helper:
            callback_url = auth_helper.callback_url
            flow_base_path = f"apple_music_auth/{values['session_id']}/"
            flow_timeout = 600
            parent_file_path = pathlib.Path(__file__).parent.resolve()
            base_url = f"{mass.webserver.base_url}/{flow_base_path}"
            flow_base_url = f"{base_url}index.html"

            async def serve_mk_auth_page(request: web.Request) -> web.FileResponse:
                auth_html_path = parent_file_path.joinpath("musickit_auth/musickit_wrapper.html")
                return web.FileResponse(
                    auth_html_path,
                    headers={"content-type": "text/html"},
                )

            async def serve_mk_auth_css(request: web.Request) -> web.FileResponse:
                auth_css_path = parent_file_path.joinpath("musickit_auth/musickit_wrapper.css")
                return web.FileResponse(
                    auth_css_path,
                    headers={"content-type": "text/css"},
                )

            async def serve_mk_glue(request: web.Request) -> web.Response:
                return_html = f"""
                const return_url='{callback_url}';
                const base_url='{base_url}';
                const app_token='{values[CONF_MUSIC_APP_TOKEN]}';
                const callback_method='{callback_method}';
                const user_token='{
                    values[CONF_MUSIC_USER_TOKEN]
                    if validate_user_token(values[CONF_MUSIC_USER_TOKEN])
                    else ""
                }';
                const user_token_timestamp='{values[CONF_MUSIC_USER_TOKEN_TIMESTAMP]}';
                const flow_timeout={max([flow_timeout - 10, 60])};
                const flow_start_time={int(time.time())};
                const mass_version='{mass.version}';
                """
                return web.Response(
                    body=return_html,
                    headers={"content-type": "text/javascript"},
                )

            mass.webserver.register_dynamic_route(
                f"/{flow_base_path}index.html", serve_mk_auth_page
            )
            mass.webserver.register_dynamic_route(f"/{flow_base_path}index.css", serve_mk_auth_css)
            mass.webserver.register_dynamic_route(f"/{flow_base_path}index.js", serve_mk_glue)

            try:
                result = await auth_helper.authenticate(flow_base_url, flow_timeout)
                values[CONF_MUSIC_USER_TOKEN] = result["music-user-token"]
                values[CONF_MUSIC_USER_TOKEN_TIMESTAMP] = result["music-user-token-timestamp"]
            except KeyError:
                # no music-user-token URL param was found, likely user cancelled the auth
                pass
            except Exception as error:
                raise LoginFailed(f"Failed to authenticate with Apple '{error}'.")
            finally:
                mass.webserver.unregister_dynamic_route(f"/{flow_base_path}index.html")
                mass.webserver.unregister_dynamic_route(f"/{flow_base_path}index.css")
                mass.webserver.unregister_dynamic_route(f"/{flow_base_path}index.js")

    # ruff: noqa: ARG001
    return (
        ConfigEntry(
            key=CONF_MUSIC_APP_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            label="MusicKit App Token",
            hidden=default_app_token_valid,
            required=True,
            value=values.get(CONF_MUSIC_APP_TOKEN) if values else None,
        ),
        ConfigEntry(
            key=CONF_MUSIC_USER_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            label="Music User Token",
            required=False,
            action="CONF_ACTION_AUTH",
            description="Authenticate with Apple Music to retrieve a valid music user token.",
            action_label="Authenticate with Apple Music",
            value=values.get(CONF_MUSIC_USER_TOKEN)
            if (
                values
                and isinstance(ts := values.get(CONF_MUSIC_USER_TOKEN_TIMESTAMP), int)
                and ts > (time.time() - (3600 * 24 * 150))
            )
            else None,
        ),
        ConfigEntry(
            key=CONF_MUSIC_USER_MANUAL_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            label="Manual Music User Token",
            required=False,
            advanced=True,
            description=(
                "Authenticate with a manual Music User Token in case the Authentication flow"
                " is unsupported (e.g. when using child accounts)."
            ),
            help_link="https://www.music-assistant.io/music-providers/apple-music/",
            value=values.get(CONF_MUSIC_USER_MANUAL_TOKEN),
        ),
        ConfigEntry(
            key=CONF_MUSIC_USER_TOKEN_TIMESTAMP,
            type=ConfigEntryType.INTEGER,
            description="Timestamp music user token was updated.",
            label="Music User Token Timestamp",
            hidden=True,
            required=True,
            default_value=0,
            value=values.get(CONF_MUSIC_USER_TOKEN_TIMESTAMP) if values else 0,
        ),
    )
