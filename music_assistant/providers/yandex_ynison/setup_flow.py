"""
Setup flow for the Yandex Music Connect (Ynison) plugin.

The user picks the Yandex account source: borrow the account of a linked Yandex Music
provider (single login, one token family) or use this plugin's own credentials. Borrowing
finishes immediately with just the linked instance id. For own credentials the user signs
in with the native ``PassportClient`` QR login — the QR code is rendered as an inline image
and completion is detected by polling; a code that elapses is minted afresh in place.

The music token (and, when "remember session" is on, the long-lived ``x_token``) plus the
display login are persisted as setup data. Own-mode token refresh stays in memory at
runtime — the plugin does not persist rotated own tokens.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import segno
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType
from ya_passport_auth import ClientConfig, PassportClient
from ya_passport_auth.exceptions import QRTimeoutError, YaPassportError

from music_assistant.helpers.config_entries import create_player_selector
from music_assistant.models.setup_flow import SetupFlowError, StepExpiredError

from .config_helpers import list_yandex_music_instances
from .constants import (
    CONF_ACCOUNT_LOGIN,
    CONF_MASS_PLAYER_ID,
    CONF_PUBLISH_NAME,
    CONF_REMEMBER_SESSION,
    CONF_TOKEN,
    CONF_X_TOKEN,
    CONF_YM_INSTANCE,
    DEFAULT_DISPLAY_NAME,
    PLAYER_ID_AUTO,
    YM_INSTANCE_OWN,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType
    from ya_passport_auth import Credentials

    from music_assistant.models.setup_flow import SetupSession


async def run_setup(session: SetupSession) -> None:
    """
    Run the Ynison setup flow: pick the account source, then borrow or QR-log in.

    :param session: The setup session driving the flow.
    """
    ym_instances = list_yandex_music_instances(session.mass)
    valid_sources = {inst_id for inst_id, _ in ym_instances}
    setup_data = dict(session.context.setup_data)
    prefill: dict[str, ConfigValueType] = {**session.context.values, **setup_data}
    default_source = str(prefill.get(CONF_YM_INSTANCE) or YM_INSTANCE_OWN)
    if default_source != YM_INSTANCE_OWN and default_source not in valid_sources:
        default_source = YM_INSTANCE_OWN
    default_player = prefill.get(CONF_MASS_PLAYER_ID)
    default_name = str(prefill.get(CONF_PUBLISH_NAME) or DEFAULT_DISPLAY_NAME)

    errors: dict[str, str] | None = None
    while True:
        values = await session.form(
            [
                _source_entry(default_source, ym_instances),
                ConfigEntry(
                    key=CONF_REMEMBER_SESSION,
                    type=ConfigEntryType.BOOLEAN,
                    required=False,
                    default_value=True,
                ),
                create_player_selector(
                    session.mass,
                    CONF_MASS_PLAYER_ID,
                    default_player,
                    PLAYER_ID_AUTO,
                ),
                ConfigEntry(
                    key=CONF_PUBLISH_NAME,
                    type=ConfigEntryType.STRING,
                    required=True,
                    default_value=default_name,
                    value=default_name,
                ),
            ],
            step_id="user",
            errors=errors,
        )
        source = str(values[CONF_YM_INSTANCE])
        remember = bool(values[CONF_REMEMBER_SESSION])
        default_player = values[CONF_MASS_PLAYER_ID]
        default_name = str(values[CONF_PUBLISH_NAME])
        identity: dict[str, ConfigValueType] = {
            CONF_MASS_PLAYER_ID: default_player,
            CONF_PUBLISH_NAME: default_name,
        }
        if source != YM_INSTANCE_OWN:
            # borrow mode: the linked Yandex Music instance owns authentication
            try:
                await session.finish({CONF_YM_INSTANCE: source, **identity})
                return
            except SetupFlowError as err:
                errors = {"base": err.translation_key or str(err)}
                default_source = source
                continue
        # own credentials: QR login
        try:
            creds = await _qr_login(session)
        except YaPassportError as err:
            errors = {"base": str(err)}
            continue
        if creds.music_token is None:
            errors = {"base": "no_music_token"}
            continue
        collected: dict[str, ConfigValueType] = {
            CONF_YM_INSTANCE: YM_INSTANCE_OWN,
            CONF_TOKEN: creds.music_token.get_secret(),
            CONF_X_TOKEN: creds.x_token.get_secret() if remember else None,
            CONF_ACCOUNT_LOGIN: creds.display_login,
            **identity,
        }
        try:
            await session.finish(collected)
            return
        except SetupFlowError as err:
            errors = {"base": err.translation_key or str(err)}


async def _qr_login(session: SetupSession) -> Credentials:
    """
    Run the native Yandex Passport QR login, refreshing the code on expiry.

    Shows the QR code as an inline image and polls until the user scans and confirms it
    in the Yandex app; whenever the scan window elapses a fresh code is minted in place.
    """
    ttl = ClientConfig().qr_poll_total_timeout_seconds
    async with PassportClient.create(config=ClientConfig()) as client:
        while True:
            qr = await client.start_qr_login()
            try:
                return await session.progress_until(
                    client.poll_qr_until_confirmed(qr, total_timeout=ttl),
                    step_id="scan_qr",
                    text="scan_qr",
                    image=_qr_image(qr.qr_url),
                    expires_in=int(ttl),
                )
            except StepExpiredError, QRTimeoutError:
                continue


def _source_entry(default_source: str, ym_instances: list[tuple[str, str]]) -> ConfigEntry:
    """Build the account-source select (linked Yandex Music instances + own credentials)."""
    return ConfigEntry(
        key=CONF_YM_INSTANCE,
        type=ConfigEntryType.STRING,
        required=True,
        default_value=default_source,
        options=[
            *(
                ConfigValueOption(value=inst_id, title=f"Yandex Music: {name}")
                for inst_id, name in ym_instances
            ),
            ConfigValueOption(value=YM_INSTANCE_OWN),
        ],
    )


def _qr_image(qr_url: str) -> str:
    """Render a QR-login URL as an SVG data URI to display in the flow."""
    return segno.make(qr_url, error="m").svg_data_uri(scale=4)
