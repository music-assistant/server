"""Setup flow for the VBAN Receiver plugin."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aiovban.enums import VBANSampleRate
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType

from music_assistant.constants import CONF_BIND_IP, CONF_BIND_PORT, CONF_ENTRY_WARN_PREVIEW
from music_assistant.helpers.util import get_ip_addresses
from music_assistant.models.setup_flow import SetupFlowError

from .constants import (
    CONF_AUDIO_CHANNELS,
    CONF_PCM_AUDIO_FORMAT,
    CONF_PCM_SAMPLE_RATE,
    CONF_SENDER_HOST,
    CONF_VBAN_STREAM_NAME,
    DEFAULT_AUDIO_CHANNELS,
    DEFAULT_PCM_AUDIO_FORMAT,
    DEFAULT_PCM_SAMPLE_RATE,
    DEFAULT_UDP_PORT,
)
from .helpers import get_supported_pcm_formats

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession


async def run_setup(session: SetupSession) -> None:
    """
    Configure the remote VBAN stream and local receiver endpoint.

    :param session: The setup session driving the flow.
    """
    setup_data = dict(session.context.setup_data)
    errors: dict[str, str] | None = None
    while True:
        prefill: dict[str, Any] = {**session.context.values, **setup_data}
        values = await session.form(
            [
                CONF_ENTRY_WARN_PREVIEW,
                ConfigEntry(
                    key=CONF_BIND_PORT,
                    type=ConfigEntryType.INTEGER,
                    required=True,
                    default_value=DEFAULT_UDP_PORT,
                    value=prefill.get(CONF_BIND_PORT),
                ),
                ConfigEntry(
                    key=CONF_VBAN_STREAM_NAME,
                    type=ConfigEntryType.STRING,
                    required=True,
                    default_value="Network AUX",
                    value=prefill.get(CONF_VBAN_STREAM_NAME),
                    validate=_validate_stream_name,  # type: ignore[arg-type]
                ),
                ConfigEntry(
                    key=CONF_SENDER_HOST,
                    type=ConfigEntryType.STRING,
                    required=True,
                    default_value="127.0.0.1",
                    value=prefill.get(CONF_SENDER_HOST),
                ),
                ConfigEntry(
                    key=CONF_PCM_AUDIO_FORMAT,
                    type=ConfigEntryType.STRING,
                    required=True,
                    default_value=DEFAULT_PCM_AUDIO_FORMAT,
                    value=prefill.get(CONF_PCM_AUDIO_FORMAT),
                    options=[
                        ConfigValueOption(value, title=value)
                        for value in get_supported_pcm_formats()
                    ],
                ),
                ConfigEntry(
                    key=CONF_PCM_SAMPLE_RATE,
                    type=ConfigEntryType.INTEGER,
                    required=True,
                    default_value=DEFAULT_PCM_SAMPLE_RATE,
                    value=prefill.get(CONF_PCM_SAMPLE_RATE),
                    options=[
                        ConfigValueOption(value, title=str(value))
                        for value in _get_vban_sample_rates()
                    ],
                ),
                ConfigEntry(
                    key=CONF_AUDIO_CHANNELS,
                    type=ConfigEntryType.INTEGER,
                    required=True,
                    default_value=DEFAULT_AUDIO_CHANNELS,
                    value=prefill.get(CONF_AUDIO_CHANNELS),
                    options=[ConfigValueOption(value, title=str(value)) for value in range(1, 9)],
                ),
                ConfigEntry(
                    key=CONF_BIND_IP,
                    type=ConfigEntryType.STRING,
                    required=True,
                    default_value="0.0.0.0",
                    value=prefill.get(CONF_BIND_IP),
                    options=[
                        ConfigValueOption(value, title=value)
                        for value in {"0.0.0.0", *await get_ip_addresses(include_ipv6=True)}
                    ],
                    advanced=True,
                ),
            ],
            step_id="user",
            errors=errors,
            last_step=True,
        )
        setup_data.update(values)
        try:
            await session.finish(setup_data)
            return
        except SetupFlowError as err:
            errors = {"base": err.translation_key or str(err)}


def _get_vban_sample_rates() -> list[int]:
    """Return the supported VBAN sample rates."""
    return [int(member.split("_")[1]) for member in VBANSampleRate.__members__]


def _validate_stream_name(config_value: str) -> bool:
    """Return whether a VBAN stream name is valid."""
    try:
        config_value.encode("ascii")
    except UnicodeEncodeError:
        return False
    return len(config_value) <= 16
