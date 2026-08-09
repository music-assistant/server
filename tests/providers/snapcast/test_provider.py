"""Tests for the Snapcast provider."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from music_assistant.constants import CONF_LOG_LEVEL, VERBOSE_LOG_LEVEL
from music_assistant.models.player_provider import PlayerProvider
from music_assistant.providers.snapcast.provider import SnapCastProvider


def _provider_with_log_level(level: int) -> SnapCastProvider:
    provider = SnapCastProvider.__new__(SnapCastProvider)
    provider.logger = logging.getLogger("snapcast-provider-test")
    provider.logger.setLevel(level)
    return provider


@pytest.mark.parametrize(
    ("provider_level", "snapcast_level"),
    [
        (logging.INFO, logging.WARNING),
        (logging.DEBUG, logging.INFO),
        (VERBOSE_LOG_LEVEL, logging.DEBUG),
    ],
)
def test_snapcast_library_log_level(provider_level: int, snapcast_level: int) -> None:
    """Snapcast logging stays quieter than the provider unless verbose logging is enabled."""
    provider = _provider_with_log_level(provider_level)

    with patch("music_assistant.providers.snapcast.provider.logging.getLogger") as get_logger:
        provider._set_snapcast_log_level()

    get_logger.assert_called_once_with("snapcast")
    get_logger.return_value.setLevel.assert_called_once_with(snapcast_level)


async def test_log_level_config_update_realigns_snapcast_logger() -> None:
    """A log-level-only config update immediately realigns Snapcast logging."""
    provider = _provider_with_log_level(logging.INFO)
    config = MagicMock()
    changed_keys = {f"values/{CONF_LOG_LEVEL}"}

    with (
        patch.object(PlayerProvider, "update_config", new_callable=AsyncMock) as update_config,
        patch.object(SnapCastProvider, "_set_snapcast_log_level") as set_log_level,
    ):
        await provider.update_config(config, changed_keys)

    update_config.assert_awaited_once_with(config, changed_keys)
    set_log_level.assert_called_once_with()


def test_lost_connection_arms_the_reload_under_the_load_task_id() -> None:
    """A lost SnapServer connection arms the reload so a (re)load starting first cancels it."""
    provider = _provider_with_log_level(logging.INFO)
    provider.config = MagicMock(instance_id="snapcast--test")
    provider.mass = MagicMock(closing=False)
    provider._stop_called = False

    provider._handle_disconnect(ConnectionError("connection lost"))

    retry = provider.mass.call_later.call_args
    assert retry.args == (5, provider.mass.load_provider, "snapcast--test")
    assert retry.kwargs == {"allow_retry": True, "task_id": "load_provider_snapcast--test"}
