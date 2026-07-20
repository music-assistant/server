"""Tests for DSP configuration and preset persistence."""

from __future__ import annotations

import base64
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.dsp import DSPConfig, DSPConfigPreset, ToneControlFilter
from music_assistant_models.enums import EventType
from music_assistant_models.errors import InvalidDataError

from music_assistant.controllers.config.dsp import MAX_IR_BYTES, DSPConfigMixin

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


class _DSPConfigStore(DSPConfigMixin):
    """In-memory DSP configuration store for focused controller tests."""

    def __init__(self) -> None:
        """Initialize the store and controller dependencies."""
        self._data: dict[str, Any] = {}
        self.update_player_dsp_preset = MagicMock()
        self.signal_event = MagicMock()
        self.mass = cast(
            "MusicAssistant",
            SimpleNamespace(
                players=SimpleNamespace(on_player_dsp_change=AsyncMock()),
                streams=SimpleNamespace(
                    audio_processing=SimpleNamespace(
                        update_player_dsp_preset=self.update_player_dsp_preset
                    )
                ),
                signal_event=self.signal_event,
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


def _wav_bytes(tmp_path: Path) -> bytes:
    """Generate a short stereo wav and return its raw bytes."""
    wav_path = tmp_path / "source.wav"
    subprocess.run(  # noqa: S603
        [  # noqa: S607
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=1000:duration=0.1:sample_rate=48000",
            "-ac",
            "2",
            str(wav_path),
        ],
        check=True,
        capture_output=True,
    )
    return wav_path.read_bytes()


async def test_upload_list_and_remove_ir(tmp_path: Path) -> None:
    """An uploaded impulse response is stored, listed and then removed."""
    config = _DSPConfigStore()
    config.mass.storage_path = str(tmp_path)

    data = base64.b64encode(_wav_bytes(tmp_path)).decode()
    record = await config.upload_dsp_ir("My Room", data)

    ir_id = record["ir_id"]
    assert record["name"] == "My Room"
    assert record["sample_rate"] == 48000
    assert record["channels"] == 2
    assert (tmp_path / "dsp_irs" / f"{ir_id}.wav").is_file()
    assert config.get_dsp_irs() == [record]

    await config.remove_dsp_ir(ir_id)
    assert config.get_dsp_irs() == []
    assert not (tmp_path / "dsp_irs" / f"{ir_id}.wav").exists()


async def test_ir_changes_signal_event(tmp_path: Path) -> None:
    """Uploading or removing an impulse response announces the resulting library."""
    config = _DSPConfigStore()
    config.mass.storage_path = str(tmp_path)

    data = base64.b64encode(_wav_bytes(tmp_path)).decode()
    record = await config.upload_dsp_ir("My Room", data)
    config.signal_event.assert_called_once_with(EventType.DSP_IRS_UPDATED, data=[record])

    config.signal_event.reset_mock()
    await config.remove_dsp_ir(record["ir_id"])
    config.signal_event.assert_called_once_with(EventType.DSP_IRS_UPDATED, data=[])


async def test_remove_unused_ir_signals_event(tmp_path: Path) -> None:
    """Removing an IR that no player or preset uses still announces the change."""
    config = _DSPConfigStore()
    config.mass.storage_path = str(tmp_path)
    await config.save_dsp_config("player-1", DSPConfig(enabled=True, output_gain=-3.0))

    data = base64.b64encode(_wav_bytes(tmp_path)).decode()
    unused = await config.upload_dsp_ir("Unused", data)
    config.signal_event.reset_mock()

    await config.remove_dsp_ir(unused["ir_id"])

    config.signal_event.assert_called_once_with(EventType.DSP_IRS_UPDATED, data=[])


async def test_upload_ir_rejects_non_audio(tmp_path: Path) -> None:
    """Uploading data that is not decodable audio raises and stores nothing."""
    config = _DSPConfigStore()
    config.mass.storage_path = str(tmp_path)

    data = base64.b64encode(b"this is not an audio file").decode()
    with pytest.raises(InvalidDataError):
        await config.upload_dsp_ir("bad", data)

    assert config.get_dsp_irs() == []
    # the transcode failure must not leave a stored wav or upload temp file behind
    assert list((tmp_path / "dsp_irs").glob("*")) == []


@pytest.mark.parametrize("evil_id", ["../evil", "../../etc/cron.d/x", "/etc/passwd", "a/b"])
async def test_remove_ir_rejects_path_traversal(tmp_path: Path, evil_id: str) -> None:
    """A crafted ir_id cannot delete a file outside the impulse response directory."""
    config = _DSPConfigStore()
    config.mass.storage_path = str(tmp_path)
    # a sentinel file the traversal ids above would resolve to if unguarded
    sentinel = tmp_path / "evil.wav"
    sentinel.write_text("keep me")

    with pytest.raises(InvalidDataError):
        await config.remove_dsp_ir(evil_id)

    assert sentinel.exists()


async def test_upload_ir_rejects_oversized_file(tmp_path: Path) -> None:
    """An upload larger than the size limit is rejected before it is written."""
    config = _DSPConfigStore()
    config.mass.storage_path = str(tmp_path)

    oversized = base64.b64encode(b"\x00" * (MAX_IR_BYTES + 1)).decode()
    with pytest.raises(InvalidDataError):
        await config.upload_dsp_ir("too big", oversized)

    assert config.get_dsp_irs() == []
