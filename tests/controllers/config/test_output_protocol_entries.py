"""Unit tests for the output protocol config entries a player renders."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType
from music_assistant_models.player import OutputProtocol

from music_assistant import constants as _constants
from music_assistant.constants import (
    CONF_ENABLED,
    CONF_ENTRY_CROSSFADE_DIFFERENT_SAMPLE_RATES,
    CONF_ENTRY_FLOW_MODE,
    CONF_FLOW_MODE,
    CONF_PLAYERS,
    CONF_PREFERRED_OUTPUT_PROTOCOL,
    CONF_PROTOCOL_EXPERIMENTAL_NOTE,
    CONF_PROTOCOL_KEY_SPLITTER,
)
from music_assistant.mass import MusicAssistant
from music_assistant.models.player import LinkedOutputProtocol

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueOption

# the common strings live next to the constants module, so this path holds from anywhere
_STRINGS_PATH = Path(_constants.__file__).resolve().parent / "strings.json"

_AIRPLAY_ID = "airplay_aabbccddeeff"
_DLNA_ID = "dlna_aabbccddeeff"
_DLNA_PREFIX = f"{_DLNA_ID}{CONF_PROTOCOL_KEY_SPLITTER}"
_PARENT_ID = "soundtouch_aabbccddeeff"


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


async def _protocol_block_entries(
    mass: MusicAssistant, protocol_entries: list[ConfigEntry]
) -> dict[str, ConfigEntry]:
    """
    Build the config block a player renders for a single (non-native) output protocol.

    :param mass: the MusicAssistant instance to build the entries with.
    :param protocol_entries: the config entries the protocol's own player reports.
    """
    player = MagicMock()
    player.needs_setup = False
    player.output_protocols = [_make_output_protocol(_DLNA_ID, "dlna", 50, available=True)]
    protocol_player = _make_protocol_player(available=True, needs_setup=False)
    protocol_player.translation_owner = "dlna"
    mass.players = MagicMock()
    mass.players.get_player.return_value = protocol_player
    with (
        patch.object(mass, "get_provider_manifest", side_effect=_make_provider_manifest),
        # the block is only built for a protocol whose provider is loaded
        patch.object(mass, "get_provider", return_value=MagicMock()),
        patch.object(mass.config, "_get_player_config_entries", return_value=protocol_entries),
    ):
        entries = await mass.config._create_output_protocol_config_entries(player)
    return {entry.key: entry for entry in entries}


async def _control_only_player_entries(
    mass: MusicAssistant,
    parent_entries: list[ConfigEntry],
    protocol_entries: list[ConfigEntry],
) -> dict[str, ConfigEntry]:
    """
    Build the full config surface of a control-only player, the way the api renders it.

    :param mass: the MusicAssistant instance to build the entries with.
    :param parent_entries: the config entries the control-only player itself reports.
    :param protocol_entries: the config entries its linked protocol player reports.
    """
    player = MagicMock()
    player.player_id = _PARENT_ID
    player.needs_setup = False
    # no native protocol: this player only controls, playback goes through the linked protocol
    player.output_protocols = [_make_output_protocol(_DLNA_ID, "dlna", 50, available=True)]
    player.linked_output_protocols = [
        LinkedOutputProtocol(output_protocol_id=_DLNA_ID, protocol_domain="dlna", priority=50)
    ]
    protocol_player = _make_protocol_player(available=True, needs_setup=False)
    protocol_player.player_id = _DLNA_ID
    protocol_player.translation_owner = "dlna"
    players = {_PARENT_ID: player, _DLNA_ID: protocol_player}
    own_entries = {_PARENT_ID: parent_entries, _DLNA_ID: protocol_entries}
    mass.players = MagicMock()
    mass.players.get_player.side_effect = lambda player_id, *_: players.get(player_id)
    mass.players.player_controls.return_value = []
    with (
        patch.object(mass, "get_provider_manifest", side_effect=_make_provider_manifest),
        # the block is only built for a protocol whose provider is loaded
        patch.object(mass, "get_provider", return_value=MagicMock()),
        patch.object(
            mass.config,
            "_get_player_config_entries",
            side_effect=lambda target: own_entries[target.player_id],
        ),
    ):
        entries = await mass.config.get_player_config_entries(_PARENT_ID)
    return {entry.key: entry for entry in entries}


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


async def test_protocol_entry_keeps_dependency_on_its_own_sibling(
    mass_minimal: MusicAssistant,
) -> None:
    """An entry gated on a sibling stays gated on it after being copied into the block."""
    entries = await _protocol_block_entries(
        mass_minimal,
        [
            ConfigEntry(key="display", type=ConfigEntryType.BOOLEAN, default_value=False),
            ConfigEntry(
                key="visualization",
                type=ConfigEntryType.STRING,
                default_value="none",
                depends_on="display",
                depends_on_value=True,
            ),
        ],
    )
    visualization = entries[f"{_DLNA_PREFIX}visualization"]
    assert visualization.depends_on == f"{_DLNA_PREFIX}display"
    # an entry pointing at a key that is not in the block reads as unmet, so it would hide
    assert visualization.depends_on in entries
    assert visualization.depends_on_value is True


async def test_protocol_entry_without_dependency_follows_the_protocol_toggle(
    mass_minimal: MusicAssistant,
) -> None:
    """An entry with no dependency of its own is gated on the protocol's enable toggle."""
    entries = await _protocol_block_entries(
        mass_minimal,
        [ConfigEntry(key="buffer_depth", type=ConfigEntryType.INTEGER, default_value=5)],
    )
    enabled_key = f"{_DLNA_PREFIX}{CONF_ENABLED}"
    assert entries[f"{_DLNA_PREFIX}buffer_depth"].depends_on == enabled_key
    assert enabled_key in entries


