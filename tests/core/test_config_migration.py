"""Tests for config migrations."""

from __future__ import annotations

import asyncio
import json
import pathlib

from music_assistant.controllers.config import ConfigController
from music_assistant.mass import MusicAssistant


async def test_zeroconf_interfaces_migrates_to_discovery_core(tmp_path: pathlib.Path) -> None:
    """Move zeroconf interface selection from players core config to discovery."""
    storage_path = tmp_path / "data"
    cache_path = tmp_path / "cache"
    storage_path.mkdir(parents=True)
    cache_path.mkdir(parents=True)
    settings_file = storage_path / "settings.json"
    settings_file.write_text(
        json.dumps(
            {
                "core": {
                    "players": {
                        "domain": "players",
                        "values": {"zeroconf_interfaces": "all"},
                    }
                }
            }
        )
    )

    mass = MusicAssistant(str(storage_path), str(cache_path))
    mass.loop = asyncio.get_running_loop()
    mass.loop_thread_id = getattr(mass.loop, "_thread_id", None) or id(mass.loop)

    config = ConfigController(mass)
    await config.setup()

    try:
        assert config.get_raw_core_config_value("discovery", "zeroconf_interfaces") == "all"
        assert config.get_raw_core_config_value("players", "zeroconf_interfaces") is None
    finally:
        await config.close()
