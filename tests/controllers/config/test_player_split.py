"""Tests for the migration that splits the stored player configs into players and protocols."""

from __future__ import annotations

import json
import logging
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from music_assistant_models.enums import IdentifierType

from music_assistant.constants import (
    CONF_PLAYER_DSP,
    CONF_PLAYER_PROTOCOLS,
    CONF_PLAYER_QUEUES,
    CONF_PLAYER_SPLIT_SESSION_ONLY,
    CONF_PLAYER_SPLIT_TREE,
    CONF_PLAYERS,
)
from music_assistant.controllers.config import player_split
from music_assistant.controllers.config.controller import ConfigController
from music_assistant.controllers.config.player_split import (
    BACKUP_SUFFIX,
    mark_player_split_done,
    migrate_player_split,
    needs_player_split,
    remove_player_records_of_older_versions,
)
from tests.common import capture_log_records

# the real server with fake providers, for what a device finds of its migrated config
from tests.scenarios.conftest import scenario  # noqa: F401

if TYPE_CHECKING:
    from tests.scenarios.harness import Scenario

AIRPLAY = "airplay--a1"
CAST = "chromecast--c1"
SONOS = "sonos--s1"
DLNA = "dlna--d1"
SQUEEZELITE = "squeezelite--q1"
SENDSPIN = "sendspin"
UNIVERSAL_PLAYER = "universal_player"

FLAGS = {CONF_PLAYER_SPLIT_SESSION_ONLY: True, CONF_PLAYER_SPLIT_TREE: True}
# what a kept browser or app player gets as the defaults its client gives it
WEB_DEFAULTS = {
    "protocol_defaults": {
        "hidden_by_default": True,
        "expose_to_ha_by_default": False,
        "private": True,
    }
}


def _record(
    player_id: str,
    provider: str,
    player_type: str = "player",
    enabled: bool = True,
    **extra: Any,
) -> dict[str, Any]:
    """Return the stored config of a player as a version from before the split wrote it."""
    return {
        "player_id": player_id,
        "provider": provider,
        "player_type": player_type,
        "enabled": enabled,
        "name": None,
        "default_name": f"Device {player_id}",
        "values": {},
        **extra,
    }


def _protocol(player_id: str, provider: str, parent_id: str | None, **extra: Any) -> dict[str, Any]:
    """Return the stored config of a protocol player from before the split."""
    extra.setdefault("values", {})
    if parent_id:
        extra["values"]["protocol_parent_id"] = parent_id
    return _record(player_id, provider, "protocol", **extra)


def _settings(*records: dict[str, Any], **trees: Any) -> dict[str, Any]:
    """Return settings data from before the split around the given player configs."""
    return {
        "server_id": "x",
        CONF_PLAYERS: {record["player_id"]: record for record in records},
        **trees,
    }


def _migrated(data: dict[str, Any]) -> dict[str, Any]:
    """
    Return the migrated copy of settings data, after checking that the result is stable.

    A second run changes nothing, and neither does a run on the result without its flags.
    """
    result = deepcopy(data)
    assert migrate_player_split(result) is True
    assert all(result[flag] is True for flag in FLAGS)
    again = deepcopy(result)
    assert migrate_player_split(again) is False
    assert again == result
    unflagged = {key: value for key, value in deepcopy(result).items() if key not in FLAGS}
    migrate_player_split(unflagged)
    assert unflagged == result
    # nothing the split stores is taken for a record of an older version
    cleaned = deepcopy(result)
    assert remove_player_records_of_older_versions(cleaned) is False
    assert cleaned == result
    return result


def _protocol_record(
    protocol_id: str,
    provider: str,
    player_id: str | None,
    role: str = "audio",
    enabled: bool = True,
    **extra: Any,
) -> dict[str, Any]:
    """Return the config of a protocol as the split stores it."""
    return {
        "protocol_id": protocol_id,
        "provider": provider,
        "player_id": player_id,
        "role": role,
        "enabled": enabled,
        "default_name": f"Device {protocol_id}",
        "values": {},
        **extra,
    }


def test_native_player_keeps_its_own_values_and_gets_a_protocol_for_the_rest() -> None:
    """A player record is split by who owns each value; what is stored next to it stays."""
    record = _record(
        "sonos_1",
        SONOS,
        name="Kitchen",
        values={
            "hide_in_ui": True,
            "volume_control": "native",
            "announce_volume": 40,
            "icon": "mdi-speaker",
            "preferred_output_protocol": "ap_1",
            "created_at": 1,
            "legacy_ids": ["old_1"],
            "output_channels": "left",
            "flow_mode": True,
            "sample_rates": ["44100$16"],
            "cached_arp_mac": "AA:BB:CC:DD:EE:01",
            "reported_mac": "AA:BB:CC:DD:EE:02",
            "provider_setting": "x",
        },
        setup_data={"token": "enc"},
    )
    dsp = {"sonos_1": {"enabled": True, "filters": []}}
    queues = {"sonos_1": {"values": {"crossfade": True}}}

    result = _migrated(_settings(record, player_dsp=dsp, player_queues=queues))

    assert result[CONF_PLAYERS] == {
        "sonos_1": _record(
            "sonos_1",
            SONOS,
            name="Kitchen",
            values={
                "hide_in_ui": True,
                "volume_control": "native",
                "announce_volume": 40,
                "icon": "mdi-speaker",
                "preferred_output_protocol": "ap_1",
                "created_at": 1,
                "legacy_ids": ["old_1"],
            },
        )
    }
    assert result[CONF_PLAYER_PROTOCOLS] == {
        "sonos_1": _protocol_record(
            "sonos_1",
            SONOS,
            "sonos_1",
            role="native",
            values={
                "output_channels": "left",
                "flow_mode": True,
                "sample_rates": ["44100$16"],
                "cached_arp_mac": "AA:BB:CC:DD:EE:01",
                "reported_mac": "AA:BB:CC:DD:EE:02",
                "provider_setting": "x",
            },
            setup_data={"token": "enc"},
        )
    }
    assert result[CONF_PLAYER_DSP] == dsp
    assert result[CONF_PLAYER_QUEUES] == queues


