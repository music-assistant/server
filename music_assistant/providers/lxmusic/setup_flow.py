"""Setup flow for the LX Music provider."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from music_assistant.models.setup_flow import SetupFlowError

from . import (
    CONF_DEFAULT_SOURCE,
    CONF_PASSWORD,
    CONF_SEARCH_SOURCES,
    CONF_SERVER_URL,
    CONF_USERNAME,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Any

    from music_assistant.models.setup_flow import SetupSession

_LOGGER = logging.getLogger(__name__)


def _existing_str(existing: Mapping[str, Any], key: str, default: str) -> str:
    """
    Return a previously collected setup value, or its default.

    :param existing: The stored ``setup_data`` mapping (may be empty).
    :param key: Config key to read.
    :param default: Fallback value when the key is absent or blank.
    """
    return str(existing.get(key) or default)


async def run_setup(session: SetupSession) -> None:
    """
    Collect the lxserver URL, credentials and source options.

    All user-facing text is resolved from the provider's ``strings.json``
    (``config_entries.<key>`` and ``setup_flow.user``), so the entries below
    carry no literal labels or descriptions.
    """
    existing = session.context.setup_data or {}
    values = await session.form(
        [
            ConfigEntry(
                key=CONF_SERVER_URL,
                type=ConfigEntryType.STRING,
                required=True,
                default_value=_existing_str(existing, CONF_SERVER_URL, "http://localhost:9527"),
            ),
            ConfigEntry(
                key=CONF_USERNAME,
                type=ConfigEntryType.STRING,
                required=True,
                default_value=_existing_str(existing, CONF_USERNAME, "admin"),
            ),
            ConfigEntry(
                key=CONF_PASSWORD,
                type=ConfigEntryType.SECURE_STRING,
                required=True,
                default_value=_existing_str(existing, CONF_PASSWORD, ""),
            ),
            ConfigEntry(
                key=CONF_DEFAULT_SOURCE,
                type=ConfigEntryType.STRING,
                required=False,
                default_value=_existing_str(existing, CONF_DEFAULT_SOURCE, "wy"),
            ),
            ConfigEntry(
                key=CONF_SEARCH_SOURCES,
                type=ConfigEntryType.STRING,
                required=False,
                default_value=_existing_str(existing, CONF_SEARCH_SOURCES, "kw,kg,tx,wy,mg"),
            ),
        ],
        step_id="user",
    )
    # Log the password length only: the plaintext would otherwise stay in the
    # log files forever.
    _LOGGER.debug(
        "lxmusic setup flow collected server_url=%r username=%r password_len=%s",
        values.get(CONF_SERVER_URL),
        values.get(CONF_USERNAME),
        len(str(values.get(CONF_PASSWORD) or "")),
    )
    try:
        await session.finish(
            {
                CONF_SERVER_URL: str(values[CONF_SERVER_URL]).strip().rstrip("/"),
                CONF_USERNAME: str(values[CONF_USERNAME]).strip(),
                CONF_PASSWORD: str(values.get(CONF_PASSWORD) or ""),
                CONF_DEFAULT_SOURCE: str(values.get(CONF_DEFAULT_SOURCE) or "wy").strip(),
                CONF_SEARCH_SOURCES: str(
                    values.get(CONF_SEARCH_SOURCES) or "kw,kg,tx,wy,mg"
                ).strip(),
            }
        )
    except SetupFlowError:
        # The config flow runner swallows setup-flow exceptions and only
        # publishes an abort to the UI, so log here to keep the cause findable.
        _LOGGER.exception("lxmusic setup flow failed to finish")
        raise
