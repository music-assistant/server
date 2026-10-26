"""Unit tests for the AirPlay Receiver latency offset option and the rendered shairport-sync config."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import ConfigEntryType

from music_assistant.providers.airplay_receiver.provider import (
    CONF_LATENCY_OFFSET_MS,
    AirPlayReceiverProvider,
)


def _config_provider(values: dict[str, Any]) -> AirPlayReceiverProvider:
    """
    Build a bare provider that reads its config values from a plain dict.

    :param values: The stored config values, keyed by config key.
    """
    prov = AirPlayReceiverProvider.__new__(AirPlayReceiverProvider)
    prov.logger = MagicMock()
    prov.mass = MagicMock()
    prov.mass.players.get_player.return_value = None
    prov.mass.streams.get_source_ip = AsyncMock(return_value=None)
    prov.get_config_value = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda key, default=None, **_kwargs: values.get(key, default)
    )
    return prov


async def _render_config(tmp_path: Path, values: dict[str, Any]) -> str:
    """
    Write a receiver's shairport-sync config file and return its text.

    :param tmp_path: Directory to write the config file into.
    :param values: The stored config values, keyed by config key.
    """
    prov = _config_provider(values)
    daemon = MagicMock()
    daemon.player_id = "player-a"
    daemon.airplay_name = "Kitchen | Music Assistant"
    daemon.port = 7123
    daemon.audio_pipe.path = str(tmp_path / "audio")
    daemon.metadata_pipe.path = str(tmp_path / "metadata")
    daemon.config_file = str(tmp_path / "shairport-sync.conf")
    await prov._create_config_file(daemon)
    return Path(daemon.config_file).read_text(encoding="utf-8")


async def test_latency_offset_entry_is_an_advanced_bounded_integer() -> None:
    """The option is an optional advanced integer, default 0, bounded to +/- 1 second."""
    prov = _config_provider({})
    entries = {entry.key: entry for entry in await prov.get_config_entries()}

    entry = entries[CONF_LATENCY_OFFSET_MS]
    assert entry.type == ConfigEntryType.INTEGER
    assert entry.default_value == 0
    assert entry.range == (-1000, 1000)
    assert entry.advanced
    assert not entry.required


@pytest.mark.parametrize(
    ("values", "rendered"),
    [
        ({}, "0.000"),
        ({CONF_LATENCY_OFFSET_MS: -500}, "-0.500"),
        ({CONF_LATENCY_OFFSET_MS: 250}, "0.250"),
    ],
)
async def test_latency_offset_is_rendered_in_the_general_section(
    tmp_path: Path, values: dict[str, Any], rendered: str
) -> None:
    """The configured offset lands in the general section as seconds."""
    content = await _render_config(tmp_path, values)

    general = content.split("general =", 1)[1].split("};", 1)[0]
    assert f"audio_backend_latency_offset_in_seconds = {rendered};" in general
    assert "{LATENCY_OFFSET_SECONDS}" not in content