def test_wrapper_becomes_the_player_of_its_protocols() -> None:
    """A universal player record keeps its id and names the provider of its best protocol."""
    wrapper = _record(
        "up0001",
        UNIVERSAL_PLAYER,
        name="Living",
        values={
            "linked_protocol_ids": ["ap_1", "cc_1"],
            "device_identifiers": {"mac_address": "AA:BB:CC:DD:EE:03", "uuid": "uuid-1_MR"},
            "device_info": {"model": "M", "manufacturer": "Acme"},
            "created_at": 5,
            "preferred_output_protocol": "cc_1",
        },
        setup_data={"stale": 1},
    )
    airplay = _protocol(
        "ap_1",
        AIRPLAY,
        "up0001",
        values={"cached_arp_mac": "AA:BB:CC:DD:EE:03", "output_codec": "pcm"},
        setup_data={"credentials": "enc"},
    )
    cast = _protocol("cc_1", CAST, "up0001", enabled=False)

    result = _migrated(_settings(wrapper, airplay, cast))

    assert result[CONF_PLAYERS] == {
        "up0001": _record(
            "up0001",
            AIRPLAY,
            name="Living",
            values={
                "device_identifiers": {"mac_address": "AA:BB:CC:DD:EE:03", "uuid": "uuid-1"},
                "device_info": {"model": "M", "manufacturer": "Acme"},
                "created_at": 5,
                "preferred_output_protocol": "cc_1",
            },
        )
    }
    # the wrapper has no protocol of its own, and the output the user switched off stays off
    assert result[CONF_PLAYER_PROTOCOLS] == {
        "ap_1": _protocol_record(
            "ap_1",
            AIRPLAY,
            "up0001",
            values={"cached_arp_mac": "AA:BB:CC:DD:EE:03", "output_codec": "pcm"},
            setup_data={"credentials": "enc"},
        ),
        "cc_1": _protocol_record("cc_1", CAST, "up0001", enabled=False),
    }


@pytest.mark.parametrize(
    ("linked", "parents"),
    [
        # the list of the wrapper was never written
        ([], {"ap_2": "up0002", "cc_2": "up0002"}),
        # one of the protocols lost its edge, the wrapper still lists it
        (["ap_2", "cc_2"], {"ap_2": None, "cc_2": "up0002"}),
    ],
    ids=["list_missing", "edge_missing"],
)
def test_half_written_wrapper_gets_its_protocols_back(
    linked: list[str], parents: dict[str, str | None]
) -> None:
    """A wrapper an interrupted shutdown left half-written still becomes one player."""
    wrapper = _record("up0002", UNIVERSAL_PLAYER, values={"linked_protocol_ids": linked})
    airplay = _protocol("ap_2", AIRPLAY, parents["ap_2"])
    cast = _protocol("cc_2", CAST, parents["cc_2"])

    result = _migrated(_settings(wrapper, airplay, cast))

    assert result[CONF_PLAYERS] == {"up0002": _record("up0002", AIRPLAY)}
    assert {
        protocol_id: record["player_id"]
        for protocol_id, record in result[CONF_PLAYER_PROTOCOLS].items()
    } == {"ap_2": "up0002", "cc_2": "up0002"}


def test_a_lost_edge_is_restored_from_the_wrapper_that_lists_it() -> None:
    """Where a wrapper and a native player both list a protocol, it belongs to the wrapper."""
    wrapper = _record("up0003", UNIVERSAL_PLAYER, values={"linked_protocol_ids": ["ap_3"]})
    native = _record("sonos_3", SONOS, values={"linked_protocol_ids": ["ap_3"]})
    airplay = _protocol("ap_3", AIRPLAY, None)

    result = _migrated(_settings(wrapper, native, airplay))

    assert result[CONF_PLAYER_PROTOCOLS]["ap_3"]["player_id"] == "up0003"


def test_a_lost_edge_is_not_guessed_between_two_players_of_a_kind() -> None:
    """A protocol two wrappers list stays unbound: it is matched again when it attaches."""
    first = _record("up0004", UNIVERSAL_PLAYER, values={"linked_protocol_ids": ["ap_4"]})
    second = _record(
        "up0005", UNIVERSAL_PLAYER, name="Den", values={"linked_protocol_ids": ["ap_4"]}
    )
    airplay = _protocol("ap_4", AIRPLAY, None, enabled=False)

    result = _migrated(_settings(first, second, airplay))

    assert result[CONF_PLAYER_PROTOCOLS] == {"ap_4": _protocol_record("ap_4", AIRPLAY, None)}
    # neither wrapper has a protocol left: only the one the user named is kept
    assert list(result[CONF_PLAYERS]) == ["up0005"]


@pytest.mark.parametrize(
    ("parent", "provider", "players"),
    [
        # the wrapper lists nothing else, so it has no protocol left
        (
            _record("up0030", UNIVERSAL_PLAYER, values={"linked_protocol_ids": ["dev_30"]}),
            SENDSPIN,
            ["dev_30", "syncgroup_30"],
        ),
        (
            _record("sonos_30", SONOS, values={"linked_protocol_ids": ["dev_30"]}),
            AIRPLAY,
            ["dev_30", "sonos_30", "syncgroup_30"],
        ),
    ],
    ids=["wrapper", "native"],
)
def test_a_player_that_is_still_listed_as_a_protocol_stays_a_player(
    parent: dict[str, Any], provider: str, players: list[str]
) -> None:
    """A record stored as a player is not taken as a protocol because a player lists it."""
    native = _record("dev_30", provider, name="Kitchen", values={"max_volume": 60})
    sync_group = _record(
        "syncgroup_30", "sync_group", "group", values={"group_members": ["dev_30"]}
    )
    dsp = {"dev_30": {"enabled": True}}
    queues = {"dev_30": {"values": {"crossfade": True}}}

    result = _migrated(
        _settings(deepcopy(parent), native, sync_group, player_dsp=dsp, player_queues=queues)
    )

    assert sorted(result[CONF_PLAYERS]) == players
    assert result[CONF_PLAYERS]["dev_30"] == _record(
        "dev_30", provider, name="Kitchen", values={"max_volume": 60}
    )
    assert result[CONF_PLAYER_PROTOCOLS]["dev_30"] == _protocol_record(
        "dev_30", provider, "dev_30", role="native"
    )
    assert result[CONF_PLAYER_PROTOCOLS]["syncgroup_30"]["values"] == {"group_members": ["dev_30"]}
    assert result[CONF_PLAYER_DSP] == dsp
    assert result[CONF_PLAYER_QUEUES] == queues


