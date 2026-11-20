"""Tests for normalising measured calibration offsets into Sendspin static delays."""

from __future__ import annotations

import pytest
from music_assistant_models.errors import InvalidDataError

from music_assistant.providers.sendspin.calibration_session import resolve_static_delays
from music_assistant.providers.sendspin.constants import MAX_SENDSPIN_STATIC_DELAY

OWNER = "provider.sendspin"


def test_the_earliest_speaker_becomes_the_reference() -> None:
    """The earliest arrival is given 0 and every other speaker a positive delay."""
    delays_ms = resolve_static_delays(
        {"early": 10.0, "late": 35.0, "middle": 12.0},
        dict.fromkeys(("early", "late", "middle"), 0),
        OWNER,
    )

    assert delays_ms == {"early": 0, "late": 25, "middle": 2}


def test_only_the_differences_between_offsets_matter() -> None:
    """
    Shifting every measurement by the same amount yields the same delays.

    The probe measures against a baseline it picked for itself, so the absolute value
    of an offset carries no meaning and a negative one is not an error.
    """
    baseline = resolve_static_delays({"a": 100.0, "b": 130.0}, {"a": 0, "b": 0}, OWNER)

    assert resolve_static_delays({"a": -20.0, "b": 10.0}, {"a": 0, "b": 0}, OWNER) == baseline


def test_a_single_speaker_needs_no_correction() -> None:
    """One speaker is by definition the earliest, whatever it measured."""
    assert resolve_static_delays({"only": 87.5}, {"only": 0}, OWNER) == {"only": 0}


def test_an_existing_delay_is_part_of_the_baseline() -> None:
    """
    A delay a player already carries counts towards how late it really is.

    Ignoring it would treat a speaker that is only on time *because* it is already
    being advanced as if it needed no correction at all.
    """
    delays_ms = resolve_static_delays({"a": 50.0, "b": 0.0}, {"a": 0, "b": 30}, OWNER)

    assert delays_ms == {"a": 20, "b": 0}


def test_a_hand_set_delay_is_respected_rather_than_flattened() -> None:
    """A speaker the user already advanced by hand keeps that advance in the result."""
    delays_ms = resolve_static_delays(
        {"amp": 0.0, "speaker": 0.0}, {"amp": 250, "speaker": 0}, OWNER
    )

    assert delays_ms == {"amp": 250, "speaker": 0}


def test_re_applying_an_already_corrected_group_changes_nothing() -> None:
    """
    Calibration converges instead of drifting.

    Apply a result, then measure again: a corrected group now arrives together, so
    every offset is equal. Feeding those back must return the delays already in place
    rather than stacking a second correction on top of the first.
    """
    applied = resolve_static_delays(
        {"a": 0.0, "b": 120.0, "c": 35.0}, dict.fromkeys(("a", "b", "c"), 0), OWNER
    )

    re_applied = resolve_static_delays(dict.fromkeys(("a", "b", "c"), 7.5), applied, OWNER)

    assert applied == {"a": 0, "b": 120, "c": 35}
    assert re_applied == applied


def test_residual_jitter_does_not_accumulate_across_runs() -> None:
    """
    Repeated calibrations stay within the rounding of the first, rather than creeping.

    A real re-measurement of a corrected group is never perfectly equal - each speaker
    comes back a fraction of a millisecond off the others, either way. Because every pass
    re-derives the total from the raw latency, that residue is bounded by the rounding
    instead of being added to the correction already in place.
    """
    first = resolve_static_delays(
        {"a": 0.0, "b": 90.0, "c": 41.0}, dict.fromkeys(("a", "b", "c"), 0), OWNER
    )
    assert first == {"a": 0, "b": 90, "c": 41}

    delays_ms = first
    for run in range(10):
        # zero-mean, sign-alternating: sub-millisecond noise, not a systematic offset
        sign = 1.0 if run % 2 else -1.0
        delays_ms = resolve_static_delays(
            {"a": 0.4 * sign, "b": 0.0, "c": -0.4 * sign}, delays_ms, OWNER
        )
        assert all(abs(delays_ms[player_id] - first[player_id]) <= 1 for player_id in first)


