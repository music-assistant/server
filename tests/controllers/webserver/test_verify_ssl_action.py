"""Tests for the webserver's Verify SSL config action."""

from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock

import pytest
from music_assistant_models.errors import InvalidDataError

from music_assistant.controllers.webserver.controller import (
    CONF_ACTION_VERIFY_SSL,
    CONF_SSL_CERTIFICATE,
    WebserverController,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import CoreConfig


@pytest.mark.parametrize("certificate", [None, "/ssl/fullchain.pem"])
async def test_verify_ssl_asks_to_save_when_not_saved(certificate: str | None) -> None:
    """Verify asks to save first when the certificate or key is not saved yet."""
    mass = MagicMock()
    mass.config.get_raw_core_config_value.return_value = "GLOBAL"
    webserver = WebserverController(mass)
    # an unsaved entry is stored as None, not left out
    values = {CONF_SSL_CERTIFICATE: certificate}
    config = MagicMock()
    config.get_value.side_effect = lambda key, _default=None: values.get(key)
    webserver.config = cast("CoreConfig", config)

    with pytest.raises(InvalidDataError) as err:
        await webserver.handle_config_action(CONF_ACTION_VERIFY_SSL)

    assert err.value.translation_key == "ssl_not_saved"
