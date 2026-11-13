"""Tests for the MilkDrop visualizer provider's viewer-facing API commands."""

from __future__ import annotations

import logging
from typing import Any, cast
from unittest.mock import Mock

from music_assistant.providers.milkdrop_visualizer.provider import (
    CAPABILITY_COMMAND,
    CONF_SHOW_ON_DASHBOARDS,
    CONFIG_COMMAND,
    MAX_REPORT_FIELD_LEN,
    MilkdropVisualizerProvider,
)


def _provider() -> tuple[MilkdropVisualizerProvider, Mock, Mock]:
    """Return a provider instance (with its logger and config-read mocks) without full setup."""
    provider = MilkdropVisualizerProvider.__new__(MilkdropVisualizerProvider)
    logger = Mock()
    mocked = cast("Any", provider)
    mocked.logger = logger
    mocked.config = Mock()
    mocked.mass = Mock()
    return provider, logger, mocked.config.get_value


async def test_loaded_in_mass_registers_nothing_while_unloading() -> None:
    """A stale instance must not take the live one's route or commands on its way out."""
    provider, _logger, _config_value = _provider()
    mocked = cast("Any", provider)
    mocked.unloading = True
    mocked._relay = Mock()
    mocked._unregister_handles = []

    await provider.loaded_in_mass()

    # the relay route unregisters by name too, and this instance is past its own close()
    mocked._relay.setup.assert_not_called()
    mocked.mass.register_api_command.assert_not_called()
    assert provider._unregister_handles == []


async def test_loaded_in_mass_registers_the_viewer_commands() -> None:
    """A live instance exposes both viewer-facing commands and keeps their unregister handles."""
    provider, _logger, _config_value = _provider()
    mocked = cast("Any", provider)
    mocked.unloading = False
    mocked._relay = Mock()
    mocked._unregister_handles = []

    await provider.loaded_in_mass()

    mocked._relay.setup.assert_called_once_with()
    registered = [call.args[0] for call in mocked.mass.register_api_command.call_args_list]
    assert registered == [CONFIG_COMMAND, CAPABILITY_COMMAND]
    assert len(provider._unregister_handles) == 2


async def test_visualizer_config_reflects_dashboard_setting() -> None:
    """The viewer config command reads show_on_dashboards from the provider config."""
    provider, _logger, config_value = _provider()
    config_value.return_value = True
    assert await provider.get_visualizer_config() == {CONF_SHOW_ON_DASHBOARDS: True}
    assert config_value.call_args.args == (CONF_SHOW_ON_DASHBOARDS,)
    config_value.return_value = False
    assert await provider.get_visualizer_config() == {CONF_SHOW_ON_DASHBOARDS: False}


async def test_failed_capability_report_is_logged_as_a_warning_with_trimmed_fields() -> None:
    """A report with an error is one warning line, and a viewer cannot forge or flood log lines."""
    provider, logger, _config_value = _provider()

    await provider.report_capability(
        webgl2=False,
        renderer="none",
        user_agent="x" * 900,
        gpu="gpu\nforged",
        error="boom\nViewer error: forged",
        dashboard_id="chromecast_abc",
    )

    logger.log.assert_called_once()
    assert logger.log.call_args.args[0] == logging.WARNING
    assert logger.log.call_args.args[2:] == (
        "chromecast_abc",
        False,
        "none",
        "gpu forged",
        "x" * MAX_REPORT_FIELD_LEN,
        "boom Viewer error: forged",
    )


async def test_capability_report_without_an_error_is_logged_at_debug() -> None:
    """A healthy report stays a debug line."""
    provider, logger, _config_value = _provider()

    await provider.report_capability(webgl2=True, renderer="milkdrop", dashboard_id="dash1")

    logger.log.assert_called_once()
    assert logger.log.call_args.args[0] == logging.DEBUG


async def test_capability_report_without_webgl2_is_logged_as_a_warning() -> None:
    """A display that cannot render is the point of the report, so it is not buried at debug."""
    provider, logger, _config_value = _provider()

    await provider.report_capability(webgl2=False, dashboard_id="dash1")

    assert logger.log.call_args.args[0] == logging.WARNING
