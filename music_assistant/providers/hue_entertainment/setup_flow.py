"""
Setup flow for the Hue Entertainment provider.

A new setup first asks where the lights are streamed to: a Hue bridge, or Hue lamps on ZHA
via Ziggurat. A reconfigure keeps that choice.

Pairs Music Assistant with a Hue bridge: the user enters the bridge IP and presses the
physical link button on top of the bridge; Music Assistant then registers an app user
(username + clientkey) and stores it - together with the bridge id (for mDNS IP tracking)
- as setup data. The bridge only accepts registration for ~30s after the button press, so
the pairing step runs in a retry loop.

For ZHA the user picks the lamps and names the player; the lamps are then checked on
Ziggurat, which also tells how many zones a gradient lamp gets.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from hue_entertainment import HueEntertainmentAPI
from hue_entertainment.zigbee import ZigguratError, read_segment_counts
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType
from music_assistant_models.errors import LoginFailed

from music_assistant.models.setup_flow import AbortFlow, SetupFlowError, StepExpiredError

from .constants import (
    BACKEND_HUE_BRIDGE,
    BACKEND_ZHA,
    CONF_BACKEND,
    CONF_BRIDGE_HOST,
    CONF_BRIDGE_ID,
    CONF_CLIENTKEY,
    CONF_USERNAME,
    CONF_ZIGBEE_LIGHTS,
    CONF_ZIGBEE_NAME,
    HUE_DEVICE_TYPE,
)
from .zigbee import ZhaUnavailableError, get_hass, hue_lights, stream_info, zha_available

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType

    from music_assistant.mass import MusicAssistant
    from music_assistant.models.setup_flow import SetupSession

    from .zigbee import ZhaLight

# a touch above the bridge's own ~30s link-button window, so the library's
# "button not pressed" timeout surfaces before the step deadline does
_PAIR_TIMEOUT = 35.0
# covers finding Ziggurat, connecting to it and asking every lamp for its segments
_ZIGBEE_CHECK_TIMEOUT = 30.0
_MAX_GRADIENT_ZONES = 3


async def run_setup(session: SetupSession) -> None:
    """
    Run the setup flow: pair with a Hue bridge, or choose the lamps on ZHA.

    :param session: The setup session driving the flow.
    """
    if session.context.kind == "reconfigure":
        backend = str(session.context.setup_data.get(CONF_BACKEND) or BACKEND_HUE_BRIDGE)
    else:
        backend = await _ask_backend(session)
    if backend == BACKEND_ZHA:
        await _setup_zigbee(session)
        return
    host_default = str(session.context.setup_data.get(CONF_BRIDGE_HOST) or "")
    errors: dict[str, str | SetupFlowError] | None = None
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
            errors = {"base": err}


async def _fetch_bridge_id(host: str, username: str) -> str | None:
    """Fetch the bridge id (for mDNS IP tracking); best-effort, never fatal."""
    api_authed = HueEntertainmentAPI(host, username)
    try:
        return await api_authed.get_bridge_id()
    except Exception:
        return None
    finally:
        await api_authed.close()


async def _ask_backend(session: SetupSession) -> str:
    """Ask where the lights are streamed to; ZHA can only be chosen while it is available."""
    errors: dict[str, str | SetupFlowError] | None = None
    while True:
        values = await session.form(
            [
                ConfigEntry(
                    key=CONF_BACKEND,
                    type=ConfigEntryType.STRING,
                    required=True,
                    default_value=BACKEND_HUE_BRIDGE,
                    options=[
                        ConfigValueOption(BACKEND_HUE_BRIDGE),
                        ConfigValueOption(
                            BACKEND_ZHA, disabled=not await zha_available(session.mass)
                        ),
                    ],
                    expanded_options=True,
                ),
            ],
            step_id="backend",
            errors=errors,
        )
        backend = str(values[CONF_BACKEND])
        # the server does not enforce disabled options, and ZHA may have gone meanwhile
        if backend != BACKEND_ZHA or await zha_available(session.mass):
            return backend
        errors = {"base": "zha_unavailable"}


async def _setup_zigbee(session: SetupSession) -> None:
    """Let the user choose the ZHA lamps and the player name, then check the lamps."""
    lights = await _zha_lights(session.mass)
    setup_data = session.context.setup_data
    stored = {str(value).rpartition("/")[0] for value in setup_data.get(CONF_ZIGBEE_LIGHTS) or ()}
    selected = [light.ieee for light in lights if light.ieee in stored]
    name = str(setup_data.get(CONF_ZIGBEE_NAME) or "")
    errors: dict[str, str | SetupFlowError] | None = None
    while True:
        values = await session.form(
            [
                ConfigEntry(key="zigbee_warning", type=ConfigEntryType.ALERT, required=False),
                ConfigEntry(
                    key=CONF_ZIGBEE_NAME,
                    type=ConfigEntryType.STRING,
                    required=False,
                    value=name or None,
                ),
                ConfigEntry(
                    key=CONF_ZIGBEE_LIGHTS,
                    type=ConfigEntryType.STRING,
                    multi_value=True,
                    required=True,
                    value=selected,
                    options=[
                        ConfigValueOption(light.ieee, title=_light_title(light)) for light in lights
                    ],
                ),
            ],
            step_id="zigbee_lights",
            errors=errors,
        )
        chosen = set(cast("list[str]", values.get(CONF_ZIGBEE_LIGHTS) or []))
        selected = [light.ieee for light in lights if light.ieee in chosen]
        name = str(values.get(CONF_ZIGBEE_NAME) or "").strip()
        if not selected:
            errors = {CONF_ZIGBEE_LIGHTS: "required"}
            continue
        # an empty name follows the rooms of the chosen lamps
        player_name = name or " & ".join(
            dict.fromkeys(light.area or light.name for light in lights if light.ieee in chosen)
        )
        try:
            zigbee_lights = await session.progress_until(
                _read_zones(session.mass, selected),
                step_id="zigbee_check",
                text="checking",
                expires_in=_ZIGBEE_CHECK_TIMEOUT,
            )
        except ZhaUnavailableError as err:
            raise AbortFlow("zha_unavailable") from err
        except ZigguratError, StepExpiredError:
            errors = {"base": "ziggurat_unreachable"}
            continue
        except Exception as err:
            errors = {"base": str(err)}
            continue
        if zigbee_lights is None:
            errors = {CONF_ZIGBEE_LIGHTS: "lights_not_found"}
            continue
        try:
            await session.finish(
                {
                    CONF_BACKEND: BACKEND_ZHA,
                    CONF_ZIGBEE_NAME: player_name,
                    CONF_ZIGBEE_LIGHTS: zigbee_lights,
                }
            )
            return
        except SetupFlowError as err:
            errors = {"base": err}


async def _zha_lights(mass: MusicAssistant) -> list[ZhaLight]:
    """Return the lamps to choose from; abort the flow when there are none or ZHA is gone."""
    hass = get_hass(mass)
    if hass is None:
        raise AbortFlow("zha_unavailable")
    try:
        lights = await hue_lights(hass)
    except Exception as err:
        raise AbortFlow("zha_unavailable") from err
    if not lights:
        raise AbortFlow("no_zigbee_lights")
    return lights


async def _read_zones(mass: MusicAssistant, ieees: list[str]) -> list[str] | None:
    """
    Return the stored form of the lamps, "<ieee>/<zones>", or None when one left ZHA.

    :param mass: The MusicAssistant instance.
    :param ieees: IEEE addresses of the chosen lamps, in channel order.
    """
    url, devices = await stream_info(mass)
    if any(ieee not in devices for ieee in ieees):
        return None
    segments = await read_segment_counts(url, [devices[ieee].nwk for ieee in ieees])
    return [
        f"{ieee}/{min(_MAX_GRADIENT_ZONES, segments.get(devices[ieee].nwk, 1))}" for ieee in ieees
    ]


def _light_title(light: ZhaLight) -> str:
    """Return the option title of a lamp: its room and name."""
    return f"{light.area} · {light.name}" if light.area else light.name
