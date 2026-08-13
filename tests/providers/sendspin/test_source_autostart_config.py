"""Tests for the line-in autostart config entries on a Sendspin player."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

from music_assistant_models.enums import PlayerType

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
    unavailable_players: list[str] | None = None,
    non_audio_players: dict[str, PlayerType] | None = None,
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
    player.mass = cast(
        "MusicAssistant",
        SimpleNamespace(
            players=SimpleNamespace(
                all_players=lambda return_unavailable=True, *_args: [
                    SimpleNamespace(player_id=pid, display_name=pid, type=player_type)
                    for pid, player_type in [
                        *((pid, PlayerType.PLAYER) for pid in known_players),
                        *(
                            (pid, PlayerType.PLAYER)
                            for pid in (unavailable_players or [])
                            if return_unavailable
                        ),
                        *(non_audio_players or {}).items(),
                    ]
                ]
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


def test_an_unavailable_player_stays_selectable() -> None:
    """A target that is merely asleep must stay offered, not vanish from the list."""
    player = _player(
        negotiated_role_ids=["source@v1"],
        line_sense=True,
        known_players=["other-speaker"],
        unavailable_players=["sleeping-speaker"],
    )
    entry = _target_entry(player)
    assert entry is not None
    assert "sleeping-speaker" in {option.value for option in entry.options or []}


def test_players_that_cannot_render_audio_are_not_offered() -> None:
    """Lights, displays and other capture-only clients are nowhere to play a line-in."""
    player = _player(
        negotiated_role_ids=["source@v1"],
        line_sense=True,
        known_players=["speaker"],
        non_audio_players={
            "lamp": PlayerType.LIGHT,
            "screen": PlayerType.DISPLAY,
            "other-turntable": PlayerType.UNKNOWN,
        },
    )
    entry = _target_entry(player)
    assert entry is not None
    assert {option.value for option in entry.options or []} == {SOURCE_AUTOSTART_OFF, "speaker"}


def test_groups_and_stereo_pairs_are_offered() -> None:
    """A line-in must be routable to a group, not just to a single speaker."""
    player = _player(
        negotiated_role_ids=["source@v1"],
        line_sense=True,
        known_players=["speaker"],
        non_audio_players={"kitchen-group": PlayerType.GROUP, "pair": PlayerType.STEREO_PAIR},
    )
    entry = _target_entry(player)
    assert entry is not None
    assert {"kitchen-group", "pair"} <= {option.value for option in entry.options or []}