async def test_unresolvable_dependency_drops_its_value_condition(
    mass_minimal: MusicAssistant,
) -> None:
    """An entry whose dependency is absent falls back without carrying its condition over."""
    entries = await _protocol_block_entries(
        mass_minimal,
        [
            ConfigEntry(
                key="flow_mode_sample_rate",
                type=ConfigEntryType.STRING,
                default_value="smart",
                depends_on=CONF_FLOW_MODE,
                depends_on_value_not=True,
            )
        ],
    )
    entry = entries[f"{_DLNA_PREFIX}flow_mode_sample_rate"]
    assert entry.depends_on == f"{_DLNA_PREFIX}{CONF_ENABLED}"
    # the condition was written for flow mode; against the toggle it would invert the gate
    assert entry.depends_on_value_not is None


async def test_crossfade_entry_still_tracks_flow_mode(mass_minimal: MusicAssistant) -> None:
    """The shared 'only without flow mode' entries keep their meaning inside a protocol block."""
    entries = await _protocol_block_entries(
        mass_minimal,
        [CONF_ENTRY_FLOW_MODE, CONF_ENTRY_CROSSFADE_DIFFERENT_SAMPLE_RATES],
    )
    crossfade = entries[f"{_DLNA_PREFIX}{CONF_ENTRY_CROSSFADE_DIFFERENT_SAMPLE_RATES.key}"]
    assert crossfade.depends_on == f"{_DLNA_PREFIX}{CONF_FLOW_MODE}"
    assert crossfade.depends_on in entries
    assert crossfade.depends_on_value_not is True
    # the shared constant is reused across players, so the block must not have mutated it
    assert CONF_ENTRY_CROSSFADE_DIFFERENT_SAMPLE_RATES.depends_on == CONF_FLOW_MODE


async def test_protocol_dependency_never_binds_to_the_players_own_setting(
    mass_minimal: MusicAssistant,
) -> None:
    """A copied entry follows the protocol's own setting, not the player's same-named one."""
    entries = await _control_only_player_entries(
        mass_minimal,
        [ConfigEntry(key=CONF_FLOW_MODE, type=ConfigEntryType.BOOLEAN, default_value=True)],
        [
            ConfigEntry(key=CONF_FLOW_MODE, type=ConfigEntryType.BOOLEAN, default_value=False),
            ConfigEntry(
                key="flow_mode_sample_rate",
                type=ConfigEntryType.STRING,
                default_value="smart",
                depends_on=CONF_FLOW_MODE,
                depends_on_value=True,
            ),
        ],
    )
    sample_rate = entries[f"{_DLNA_PREFIX}flow_mode_sample_rate"]
    assert sample_rate.depends_on == f"{_DLNA_PREFIX}{CONF_FLOW_MODE}"
    assert sample_rate.depends_on_value is True
    # both settings are rendered side by side on this player, so a bare key would have
    # gated the protocol's entry on the player's own flow mode instead of the protocol's
    assert entries[f"{_DLNA_PREFIX}{CONF_FLOW_MODE}"].default_value is False
    assert entries[CONF_FLOW_MODE].default_value is True


async def _preferred_entry_with_default_domain(
    mass: MusicAssistant,
    protocols: list[OutputProtocol],
    default_domain: str | None,
) -> ConfigEntry:
    """Build the preferred-output entry for a player that declares a default protocol domain."""
    player = MagicMock()
    player.needs_setup = False
    player.output_protocols = protocols
    player.default_output_protocol_domain = default_domain
    mass.players = MagicMock()
    mass.players.get_player.return_value = None
    with patch.object(mass, "get_provider_manifest", side_effect=_make_provider_manifest):
        entries = await mass.config._create_output_protocol_config_entries(player)
    return next(entry for entry in entries if entry.key == CONF_PREFERRED_OUTPUT_PROTOCOL)


