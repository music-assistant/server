"""Setup flow for the Yoto music provider."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from .pkce import build_authorization, exchange_code

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession

CONF_CLIENT_ID = "client_id"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_CALLBACK_URL = "callback_url"
REDIRECT_URI = "http://localhost:8095/callback"
UNOFFICIAL_WARNING = (
    "This is an unofficial integration that is not affiliated with, supported by, or endorsed "
    "by Yoto. It relies on undocumented interfaces that may change without notice."
)


async def run_setup(session: SetupSession) -> None:
    """
    Run the Yoto PKCE browser authorization flow.

    :param session: The setup session driving the flow.
    """
    initial = await session.form(
        [
            ConfigEntry(key="warning", type=ConfigEntryType.LABEL, label=UNOFFICIAL_WARNING),
            ConfigEntry(
                key=CONF_CLIENT_ID,
                type=ConfigEntryType.STRING,
                required=True,
                help_link="https://yoto.dev/get-started/start-here/",
            ),
        ],
        step_id="client",
    )
    client_id = str(initial[CONF_CLIENT_ID]).strip()
    authorization_url, verifier = build_authorization(client_id, REDIRECT_URI)
    callback = await session.form(
        [
            ConfigEntry(
                key="authorization_instructions",
                type=ConfigEntryType.LABEL,
                help_link=authorization_url,
            ),
            ConfigEntry(key=CONF_CALLBACK_URL, type=ConfigEntryType.STRING, required=True),
        ],
        step_id="authorize",
        last_step=True,
    )
    refresh_token = await exchange_code(
        session.mass.http_session,
        client_id,
        REDIRECT_URI,
        verifier,
        str(callback[CONF_CALLBACK_URL]),
    )
    await session.finish(
        {
            CONF_CLIENT_ID: client_id,
            CONF_REFRESH_TOKEN: refresh_token,
        }
    )
