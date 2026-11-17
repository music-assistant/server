"""Tests for the settings migration that gives every player one Sendspin output."""

from __future__ import annotations

import logging
from copy import deepcopy
from typing import Any

import pytest

from music_assistant.constants import (
    CONF_PROTOCOL_EXPERIMENTAL_NOTE,
    CONF_PROTOCOL_OFF_BY_DEFAULT,
    CONF_SENDSPIN_ONE_OUTPUT_MIGRATED,
    CONF_UNDERLYING_PLAYER_ID,
)
from music_assistant.controllers.config import migrations
from music_assistant.controllers.config.migrations import (
    LEGACY_SENDSPIN_BRIDGE_ORDER,
    _migrate_sendspin_one_output,
    migrate,
)
from music_assistant.providers.sendspin.constants import (
    CONF_SENDSPIN_BRIDGE_STATIC_DELAYS,
    CONF_SENDSPIN_CAST_OPT_IN,
    CONF_SENDSPIN_STATIC_DELAY,
)

PLAYER_ID = "pl0123456789ab"
AIRPLAY_ID = "apa8bbccddeeff"
AIRPLAY_OUTPUT = "spb_a8bbccddeeff"
SQUEEZELITE_ID = "aa:bb:cc:dd:ee:ff"
CAST_ID = "4f6c2b1a-0000-4000-8000-aabbccddeeff"
# squeezelite and Cast report the MAC address of the device as it is
PLAIN_OUTPUT = "spb_aabbccddeeff"
NOTE = "sendspin_cast_experimental"
IDS = {"airplay": AIRPLAY_ID, "squeezelite": SQUEEZELITE_ID, "chromecast": CAST_ID}


def _settings(*domains: str) -> dict[str, Any]:
    """
    Return the settings of a player with a protocol of each of the given providers.

    :param domains: The domains of the providers, each with an instance id of its own.
    """
    data: dict[str, Any] = {
        "players": {PLAYER_ID: {"player_id": PLAYER_ID, "enabled": True, "values": {}}},
        "player_protocols": {},
        "providers": {"sendspin": {"domain": "sendspin", "enabled": True}},
    }
    for domain in domains:
        data["providers"][f"{domain}--x1"] = {"domain": domain, "enabled": True}
        data["player_protocols"][IDS[domain]] = {
            "protocol_id": IDS[domain],
            "provider": f"{domain}--x1",
            "player_id": PLAYER_ID,
            "enabled": True,
            "values": {},
        }
    return data