def test_offsets_are_rounded_to_whole_milliseconds() -> None:
    """The protocol carries whole milliseconds, so fractional measurements are rounded."""
    delays_ms = resolve_static_delays(
        {"a": 0.0, "down": 12.4, "up": 40.6}, dict.fromkeys(("a", "down", "up"), 0), OWNER
    )

    assert delays_ms == {"a": 0, "down": 12, "up": 41}


@pytest.mark.parametrize("bad_offset", [float("nan"), float("inf"), float("-inf")])
def test_a_measurement_that_is_not_a_number_raises(bad_offset: float) -> None:
    """
    A non-finite measurement is refused before it can reach the minimum.

    ``min()`` over a NaN is order-dependent, so a single unusable value would either
    poison one delay or all of them depending on which key came first.
    """
    with pytest.raises(InvalidDataError) as raised:
        resolve_static_delays({"good": 0.0, "bad": bad_offset}, {"good": 0, "bad": 0}, OWNER)

    assert raised.value.translation_key == "calibration_offset_not_finite"
    assert raised.value.translation_args == ["bad"]


def test_a_measurement_beyond_the_supported_range_is_refused_not_clamped() -> None:
    """A delay no speaker could need is surfaced rather than turned into the maximum."""
    beyond_range = float(MAX_SENDSPIN_STATIC_DELAY + 1)

    with pytest.raises(InvalidDataError) as raised:
        resolve_static_delays({"a": 0.0, "absurd": beyond_range}, {"a": 0, "absurd": 0}, OWNER)

    assert raised.value.translation_key == "calibration_delay_out_of_range"
    # the message has to name the player and the value for the user to act on it
    assert raised.value.translation_args == ["absurd", int(beyond_range), MAX_SENDSPIN_STATIC_DELAY]


def test_the_top_of_the_supported_range_is_still_applied() -> None:
    """The boundary itself is a legitimate correction, not an out-of-range measurement."""
    delays_ms = resolve_static_delays(
        {"a": 0.0, "far": float(MAX_SENDSPIN_STATIC_DELAY)}, {"a": 0, "far": 0}, OWNER
    )

    assert delays_ms == {"a": 0, "far": MAX_SENDSPIN_STATIC_DELAY}


def test_an_existing_delay_counts_towards_the_range() -> None:
    """The applied value is checked, so a delay already in place cannot push past the top."""
    with pytest.raises(InvalidDataError) as raised:
        resolve_static_delays(
            {"a": 0.0, "b": 10.0}, {"a": 0, "b": MAX_SENDSPIN_STATIC_DELAY}, OWNER
        )

    assert raised.value.translation_key == "calibration_delay_out_of_range"


def test_a_player_outside_the_group_raises() -> None:
    """A measurement for a player that is not being calibrated is refused."""
    with pytest.raises(InvalidDataError) as raised:
        resolve_static_delays({"a": 0.0, "stranger": 5.0}, {"a": 0, "b": 0}, OWNER)

    assert raised.value.translation_key == "calibration_player_not_in_session"
    assert raised.value.translation_args == ["stranger"]


def test_an_unmeasured_group_member_raises() -> None:
    """
    A partial result is refused rather than applied to the speakers it covers.

    Correcting a subset leaves the rest normalised against a different baseline, so a
    partly measured group would come out less aligned than it went in.
    """
    with pytest.raises(InvalidDataError) as raised:
        resolve_static_delays({"a": 0.0}, {"a": 0, "forgotten": 0}, OWNER)

    assert raised.value.translation_key == "calibration_player_not_measured"
    assert raised.value.translation_args == ["forgotten"]


def test_no_measurements_at_all_raises() -> None:
    """An empty result is refused instead of normalising over nothing."""
    with pytest.raises(InvalidDataError) as raised:
        resolve_static_delays({}, {"a": 0}, OWNER)

    assert raised.value.translation_key == "calibration_no_measurements"


def test_the_errors_carry_the_provider_as_their_translation_owner() -> None:
    """The messages resolve against this provider's own strings, not just common."""
    with pytest.raises(InvalidDataError) as raised:
        resolve_static_delays({}, {"a": 0}, OWNER)

    assert raised.value.translation_owner == OWNER