def test_empty_wrapper_is_only_kept_when_the_user_customised_it() -> None:
    """A wrapper without protocols goes, unless it holds something the user set up."""
    plain = _record(
        "up0006",
        UNIVERSAL_PLAYER,
        values={"linked_protocol_ids": [], "created_at": 1, "device_info": {"model": "M"}},
    )
    named = _record(
        "up0007",
        UNIVERSAL_PLAYER,
        name="Kitchen",
        values={
            "linked_protocol_ids": ["gone"],
            "device_identifiers": {"uuid": "uuid-7_MR"},
            "device_info": {"model": "M", "manufacturer": "Acme"},
        },
        setup_data={"stale": 1},
    )
    with_value = _record("up0008", UNIVERSAL_PLAYER, values={"icon": "mdi-x"})
    with_dsp = _record("up0009", UNIVERSAL_PLAYER)
    with_queue = _record("up0010", UNIVERSAL_PLAYER)
    disabled = _record("up0011", UNIVERSAL_PLAYER, enabled=False)
    dsp = {"up0006": {"enabled": False}, "up0009": {"enabled": True}}
    queues = {"up0010": {"values": {"crossfade": True}}}

    result = _migrated(
        _settings(
            plain,
            named,
            with_value,
            with_dsp,
            with_queue,
            disabled,
            player_dsp=dsp,
            player_queues=queues,
        )
    )

    # a kept one names no provider: it waits for a device that matches its identifiers
    assert result[CONF_PLAYERS] == {
        "up0007": _record(
            "up0007",
            "",
            name="Kitchen",
            values={
                "device_identifiers": {"uuid": "uuid-7"},
                "device_info": {"model": "M", "manufacturer": "Acme"},
            },
        ),
        "up0008": _record("up0008", "", values={"icon": "mdi-x"}),
        "up0009": _record("up0009", ""),
        "up0010": _record("up0010", ""),
        "up0011": _record("up0011", "", enabled=False),
    }
    assert result[CONF_PLAYER_PROTOCOLS] == {}
    assert result[CONF_PLAYER_DSP] == {"up0009": {"enabled": True}}
    assert result[CONF_PLAYER_QUEUES] == queues


@pytest.mark.parametrize(
    ("linked", "players"),
    [
        (["ap_31", "cc_31"], {"ap_31": "up0031", "cc_31": "up0031"}),
        # a customised one without protocols is kept for a device that returns
        ([], {}),
    ],
    ids=["with_protocols", "without_protocols"],
)
def test_wrapper_is_stored_as_a_player_whatever_type_it_was_created_with(
    linked: list[str], players: dict[str, str]
) -> None:
    """A wrapper whose creation was interrupted still stored the type of a group."""
    wrapper = _record(
        "up0031", UNIVERSAL_PLAYER, "group", name="Den", values={"linked_protocol_ids": linked}
    )
    providers = {"ap_31": AIRPLAY, "cc_31": CAST}
    protocols = [
        _protocol(protocol_id, providers[protocol_id], "up0031") for protocol_id in players
    ]

    result = _migrated(_settings(wrapper, *protocols))

    assert result[CONF_PLAYERS]["up0031"]["player_type"] == "player"
    assert {
        protocol_id: record["player_id"]
        for protocol_id, record in result[CONF_PLAYER_PROTOCOLS].items()
    } == players


def test_protocol_without_a_player_is_unbound_and_enabled() -> None:
    """The enabled flag of a protocol without a player was that of its device."""
    lone = _protocol("ap_5", AIRPLAY, None, enabled=False, values={"cached_arp_mac": "m"})

    result = _migrated(_settings(lone))

    assert result[CONF_PLAYERS] == {}
    assert result[CONF_PLAYER_PROTOCOLS] == {
        "ap_5": _protocol_record("ap_5", AIRPLAY, None, values={"cached_arp_mac": "m"})
    }


def test_record_with_an_edge_is_a_protocol_whatever_type_it_stores() -> None:
    """A record that points at a player is a protocol of it, also when stored as a player."""
    native = _record("sq_6", SQUEEZELITE)
    dlna = _record("dl_6", DLNA, values={"protocol_parent_id": "sq_6", "output_channels": "mono"})

    result = _migrated(_settings(native, dlna))

    assert list(result[CONF_PLAYERS]) == ["sq_6"]
    assert result[CONF_PLAYER_PROTOCOLS]["dl_6"] == _protocol_record(
        "dl_6", DLNA, "sq_6", values={"output_channels": "mono"}
    )


def test_edge_to_nothing_usable_is_dropped() -> None:
    """An edge to a missing record, to itself or to another protocol leaves a protocol unbound."""
    missing = _protocol("ap_7a", AIRPLAY, "gone", enabled=False)
    itself = _protocol("ap_7b", AIRPLAY, "ap_7b")
    child = _protocol("cc_7c", CAST, "ap_7a")

    result = _migrated(_settings(missing, itself, child))

    assert result[CONF_PLAYERS] == {}
    assert result[CONF_PLAYER_PROTOCOLS] == {
        "ap_7a": _protocol_record("ap_7a", AIRPLAY, None),
        "ap_7b": _protocol_record("ap_7b", AIRPLAY, None),
        "cc_7c": _protocol_record("cc_7c", CAST, None),
    }


@pytest.mark.parametrize(
    ("protocols", "kept"),
    [
        # the enabled one
        (
            [
                _protocol("ap_8a", AIRPLAY, "up0008", enabled=False),
                _protocol("ap_8b", AIRPLAY, "up0008", enabled=False, setup_data={"x": 1}),
                _protocol("ap_8c", AIRPLAY, "up0008"),
            ],
            "ap_8c",
        ),
        # else the one that was set up
        (
            [
                _protocol("ap_8a", AIRPLAY, "up0008", enabled=False),
                _protocol("ap_8b", AIRPLAY, "up0008", enabled=False, setup_data={"x": 1}),
            ],
            "ap_8b",
        ),
        # else the lowest id
        (
            [_protocol("ap_8b", AIRPLAY, "up0008"), _protocol("ap_8a", AIRPLAY, "up0008")],
            "ap_8a",
        ),
    ],
    ids=["enabled", "set_up", "lowest_id"],
)
def test_one_protocol_per_domain_stays_on_a_player(
    protocols: list[dict[str, Any]], kept: str
) -> None:
    """Of several protocols of one domain that point at a player, one keeps its edge."""
    wrapper = _record("up0008", UNIVERSAL_PLAYER)

    result = _migrated(_settings(wrapper, *deepcopy(protocols)))

    edges = {
        protocol_id: (record["player_id"], record["enabled"])
        for protocol_id, record in result[CONF_PLAYER_PROTOCOLS].items()
    }
    kept_enabled = next(p for p in protocols if p["player_id"] == kept)["enabled"]
    assert edges == {
        protocol["player_id"]: ("up0008", kept_enabled)
        if protocol["player_id"] == kept
        else (None, True)
        for protocol in protocols
    }


