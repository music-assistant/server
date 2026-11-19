"""Rules for what the result of a provider's action may hand to a client."""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from music_assistant_models.enums import ProviderTier

from music_assistant.constants import MASS_LOGGER_NAME

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigActionResult

    from music_assistant.models.provider import Provider

LOGGER = logging.getLogger(f"{MASS_LOGGER_NAME}.action_result")


def without_insecure_open_url(provider: Provider, result: ConfigActionResult) -> ConfigActionResult:
    """
    Return the action result of a provider without its url when it may not be opened.

    The url of a community provider is only opened over https: otherwise this returns a copy of
    the result without it, with a warning in the log. A built-in provider keeps an http url, as
    it may open an address on the server itself. The result is returned as it is in any other
    case.

    :param provider: The provider instance that answered the action.
    :param result: The result it answered, which is never changed.
    """
    if not (
        result.open_url
        and provider.manifest.tier == ProviderTier.COMMUNITY
        and urlsplit(result.open_url).scheme.lower() != "https"
    ):
        return result
    LOGGER.warning(
        "Provider %s answered a url that is not https, which is not opened: %s",
        provider.instance_id,
        result.open_url,
    )
    return replace(result, open_url=None)
