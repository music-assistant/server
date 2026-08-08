"""Tests for the AirPlay pairing session (cliairplay pair-setup handling)."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.errors import PlayerCommandFailed

from music_assistant.providers.airplay.constants import StreamingProtocol
from music_assistant.providers.airplay.pairing import AirPlayPairing

# stderr of a pair-setup run that hit the ATV's pairing rate limit (HAP backoff)
BACKOFF_STDERR = [
    "[01:08:00.786] ap2_hap_create:608 [HAP] Created context without credentials",
    "[01:08:00.786] ap2_hap_pair_setup_pin:1197 [HAP] Starting HomeKit pair-setup (PIN)...",
    "[01:08:00.796] ap2_hap_pair_setup_pin:1215 [HAP] /pair-pin-start -> 200",
    "[01:08:00.927] ap2_hap_pair_setup_pin:1236 [HAP] Pair-setup M2 error tag: 3",
    "Pairing failed.",
]


def _make_pairing() -> AirPlayPairing:
    """Build an AirPlay 2 pairing session without starting anything."""
    return AirPlayPairing(
        address="192.168.68.60",
        name="Apple TV",
        protocol=StreamingProtocol.AIRPLAY2,
        logger=logging.getLogger("test.airplay.pairing"),
        port=7000,
        device_id="ABCDEF0123456789",
    )


def _attach_proc(pairing: AirPlayPairing, **overrides: object) -> MagicMock:
    """Attach a fake live pair-setup process to the pairing session."""
    proc = MagicMock()
    proc.closed = False
    proc.write = AsyncMock()
    proc.read = AsyncMock(return_value=b"")
    proc.wait_with_timeout = AsyncMock(return_value=1)
    proc.kill = AsyncMock()
    for name, value in overrides.items():
        setattr(proc, name, value)
    pairing._pair_proc = proc
    return proc


def test_pair_setup_error_prefers_specific_line_over_trailer() -> None:
    """The generic "Pairing failed." trailer must not hide the specific error line."""
    pairing = _make_pairing()
    pairing._pair_proc_stderr = list(BACKOFF_STDERR)
    assert pairing._pair_setup_error() == "Pair-setup M2 error tag: 3"


def test_pair_setup_error_falls_back_to_trailer() -> None:
    """With no specific line, the generic trailer is still better than nothing."""
    pairing = _make_pairing()
    pairing._pair_proc_stderr = ["Enter the PIN shown on the device: ", "Pairing failed."]
    assert pairing._pair_setup_error() == "Pairing failed."


async def test_backoff_failure_carries_specific_translation() -> None:
    """A device in HAP backoff (error tag 3) surfaces as the pairing_backoff error."""
    pairing = _make_pairing()
    _attach_proc(pairing, write=AsyncMock(side_effect=BrokenPipeError))
    pairing._pair_proc_stderr = list(BACKOFF_STDERR)

    with pytest.raises(PlayerCommandFailed) as excinfo:
        await pairing.finish_pairing(pin="1234")

    assert excinfo.value.translation_key == "pairing_backoff"
    assert "error tag: 3" in str(excinfo.value)


async def test_wrong_pin_failure_carries_specific_translation() -> None:
    """A rejected PIN (error tag 2 at M4) surfaces as the pairing_wrong_pin error."""
    pairing = _make_pairing()
    _attach_proc(pairing)
    pairing._pair_proc_stderr = [
        "[10:00:01.000] ap2_hap_pair_setup_pin:1310 [HAP] Pair-setup M4 error tag: 2",
        "Pairing failed.",
    ]

    with pytest.raises(PlayerCommandFailed) as excinfo:
        await pairing.finish_pairing(pin="0000")

    assert excinfo.value.translation_key == "pairing_wrong_pin"


async def test_unclassified_failure_keeps_default_translation() -> None:
    """Failures without a HAP error tag keep the generic player command error."""
    pairing = _make_pairing()
    _attach_proc(pairing)
    pairing._pair_proc_stderr = ["Cannot connect to 192.168.68.60:7000"]

    with pytest.raises(PlayerCommandFailed) as excinfo:
        await pairing.finish_pairing(pin="1234")

    assert excinfo.value.translation_key == PlayerCommandFailed.translation_key
    assert "Cannot connect" in str(excinfo.value)
