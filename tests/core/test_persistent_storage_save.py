"""
Regression tests for persistent settings storage durability (support issue #5716).

Users reported complete config loss after a power failure or unclean stop:
both ``settings.json`` AND ``settings.json.backup`` ended up as zero-length
files. The old save path renamed the live settings file to the backup and then
rewrote ``settings.json`` in place, so a single crash inside the write window
(or a failure during serialization) could destroy both generations at once.

These tests pin down the safe behavior:
- a failed save must leave the existing files on disk untouched
- an empty/corrupt settings file must never be rotated over a good backup
- a successful save rotates the previous file to the backup atomically
"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from music_assistant_models.enums import PlayerType

from music_assistant.constants import CONF_PLAYERS
from music_assistant.controllers.config import ConfigController


def _make_controller(tmp_path: Path) -> ConfigController:
    mass = SimpleNamespace(storage_path=str(tmp_path))
    return ConfigController(mass)  # type: ignore[arg-type]


async def test_save_rotates_previous_file_to_backup(tmp_path: Path) -> None:
    """A successful save keeps the previous generation as a valid backup."""
    controller = _make_controller(tmp_path)
    controller._data = {"generation": 1}
    await controller._async_save()
    controller._data = {"generation": 2}
    await controller._async_save()

    assert json.loads(Path(controller.filename).read_text()) == {"generation": 2}
    assert json.loads(Path(f"{controller.filename}.backup").read_text()) == {"generation": 1}
    assert not os.path.isfile(f"{controller.filename}.tmp")


async def test_failed_save_leaves_existing_files_untouched(tmp_path: Path) -> None:
    """A save that fails mid-write may not corrupt the files already on disk."""
    controller = _make_controller(tmp_path)
    controller._data = {"generation": 1}
    await controller._async_save()
    controller._data = {"generation": 2}
    await controller._async_save()

    controller._data = {"generation": 3}
    with (
        patch(
            "music_assistant.controllers.config.async_json_dumps",
            side_effect=RuntimeError("serialization failed"),
        ),
        pytest.raises(RuntimeError),
    ):
        await controller._async_save()

    assert json.loads(Path(controller.filename).read_text()) == {"generation": 2}
    assert json.loads(Path(f"{controller.filename}.backup").read_text()) == {"generation": 1}


async def test_empty_settings_file_does_not_clobber_backup(tmp_path: Path) -> None:
    """
    A zero-length settings file (crash leftover) must never replace a good backup.

    This is the exact scenario from issue #5716: after loading from the backup,
    the first save used to rotate the empty main file over the backup, making
    the loss permanent if anything went wrong before the new write completed.
    """
    controller = _make_controller(tmp_path)
    Path(controller.filename).write_text("")
    Path(f"{controller.filename}.backup").write_text(json.dumps({"recovered": True}))

    await controller._load()
    assert controller._data == {"recovered": True}

    await controller._async_save()

    assert json.loads(Path(controller.filename).read_text()) == {"recovered": True}
    assert json.loads(Path(f"{controller.filename}.backup").read_text()) == {"recovered": True}


async def test_corrupt_settings_file_does_not_clobber_backup(tmp_path: Path) -> None:
    """A non-empty but corrupt settings file (torn write) must never replace a good backup."""
    controller = _make_controller(tmp_path)
    Path(controller.filename).write_text('{"truncated": tr')
    Path(f"{controller.filename}.backup").write_text(json.dumps({"recovered": True}))

    await controller._load()
    assert controller._data == {"recovered": True}

    await controller._async_save()

    assert json.loads(Path(controller.filename).read_text()) == {"recovered": True}
    assert json.loads(Path(f"{controller.filename}.backup").read_text()) == {"recovered": True}


async def test_save_succeeds_when_directory_fsync_unsupported(tmp_path: Path) -> None:
    """The best-effort directory fsync may not fail the save on unsupported platforms."""
    controller = _make_controller(tmp_path)
    controller._data = {"generation": 1}
    with patch(
        "music_assistant.controllers.config.os.open",
        side_effect=OSError("fsync on directory not supported"),
    ):
        await controller._async_save()

    assert json.loads(Path(controller.filename).read_text()) == {"generation": 1}


async def test_player_config_summary_read_does_not_rewrite_settings(
    tmp_path: Path,
) -> None:
    """A config/players read must not dirty raw player config that is later saved."""
    mass = SimpleNamespace(storage_path=str(tmp_path), players=MagicMock())
    controller = ConfigController(mass)  # type: ignore[arg-type]
    controller.initialized = True
    player_id = "upe45f0170ef67"
    raw_config = {
        "player_id": player_id,
        "provider": "universal_player",
        "player_type": "player",
        "enabled": True,
        "name": "WC-Player",
        "default_name": "solarium-bath-sl",
        "values": {
            "hide_in_ui": True,
            "announce_volume_min": 55,
            "announce_volume_max": 98,
            "play_media_overrides_group": False,
            "linked_protocol_ids": ["e4:5f:01:70:ef:67"],
        },
    }
    controller._data = {CONF_PLAYERS: {player_id: deepcopy(raw_config)}}
    live_player = SimpleNamespace(
        state=SimpleNamespace(
            name="solarium-bath-sl",
            available=False,
            type=PlayerType.PLAYER,
        )
    )
    mass.players.get_player.return_value = live_player

    configs = await controller.get_player_configs(include_values=False)
    await controller._async_save()

    assert configs[0].default_name == "solarium-bath-sl"
    assert controller._data[CONF_PLAYERS][player_id] == raw_config
    saved = json.loads(Path(controller.filename).read_text())
    assert saved[CONF_PLAYERS][player_id] == raw_config
