"""Tests for the line-in autostart config entries on a Sendspin player."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

from music_assistant.providers.sendspin.constants import (
    CONF_SOURCE_AUTOSTART_TARGET,
    SOURCE_AUTOSTART_OFF,
)
from music_assistant.providers.sendspin.player import SendspinBasePlayer

if TYPE_CHECKING:
    from aiosendspin.server.client import SendspinClient
    from music_assistant_models.config_entries import ConfigEntry

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


def _player(
    *,
    negotiated_role_ids: list[str],
    line_sense: bool,
    known_players: list[str],
    stored_target: str | None = None,
) -> SendspinBasePlayer:
    player = SendspinBasePlayer.__new__(SendspinBasePlayer)
    player._player_id = "turntable"
    player._Player__attr_protocol_parent_id = None  # type: ignore[attr-defined]
    player.api = cast(
        "SendspinClient",
        SimpleNamespace(
            negotiated_role_ids=negotiated_role_ids,
            info_or_none=SimpleNamespace(
                source_support=SimpleNamespace(features=SimpleNamespace(line_sense=line_sense))
            ),
        ),
    )
    values = (
        {("turntable", CONF_SOURCE_AUTOSTART_TARGET): stored_target}
        if stored_target is not None
        else {}
    )
    player.mass = cast(
        "MusicAssistant",
        SimpleNamespace(
            players=SimpleNamespace(
                all_players=lambda *_args: [
                    SimpleNamespace(player_id=pid, display_name=pid) for pid in known_players
                ]
            ),
            config=SimpleNamespace(
                get_raw_player_config_value=lambda pid, key, default=None: values.get(
                    (pid, key), default
                )
            ),
        ),
    )
    return player


def _target_entry(player: SendspinBasePlayer) -> ConfigEntry | None:
    entries = player._get_source_autostart_config_entries()
    return next((e for e in entries if e.key == CONF_SOURCE_AUTOSTART_TARGET), None)


def test_no_autostart_entries_without_line_sense() -> None:
    """A source that cannot report signal presence has no trigger, so it gets no setting."""
    player = _player(negotiated_role_ids=["source@v1"], line_sense=False, known_players=["speaker"])
    assert player._get_source_autostart_config_entries() == []


def test_no_autostart_entries_for_a_client_without_a_source_role() -> None:
    """A plain speaker never shows line-in settings."""
    player = _player(negotiated_role_ids=["player@v1"], line_sense=True, known_players=["speaker"])
    assert player._get_source_autostart_config_entries() == []


def test_capture_only_source_defaults_to_off() -> None:
    """With no player of its own there is no sensible target, so autostart stays off."""
    player = _player(negotiated_role_ids=["source@v1"], line_sense=True, known_players=["speaker"])
    entry = _target_entry(player)
    assert entry is not None
    assert entry.default_value == SOURCE_AUTOSTART_OFF


def test_stored_target_survives_an_unavailable_player() -> None:
    """A target that is merely asleep must stay selected, not silently reset to off."""
    player = _player(
        negotiated_role_ids=["source@v1"],
        line_sense=True,
        known_players=["speaker"],
        stored_target="speaker",
    )
    entry = _target_entry(player)
    assert entry is not None
    assert entry.value == "speaker"


def test_target_pointing_at_a_deleted_player_falls_back() -> None:
    """A target that no longer exists at all cannot stay selected."""
    player = _player(
        negotiated_role_ids=["source@v1"],
        line_sense=True,
        known_players=["speaker"],
        stored_target="removed-speaker",
    )
    entry = _target_entry(player)
    assert entry is not None
    assert entry.value is None