def test_outputs_of_a_disabled_player_are_enabled_again() -> None:
    """A disabled player stays disabled, its outputs do not stay switched off with it."""
    native = _record("sonos_9", SONOS, enabled=False)
    airplay = _protocol("ap_9", AIRPLAY, "sonos_9")
    cast = _protocol("cc_9", CAST, "sonos_9", enabled=False)

    result = _migrated(_settings(native, airplay, cast))

    assert result[CONF_PLAYERS]["sonos_9"]["enabled"] is False
    assert {
        protocol_id: record["enabled"]
        for protocol_id, record in result[CONF_PLAYER_PROTOCOLS].items()
    } == {"sonos_9": True, "ap_9": True, "cc_9": True}


def test_group_members_name_players() -> None:
    """A member or allow list that names a protocol names its player after the split, once."""
    wrapper = _record("up0010", UNIVERSAL_PLAYER)
    airplay = _protocol("ap_10", AIRPLAY, "up0010")
    native = _record("sonos_10", SONOS)
    sync_group = _record(
        "syncgroup_1",
        "sync_group",
        "group",
        values={
            "group_members": ["ap_10", "sonos_10", "up0010"],
            "allowed_members": ["sonos_10", "ap_10", "up0010"],
            "power_control": "fake",
        },
    )
    universal_group = _record(
        "ugp_1", "universal_group", "group", values={"group_members": ["ap_10", "sonos_10"]}
    )

    result = _migrated(_settings(wrapper, airplay, native, sync_group, universal_group))

    assert result[CONF_PLAYERS]["syncgroup_1"]["values"] == {"power_control": "fake"}
    assert result[CONF_PLAYER_PROTOCOLS]["syncgroup_1"] == _protocol_record(
        "syncgroup_1",
        "sync_group",
        "syncgroup_1",
        role="virtual",
        values={"group_members": ["up0010", "sonos_10"], "allowed_members": ["sonos_10", "up0010"]},
    )
    assert result[CONF_PLAYER_PROTOCOLS]["ugp_1"]["role"] == "virtual"
    assert result[CONF_PLAYER_PROTOCOLS]["ugp_1"]["values"] == {
        "group_members": ["up0010", "sonos_10"]
    }


