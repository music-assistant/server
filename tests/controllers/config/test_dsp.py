"""Tests for DSP configuration and preset persistence."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.dsp import DSPConfig, DSPConfigPreset, ToneControlFilter

from music_assistant.controllers.config.dsp import DSPConfigMixin

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


class _DSPConfigStore(DSPConfigMixin):
    """In-memory DSP configuration store for focused controller tests."""

    def __init__(self) -> None:
        """Initialize the store and controller dependencies."""
        self._data: dict[str, Any] = {}
        self.update_player_dsp_preset = MagicMock()
        self.mass = cast(
            "MusicAssistant",
            SimpleNamespace(
                players=SimpleNamespace(on_player_dsp_change=AsyncMock()),
                streams=SimpleNamespace(
                    audio_processing=SimpleNamespace(
                        update_player_dsp_preset=self.update_player_dsp_preset
                    )
                ),
                signal_event=MagicMock(),
            ),
        )

    def get(self, key: str, default: Any = None) -> Any:
        """Return a value from the nested test store."""
        value: Any = self._data
        for subkey in key.split("/"):
            if not isinstance(value, dict) or subkey not in value:
                return default
            value = value[subkey]
        return value

    def set(self, key: str, value: Any) -> None:
        """Set a value in the nested test store."""
        parent = self._data
        subkeys = key.split("/")
        for subkey in subkeys[:-1]:
            parent = parent.setdefault(subkey, {})
        parent[subkeys[-1]] = value

    def remove(self, key: str) -> None:
        """Remove a value from the nested test store."""
        parent = self._data
        subkeys = key.split("/")
        for subkey in subkeys[:-1]:
            if subkey not in parent:
                return
            parent = parent[subkey]
        parent.pop(subkeys[-1], None)


async def test_apply_preset_and_manual_save_reset_identity() -> None:
    """Preset application persists identity and a manual save clears it."""
    config = _DSPConfigStore()
    preset = await config.save_dsp_presets(
        DSPConfigPreset(
            name="Warm",
            preset_id="warm",
            config=DSPConfig(
                enabled=True,
                filters=[ToneControlFilter(enabled=True, bass_level=2.0)],
                preset_id="other",
            ),
        )
    )

    applied = await config.apply_dsp_preset("player-1", "warm")

    assert preset.config.preset_id is None
    assert applied.preset_id == "warm"
    assert config.get_player_dsp_config("player-1") == applied
    applied.input_gain = -1.5
    saved = await config.save_dsp_config("player-1", applied)
    assert saved.preset_id is None
    assert config.get_player_dsp_config("player-1").preset_id is None


async def test_apply_missing_preset_fails() -> None:
    """Applying an unknown preset reports invalid input."""
    config = _DSPConfigStore()

    with pytest.raises(KeyError, match="missing"):
        await config.apply_dsp_preset("player-1", "missing")


async def test_preset_setting_update_clears_assignments() -> None:
    """Changing preset settings clears selection without changing player DSP."""
    config = _DSPConfigStore()
    original = DSPConfig(enabled=False, input_gain=-2.0)
    await config.save_dsp_presets(DSPConfigPreset(name="Quiet", preset_id="quiet", config=original))
    await config.apply_dsp_preset("player-1", "quiet")

    await config.save_dsp_presets(
        DSPConfigPreset(
            name="Quieter",
            preset_id="quiet",
            config=DSPConfig(enabled=False, input_gain=-4.0),
        )
    )

    player_config = config.get_player_dsp_config("player-1")
    assert player_config.input_gain == -2.0
    assert player_config.preset_id is None
    config.update_player_dsp_preset.assert_called_with("player-1", None)


async def test_preset_rename_preserves_assignments() -> None:
    """Renaming a preset keeps matching player selections."""
    config = _DSPConfigStore()
    preset_config = DSPConfig(enabled=False, output_gain=-1.0)
    await config.save_dsp_presets(
        DSPConfigPreset(name="Original", preset_id="named", config=preset_config)
    )
    await config.apply_dsp_preset("player-1", "named")

    await config.save_dsp_presets(
        DSPConfigPreset(name="Renamed", preset_id="named", config=preset_config)
    )

    assert config.get_player_dsp_config("player-1").preset_id == "named"


async def test_remove_preset_clears_assignments() -> None:
    """Removing a preset keeps copied values but clears its selection."""
    config = _DSPConfigStore()
    await config.save_dsp_presets(
        DSPConfigPreset(
            name="Night",
            preset_id="night",
            config=DSPConfig(enabled=False, output_gain=-3.0),
        )
    )
    await config.apply_dsp_preset("player-1", "night")

    await config.remove_dsp_preset("night")

    player_config = config.get_player_dsp_config("player-1")
    assert player_config.output_gain == -3.0
    assert player_config.preset_id is None
    assert await config.get_dsp_presets() == []
