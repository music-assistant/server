"""Unit tests for unavailable output protocols being offered disabled, not omitted."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from music_assistant_models.player import OutputProtocol

from music_assistant import constants as _constants
from music_assistant.constants import CONF_ENABLED, CONF_PLAYERS, CONF_PREFERRED_OUTPUT_PROTOCOL
from music_assistant.mass import MusicAssistant

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption

# the common strings live next to the constants module, so this path holds from anywhere
_STRINGS_PATH = Path(_constants.__file__).resolve().parent / "strings.json"

_AIRPLAY_ID = "airplay_aabbccddeeff"
_DLNA_ID = "dlna_aabbccddeeff"


def _make_output_protocol(
    output_protocol_id: str, domain: str, priority: int, *, available: bool
) -> OutputProtocol:
    """Return an output protocol entry for a linked protocol player."""
    return OutputProtocol(
        output_protocol_id=output_protocol_id,
        name=domain.title(),
        protocol_domain=domain,
        priority=priority,
        available=available,
    )


def _make_protocol_player(*, available: bool, needs_setup: bool) -> MagicMock:
    """Return a protocol player mock in the given availability/setup state."""
    player = MagicMock()
    player.available = available
    player.needs_setup = needs_setup
    player.available_for_playback = available and not needs_setup
    return player


def _make_provider_manifest(domain: str) -> MagicMock:
    """Return a provider manifest mock named after its domain."""
    manifest = MagicMock()
    manifest.name = domain.title()
    return manifest


async def _preferred_output_entry(
    mass: MusicAssistant,
    protocols: list[OutputProtocol],
    protocol_player: MagicMock | None = None,
) -> ConfigEntry:
    """Build the preferred-output-protocol entry for a player with the given outputs."""
    player = MagicMock()
    player.needs_setup = False
    player.output_protocols = protocols
    mass.players = MagicMock()
    mass.players.get_player.return_value = protocol_player
    # the entries name each protocol after its provider, which mass_minimal does not load
    with patch.object(mass, "get_provider_manifest", side_effect=_make_provider_manifest):
        entries = await mass.config._create_output_protocol_config_entries(player)
    return next(entry for entry in entries if entry.key == CONF_PREFERRED_OUTPUT_PROTOCOL)


def _option(entry: ConfigEntry, value: str) -> ConfigValueOption:
    """Return the entry's option for the given value."""
    return next(option for option in entry.options if option.value == value)


def _assert_disabled_with_reason(option: ConfigValueOption, reason: str) -> None:
    """Assert the option is disabled for the given reason, and that the reason has a string."""
    assert option.disabled is True
    assert option.translation_key == reason
    # the reason is only rendered when it is authored, so guard against drift
    strings = json.loads(_STRINGS_PATH.read_text(encoding="utf-8"))
    assert reason in strings["config_entries"][CONF_PREFERRED_OUTPUT_PROTOCOL]["disabled_reasons"]


async def test_output_that_needs_setup_is_offered_disabled(mass_minimal: MusicAssistant) -> None:
    """An output awaiting setup stays listed, disabled, and says why."""
    entry = await _preferred_output_entry(
        mass_minimal,
        [
            _make_output_protocol(_AIRPLAY_ID, "airplay", 10, available=False),
            _make_output_protocol(_DLNA_ID, "dlna", 50, available=True),
        ],
        _make_protocol_player(available=True, needs_setup=True),
    )
    _assert_disabled_with_reason(_option(entry, _AIRPLAY_ID), "needs_setup")
    assert _option(entry, _DLNA_ID).disabled is False


async def test_offline_output_reports_unavailable(mass_minimal: MusicAssistant) -> None:
    """An output whose player is gone reads as unavailable rather than needing setup."""
    entry = await _preferred_output_entry(
        mass_minimal,
        [_make_output_protocol(_AIRPLAY_ID, "airplay", 10, available=False)],
        None,
    )
    _assert_disabled_with_reason(_option(entry, _AIRPLAY_ID), "unavailable")


async def test_output_turned_off_reports_turned_off(mass_minimal: MusicAssistant) -> None:
    """An output the user turned off says so, so the enable toggle below makes sense."""
    mass_minimal.config.set(f"{CONF_PLAYERS}/{_AIRPLAY_ID}/{CONF_ENABLED}", False)
    entry = await _preferred_output_entry(
        mass_minimal,
        [_make_output_protocol(_AIRPLAY_ID, "airplay", 10, available=False)],
        _make_protocol_player(available=True, needs_setup=True),
    )
    _assert_disabled_with_reason(_option(entry, _AIRPLAY_ID), "turned_off")


async def test_default_is_never_a_disabled_option(mass_minimal: MusicAssistant) -> None:
    """A native output that can not be used must not become the entry's default."""
    entry = await _preferred_output_entry(
        mass_minimal,
        [
            OutputProtocol(
                output_protocol_id="native",
                name="Sonos",
                protocol_domain="sonos",
                priority=0,
                available=False,
                is_native=True,
            ),
            _make_output_protocol(_DLNA_ID, "dlna", 50, available=True),
        ],
    )
    assert entry.default_value == "auto"
    assert _option(entry, entry.default_value).disabled is False