@pytest.mark.parametrize("key", ["group_members", "allowed_members"])
@pytest.mark.parametrize(
    "malformed",
    [{}, [], {"player_id": "sonos_17"}],
    ids=["empty_mapping", "empty_list", "mapping"],
)
def test_malformed_group_member_is_left_out(key: str, malformed: Any) -> None:
    """A member that is no id is left out of its list and logged once; the rest migrates."""
    native = _record("sonos_17", SONOS)
    airplay = _protocol("ap_17", AIRPLAY, "sonos_17")
    other = _record("sonos_18", SONOS)
    group = _record("syncgroup_17", "sync_group", "group", values={key: ["ap_17", malformed]})
    healthy = _record("syncgroup_18", "sync_group", "group", values={key: ["sonos_18"]})

    with capture_log_records(player_split.LOGGER) as records:
        result = _migrated(_settings(native, airplay, other, group, healthy))

    protocols = result[CONF_PLAYER_PROTOCOLS]
    assert protocols["syncgroup_17"]["values"] == {key: ["sonos_17"]}
    assert protocols["syncgroup_18"]["values"] == {key: ["sonos_18"]}
    assert sorted(protocols) == ["ap_17", "sonos_17", "sonos_18", "syncgroup_17", "syncgroup_18"]
    warnings = [record.getMessage() for record in records if record.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert "syncgroup_17" in warnings[0]


def test_session_only_players_are_deleted_unless_customised() -> None:
    """A Sendspin player that is no protocol of a device is only kept when the user set it up."""
    records = [
        _record("ss_plain", SENDSPIN),
        _record("ss_auto", SENDSPIN, values={"sendspin_static_delay": 30, "created_at": 1}),
        _record("ss_dsp_off", SENDSPIN),
        _record("ss_light", SENDSPIN, "light"),
        _record("ss_named", SENDSPIN, name="Tablet"),
        _record("ss_value", SENDSPIN, values={"hide_in_ui": True, "sendspin_static_delay": 5}),
        _record("ss_dsp", SENDSPIN),
        _record("ss_queue", SENDSPIN),
        _record("ss_disabled", SENDSPIN, enabled=False),
        # the Sendspin clients of devices are protocols: of a player, or still without one
        _record("sq_hw", SQUEEZELITE),
        _protocol("esp_1", SENDSPIN, "sq_hw"),
        _protocol("esp_2", SENDSPIN, None),
    ]
    dsp = {"ss_dsp": {"enabled": True}, "ss_dsp_off": {"enabled": False}}
    queues = {"ss_queue": {"values": {"crossfade": True}}}

    result = _migrated(_settings(*records, player_dsp=dsp, player_queues=queues))

    assert sorted(result[CONF_PLAYERS]) == [
        "sq_hw",
        "ss_disabled",
        "ss_dsp",
        "ss_named",
        "ss_queue",
        "ss_value",
    ]
    assert result[CONF_PLAYERS]["ss_disabled"] == _record(
        "ss_disabled", SENDSPIN, enabled=False, values={"session_only": True, **WEB_DEFAULTS}
    )
    assert result[CONF_PLAYERS]["ss_named"] == _record(
        "ss_named", SENDSPIN, name="Tablet", values={"session_only": True, **WEB_DEFAULTS}
    )
    assert result[CONF_PLAYERS]["ss_value"]["values"] == {
        "hide_in_ui": True,
        "session_only": True,
        **WEB_DEFAULTS,
    }
    assert result[CONF_PLAYER_PROTOCOLS]["ss_value"] == _protocol_record(
        "ss_value", SENDSPIN, "ss_value", role="native", values={"sendspin_static_delay": 5}
    )
    assert sorted(result[CONF_PLAYER_PROTOCOLS]) == [
        "esp_1",
        "esp_2",
        "sq_hw",
        "ss_disabled",
        "ss_dsp",
        "ss_named",
        "ss_queue",
        "ss_value",
    ]
    # a deleted one keeps what is stored next to it: its id comes back when it connects again
    assert result[CONF_PLAYER_DSP] == dsp
    assert result[CONF_PLAYER_QUEUES] == queues
    assert set(result) == {
        "server_id",
        CONF_PLAYERS,
        CONF_PLAYER_PROTOCOLS,
        CONF_PLAYER_DSP,
        CONF_PLAYER_QUEUES,
        *FLAGS,
    }


@pytest.mark.parametrize(
    ("records", "players", "protocols"),
    [
        # a browser or app client whose audio input the user declined, and nothing else
        (
            [
                _record(
                    "ss_50",
                    SENDSPIN,
                    values={
                        "source_input_declined": True,
                        "sendspin_static_delay": 5,
                        "created_at": 1,
                    },
                )
            ],
            {
                "ss_50": _record(
                    "ss_50",
                    SENDSPIN,
                    values={"created_at": 1, "session_only": True, **WEB_DEFAULTS},
                )
            },
            {
                "ss_50": _protocol_record(
                    "ss_50",
                    SENDSPIN,
                    "ss_50",
                    role="native",
                    values={"source_input_declined": True, "sendspin_static_delay": 5},
                )
            },
        ),
        # one the user set up otherwise as well
        (
            [
                _record(
                    "ss_51",
                    SENDSPIN,
                    name="Tablet",
                    values={"source_input_declined": True, "hide_in_ui": True},
                )
            ],
            {
                "ss_51": _record(
                    "ss_51",
                    SENDSPIN,
                    name="Tablet",
                    values={"hide_in_ui": True, "session_only": True, **WEB_DEFAULTS},
                )
            },
            {
                "ss_51": _protocol_record(
                    "ss_51",
                    SENDSPIN,
                    "ss_51",
                    role="native",
                    values={"source_input_declined": True},
                )
            },
        ),
        # the Sendspin client of a speaker with a line-in, a protocol of the speaker's player
        (
            [
                _record("sq_52", SQUEEZELITE),
                _protocol("ss_52", SENDSPIN, "sq_52", values={"source_input_declined": True}),
            ],
            {"sq_52": _record("sq_52", SQUEEZELITE)},
            {
                "sq_52": _protocol_record("sq_52", SQUEEZELITE, "sq_52", role="native"),
                "ss_52": _protocol_record(
                    "ss_52", SENDSPIN, "sq_52", values={"source_input_declined": True}
                ),
            },
        ),
    ],
    ids=["session_only_declined", "session_only_declined_and_set_up", "protocol_of_a_speaker"],
)
def test_a_declined_audio_input_stays_with_the_protocol_of_its_client(
    records: list[dict[str, Any]],
    players: dict[str, Any],
    protocols: dict[str, Any],
) -> None:
    """The answer of the user to the audio input of a Sendspin client survives the split."""
    result = _migrated(_settings(*deepcopy(records)))

    assert result[CONF_PLAYERS] == players
    assert result[CONF_PLAYER_PROTOCOLS] == protocols


def _without_values(record: dict[str, Any]) -> dict[str, Any]:
    """Return a stored record that holds no values at all."""
    return {key: value for key, value in record.items() if key != "values"}


@pytest.mark.parametrize(
    ("record", "values"),
    [
        (
            _record("ss_vis", SENDSPIN, "visualizer", name="Lights"),
            {
                "session_only": True,
                "protocol_defaults": {
                    "hidden_by_default": True,
                    "expose_to_ha_by_default": False,
                    "private": False,
                },
            },
        ),
        (
            _record("ss_src", SENDSPIN, "source", name="Turntable"),
            {
                "session_only": True,
                "protocol_defaults": {
                    "hidden_by_default": True,
                    "expose_to_ha_by_default": False,
                    "private": False,
                },
            },
        ),
        (
            _record("ss_display", SENDSPIN, "display", name="Kitchen display"),
            {
                "session_only": True,
                "protocol_defaults": {
                    "hidden_by_default": True,
                    "expose_to_ha_by_default": False,
                    "private": False,
                },
            },
        ),
        (
            _record("ss_light", SENDSPIN, "light", name="Strip"),
            {
                "session_only": True,
                "protocol_defaults": {
                    "hidden_by_default": True,
                    "expose_to_ha_by_default": False,
                    "private": False,
                },
            },
        ),
        # a kind of client whose record does not tell what its client gives it
        (_record("ss_unknown", SENDSPIN, "unknown", name="Gadget"), {"session_only": True}),
        (
            {**_record("ss_untyped", SENDSPIN, name="Tablet"), "player_type": None},
            {"session_only": True},
        ),
        (
            _without_values(_record("ss_bare", SENDSPIN, name="Tablet")),
            {"session_only": True, **WEB_DEFAULTS},
        ),
        (
            _record("ss_garbled", SENDSPIN, name="Tablet", values="garbled"),
            {"session_only": True, **WEB_DEFAULTS},
        ),
        # defaults that are stored already are never overwritten, whatever they hold
        (
            _record(
                "ss_stored",
                SENDSPIN,
                name="Tablet",
                values={"protocol_defaults": {"hidden_by_default": False}},
            ),
            {"session_only": True, "protocol_defaults": {"hidden_by_default": False}},
        ),
        (
            _record("ss_odd", SENDSPIN, name="Tablet", values={"protocol_defaults": "odd"}),
            {"session_only": True, "protocol_defaults": "odd"},
        ),
    ],
    ids=[
        "visualizer",
        "source",
        "display",
        "light",
        "unknown",
        "untyped",
        "no_values",
        "garbled",
        "stored",
        "odd",
    ],
)
def test_a_kept_session_only_player_gets_the_defaults_of_its_kind_of_client(
    record: dict[str, Any], values: dict[str, Any]
) -> None:
    """A browser, app, visualizer or source player the split keeps gets what its client gives."""
    result = _migrated(_settings(record))

    assert result[CONF_PLAYERS][record["player_id"]]["values"] == values


@pytest.mark.parametrize("tree", [None, ["ss_40"]], ids=["null", "list"])
def test_dsp_and_queue_trees_that_are_no_mapping_hold_no_customisation(
    tree: list[str] | None,
) -> None:
    """A DSP or queue tree that can not be read does not keep a player from its migration."""
    data = _settings(
        _record("ss_40", SENDSPIN),
        _record("ss_41", SENDSPIN, name="Tablet"),
        _record("up0040", UNIVERSAL_PLAYER),
        player_dsp=deepcopy(tree),
        player_queues=deepcopy(tree),
    )

    with capture_log_records(player_split.LOGGER) as records:
        result = _migrated(data)

    assert result[CONF_PLAYERS] == {
        "ss_41": _record(
            "ss_41", SENDSPIN, name="Tablet", values={"session_only": True, **WEB_DEFAULTS}
        )
    }
    assert (result[CONF_PLAYER_DSP], result[CONF_PLAYER_QUEUES]) == (tree, tree)
    assert [record.getMessage() for record in records if record.levelno > logging.INFO] == []


def test_record_that_is_no_mapping_is_left_alone() -> None:
    """A record that can not be read is logged and skipped, the others are migrated."""
    data = _settings(_record("sonos_11", SONOS), _record("odd", SONOS, values="not a dict"))
    data[CONF_PLAYERS]["broken"] = "not a dict"

    with capture_log_records(player_split.LOGGER) as records:
        result = _migrated(data)

    assert result[CONF_PLAYERS]["broken"] == "not a dict"
    assert result[CONF_PLAYERS]["odd"]["values"] == {}
    assert sorted(result[CONF_PLAYER_PROTOCOLS]) == ["odd", "sonos_11"]
    assert any("broken" in record.getMessage() for record in records)


def test_record_that_fails_to_convert_is_skipped() -> None:
    """A failure on one record does not stop the migration of the others."""
    data = _settings(_record("sonos_12", SONOS), _record("sonos_13", SONOS))
    split = player_split._split_player_record

    def _fail_for_one(protocols: dict[str, Any], player_id: str, record: dict[str, Any]) -> None:
        if player_id == "sonos_12":
            raise ValueError(player_id)
        split(protocols, player_id, record)

    with (
        patch.object(player_split, "_split_player_record", _fail_for_one),
        capture_log_records(player_split.LOGGER) as records,
    ):
        assert migrate_player_split(data) is True

    assert list(data[CONF_PLAYER_PROTOCOLS]) == ["sonos_13"]
    assert data[CONF_PLAYER_SPLIT_TREE] is True
    assert [record.levelno for record in records] == [logging.ERROR]


def test_step_that_fails_as_a_whole_raises_before_its_flag_is_set() -> None:
    """A failing step is not marked as done, so the next start runs it again."""
    data = _settings(_record("sonos_14", SONOS))

    with (
        patch.object(player_split, "_restore_missing_edges", side_effect=ValueError("boom")),
        pytest.raises(ValueError, match="boom"),
    ):
        migrate_player_split(data)

    assert CONF_PLAYER_SPLIT_TREE not in data


def test_session_only_step_does_not_run_on_split_data() -> None:
    """With only the flag of the tree step set, no player is taken for a session-only one."""
    data = _migrated(
        _settings(_record("up0015", UNIVERSAL_PLAYER), _protocol("ss_15", SENDSPIN, "up0015"))
    )
    del data[CONF_PLAYER_SPLIT_SESSION_ONLY]
    expected = deepcopy(data) | {CONF_PLAYER_SPLIT_SESSION_ONLY: True}

    assert migrate_player_split(data) is True

    assert data == expected


def test_protocol_config_of_the_new_shape_wins_over_a_leftover_of_the_old_one() -> None:
    """Where a protocol is stored in both shapes, the old record goes without overwriting."""
    data = _settings(_record("sonos_16", SONOS), _protocol("ap_16", AIRPLAY, "sonos_16"))
    stored = _protocol_record("ap_16", AIRPLAY, "sonos_16", values={"output_codec": "flac"})
    data[CONF_PLAYER_PROTOCOLS] = {"ap_16": deepcopy(stored)}

    result = _migrated(data)

    assert list(result[CONF_PLAYERS]) == ["sonos_16"]
    assert result[CONF_PLAYER_PROTOCOLS]["ap_16"] == stored


def test_new_installation_is_marked_as_split() -> None:
    """A config that never held player records needs no migration."""
    data: dict[str, Any] = {}
    assert needs_player_split(data) is True

    mark_player_split_done(data)

    assert data == FLAGS
    assert needs_player_split(data) is False
    assert migrate_player_split(data) is False


def _split_settings() -> dict[str, Any]:
    """Return a split config with the kinds of player records this version stores."""
    players = [
        _record("sonos_1", SONOS, values={"icon": "speaker"}),
        # a converted wrapper, which names the provider of its best protocol
        _record("up0032", AIRPLAY, name="Den TV", values={"device_info": {"model": "M"}}),
        _record("pl0123456789ab", CAST, values={"created_at": 1}),
        # a wrapper the user customised, kept without any protocol
        _record("up0007", "", name="Kitchen"),
        _record("ss_speaker", SENDSPIN),
        # a browser player the user customised, of which nothing else is stored anymore
        _record("ss_marked", SENDSPIN, values={"session_only": True, "created_at": 1}),
    ]
    protocols = [
        _protocol_record("sonos_1", SONOS, "sonos_1", role="native"),
        _protocol_record("airplay_sonos", AIRPLAY, "sonos_1"),
        _protocol_record("airplay_den", AIRPLAY, "up0032"),
        _protocol_record("cast_den", CAST, "up0032"),
        _protocol_record("cast_new", CAST, "pl0123456789ab"),
        _protocol_record("ss_speaker", SENDSPIN, "ss_speaker", role="native"),
    ]
    return _settings(
        *players,
        player_protocols={record["protocol_id"]: record for record in protocols},
        player_dsp={"sonos_1": {"enabled": True}},
        player_queues={"up0032": {"values": {"crossfade": True}}},
        **FLAGS,
    )


# what an older version stores for the players of a split config, by player id
OLDER_VERSION_PLAYERS: dict[str, dict[str, Any]] = {
    record["player_id"]: record
    for record in (
        # the protocols of the players it finds, stored as protocol players again
        _protocol("airplay_den", AIRPLAY, "upa1b2c3d4"),
        _protocol("airplay_sonos", AIRPLAY, "sonos_1"),
        # one whose type was not updated yet
        _record("cast_den", CAST),
        # the new wrapper of a device without a native provider
        _record(
            "upa1b2c3d4",
            UNIVERSAL_PLAYER,
            values={"linked_protocol_ids": ["airplay_den", "cast_den"]},
        ),
        # a device it saw first, with a wrapper and protocols the split never saw
        _protocol("airplay_tv", AIRPLAY, "upb2c3d4e5"),
        _record("dlna_tv", DLNA, values={"protocol_parent_id": "upb2c3d4e5"}),
        # a protocol player whose wrapper was removed, which drops its link
        _protocol("airplay_gone", AIRPLAY, None),
        _record("upb2c3d4e5", UNIVERSAL_PLAYER, values={"linked_protocol_ids": ["airplay_tv"]}),
        # browser players, without the marker of a player that only exists while connected
        _record("ss_web_1", SENDSPIN, values={"sendspin_static_delay": 30}),
        _record("ss_web_2", SENDSPIN, name="Tablet"),
    )
}


def test_records_an_older_version_stored_for_a_split_config_are_removed() -> None:
    """Only the players of the split stay, with their records and protocols as they are."""
    expected = _split_settings()
    # what the older version adds to the record of a player the split kept is left there
    expected[CONF_PLAYERS]["sonos_1"]["values"] |= {
        "linked_protocol_ids": ["airplay_sonos"],
        "output_codec": "mp3",
    }
    data = deepcopy(expected)
    data[CONF_PLAYERS] |= deepcopy(OLDER_VERSION_PLAYERS)
    data[CONF_PLAYER_DSP] |= {"upa1b2c3d4": {"enabled": True}, "ss_web_1": {"enabled": False}}
    data[CONF_PLAYER_QUEUES] |= {"upa1b2c3d4": {"values": {}}}
    expected[CONF_PLAYERS]["ss_web_2"] = _record(
        "ss_web_2", SENDSPIN, name="Tablet", values={"session_only": True, **WEB_DEFAULTS}
    )
    # a browser player keeps what is stored next to it: its id comes back when it connects
    expected[CONF_PLAYER_DSP]["ss_web_1"] = {"enabled": False}

    with capture_log_records(player_split.LOGGER) as records:
        assert remove_player_records_of_older_versions(data) is True

    assert data == expected
    removed = set(OLDER_VERSION_PLAYERS) - {"ss_web_2"}
    assert sorted(record.getMessage() for record in records if record.levelno == logging.INFO) == (
        sorted(f"Removed the stored player {pid}, which an older version stored" for pid in removed)
    )
    assert [record.levelno for record in records if record.levelno > logging.INFO] == [
        logging.WARNING
    ]
    again = deepcopy(data)
    with capture_log_records(player_split.LOGGER) as records:
        assert remove_player_records_of_older_versions(again) is False
    assert again == data
    assert records == []


@pytest.mark.parametrize(
    "new_player",
    [
        # the user unlinked it, which gives it a player of its own
        _record("plaabbccddeeff", SONOS, values={"created_at": 2}),
        # its stored link was dropped at an attach, and it was matched to another player
        None,
    ],
    ids=["unlinked", "matched_elsewhere"],
)
def test_a_player_whose_own_protocol_went_to_another_player_stays(
    new_player: dict[str, Any] | None,
) -> None:
    """A player from before the split keeps its record while any protocol still names it."""
    data = _split_settings()
    if new_player is not None:
        data[CONF_PLAYERS][new_player["player_id"]] = new_player
    owner = new_player["player_id"] if new_player is not None else "pl0123456789ab"
    data[CONF_PLAYER_PROTOCOLS]["sonos_1"]["player_id"] = owner
    data[CONF_PLAYER_QUEUES]["sonos_1"] = {"values": {"crossfade": True}}
    expected = deepcopy(data)

    assert remove_player_records_of_older_versions(data) is False
    assert data == expected
    assert remove_player_records_of_older_versions(data) is False
    assert data == expected


@pytest.mark.parametrize("name", [None, "My speaker"], ids=["unnamed", "named"])
def test_a_sendspin_player_this_version_created_stays_without_its_protocol(
    name: str | None,
) -> None:
    """
    A permanent Sendspin player whose client turned into a session-only one is left alone.

    Its protocol record names the session player the user customised since.
    """
    data = _split_settings()
    data[CONF_PLAYERS]["pl00aa11bb22cc"] = _record(
        "pl00aa11bb22cc", SENDSPIN, name=name, values={"created_at": 1}
    )
    data[CONF_PLAYERS]["ss_client"] = _record(
        "ss_client", SENDSPIN, values={"session_only": True, "created_at": 2}
    )
    data[CONF_PLAYER_PROTOCOLS]["ss_client"] = _protocol_record(
        "ss_client", SENDSPIN, "ss_client", role="native"
    )
    expected = deepcopy(data)

    assert remove_player_records_of_older_versions(data) is False
    assert data == expected


def test_the_old_lists_of_leftovers_protect_nothing() -> None:
    """In a split config only the protocol records say who owns what: one run removes all."""
    data = _split_settings()
    data[CONF_PLAYERS] |= {
        "ss_a": _record("ss_a", SENDSPIN, values={"linked_protocol_ids": ["ss_b"]}),
        "ss_b": _record("ss_b", SENDSPIN),
    }
    expected = _split_settings()

    assert remove_player_records_of_older_versions(data) is True
    assert data == expected
    assert remove_player_records_of_older_versions(data) is False
    assert data == expected


def test_an_unsplit_config_is_left_to_the_split() -> None:
    """Before the split, the records of the old shape are the ones the split converts."""
    data = _settings(_record("up0040", UNIVERSAL_PLAYER), _protocol("ap_40", AIRPLAY, "up0040"))
    expected = deepcopy(data)

    assert remove_player_records_of_older_versions(data) is False
    assert data == expected


@pytest.mark.parametrize(
    "trees",
    [
        {CONF_PLAYERS: {"broken": "not a dict"}},
        {CONF_PLAYERS: {"odd": _record("odd", SONOS, values="not a dict")}},
        {CONF_PLAYERS: "not a dict"},
        {CONF_PLAYER_PROTOCOLS: "not a dict"},
        {CONF_PLAYER_PROTOCOLS: {"sonos_1": "not a dict"}},
    ],
    ids=["record", "values", "players", "protocols", "protocol_record"],
)
def test_records_that_can_not_be_read_are_left_alone(trees: dict[str, Any]) -> None:
    """What can not be read stays as it is, and the cleanup does not raise for it."""
    data = _split_settings()
    for key, tree in trees.items():
        if isinstance(tree, dict) and isinstance(data.get(key), dict):
            data[key] |= deepcopy(tree)
        else:
            data[key] = deepcopy(tree)
    expected = deepcopy(data)

    assert remove_player_records_of_older_versions(data) is False
    assert data == expected


def _make_controller(tmp_path: Path) -> ConfigController:
    """Return a config controller that stores its settings in a folder."""
    return ConfigController(SimpleNamespace(storage_path=str(tmp_path)))  # type: ignore[arg-type]


async def test_settings_from_before_the_split_are_kept_as_a_backup(tmp_path: Path) -> None:
    """The settings file is copied once before the split, and the split is saved."""
    settings = tmp_path / "settings.json"
    backup = tmp_path / f"settings.json{BACKUP_SUFFIX}"
    before = json.dumps(_settings(_record("sonos_1", SONOS)))
    settings.write_text(before)

    with capture_log_records(player_split.LOGGER) as records:
        await _make_controller(tmp_path)._load()

    assert backup.read_text() == before
    stored = json.loads(settings.read_text())
    assert stored[CONF_PLAYER_SPLIT_TREE] is True
    assert list(stored[CONF_PLAYER_PROTOCOLS]) == ["sonos_1"]
    assert [r.levelno for r in records if str(backup) in r.getMessage()] == [logging.INFO]

    # a later start leaves the copy alone
    with capture_log_records(player_split.LOGGER) as records:
        await _make_controller(tmp_path)._load()

    assert backup.read_text() == before
    assert records == []


async def test_backup_is_not_overwritten_by_a_retry(tmp_path: Path) -> None:
    """A start that runs the split again keeps the copy the first one made."""
    settings = tmp_path / "settings.json"
    backup = tmp_path / f"settings.json{BACKUP_SUFFIX}"
    backup.write_text("the first copy")
    settings.write_text(json.dumps(_settings(_record("sonos_1", SONOS))))

    await _make_controller(tmp_path)._load()

    assert backup.read_text() == "the first copy"


async def test_empty_backup_is_written_again(tmp_path: Path) -> None:
    """An empty copy, as a power failure can leave it, does not count as a backup."""
    settings = tmp_path / "settings.json"
    backup = tmp_path / f"settings.json{BACKUP_SUFFIX}"
    before = json.dumps(_settings(_record("sonos_1", SONOS)))
    settings.write_text(before)
    backup.write_text("")

    await _make_controller(tmp_path)._load()

    assert backup.read_text() == before


async def test_start_stops_when_the_backup_can_not_be_written(tmp_path: Path) -> None:
    """Without a backup nothing is migrated: the file stays as it was for the next start."""
    settings = tmp_path / "settings.json"
    before = json.dumps(_settings(_record("sonos_1", SONOS)))
    settings.write_text(before)
    controller = _make_controller(tmp_path)

    with (
        patch.object(Path, "replace", side_effect=OSError("disk full")),
        pytest.raises(RuntimeError, match="disk full"),
    ):
        await controller._load()

    assert settings.read_text() == before
    assert not (tmp_path / f"settings.json{BACKUP_SUFFIX}").exists()


async def test_split_runs_again_after_a_save_that_was_interrupted(tmp_path: Path) -> None:
    """A crash between the two renames of a save leaves the old file, which is split again."""
    before = _settings(
        _record("up0001", UNIVERSAL_PLAYER, name="Living"), _protocol("ap_1", AIRPLAY, "up0001")
    )
    complete = tmp_path / "complete"
    complete.mkdir()
    (complete / "settings.json").write_text(json.dumps(before))
    await _make_controller(complete)._load()
    expected = json.loads((complete / "settings.json").read_text())

    # the previous file was rotated to the backup, the new one never got its name
    interrupted = tmp_path / "interrupted"
    interrupted.mkdir()
    (interrupted / "settings.json.backup").write_text(json.dumps(before))
    (interrupted / "settings.json.tmp").write_text(json.dumps(expected)[:40])
    await _make_controller(interrupted)._load()

    assert json.loads((interrupted / "settings.json").read_text()) == expected
    assert json.loads((interrupted / f"settings.json{BACKUP_SUFFIX}").read_text()) == before


async def test_a_declined_audio_input_is_found_under_the_id_of_its_client(
    tmp_path: Path,
) -> None:
    """After the upgrade a Sendspin client reads the decline from its own protocol config."""
    before = _settings(
        _record("ss_50", SENDSPIN, values={"source_input_declined": True}),
        _record("sq_52", SQUEEZELITE),
        _protocol("ss_52", SENDSPIN, "sq_52", values={"source_input_declined": True}),
    )
    (tmp_path / "settings.json").write_text(json.dumps(before))
    controller = _make_controller(tmp_path)

    await controller._load()
    # as setup() does right after the load; its further steps need a running server
    controller.initialized = True

    for client_id in ("ss_50", "ss_52"):
        assert controller.get_protocol_value(client_id, "source_input_declined", False) is True


async def test_start_with_a_malformed_group_member_succeeds_every_time(tmp_path: Path) -> None:
    """A start migrates a group with a malformed member, and so does every start after it."""
    settings = tmp_path / "settings.json"
    group = _record(
        "syncgroup_19",
        "sync_group",
        "group",
        values={"group_members": ["sonos_19", {"player_id": "sonos_19"}]},
    )
    settings.write_text(json.dumps(_settings(_record("sonos_19", SONOS), group)))

    for _ in range(2):
        await _make_controller(tmp_path)._load()

        stored = json.loads(settings.read_text())
        assert stored[CONF_PLAYER_PROTOCOLS]["syncgroup_19"]["values"] == {
            "group_members": ["sonos_19"]
        }


async def test_new_installation_gets_no_backup(tmp_path: Path) -> None:
    """A server without a settings file starts out split."""
    controller = _make_controller(tmp_path)

    await controller._load()

    assert controller._data == FLAGS
    assert list(tmp_path.iterdir()) == []


async def test_protocols_attach_to_a_wrapper_that_was_stored_as_a_group(
    scenario: Scenario,  # noqa: F811
) -> None:
    """The protocols of a wrapper whose creation was interrupted come back on its player."""
    airplay = await scenario.add_provider("airplay")
    chromecast = await scenario.add_provider("chromecast")
    den = {IdentifierType.MAC_ADDRESS: "00:11:22:33:44:55"}
    den_airplay = airplay.protocol("airplay_den", "Den", role="audio", identifiers=den)
    den_cast = chromecast.protocol("cast_den", "Den", role="audio", identifiers=den)
    scenario.set_present("airplay_den", True)
    scenario.set_present("cast_den", True)
    wrapper = _record(
        "up0032",
        UNIVERSAL_PLAYER,
        "group",
        name="Den TV",
        values={"linked_protocol_ids": ["airplay_den", "cast_den"]},
    )
    stored = [
        wrapper,
        _protocol("airplay_den", airplay.instance_id, "up0032"),
        _protocol("cast_den", chromecast.instance_id, "up0032"),
    ]

    await scenario.seed_pre_split_config({record["player_id"]: record for record in stored})
    await scenario.idle()

    player = scenario.single_player(den_airplay, den_cast)
    assert (player.player_id, player.name) == ("up0032", "Den TV")
    assert [visible.player_id for visible in scenario.visible_players()] == ["up0032"]
