"""
Hue Lights Sync Plugin for Music Assistant.

Syncs Philips Hue lights in Entertainment Areas to music using the
Sendspin visualization pipeline and the Hue Entertainment API (DTLS streaming).

Each Entertainment Area on a paired Hue bridge appears as a virtual player
in Music Assistant. Playing music to the player activates entertainment mode
and makes the lights react to the music in real time.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType, ProviderFeature
from music_assistant_models.errors import LoginFailed, SetupFailedError

from music_assistant.providers.hue_entertainment.hue_sendspin_bridge import HueEntertainmentAPI

from .constants import (
    COLOR_MODES,
    CONF_ACTION_PAIR,
    CONF_BRIDGE_HOST,
    CONF_BRIDGE_ID,
    CONF_BRIGHTNESS,
    CONF_CLIENTKEY,
    CONF_COLOR_MODE,
    CONF_HUE_LATENCY_MS,
    CONF_USERNAME,
    DEFAULT_COLOR_MODE,
    DEFAULT_HUE_LATENCY_MS,
)

LOGGER = logging.getLogger(__name__)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES: set[ProviderFeature] = set()


async def setup(
    mass: MusicAssistant,
    manifest: ProviderManifest,
    config: ProviderConfig,
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    from .provider import HueEntertainmentProvider  # noqa: PLC0415

    if not config.get_value(CONF_BRIDGE_HOST) or not config.get_value(CONF_USERNAME):
        msg = "Hue bridge not paired. Please configure the bridge host and complete pairing."
        raise SetupFailedError(msg)

    return HueEntertainmentProvider(mass, manifest, config, SUPPORTED_FEATURES)


async def _handle_pair_action(values: dict[str, ConfigValueType]) -> None:
    """Execute the pairing flow when the pair button is clicked."""
    host = str(values.get(CONF_BRIDGE_HOST, "")).strip()
    if not host:
        msg = "Bridge host/IP address is required for pairing"
        raise LoginFailed(msg)

    LOGGER.info("Starting Hue bridge pairing with %s ...", host)
    api = HueEntertainmentAPI(host)
    try:
        credentials = await api.pair()
        values[CONF_USERNAME] = credentials["username"]
        values[CONF_CLIENTKEY] = credentials["clientkey"]
        # Fetch and store bridge ID for mDNS IP tracking
        api_authed = HueEntertainmentAPI(host, credentials["username"])
        try:
            bridge_id = await api_authed.get_bridge_id()
            if bridge_id:
                values[CONF_BRIDGE_ID] = bridge_id
        finally:
            await api_authed.close()
        LOGGER.info("Hue bridge pairing successful!")
    except TimeoutError as err:
        LOGGER.warning("Hue bridge pairing timed out: %s", err)
        raise LoginFailed(str(err)) from err
    except Exception as err:
        LOGGER.warning("Hue bridge pairing failed: %s", err)
        msg = f"Failed to connect to Hue bridge at {host}: {err}"
        raise LoginFailed(msg) from err
    finally:
        await api.close()


async def get_config_entries(
    mass: MusicAssistant,  # noqa: ARG001
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    :param action: Action key called from config entries UI.
    :param values: Intermediate raw values for config entries.
    """
    if action == CONF_ACTION_PAIR and values:
        await _handle_pair_action(values)

    # Determine pairing state from current values
    paired = bool((values or {}).get(CONF_USERNAME) not in (None, ""))

    # Build status label based on pairing state
    if action == CONF_ACTION_PAIR and paired:
        status_text = "Successfully paired with Hue bridge! Click save to complete setup."
    elif paired:
        status_text = "Paired with Hue bridge."
    else:
        status_text = (
            "Press the physical button on your Hue bridge, "
            "then click the pair button below within 30 seconds."
        )

    return (
        ConfigEntry(
            key=CONF_BRIDGE_HOST,
            type=ConfigEntryType.STRING,
            label="Bridge IP Address",
            description="IP address of the Philips Hue bridge.",
            required=True,
            value=values.get(CONF_BRIDGE_HOST) if values else None,
        ),
        ConfigEntry(
            key=CONF_BRIDGE_ID,
            type=ConfigEntryType.STRING,
            label="Bridge ID",
            required=False,
            hidden=True,
            value=values.get(CONF_BRIDGE_ID) if values else None,
        ),
        ConfigEntry(
            key="status_label",
            type=ConfigEntryType.LABEL if paired else ConfigEntryType.ALERT,
            label=status_text,
            depends_on=CONF_BRIDGE_HOST,
        ),
        ConfigEntry(
            key=CONF_ACTION_PAIR,
            type=ConfigEntryType.ACTION,
            label="(Re)Pair with Hue Bridge" if paired else "Pair with Hue Bridge",
            description="Connects to the bridge and retrieves the API credentials.",
            action=CONF_ACTION_PAIR,
            depends_on=CONF_BRIDGE_HOST,
            required=False,
            hidden=action == CONF_ACTION_PAIR and paired,
        ),
        ConfigEntry(
            key=CONF_USERNAME,
            type=ConfigEntryType.SECURE_STRING,
            label="Hue API Username",
            hidden=True,
            required=False,
            default_value="",
            value=values.get(CONF_USERNAME, "") if values else "",
        ),
        ConfigEntry(
            key=CONF_CLIENTKEY,
            type=ConfigEntryType.SECURE_STRING,
            label="Hue Client Key",
            hidden=True,
            required=False,
            default_value="",
            value=values.get(CONF_CLIENTKEY, "") if values else "",
        ),
        ConfigEntry(
            key=CONF_BRIGHTNESS,
            type=ConfigEntryType.INTEGER,
            label="Brightness",
            description="Overall light brightness (0-100).",
            default_value=100,
            range=(0, 100),
            category="settings",
        ),
        ConfigEntry(
            key=CONF_COLOR_MODE,
            type=ConfigEntryType.STRING,
            label="Mode",
            description=(
                "Visualization style. "
                "Smooth (default): spectrum-driven brightness with a slowly "
                "drifting palette. "
                "Ambient: colour cycling only, no brightness modulation. "
                "Flashing: brightness pulse on every beat, stronger on downbeats. "
                "Energetic: large brightness swings on hits plus fast palette rotation."
            ),
            default_value=DEFAULT_COLOR_MODE,
            options=[ConfigValueOption(mode.capitalize(), mode) for mode in COLOR_MODES],
            category="settings",
        ),
        ConfigEntry(
            key=CONF_HUE_LATENCY_MS,
            type=ConfigEntryType.INTEGER,
            label="Light latency (ms)",
            description=(
                "Milliseconds to render light updates ahead of the audio, to "
                "offset the Hue bridge and DTLS delay. Increase if the lights "
                "lag the music, decrease if they run ahead of it."
            ),
            default_value=DEFAULT_HUE_LATENCY_MS,
            range=(0, 3000),
            immediate_apply=True,
            category="settings",
        ),
    )
