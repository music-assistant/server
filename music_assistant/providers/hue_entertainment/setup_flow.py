"""
Setup flow for the Hue Entertainment provider.

Pairs Music Assistant with a Hue bridge: the user enters the bridge IP and presses the
physical link button on top of the bridge; Music Assistant then registers an app user
(username + clientkey) and stores it - together with the bridge id (for mDNS IP tracking)
- as setup data. The bridge only accepts registration for ~30s after the button press, so
the pairing step runs in a retry loop.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hue_entertainment import HueEntertainmentAPI
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType
from music_assistant_models.errors import LoginFailed

from music_assistant.models.setup_flow import SetupFlowError, StepExpiredError

from .constants import (
    CONF_BRIDGE_HOST,
    CONF_BRIDGE_ID,
    CONF_CLIENTKEY,
    CONF_USERNAME,
    HUE_DEVICE_TYPE,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType

    from music_assistant.models.setup_flow import SetupSession

# a touch above the bridge's own ~30s link-button window, so the library's
# "button not pressed" timeout surfaces before the step deadline does
_PAIR_TIMEOUT = 35.0


async def run_setup(session: SetupSession) -> None:
    """
    Run the Hue bridge pairing flow.

    :param session: The setup session driving the flow.
    """
    host_default = str(session.context.setup_data.get(CONF_BRIDGE_HOST) or "")
    errors: dict[str, str] | None = None
    while True:
        values = await session.form(
            [
                ConfigEntry(key="pair_intro", type=ConfigEntryType.LABEL),
                ConfigEntry(
                    key=CONF_BRIDGE_HOST,
                    type=ConfigEntryType.STRING,
                    required=True,
                    value=host_default or None,
                ),
            ],
            step_id="user",
            errors=errors,
        )
        host = str(values[CONF_BRIDGE_HOST]).strip()
        host_default = host
        api = HueEntertainmentAPI(host)
        try:
            credentials = await session.progress_until(
                api.pair(device_type=HUE_DEVICE_TYPE),
                step_id="pairing",
                text="press_button",
                expires_in=_PAIR_TIMEOUT,
            )
            bridge_id = await _fetch_bridge_id(host, credentials["username"])
        except StepExpiredError, TimeoutError:
            errors = {"base": "button_not_pressed"}
            continue
        except LoginFailed as err:
            errors = {"base": err.translation_key or str(err)}
            continue
        except Exception as err:
            errors = {"base": str(err)}
            continue
        finally:
            await api.close()
        collected: dict[str, ConfigValueType] = {
            CONF_BRIDGE_HOST: host,
            CONF_USERNAME: str(credentials["username"]),
            CONF_CLIENTKEY: str(credentials["clientkey"]),
        }
        if bridge_id:
            collected[CONF_BRIDGE_ID] = bridge_id
        try:
            await session.finish(collected)
            return
        except SetupFlowError as err:
            errors = {"base": err.translation_key or str(err)}


async def _fetch_bridge_id(host: str, username: str) -> str | None:
    """Fetch the bridge id (for mDNS IP tracking); best-effort, never fatal."""
    api_authed = HueEntertainmentAPI(host, username)
    try:
        return await api_authed.get_bridge_id()
    except Exception:
        return None
    finally:
        await api_authed.close()
