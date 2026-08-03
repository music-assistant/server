"""Tests for DSP configuration and preset persistence."""

from __future__ import annotations

import base64
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.dsp import (
    ConvolutionFilter,
    DSPConfig,
    DSPConfigPreset,
    ToneControlFilter,
)
from music_assistant_models.enums import EventType
from music_assistant_models.errors import InvalidDataError

from music_assistant.controllers.config.dsp import (
    MAX_IR_BYTES,
    MAX_IR_SECONDS,
    DSPConfigMixin,
)

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


class _DSPConfigStore(DSPConfigMixin):
    """In-memory DSP configuration store for focused controller tests."""

    def __init__(self) -> None:
        """Initialize the store and controller dependencies."""
        self._data: dict[str, Any] = {}
        self.update_player_dsp_preset = MagicMock()
        self.signal_event = MagicMock()
        self.on_player_dsp_change = AsyncMock()
        self.mass = cast(
            "MusicAssistant",
            SimpleNamespace(
                players=SimpleNamespace(on_player_dsp_change=self.on_player_dsp_change),
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


def _wav_bytes(tmp_path: Path, channels: int = 2, duration: float = 0.1) -> bytes:
    """Generate a short wav with the given channel count and return its raw bytes."""
    wav_path = tmp_path / f"source_{channels}ch_{duration}s.wav"
    subprocess.run(  # noqa: S603
        [  # noqa: S607
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=1000:duration={duration}:sample_rate=48000",
            "-ac",
            str(channels),
            str(wav_path),
        ],
        check=True,
        capture_output=True,
    )
    return wav_path.read_bytes()


def _silent_flac_bytes(tmp_path: Path, duration: float) -> bytes:
    """Generate a silent flac of the given length, which compresses to very little."""
    flac_path = tmp_path / f"silence_{duration}s.flac"
    subprocess.run(  # noqa: S603
        [  # noqa: S607
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=48000:cl=stereo",
            "-t",
            str(duration),
            "-c:a",
            "flac",
            str(flac_path),
        ],
        check=True,
        capture_output=True,
    )
    return flac_path.read_bytes()


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


async def test_upload_ir_rejects_multichannel_file(tmp_path: Path) -> None:
    """A file with more than two channels is rejected rather than silently downmixed."""
    config = _DSPConfigStore()
    config.mass.storage_path = str(tmp_path)

    data = base64.b64encode(_wav_bytes(tmp_path, channels=4)).decode()
    with pytest.raises(InvalidDataError, match="only mono and stereo"):
        await config.upload_dsp_ir("true stereo", data)

    assert config.get_dsp_irs() == []
    assert list((tmp_path / "dsp_irs").glob("*")) == []


async def test_upload_ir_rejects_long_file(tmp_path: Path) -> None:
    """An impulse response longer than the limit is rejected, as afir would fail on it."""
    config = _DSPConfigStore()
    config.mass.storage_path = str(tmp_path)

    data = base64.b64encode(_wav_bytes(tmp_path, duration=MAX_IR_SECONDS + 1)).decode()
    with pytest.raises(InvalidDataError, match="seconds"):
        await config.upload_dsp_ir("too long", data)

    assert config.get_dsp_irs() == []
    assert list((tmp_path / "dsp_irs").glob("*")) == []


def _encoded_bytes(tmp_path: Path, name: str, *codec_args: str) -> bytes:
    """Re-encode a short sine with the given ffmpeg codec arguments."""
    out_path = tmp_path / name
    subprocess.run(  # noqa: S603
        [  # noqa: S607
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=1000:duration=0.5:sample_rate=48000",
            "-ac",
            "2",
            *codec_args,
            str(out_path),
        ],
        check=True,
        capture_output=True,
    )
    return out_path.read_bytes()


@pytest.mark.parametrize(
    ("name", "codec_args"),
    [
        ("ir.mp3", ("-c:a", "libmp3lame")),
        ("ir.m4a", ("-c:a", "aac")),
        ("ir.ogg", ("-c:a", "libvorbis")),
    ],
)
async def test_upload_ir_rejects_lossy_codec(
    tmp_path: Path, name: str, codec_args: tuple[str, ...]
) -> None:
    """A lossy encode smears the impulse, so it is refused rather than silently applied."""
    config = _DSPConfigStore()
    config.mass.storage_path = str(tmp_path)

    data = base64.b64encode(_encoded_bytes(tmp_path, name, *codec_args)).decode()
    with pytest.raises(InvalidDataError, match="only lossless"):
        await config.upload_dsp_ir("lossy", data)

    assert config.get_dsp_irs() == []
    assert list((tmp_path / "dsp_irs").glob("*")) == []


@pytest.mark.parametrize(
    ("name", "codec_args"),
    [
        ("ir.flac", ("-c:a", "flac")),
        ("ir.aiff", ("-c:a", "pcm_s16be")),
    ],
)
async def test_upload_ir_accepts_lossless_container(
    tmp_path: Path, name: str, codec_args: tuple[str, ...]
) -> None:
    """A lossless container other than wav is accepted, so the check is not wav-only."""
    config = _DSPConfigStore()
    config.mass.storage_path = str(tmp_path)

    data = base64.b64encode(_encoded_bytes(tmp_path, name, *codec_args)).decode()
    record = await config.upload_dsp_ir("Room", data)

    assert config.get_dsp_irs() == [record]


async def test_upload_ir_transcode_is_length_bounded(tmp_path: Path) -> None:
    """A tiny but very long upload is truncated on the way in, not written out in full."""
    config = _DSPConfigStore()
    config.mass.storage_path = str(tmp_path)

    data = base64.b64encode(_silent_flac_bytes(tmp_path, duration=3600)).decode()
    with pytest.raises(InvalidDataError) as excinfo:
        await config.upload_dsp_ir("very long", data)

    # an unbounded transcode would report the source length, after writing gigabytes
    reported = re.search(r"is ([\d.]+) seconds", str(excinfo.value))
    assert reported is not None
    assert float(reported.group(1)) <= MAX_IR_SECONDS + 1

    assert list((tmp_path / "dsp_irs").glob("*")) == []


async def test_upload_ir_cleans_up_after_an_unexpected_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure the upload does not anticipate still leaves no orphaned file behind."""
    config = _DSPConfigStore()
    config.mass.storage_path = str(tmp_path)
    monkeypatch.setattr(
        "music_assistant.controllers.config.dsp.async_parse_tags",
        AsyncMock(side_effect=RuntimeError("boom")),
    )

    data = base64.b64encode(_wav_bytes(tmp_path)).decode()
    with pytest.raises(RuntimeError):
        await config.upload_dsp_ir("broken", data)

    assert config.get_dsp_irs() == []
    assert list((tmp_path / "dsp_irs").glob("*")) == []


async def test_remove_ir_with_nothing_to_remove_is_a_no_op(tmp_path: Path) -> None:
    """An id with neither a record nor a file leaves the library untouched and silent."""
    config = _DSPConfigStore()
    config.mass.storage_path = str(tmp_path)

    await config.remove_dsp_ir("abc123")

    config.signal_event.assert_not_called()


async def test_remove_ir_drops_record_with_unusable_id(tmp_path: Path) -> None:
    """A stored id that no longer passes validation can still be removed from the library."""
    config = _DSPConfigStore()
    config.mass.storage_path = str(tmp_path)
    config.set("player_dsp_irs", {"Legacy ID": {"ir_id": "Legacy ID", "name": "old"}})

    await config.remove_dsp_ir("Legacy ID")

    assert config.get_dsp_irs() == []


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


async def test_save_dsp_config_rejects_unknown_ir() -> None:
    """A player config naming an impulse response this server does not hold is refused."""
    config = _DSPConfigStore()

    with pytest.raises(InvalidDataError, match="Unknown impulse response"):
        await config.save_dsp_config(
            "player-1",
            DSPConfig(enabled=True, filters=[ConvolutionFilter(enabled=True, ir_id="gone")]),
        )

    assert config.get_player_dsp_config("player-1").filters == []


async def test_save_dsp_preset_rejects_unknown_ir() -> None:
    """A preset naming an impulse response this server does not hold is refused."""
    config = _DSPConfigStore()

    with pytest.raises(InvalidDataError, match="Unknown impulse response"):
        await config.save_dsp_presets(
            DSPConfigPreset(
                name="Room",
                preset_id="room",
                config=DSPConfig(
                    enabled=True, filters=[ConvolutionFilter(enabled=True, ir_id="gone")]
                ),
            )
        )

    assert await config.get_dsp_presets() == []


async def test_save_dsp_config_allows_blank_ir() -> None:
    """A convolution filter with nothing selected yet still saves, as removal leaves it blank."""
    config = _DSPConfigStore()

    saved = await config.save_dsp_config(
        "player-1",
        DSPConfig(enabled=True, filters=[ConvolutionFilter(enabled=True, ir_id="")]),
    )

    saved_filter = saved.filters[0]
    assert isinstance(saved_filter, ConvolutionFilter)
    assert saved_filter.ir_id == ""


async def test_remove_ir_clears_references(tmp_path: Path) -> None:
    """Removing an IR blanks its id from any player config or preset that used it."""
    config = _DSPConfigStore()
    config.mass.storage_path = str(tmp_path)
    config.set("player_dsp_irs", {"abc123": {"ir_id": "abc123", "name": "Room"}})
    await config.save_dsp_config(
        "player-1",
        DSPConfig(
            enabled=True,
            filters=[ConvolutionFilter(enabled=True, ir_id="abc123", gain=2.0)],
        ),
    )
    await config.save_dsp_presets(
        DSPConfigPreset(
            name="Room",
            preset_id="room",
            config=DSPConfig(
                enabled=True,
                filters=[ConvolutionFilter(enabled=True, ir_id="abc123")],
            ),
        )
    )

    await config.remove_dsp_ir("abc123")

    player_filter = config.get_player_dsp_config("player-1").filters[0]
    assert isinstance(player_filter, ConvolutionFilter)
    assert player_filter.ir_id == ""
    assert player_filter.gain == 2.0
    preset_filter = (await config.get_dsp_presets())[0].config.filters[0]
    assert isinstance(preset_filter, ConvolutionFilter)
    assert preset_filter.ir_id == ""


async def test_remove_ir_rebuilds_the_stream_of_an_affected_player(tmp_path: Path) -> None:
    """Blanking a convolution filter reapplies the DSP, as a saved config change would."""
    config = _DSPConfigStore()
    config.mass.storage_path = str(tmp_path)
    config.set("player_dsp_irs", {"abc123": {"ir_id": "abc123", "name": "Room"}})
    await config.save_dsp_config(
        "player-1",
        DSPConfig(enabled=True, filters=[ConvolutionFilter(enabled=True, ir_id="abc123")]),
    )
    await config.save_dsp_config("player-2", DSPConfig(enabled=True, output_gain=-3.0))
    config.on_player_dsp_change.reset_mock()

    await config.remove_dsp_ir("abc123")

    config.on_player_dsp_change.assert_awaited_once_with("player-1")


async def test_remove_ir_leaves_a_disabled_player_dsp_alone(tmp_path: Path) -> None:
    """A player with DSP switched off hears nothing different, so it is not restarted."""
    config = _DSPConfigStore()
    config.mass.storage_path = str(tmp_path)
    config.set("player_dsp_irs", {"abc123": {"ir_id": "abc123", "name": "Room"}})
    await config.save_dsp_config(
        "player-1",
        DSPConfig(enabled=False, filters=[ConvolutionFilter(enabled=True, ir_id="abc123")]),
    )
    config.on_player_dsp_change.reset_mock()

    await config.remove_dsp_ir("abc123")

    config.on_player_dsp_change.assert_not_awaited()