async def test_default_domain_available_stays_auto(
    mass_minimal: MusicAssistant,
) -> None:
    """A no-native player defaults to auto even when its default-domain output is available."""
    entry = await _preferred_entry_with_default_domain(
        mass_minimal,
        [
            _make_output_protocol(_DLNA_ID, "dlna", 50, available=True),
            _make_output_protocol(_AIRPLAY_ID, "airplay", 10, available=True),
        ],
        default_domain="dlna",
    )
    # The stored default must not depend on which linked protocols happen to be available;
    # runtime selection applies the default domain, but the persisted entry stays "auto".
    assert entry.default_value == "auto"
    # auto and both protocols remain selectable so the user can still override
    assert {option.value for option in entry.options} >= {"auto", _DLNA_ID, _AIRPLAY_ID}


async def test_default_domain_unavailable_stays_auto(mass_minimal: MusicAssistant) -> None:
    """A no-native player defaults to auto even when its default-domain output is unavailable."""
    entry = await _preferred_entry_with_default_domain(
        mass_minimal,
        [
            _make_output_protocol(_DLNA_ID, "dlna", 50, available=False),
            _make_output_protocol(_AIRPLAY_ID, "airplay", 10, available=True),
        ],
        default_domain="dlna",
    )
    assert entry.default_value == "auto"


async def test_default_domain_absent_stays_auto(mass_minimal: MusicAssistant) -> None:
    """A player whose default domain is not among its outputs falls back to auto."""
    entry = await _preferred_entry_with_default_domain(
        mass_minimal,
        [_make_output_protocol(_AIRPLAY_ID, "airplay", 10, available=True)],
        default_domain="dlna",
    )
    assert entry.default_value == "auto"


async def test_no_default_domain_stays_auto(mass_minimal: MusicAssistant) -> None:
    """Without a declared default domain, a player with no native output defaults to auto."""
    entry = await _preferred_entry_with_default_domain(
        mass_minimal,
        [_make_output_protocol(_DLNA_ID, "dlna", 50, available=True)],
        default_domain=None,
    )
    assert entry.default_value == "auto"


async def test_available_native_ignores_default_domain(mass_minimal: MusicAssistant) -> None:
    """An available native output still wins over a declared default protocol domain."""
    native = OutputProtocol(
        output_protocol_id="native_x",
        name="Native",
        protocol_domain="soundtouch",
        is_native=True,
        priority=1,
        available=True,
    )
    entry = await _preferred_entry_with_default_domain(
        mass_minimal,
        [native, _make_output_protocol(_DLNA_ID, "dlna", 50, available=True)],
        default_domain="dlna",
    )
    assert entry.default_value == "native"


async def test_experimental_output_defaults_to_off_with_a_warning(
    mass_minimal: MusicAssistant,
) -> None:
    """An output its provider flagged as experimental is opt-in and says why."""
    mass_minimal.config.set(
        f"{CONF_PLAYERS}/{_DLNA_ID}/values/{CONF_PROTOCOL_EXPERIMENTAL_NOTE}",
        "sendspin_cast_experimental",
    )
    entries = await _protocol_block_entries(mass_minimal, [])
    enabled = entries[f"{_DLNA_PREFIX}{CONF_ENABLED}"]
    assert enabled.default_value is False
    assert enabled.translation_key == "protocol_enable_experimental"
    warning = entries[f"{_DLNA_PREFIX}sendspin_cast_experimental"]
    assert warning.type is ConfigEntryType.ALERT
    # the note is owned by the protocol's provider, not by the player rendering it
    assert warning.translation_owner == "provider.dlna"
    assert warning.translation_key == "sendspin_cast_experimental"
    # the warning explains why the toggle is off, so it must not be hidden behind it
    assert warning.depends_on is None


async def test_experimental_output_reads_off_without_a_stored_value(
    mass_minimal: MusicAssistant,
) -> None:
    """An experimental output nobody opted into reads as off, not as on."""
    mass_minimal.config.set(
        f"{CONF_PLAYERS}/{_DLNA_ID}/values/{CONF_PROTOCOL_EXPERIMENTAL_NOTE}",
        "sendspin_cast_experimental",
    )
    entries = await _protocol_block_entries(mass_minimal, [])
    assert entries[f"{_DLNA_PREFIX}{CONF_ENABLED}"].value is False


async def test_plain_output_keeps_its_default_and_label(mass_minimal: MusicAssistant) -> None:
    """An output without the flag is unchanged: on by default, no warning."""
    entries = await _protocol_block_entries(mass_minimal, [])
    enabled = entries[f"{_DLNA_PREFIX}{CONF_ENABLED}"]
    assert enabled.default_value is True
    assert enabled.translation_key == "protocol_enable"
    assert not [key for key in entries if "experimental" in key]
