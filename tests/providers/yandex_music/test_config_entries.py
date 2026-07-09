"""Unit tests for the provider config-flow entries (provider package init)."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest import mock

from music_assistant.providers.yandex_music import get_config_entries
from music_assistant.providers.yandex_music.constants import (
    CONF_ACTION_AUTH_DEVICE,
    CONF_SESSION_ID,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType


async def test_get_config_entries_label_prompts_save_after_auth() -> None:
    """
    After a successful login action, the label must warn about the Save step.

    Tokens live only in the dialog values until the user hits Save — a missed
    Save silently discards the whole login.
    """
    mock_mass = mock.MagicMock()
    values: dict[str, ConfigValueType] = {CONF_SESSION_ID: "sess-1"}

    with mock.patch(
        "music_assistant.providers.yandex_music.perform_device_auth",
        new=mock.AsyncMock(return_value=("x_tok", "music_tok", "refresh_tok")),
    ):
        entries = await get_config_entries(mock_mass, None, CONF_ACTION_AUTH_DEVICE, values)

    label = next(e for e in entries if e.key == "label_text")
    assert label.label is not None
    assert label.label.startswith("⚠")
    assert "Save" in label.label
