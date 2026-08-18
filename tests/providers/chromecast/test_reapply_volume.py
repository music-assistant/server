"""
Tests for the Cast override of Player.reapply_volume.

The generic detour rounds up to one whole percent, which is audible on plenty of hardware. Cast
carries a volume as a float, so the configured step is passed through as given - a fraction of a
percent, which a receiver still applies but nobody hears.
"""

from __future__ import annotations

from typing import cast
from unittest.mock import MagicMock, patch
from uuid import uuid4

from music_assistant.providers.chromecast.player import ChromecastPlayer

STEP_PCT = 0.4  # what a user would type in the config
STEP = STEP_PCT / 100  # what the cast device receives


def _make_player(volume_level: float) -> ChromecastPlayer:
    """Build a ChromecastPlayer whose device reports the given volume."""
    info = MagicMock()
    info.manufacturer = "Terris"
    info.model_name = "Terris CCM283"
    info.friendly_name = "Arbeitszimmer"
    info.is_audio_group = False
    info.is_multichannel_group = False
    info.host = "10.0.0.10"
    info.mac_address = "00:11:22:33:44:55"
    info.uuid = uuid4()
    provider = MagicMock()
    provider.mass.closing = False
    provider.mz_mgr = MagicMock()
    chromecast = MagicMock()
    chromecast.status.volume_level = volume_level
    with patch("music_assistant.providers.chromecast.player.CastStatusListener"):
        return ChromecastPlayer(provider, str(info.uuid), info, chromecast)


def _sent_volumes(player: ChromecastPlayer) -> list[float]:
    """Return the volumes handed to the cast device, in order."""
    return [call.args[0] for call in cast("MagicMock", player.cc).set_volume.call_args_list]


async def test_detour_uses_the_configured_step_and_restores() -> None:
    """The detour is the configured step, small enough to be inaudible, then the level returns."""
    player = _make_player(0.30)
    await player.reapply_volume(STEP_PCT)
    sent = _sent_volumes(player)
    assert len(sent) == 2
    assert sent[0] == 0.30 - STEP
    assert sent[1] == 0.30


async def test_detour_is_far_smaller_than_the_generic_one_percent() -> None:
    """
    The point of the override: a whole percent is audible on some devices, a fraction is not.

    Pinned as a ratio rather than a constant so a change to the step has to be deliberate.
    """
    player = _make_player(0.30)
    await player.reapply_volume(STEP_PCT)
    detour_size = abs(_sent_volumes(player)[0] - 0.30)
    assert detour_size < 0.01 / 2


async def test_detour_goes_up_when_the_level_is_below_one_step() -> None:
    """Near zero the detour has to go up: a negative volume is not a value cast accepts."""
    player = _make_player(0.001)
    await player.reapply_volume(STEP_PCT)
    sent = _sent_volumes(player)
    assert sent[0] == 0.001 + STEP
    assert sent[1] == 0.001


async def test_detour_is_derived_from_the_device_not_from_mass() -> None:
    """
    The detour must be based on what the device reports, not on what MA believes.

    The two can disagree - the device reporting a volume it never applied is the whole reason
    this exists - and only the device's own value is what a repeat would be compared against.
    """
    player = _make_player(0.42)
    player._attr_volume_level = 10
    await player.reapply_volume(STEP_PCT)
    assert _sent_volumes(player)[0] == 0.42 - STEP


async def test_no_device_status_sends_nothing() -> None:
    """A receiver that has not reported a status yet has no level to detour around."""
    player = _make_player(0.30)
    cast("MagicMock", player.cc).status = None
    await player.reapply_volume(STEP_PCT)
    assert _sent_volumes(player) == []


async def test_configured_step_is_used_without_rounding() -> None:
    """
    Cast honours the configured step exactly, where the generic path rounds it up.

    That is the whole reason this override exists: rounding 0.4 up to a whole percent is
    audible on some devices, and the configured value is what the user tuned by ear.
    """
    player = _make_player(0.30)
    await player.reapply_volume(0.4)
    assert _sent_volumes(player)[0] == 0.30 - 0.004


async def test_oversized_step_stays_within_the_cast_range() -> None:
    """Cast volume is 0..1, so an oversized step must not push the detour outside it."""
    player = _make_player(0.30)
    await player.reapply_volume(90)
    sent = _sent_volumes(player)
    assert all(0.0 <= level <= 1.0 for level in sent)
    assert sent[-1] == 0.30


async def test_step_that_fits_nowhere_sends_nothing() -> None:
    """At full volume with an oversized step there is no room either way, so nothing is sent."""
    player = _make_player(1.0)
    await player.reapply_volume(150)
    assert _sent_volumes(player) == []


async def test_detour_respects_the_min_volume_limit() -> None:
    """
    A configured min-volume limit (MA's 0..100 scale) bounds the detour in cast's 0..1 units.

    At the floor the detour must go up, never below it - a below-floor value would be reported
    back as out of range and dragged to the floor by the controller's limit enforcement.
    """
    player = _make_player(0.20)  # cast level 0.20 == the min-volume floor of 20
    await player.reapply_volume(STEP_PCT, 20, 100)
    sent = _sent_volumes(player)
    assert sent[0] == 0.20 + STEP  # up, not below the floor
    assert sent[-1] == 0.20