def _output(
    data: dict[str, Any],
    output_id: str,
    carrier: str,
    *,
    enabled: bool = True,
    values: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Store a Sendspin output of the player, as the bridge of one of its protocols left it.

    :param data: The settings.
    :param output_id: The client id of the output.
    :param carrier: The domain of the provider whose protocol the output rides on.
    :param enabled: Whether the output is switched on.
    :param values: Other values of the output.
    :return: The stored record.
    """
    record: dict[str, Any] = {
        "protocol_id": output_id,
        "provider": "sendspin",
        "player_id": PLAYER_ID,
        "enabled": enabled,
        "values": {CONF_UNDERLYING_PLAYER_ID: IDS.get(carrier, carrier), **(values or {})},
    }
    data["player_protocols"][output_id] = record
    return record


def _outputs(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return the stored Sendspin outputs, by their id."""
    return {
        protocol_id: record
        for protocol_id, record in data["player_protocols"].items()
        if isinstance(record, dict) and record.get("provider") == "sendspin"
    }


def test_the_keys_of_the_migration_are_those_the_bridges_use() -> None:
    """The migration writes what the bridge managers read, in the order they had before."""
    assert migrations._SENDSPIN_STATIC_DELAY == CONF_SENDSPIN_STATIC_DELAY
    assert migrations._SENDSPIN_BRIDGE_STATIC_DELAYS == CONF_SENDSPIN_BRIDGE_STATIC_DELAYS
    assert migrations._SENDSPIN_CAST_OPT_IN == CONF_SENDSPIN_CAST_OPT_IN
    assert LEGACY_SENDSPIN_BRIDGE_ORDER == ("airplay", "squeezelite", "chromecast")


# ---------------------------------------------------------------- one output


@pytest.mark.parametrize("carrier", ["airplay", "squeezelite"])
@pytest.mark.parametrize("enabled", [True, False])
def test_the_one_output_of_airplay_or_squeezelite_stays_as_it_is(
    carrier: str, enabled: bool
) -> None:
    """An output AirPlay or squeezelite carries is the user's, switched on or off."""
    data = _settings(carrier)
    _output(data, PLAIN_OUTPUT, carrier, enabled=enabled, values={CONF_SENDSPIN_STATIC_DELAY: 77})
    before = deepcopy(data)

    assert _migrate_sendspin_one_output(data) is True

    assert data.pop(CONF_SENDSPIN_ONE_OUTPUT_MIGRATED) is True
    assert data == before


def test_a_cast_output_that_is_off_is_marked_as_its_default() -> None:
    """A Cast output that is switched off was left off by the bridge, as far as is known."""
    data = _settings("chromecast")
    output = _output(data, PLAIN_OUTPUT, "chromecast", enabled=False)
    output["values"][CONF_PROTOCOL_EXPERIMENTAL_NOTE] = NOTE

    _migrate_sendspin_one_output(data)

    assert output["enabled"] is False
    assert output["values"] == {
        CONF_UNDERLYING_PLAYER_ID: CAST_ID,
        CONF_PROTOCOL_EXPERIMENTAL_NOTE: NOTE,
        CONF_PROTOCOL_OFF_BY_DEFAULT: True,
    }


def test_a_cast_output_that_is_on_is_marked_as_opted_in() -> None:
    """A Cast output that is switched on was switched on by the user."""
    data = _settings("chromecast")
    output = _output(data, PLAIN_OUTPUT, "chromecast")

    _migrate_sendspin_one_output(data)

    assert output["enabled"] is True
    assert output["values"] == {CONF_UNDERLYING_PLAYER_ID: CAST_ID, CONF_SENDSPIN_CAST_OPT_IN: True}


@pytest.mark.parametrize("enabled", [True, False])
def test_a_cast_output_never_offered_to_the_user_is_marked_as_its_default(enabled: bool) -> None:
    """A Cast output the bridge had yet to switch off is left off, not read as opted in."""
    data = _settings("chromecast")
    data["player_protocols"][CAST_ID]["values"]["sendspin_opt_out_pending"] = True
    output = _output(data, PLAIN_OUTPUT, "chromecast", enabled=enabled)

    _migrate_sendspin_one_output(data)

    assert output["enabled"] is False
    assert output["values"] == {
        CONF_UNDERLYING_PLAYER_ID: CAST_ID,
        CONF_PROTOCOL_OFF_BY_DEFAULT: True,
    }
    assert data["player_protocols"][CAST_ID]["values"] == {}, "the Cast marker is left behind"


def test_a_cast_output_with_its_warning_is_settled_whatever_its_carrier_still_has() -> None:
    """The warning is stored when the bridge is done with an output, the Cast marker after it."""
    data = _settings("chromecast")
    data["player_protocols"][CAST_ID]["values"]["sendspin_opt_out_pending"] = True
    output = _output(
        data, PLAIN_OUTPUT, "chromecast", values={CONF_PROTOCOL_EXPERIMENTAL_NOTE: NOTE}
    )

    _migrate_sendspin_one_output(data)

    assert output["enabled"] is True
    assert output["values"][CONF_SENDSPIN_CAST_OPT_IN] is True
    assert data["player_protocols"][CAST_ID]["values"] == {}, "the Cast marker is left behind"


# ---------------------------------------------------------------- several outputs


def test_the_airplay_output_in_use_stays_next_to_a_cast_default() -> None:
    """The output the player uses stays switched on, with the delay Cast had set aside."""
    data = _settings("airplay", "chromecast")
    _output(data, AIRPLAY_OUTPUT, "airplay", values={CONF_SENDSPIN_STATIC_DELAY: 77})
    _output(
        data,
        PLAIN_OUTPUT,
        "chromecast",
        enabled=False,
        values={CONF_SENDSPIN_STATIC_DELAY: 330, CONF_PROTOCOL_EXPERIMENTAL_NOTE: NOTE},
    )

    _migrate_sendspin_one_output(data)

    assert _outputs(data) == {
        AIRPLAY_OUTPUT: {
            "protocol_id": AIRPLAY_OUTPUT,
            "provider": "sendspin",
            "player_id": PLAYER_ID,
            "enabled": True,
            "values": {
                CONF_UNDERLYING_PLAYER_ID: AIRPLAY_ID,
                CONF_SENDSPIN_STATIC_DELAY: 77,
                CONF_SENDSPIN_BRIDGE_STATIC_DELAYS: {CAST_ID: 330},
            },
        }
    }


def test_an_airplay_output_the_user_switched_off_stays_next_to_a_cast_default() -> None:
    """A player without Sendspin by the choice of its user stays without, whoever carries it."""
    data = _settings("squeezelite", "airplay", "chromecast")
    _output(data, AIRPLAY_OUTPUT, "airplay", enabled=False, values={CONF_SENDSPIN_STATIC_DELAY: 77})
    _output(
        data,
        PLAIN_OUTPUT,
        "chromecast",
        enabled=False,
        values={CONF_PROTOCOL_EXPERIMENTAL_NOTE: NOTE},
    )

    _migrate_sendspin_one_output(data)

    (output_id, output), *others = _outputs(data).items()
    assert not others, "the player still has more than one Sendspin output"
    assert output_id == AIRPLAY_OUTPUT, "the Cast default was kept instead of the output in use"
    assert output["enabled"] is False
    assert CONF_PROTOCOL_OFF_BY_DEFAULT not in output["values"], (
        "the off of the user reads as a default of the Cast bridge"
    )
    assert output["values"][CONF_SENDSPIN_STATIC_DELAY] == 77


@pytest.mark.parametrize(
    ("switched_off", "kept"),
    [
        pytest.param((), "airplay", id="all on: airplay"),
        pytest.param(("airplay",), "squeezelite", id="airplay off: squeezelite"),
        pytest.param(("airplay", "squeezelite"), "chromecast", id="only cast on: cast"),
    ],
)
def test_the_output_of_the_first_protocol_that_is_switched_on_stays(
    switched_off: tuple[str, ...], kept: str
) -> None:
    """The bridges took a device in a fixed order, among the protocols that were switched on."""
    data = _settings("airplay", "squeezelite", "chromecast")
    for domain in switched_off:
        data["player_protocols"][IDS[domain]]["enabled"] = False
    _output(data, "spb_1", "chromecast", values={CONF_PROTOCOL_EXPERIMENTAL_NOTE: NOTE})
    _output(data, "spb_2", "squeezelite")
    _output(data, "spb_3", "airplay")

    _migrate_sendspin_one_output(data)

    (output,) = _outputs(data).values()
    assert output["values"][CONF_UNDERLYING_PLAYER_ID] == IDS[kept]
    # the opt-in of the Cast output is kept, the warning only while Cast carries the output
    assert output["values"][CONF_SENDSPIN_CAST_OPT_IN] is True
    assert (CONF_PROTOCOL_EXPERIMENTAL_NOTE in output["values"]) is (kept == "chromecast")


def test_a_disabled_provider_counts_as_its_protocol_switched_off() -> None:
    """The output of a protocol whose provider is disabled is not the one in use."""
    data = _settings("airplay", "squeezelite")
    data["providers"]["airplay--x1"]["enabled"] = False
    _output(data, AIRPLAY_OUTPUT, "airplay")
    _output(data, PLAIN_OUTPUT, "squeezelite", enabled=False)

    _migrate_sendspin_one_output(data)

    assert list(_outputs(data)) == [PLAIN_OUTPUT]
    assert _outputs(data)[PLAIN_OUTPUT]["enabled"] is False


def test_an_output_that_is_switched_on_stays_among_those_no_protocol_carries() -> None:
    """Outputs whose carriers are all switched off: one that is on stays, else the lowest id."""
    data = _settings("airplay", "squeezelite")
    for domain in ("airplay", "squeezelite"):
        data["player_protocols"][IDS[domain]]["enabled"] = False
    _output(data, "spb_1", "gone_1", enabled=False)
    _output(data, "spb_2", "gone_2")
    _output(data, "spb_3", "gone_3")

    _migrate_sendspin_one_output(data)

    assert list(_outputs(data)) == ["spb_2"]


def test_the_order_of_the_bridges_does_not_count_among_carriers_that_are_switched_off() -> None:
    """Outputs whose stored carriers are all switched off: the one that is on stays."""
    data = _settings("airplay", "squeezelite")
    for domain in ("airplay", "squeezelite"):
        data["player_protocols"][IDS[domain]]["enabled"] = False
    _output(data, AIRPLAY_OUTPUT, "airplay", enabled=False)
    _output(data, PLAIN_OUTPUT, "squeezelite")

    _migrate_sendspin_one_output(data)

    assert list(_outputs(data)) == [PLAIN_OUTPUT], "the output that is switched on was removed"
    assert _outputs(data)[PLAIN_OUTPUT]["enabled"] is True


def test_the_off_of_the_user_stays_next_to_a_cast_default_on_a_carrier_that_is_on() -> None:
    """A Cast default never wins over an output the user switched off, whichever carrier is on."""
    data = _settings("airplay", "chromecast")
    data["player_protocols"][AIRPLAY_ID]["enabled"] = False
    _output(data, AIRPLAY_OUTPUT, "airplay", enabled=False)
    _output(
        data,
        PLAIN_OUTPUT,
        "chromecast",
        enabled=False,
        values={CONF_PROTOCOL_EXPERIMENTAL_NOTE: NOTE},
    )

    _migrate_sendspin_one_output(data)

    (output_id, output), *others = _outputs(data).items()
    assert not others, "the player still has more than one Sendspin output"
    assert output_id == AIRPLAY_OUTPUT, "the Cast default was kept instead of the off of the user"
    assert output["enabled"] is False
    assert CONF_PROTOCOL_OFF_BY_DEFAULT not in output["values"]


def test_the_output_that_stays_takes_the_settings_it_has_none_of() -> None:
    """Settings of a removed output move over, what identifies it does not."""
    data = _settings("airplay", "squeezelite")
    data["players"][PLAYER_ID]["values"] = {
        "preferred_output_protocol": PLAIN_OUTPUT,
        "volume_control": PLAIN_OUTPUT,
        "mute_control": AIRPLAY_ID,
    }
    _output(data, AIRPLAY_OUTPUT, "airplay", values={"output_channels": "left"})
    _output(
        data,
        PLAIN_OUTPUT,
        "squeezelite",
        enabled=False,
        values={
            "output_channels": "right",
            "sample_rates": ["44100:16"],
            "identifiers": [["mac_address", "AA:BB:CC:DD:EE:FF"]],
            "cached_arp_mac": "AA:BB:CC:DD:EE:FF",
            CONF_SENDSPIN_STATIC_DELAY: 20,
            CONF_SENDSPIN_BRIDGE_STATIC_DELAYS: {AIRPLAY_ID: 77, CAST_ID: 330},
        },
    )

    _migrate_sendspin_one_output(data)

    assert _outputs(data)[AIRPLAY_OUTPUT]["values"] == {
        CONF_UNDERLYING_PLAYER_ID: AIRPLAY_ID,
        "output_channels": "left",
        "sample_rates": ["44100:16"],
        # the delay set while AirPlay carried the removed output is its delay again
        CONF_SENDSPIN_STATIC_DELAY: 77,
        CONF_SENDSPIN_BRIDGE_STATIC_DELAYS: {SQUEEZELITE_ID: 20, CAST_ID: 330},
    }
    # a choice of the player that named the removed output names the one that stays
    assert data["players"][PLAYER_ID]["values"] == {
        "preferred_output_protocol": AIRPLAY_OUTPUT,
        "volume_control": AIRPLAY_OUTPUT,
        "mute_control": AIRPLAY_ID,
    }


def test_a_removed_output_is_logged_in_full(caplog: pytest.LogCaptureFixture) -> None:
    """What the migration removes can be read back from the log."""
    data = _settings("airplay", "squeezelite")
    _output(data, AIRPLAY_OUTPUT, "airplay")
    removed = deepcopy(
        _output(data, PLAIN_OUTPUT, "squeezelite", enabled=False, values={"setting": "kept?"})
    )

    with caplog.at_level(logging.INFO, logger=migrations.LOGGER.name):
        _migrate_sendspin_one_output(data)

    (record,) = [record for record in caplog.records if PLAIN_OUTPUT in record.getMessage()]
    assert str(removed) in record.getMessage()
    assert PLAYER_ID in record.getMessage()
    assert AIRPLAY_OUTPUT in record.getMessage()


# ---------------------------------------------------------------- outputs without a carrier or player


@pytest.mark.parametrize(
    ("enabled", "note", "marked"),
    [
        (False, NOTE, {CONF_PROTOCOL_OFF_BY_DEFAULT: True}),
        (True, NOTE, {CONF_SENDSPIN_CAST_OPT_IN: True}),
        # without the warning of Cast, nothing tells whose output it was
        (False, None, {}),
    ],
)
def test_an_output_whose_carrier_is_gone_is_a_cast_output_by_its_warning(
    enabled: bool, note: str | None, marked: dict[str, bool]
) -> None:
    """The warning the Cast bridge stored tells a Cast output when its protocol is gone."""
    data = _settings("airplay")
    values = {CONF_PROTOCOL_EXPERIMENTAL_NOTE: note} if note else {}
    output = _output(data, PLAIN_OUTPUT, "gone", enabled=enabled, values=values)

    _migrate_sendspin_one_output(data)

    assert output["enabled"] is enabled
    assert output["values"] == {CONF_UNDERLYING_PLAYER_ID: "gone", **values, **marked}


@pytest.mark.parametrize("player_id", [None, "pl_removed", 7])
def test_outputs_without_a_stored_player_are_marked_but_not_folded(player_id: Any) -> None:
    """An output that belongs to no stored player is left for the bridge that meets it."""
    data = _settings("airplay", "chromecast")
    first = _output(data, AIRPLAY_OUTPUT, "airplay")
    second = _output(data, PLAIN_OUTPUT, "chromecast", enabled=False)
    first["player_id"] = second["player_id"] = player_id

    _migrate_sendspin_one_output(data)

    assert set(_outputs(data)) == {AIRPLAY_OUTPUT, PLAIN_OUTPUT}
    assert second["values"][CONF_PROTOCOL_OFF_BY_DEFAULT] is True


# ---------------------------------------------------------------- once, and never raising


def test_a_second_run_changes_nothing() -> None:
    """The migration runs once: an output switched off afterwards is no default of Cast."""
    data = _settings("airplay", "chromecast")
    _output(data, AIRPLAY_OUTPUT, "airplay")
    cast_output = _output(data, PLAIN_OUTPUT, "chromecast")
    assert _migrate_sendspin_one_output(data) is True
    # the user switches the output off, which Cast carries after AirPlay was switched off
    data["player_protocols"][AIRPLAY_ID]["enabled"] = False
    _output(data, AIRPLAY_OUTPUT, "chromecast", enabled=False)
    del cast_output
    after_first = deepcopy(data)

    assert _migrate_sendspin_one_output(data) is False, "the second run reports a change"

    assert data == after_first


def test_the_fold_itself_is_done_after_one_run() -> None:
    """Without its guard too, a second run finds nothing left to fold or mark."""
    data = _settings("airplay", "squeezelite", "chromecast")
    _output(data, AIRPLAY_OUTPUT, "airplay", enabled=False, values={CONF_SENDSPIN_STATIC_DELAY: 77})
    _output(
        data,
        PLAIN_OUTPUT,
        "chromecast",
        enabled=False,
        values={CONF_PROTOCOL_EXPERIMENTAL_NOTE: NOTE},
    )
    _migrate_sendspin_one_output(data)
    after_first = deepcopy(data)
    del data[CONF_SENDSPIN_ONE_OUTPUT_MIGRATED]

    _migrate_sendspin_one_output(data)

    assert data == after_first


@pytest.mark.parametrize(
    "data",
    [
        pytest.param({}, id="empty"),
        pytest.param({"player_protocols": None, "players": None, "providers": None}, id="nulls"),
        pytest.param({"player_protocols": [], "players": "x", "providers": 3}, id="wrong types"),
    ],
)
def test_the_guard_is_claimed_when_there_is_nothing_to_migrate(data: dict[str, Any]) -> None:
    """A config without Sendspin outputs is marked as migrated all the same."""
    before = deepcopy(data)

    assert _migrate_sendspin_one_output(data) is True

    assert data.pop(CONF_SENDSPIN_ONE_OUTPUT_MIGRATED) is True
    assert data == before


def test_malformed_records_are_passed_over() -> None:
    """Records that are no record, or miss what the migration reads, never make it raise."""
    data = _settings("airplay", "chromecast")
    protocols = data["player_protocols"]
    _output(data, AIRPLAY_OUTPUT, "airplay")
    _output(data, PLAIN_OUTPUT, "chromecast", enabled=False)
    protocols["no_record"] = "garbage"
    protocols["null_record"] = None
    protocols["null_values"] = {"provider": "sendspin", "player_id": PLAYER_ID, "values": None}
    protocols["list_values"] = {"provider": "sendspin", "player_id": PLAYER_ID, "values": []}
    protocols["no_provider"] = {"player_id": PLAYER_ID, "values": {CONF_UNDERLYING_PLAYER_ID: "x"}}
    protocols["odd_provider"] = {"provider": ["sendspin"], "values": {}}
    protocols["no_player"] = {"provider": "sendspin", "values": {CONF_UNDERLYING_PLAYER_ID: 5}}
    protocols["carrier_record"] = ["garbage"]
    malformed = {key: deepcopy(protocols[key]) for key in protocols if not key.startswith("spb_")}
    # an output of the player that can be read, whose carrier is a record that is none
    protocols["spb_odd_carrier"] = {
        "provider": "sendspin",
        "player_id": PLAYER_ID,
        "enabled": False,
        "values": {CONF_UNDERLYING_PLAYER_ID: "carrier_record"},
    }
    malformed.pop(AIRPLAY_ID)
    malformed.pop(CAST_ID)
    protocols[CAST_ID]["provider"] = {"odd": "provider"}
    # the provider entry of AirPlay is gone, and that of Sendspin is no entry
    del data["providers"]["airplay--x1"]
    data["providers"]["sendspin"] = None
    data["players"][PLAYER_ID]["values"] = None

    assert _migrate_sendspin_one_output(data) is True

    assert data[CONF_SENDSPIN_ONE_OUTPUT_MIGRATED] is True
    assert {key: protocols[key] for key in malformed} == malformed, "a malformed record changed"
    # the three outputs that can be read are folded all the same
    assert [key for key in protocols if key.startswith("spb_")] == [AIRPLAY_OUTPUT]


@pytest.mark.parametrize(
    "carrier_values",
    [None, [], "garbage", {"sendspin_opt_out_pending": "yes"}, {"sendspin_opt_out_pending": None}],
)
def test_a_damaged_cast_marker_never_makes_the_migration_raise(carrier_values: Any) -> None:
    """Damaged values of a Cast protocol, and outputs that miss what is read, are passed over."""
    never_offered = isinstance(carrier_values, dict) and bool(
        carrier_values["sendspin_opt_out_pending"]
    )
    data = _settings("chromecast")
    data["player_protocols"][CAST_ID]["values"] = carrier_values
    _output(data, PLAIN_OUTPUT, "chromecast")
    # an output without a stored switch, and one whose player is gone
    del data["player_protocols"][PLAIN_OUTPUT]["enabled"]
    gone = _output(data, "spb_gone", "chromecast", values={"sendspin_opt_out_pending": True})
    gone["player_id"] = "pl_removed"

    assert _migrate_sendspin_one_output(data) is True

    assert data[CONF_SENDSPIN_ONE_OUTPUT_MIGRATED] is True
    output = _outputs(data)[PLAIN_OUTPUT]
    assert output.get("enabled", True) is not never_offered
    assert (CONF_SENDSPIN_CAST_OPT_IN in output["values"]) is not never_offered
    if isinstance(carrier_values, dict):
        assert "sendspin_opt_out_pending" not in carrier_values
    else:
        assert data["player_protocols"][CAST_ID]["values"] == carrier_values
    after_first = deepcopy(data)
    del data[CONF_SENDSPIN_ONE_OUTPUT_MIGRATED]
    _migrate_sendspin_one_output(data)
    assert data == after_first, "a second run changed the settings"


def test_a_player_that_can_not_be_folded_is_skipped(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A fold that fails leaves that player as it was, and the others are folded."""
    data = _settings("airplay", "squeezelite")
    other_player = "pl00000000000a"
    data["players"][other_player] = {"player_id": other_player, "values": {}}
    _output(data, AIRPLAY_OUTPUT, "airplay")
    _output(data, PLAIN_OUTPUT, "squeezelite", enabled=False)
    for output_id in ("spb_other_1", "spb_other_2"):
        _output(data, output_id, "gone")["player_id"] = other_player
    fold = migrations._fold_sendspin_outputs

    def _failing_fold(settings: dict[str, Any], player_id: str, output_ids: list[str]) -> None:
        if player_id == PLAYER_ID:
            raise RuntimeError("unexpected data")
        fold(settings, player_id, output_ids)

    monkeypatch.setattr(migrations, "_fold_sendspin_outputs", _failing_fold)

    with caplog.at_level(logging.ERROR, logger=migrations.LOGGER.name):
        assert _migrate_sendspin_one_output(data) is True

    assert set(_outputs(data)) == {AIRPLAY_OUTPUT, PLAIN_OUTPUT, "spb_other_1"}
    assert data[CONF_SENDSPIN_ONE_OUTPUT_MIGRATED] is True
    assert any(PLAYER_ID in record.getMessage() for record in caplog.records)


def test_a_failure_of_the_whole_step_is_logged_not_raised(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Whatever goes wrong, the migration returns and does not run again."""
    data = _settings("chromecast")
    _output(data, PLAIN_OUTPUT, "chromecast", enabled=False)

    def _fail(_settings: dict[str, Any]) -> None:
        raise RuntimeError("unexpected data")

    monkeypatch.setattr(migrations, "_mark_cast_sendspin_outputs", _fail)

    with caplog.at_level(logging.ERROR, logger=migrations.LOGGER.name):
        assert _migrate_sendspin_one_output(data) is True

    assert data[CONF_SENDSPIN_ONE_OUTPUT_MIGRATED] is True
    assert caplog.records, "the failure was not logged"


async def test_migrate_folds_the_sendspin_outputs_of_a_split_config() -> None:
    """The step is part of the migration of the settings, after the split of the players."""
    data = _settings("airplay", "chromecast")
    _output(data, AIRPLAY_OUTPUT, "airplay")
    _output(data, PLAIN_OUTPUT, "chromecast", enabled=False)

    assert await migrate(data) is True

    assert list(_outputs(data)) == [AIRPLAY_OUTPUT]
    assert data[CONF_SENDSPIN_ONE_OUTPUT_MIGRATED] is True
    assert await migrate(data) is False
