"""Regression tests for persisted MSX Bridge settings."""

from copy import deepcopy
from typing import Any

import pytest

from music_assistant.controllers.config.migrations import migrate


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("legacy_value", [True, False, None])
async def test_msx_bridge_stored_settings_migrate(enabled: bool, legacy_value: bool | None) -> None:
    """Disabled instances and falsy removed settings migrate before provider loading."""
    data: dict[str, Any] = {
        "providers": {
            "msx_bridge_instance": {
                "domain": "msx_bridge",
                "enabled": enabled,
                "values": {
                    "enable_player_grouping": legacy_value,
                    "group_stream_mode": "shared",
                    "port": 8099,
                },
            },
            "other": {
                "domain": "other",
                "values": {
                    "enable_player_grouping": False,
                    "group_stream_mode": "shared",
                },
            },
        }
    }
    expected = deepcopy(data)
    expected["providers"]["msx_bridge_instance"]["values"] = {
        "group_stream_mode": "independent",
        "port": 8099,
    }
    assert await migrate(data) is True
    assert data == expected
    assert await migrate(data) is False
    assert data == expected


@pytest.mark.parametrize(
    "values", [{}, {"group_stream_mode": "redirect"}, {"group_stream_mode": "independent"}, None]
)
async def test_current_msx_settings_are_unchanged(values: Any) -> None:
    """Migration preserves current modes and missing values without writing settings."""
    data = {"providers": {"msx": {"domain": "msx_bridge", "values": values}}}
    expected = deepcopy(data)
    assert await migrate(data) is False
    assert data == expected
